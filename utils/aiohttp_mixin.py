"""
AioHttp ClientSession 共享管理

提供 Mixin 模式管理全局共享的 aiohttp.ClientSession 实例，含：
- 显式生命周期管理（initialize_http_session / close_http_session）
- 内置自动重试（429/5xx + 连接错误，指数退避，支持 Retry-After）
- 请求追踪日志（脱敏敏感 header）

用法：
    class MyService(AioHttpClientSessionMixin):
        async def fetch(self, url: str) -> dict:
            async with self._http_request_with_retry('GET', url) as resp:
                return await resp.json()

    # 启动时
    await AioHttpClientSessionMixin.initialize_http_session()
    # 关闭时
    await AioHttpClientSessionMixin.close_http_session()

:author: Foxerine <https://github.com/Foxerine>
"""
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar, final

import aiohttp
from aiohttp import TraceConfig, TraceRequestStartParams
from charset_normalizer import is_binary
from loguru import logger as l

from meta_config import meta_config

# 日志中 Body 的最大长度，超过截断。None 表示不截断
_log_body_max_length: int | None = 500

# Body 收集硬上限（防止大文件流式上传导致 OOM）
_MAX_BODY_COLLECT_BYTES = 1024 * 1024  # 1 MB

# 二进制 head 预览字节数
_BINARY_HEAD_BYTES = 512

_SENSITIVE_HEADERS: frozenset[str] = frozenset({
    'authorization',
    'authorizationtype',
    'x-api-key',
    'x-secret-key',
    'x-wechat-uin',
    'cookie',
    'set-cookie',
    'proxy-authorization',
})
"""日志中需脱敏的敏感 header（全小写匹配）"""


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """脱敏敏感 header 值，保留前 8 字符用于调试"""
    return {
        k: (f"{v[:8]}***" if len(v) > 8 else "***") if k.lower() in _SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }


async def _on_request_start(
    session: aiohttp.ClientSession,
    trace_config_ctx: Any,
    params: TraceRequestStartParams,
) -> None:
    trace_config_ctx.method = params.method
    trace_config_ctx.url = params.url
    trace_config_ctx.headers = _sanitize_headers(dict(params.headers))
    trace_config_ctx.body_chunks = []
    trace_config_ctx.body_total_size = 0
    trace_config_ctx.body_head = b''
    trace_config_ctx.body_is_binary = None


async def _on_request_chunk_sent(
    session: aiohttp.ClientSession,
    trace_config_ctx: Any,
    params: aiohttp.TraceRequestChunkSentParams,
) -> None:
    trace_config_ctx.body_total_size += len(params.chunk)

    if trace_config_ctx.body_is_binary is None:
        sample = params.chunk[:_BINARY_HEAD_BYTES]
        trace_config_ctx.body_is_binary = bool(sample and is_binary(sample))
        if trace_config_ctx.body_is_binary:
            trace_config_ctx.body_head = sample
            return

    if trace_config_ctx.body_is_binary:
        return

    if trace_config_ctx.body_total_size <= _MAX_BODY_COLLECT_BYTES:
        trace_config_ctx.body_chunks.append(params.chunk)


async def _on_request_end(
    session: aiohttp.ClientSession,
    trace_config_ctx: Any,
    params: aiohttp.TraceRequestEndParams,
) -> None:
    total_size: int = trace_config_ctx.body_total_size

    if trace_config_ctx.body_is_binary or total_size > _MAX_BODY_COLLECT_BYTES:
        head: bytes = trace_config_ctx.body_head
        body_str = f"(binary, {total_size} bytes) [{head[:64].hex(' ')}...]"
    elif trace_config_ctx.body_chunks:
        body = b''.join(trace_config_ctx.body_chunks)
        body_str = body.decode('utf-8', errors='replace')
        if _log_body_max_length is not None and len(body_str) > _log_body_max_length:
            half = _log_body_max_length // 2
            body_str = f"{body_str[:half]}...({len(body_str)} chars)...{body_str[-half:]}"
    else:
        body_str = "(empty)"

    trace_config_ctx.body_chunks.clear()
    l.debug(
        f"[HTTP] {trace_config_ctx.method} {trace_config_ctx.url} → {params.response.status}\n"
        f"Headers: {trace_config_ctx.headers}\n"
        f"Body: {body_str}"
    )


async def _on_request_exception(
    session: aiohttp.ClientSession,
    trace_config_ctx: Any,
    params: aiohttp.TraceRequestExceptionParams,
) -> None:
    trace_config_ctx.body_chunks.clear()
    l.error(
        f"[HTTP Exception] {trace_config_ctx.method} {trace_config_ctx.url}\n"
        f"Exception: {type(params.exception).__name__}: {params.exception}"
    )


def _create_trace_config() -> TraceConfig:
    trace_config = TraceConfig()
    trace_config.on_request_start.append(_on_request_start)
    trace_config.on_request_chunk_sent.append(_on_request_chunk_sent)
    trace_config.on_request_end.append(_on_request_end)
    trace_config.on_request_exception.append(_on_request_exception)
    return trace_config


@final
class _RetryableStatusError(Exception):
    """内部异常：HTTP 可重试状态码，用于跳出 async with 块触发重试"""
    __slots__ = ('status', 'body', 'retry_after')

    def __init__(self, status: int, body: str, retry_after: str | None) -> None:
        super().__init__(status, body)
        self.status = status
        self.body = body
        self.retry_after = retry_after


RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
"""默认触发自动重试的 HTTP 状态码"""


class AioHttpClientSessionMixin:
    """
    全局共享 aiohttp.ClientSession 的 Mixin。

    所有继承此 Mixin 的类共享同一个 ClientSession 实例。
    必须在应用启动时调用 `initialize_http_session()`，关闭时调用 `close_http_session()`。
    """

    _http_session: ClassVar[aiohttp.ClientSession | None] = None

    @classmethod
    async def initialize_http_session(
        cls,
        log_body_max_length: int | None = 500,
        **session_kwargs: Any,
    ) -> None:
        """
        初始化共享 ClientSession。需在 asyncio 事件循环中调用。

        :param log_body_max_length: 日志中 body 最大长度，超过则截断。None 表示不截断
        :param session_kwargs: 传递给 aiohttp.ClientSession 的额外参数
        """
        global _log_body_max_length
        if cls._http_session is not None and not cls._http_session.closed:
            raise RuntimeError("HTTP session already initialized")
        _log_body_max_length = log_body_max_length

        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=30,
            keepalive_timeout=60,
            force_close=False,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
        )

        timeout = aiohttp.ClientTimeout(
            total=meta_config.AIOHTTP_TIMEOUT_TOTAL,
            connect=meta_config.AIOHTTP_TIMEOUT_CONNECT,
            sock_read=meta_config.AIOHTTP_TIMEOUT_SOCK_READ,
        )

        session_kwargs.setdefault('connector', connector)
        session_kwargs.setdefault('timeout', timeout)
        session_kwargs.setdefault('trust_env', False)
        session_kwargs.setdefault('trace_configs', [_create_trace_config()])

        cls._http_session = aiohttp.ClientSession(**session_kwargs)  # pyright: ignore[reportAttributeAccessIssue]

    @classmethod
    def get_http_session(cls) -> aiohttp.ClientSession:
        """获取共享 ClientSession（未初始化时抛出 RuntimeError）"""
        session = cls._http_session
        if session is None or session.closed:
            raise RuntimeError(
                "HTTP session not initialized. "
                "Call `AioHttpClientSessionMixin.initialize_http_session()` at startup."
            )
        return session

    @property
    def http_session(self) -> aiohttp.ClientSession:
        """获取共享 ClientSession（实例属性）"""
        return self.__class__.get_http_session()

    @classmethod
    async def close_http_session(cls) -> None:
        """关闭共享 ClientSession（幂等）"""
        session = cls._http_session
        if session is None or session.closed:
            cls._http_session = None  # pyright: ignore[reportAttributeAccessIssue]
            return
        await session.close()
        cls._http_session = None  # pyright: ignore[reportAttributeAccessIssue]

    @asynccontextmanager
    async def _http_request_with_retry(
        self,
        method: str,
        url: str,
        *,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
        retryable_statuses: frozenset[int] = RETRYABLE_STATUS_CODES,
        should_retry: Callable[[int, str], bool] | None = None,
        **request_kwargs: Any,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """
        带自动重试的 HTTP 请求上下文管理器。

        对瞬时网络故障（连接错误、超时）和可重试的 HTTP 状态码（429/5xx）
        自动重试，使用指数退避策略，支持 Retry-After 响应头。

        用法::
            async with self._http_request_with_retry('POST', url, json=data) as resp:
                if resp.status != 200:
                    raise RuntimeError(await resp.text())
                return await resp.json()

        :param method: HTTP 方法
        :param url: 请求 URL
        :param max_retries: 最大重试次数（不含首次，默认 3）
        :param retry_base_delay: 首次重试延迟（秒）
        :param retry_max_delay: 重试延迟上限（秒）
        :param retryable_statuses: 触发重试的 HTTP 状态码集合
        :param should_retry: 自定义重试判断 (status, body) -> bool
        :param request_kwargs: 传递给 aiohttp request 的额外参数
        """
        delay = retry_base_delay

        for attempt in range(max_retries + 1):
            is_last_attempt = attempt == max_retries
            yielded = False
            try:
                async with self.http_session.request(method, url, **request_kwargs) as response:
                    if not is_last_attempt and not (200 <= response.status < 300):
                        if response.status in retryable_statuses:
                            error_body = await response.text()
                            retry_after = response.headers.get('Retry-After')
                            raise _RetryableStatusError(response.status, error_body, retry_after)
                        if should_retry is not None:
                            error_body = await response.text()
                            if should_retry(response.status, error_body):
                                raise _RetryableStatusError(
                                    response.status, error_body, response.headers.get('Retry-After')
                                )
                    yielded = True
                    yield response
                    return

            except _RetryableStatusError as e:
                actual_delay = delay
                if e.retry_after is not None:
                    try:
                        actual_delay = max(float(e.retry_after), delay)
                    except ValueError:
                        pass
                l.warning(
                    f"[HTTP Retry] {method} {url} 返回 {e.status} "
                    f"(重试 {attempt + 1}/{max_retries}, 等待 {actual_delay:.1f}s): "
                    f"{e.body[:200]}"
                )
                await asyncio.sleep(actual_delay)
                delay = min(delay * 2, retry_max_delay)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if yielded:
                    raise
                if not is_last_attempt:
                    l.warning(
                        f"[HTTP Retry] {method} {url} 失败 "
                        f"(重试 {attempt + 1}/{max_retries}, 等待 {delay:.1f}s): "
                        f"{type(e).__name__}: {e}"
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, retry_max_delay)
                else:
                    raise
