"""
通用 HTTP 异常辅助函数

本文件保持跨项目通用，不包含业务特定的描述文案。
调用方在具体业务场景中传入 detail 消息。

设计原则：禁止端点直接 `raise HTTPException(status_code=..., detail=...)`，
统一通过本模块的 `raise_*` 系列函数。原因：
- 集中维护状态码语义（如 529 上游过载、529 仅 Cloudflare 约定）
- 便于全局变更（如未来需要统一日志或 metrics）
- 让端点代码意图更清晰（`raise_not_found("media")` vs `raise HTTPException(404)`）

:author: Foxerine <https://github.com/Foxerine>
"""
from typing import Any, NoReturn

import pydantic
from fastapi import HTTPException
from pydantic import BaseModel
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_413_CONTENT_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_501_NOT_IMPLEMENTED,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)

# --- 400 ---

def ensure_request_param(to_check: Any, detail: str) -> None:
    """
    Ensures a parameter exists. If not, raises a 400 Bad Request.
    This function returns None if the check passes.
    """
    if not to_check:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=detail)

def raise_bad_request(detail: str = '') -> NoReturn:
    """Raises an HTTP 400 Bad Request exception."""
    raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=detail)


def reject_forbidden_fields(data: BaseModel, forbidden: type[BaseModel]) -> None:
    """
    显式拒绝请求体中来自 ``forbidden`` 模型的字段（权限边界守卫）。

    用途：Owner 端点复用 ``*AdminUpdateRequest`` 请求体（包含全部可更新字段），
    但某些字段仅允许 Admin 修改。此函数在端点顶部调用一次，若 Owner 客户端
    显式提交了任何 admin-only 字段，立即返回 400 并列出违规字段。

    原理：基于 Pydantic v2 的 ``model_fields_set`` —— 这是追踪"客户端**实际**
    提交了哪些字段"的唯一可靠 API（与字段是否有默认值无关）。

    :param data: 待检查的请求体实例
    :param forbidden: 声明禁止字段的模型类（通常是一个 Admin-only Mixin）
    :raises HTTPException 400: data 的 ``model_fields_set`` 与 ``forbidden.model_fields`` 有交集
    """
    violated = data.model_fields_set & set(forbidden.model_fields)
    if violated:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"以下字段不允许自行修改: {', '.join(sorted(violated))}"
        )

def raise_unauthorized(detail: str) -> NoReturn:
    """Raises an HTTP 401 Unauthorized exception."""
    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail=detail)

def raise_insufficient_quota(detail: str = "积分不足，请充值") -> NoReturn:
    """Raises an HTTP 402 Payment Required exception."""
    raise HTTPException(status_code=HTTP_402_PAYMENT_REQUIRED, detail=detail)

def raise_forbidden(detail: str) -> NoReturn:
    """Raises an HTTP 403 Forbidden exception."""
    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail=detail)

def raise_not_found(detail: str) -> NoReturn:
    """Raises an HTTP 404 Not Found exception."""
    raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=detail)

def raise_conflict(detail: str | BaseModel) -> NoReturn:
    """
    Raises an HTTP 409 Conflict exception.

    ``detail`` 接受简单字符串，或 Pydantic ``BaseModel``（含 SQLModel/SQLModelBase）
    用于携带结构化 conflict 详情——前端按 schema 解析。
    传入 BaseModel 时通过 ``model_dump(mode='json')`` 显式序列化为 JSON 可编码 dict。
    """
    payload: str | dict[str, Any] = (
        detail.model_dump(mode='json') if isinstance(detail, BaseModel) else detail
    )
    raise HTTPException(status_code=HTTP_409_CONFLICT, detail=payload)

def raise_payload_too_large(detail: str = "请求内容过大") -> NoReturn:
    """Raises an HTTP 413 Payload Too Large exception."""
    raise HTTPException(status_code=HTTP_413_CONTENT_TOO_LARGE, detail=detail)

def raise_unprocessable_entity(detail: str) -> NoReturn:
    """Raises an HTTP 422 Unprocessable Entity exception."""
    raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


_VALUE_ERROR_PREFIX = "Value error, "


def format_pydantic_validation_detail(exc: pydantic.ValidationError) -> str:
    """把 ``pydantic.ValidationError`` 的多条错误转成用户向 detail 字符串。

    模型层 ``model_post_init`` 抛 ``ValueError`` → Pydantic 包成
    ``ValidationError`` 时，``msg`` 字段以 ``"Value error, "`` 前缀开头；
    剥除后是清晰的中文错误描述。多条错误用 ``"; "`` 拼接。

    用途：dep 函数体内手工实例化 Pydantic/SQLModel 模型 catch 到的 ValidationError
    （这条路径不在 FastAPI request parsing 链上，须显式转 422）。
    """
    parts: list[str] = []
    for err in exc.errors():
        msg = err.get("msg") or "参数校验失败"
        if msg.startswith(_VALUE_ERROR_PREFIX):
            msg = msg[len(_VALUE_ERROR_PREFIX):]
        parts.append(msg)
    return "; ".join(parts)

def raise_too_many_requests(detail: str, retry_after: int | None = None) -> NoReturn:
    """Raises an HTTP 429 Too Many Requests exception.

    :param detail: 错误描述
    :param retry_after: 建议客户端等待的秒数（Retry-After 响应头，RFC 6585）
    """
    headers = {'Retry-After': str(retry_after)} if retry_after is not None else None
    raise HTTPException(status_code=HTTP_429_TOO_MANY_REQUESTS, detail=detail, headers=headers)

HTTP_529_SITE_OVERLOADED = 529
"""非标准状态码（Cloudflare 约定）：上游服务过载/限流"""

def raise_upstream_rate_limit(detail: str) -> NoReturn:
    """Raises an HTTP 529 (upstream rate-limited/overloaded) exception."""
    raise HTTPException(status_code=HTTP_529_SITE_OVERLOADED, detail=detail)

# --- 500 ---

def raise_internal_error(detail: str = "服务器出现故障，请稍后再试或联系管理员") -> NoReturn:
    """Raises an HTTP 500 Internal Server Error exception."""
    raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)

def raise_bad_gateway(detail: str) -> NoReturn:
    """Raises an HTTP 502 Bad Gateway exception."""
    raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=detail)

def raise_not_implemented(detail: str = "尚未支持这种方法") -> NoReturn:
    """Raises an HTTP 501 Not Implemented exception."""
    raise HTTPException(status_code=HTTP_501_NOT_IMPLEMENTED, detail=detail)

def raise_service_unavailable(detail: str) -> NoReturn:
    """Raises an HTTP 503 Service Unavailable exception."""
    raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

def raise_gateway_timeout(detail: str) -> NoReturn:
    """Raises an HTTP 504 Gateway Timeout exception."""
    raise HTTPException(status_code=HTTP_504_GATEWAY_TIMEOUT, detail=detail)
