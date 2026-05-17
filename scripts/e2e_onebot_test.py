"""
OneBot v11 协议端到端验证脚本

把 ilink-onebot 当作一个普通的 OneBot v11 实现，从外部连接 WS 服务端，
逐项验证它的行为与 OneBot v11 规范一致。

测试维度：
1. **连接握手**：能否成功连接 ws://host:port/，是否能收到 lifecycle/connect 事件
2. **元信息动作**：get_version_info / get_status / get_login_info / can_send_image
   / can_send_record 的响应字段是否完整且类型正确
3. **不支持动作的拒绝**：delete_msg / send_group_msg / get_friend_list 等返回
   retcode=1404
4. **格式错误的拒绝**：缺 user_id 的 send_private_msg 返回 retcode=1400
5. **心跳事件**：在合理时间内收到至少一次 heartbeat meta_event，且 interval/status
   字段格式正确
6. **可选的实际消息收发**：若已知一个 user_id（用户先在微信侧发过消息），
   可发送 send_private_msg 并验证 iLink 实际投递成功

用法（项目根目录下，已用 uv sync 安装依赖且 ilink-onebot run 正在运行）::

    .venv/Scripts/python.exe scripts/e2e_onebot_test.py
    .venv/Scripts/python.exe scripts/e2e_onebot_test.py --url ws://127.0.0.1:6700/
    .venv/Scripts/python.exe scripts/e2e_onebot_test.py --send-to 1234567890 --send-text "hello"

退出码：
- 0：所有断言通过
- 1：至少一个断言失败（详细错误打印到 stderr）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import orjson
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed


# ===== 测试结果聚合 =====

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ''


@dataclass
class TestRunner:
    """逐项跑测试，最后聚合输出"""

    results: list[TestResult] = field(default_factory=list)

    async def run(
        self,
        name: str,
        coro_fn: Callable[[], Awaitable[None]],
    ) -> None:
        print(f"[..] {name}", flush=True)
        try:
            await coro_fn()
            self.results.append(TestResult(name=name, passed=True))
            print(f"\r[OK] {name}", flush=True)
        except AssertionError as e:
            self.results.append(TestResult(name=name, passed=False, detail=str(e)))
            print(f"\r[FAIL] {name}", flush=True)
            print(f"       assertion: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            self.results.append(
                TestResult(name=name, passed=False, detail=f"{type(e).__name__}: {e}")
            )
            print(f"\r[ERR] {name}", flush=True)
            print(f"       {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def summary(self) -> bool:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        print()
        print(f"=== Summary: {passed}/{total} passed ===")
        if passed < total:
            print()
            print("Failed tests:")
            for r in self.results:
                if not r.passed:
                    print(f"  - {r.name}: {r.detail}")
        return passed == total


# ===== WebSocket 客户端 =====

class OneBotClient:
    """
    简易 OneBot v11 客户端：发动作 + 等响应（按 echo 匹配）+ 收事件队列。
    """

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    async def __aenter__(self) -> 'OneBotClient':
        self._reader_task = asyncio.create_task(self._reader_loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._closed = True
        if self._reader_task is not None and not self._reader_task.done():
            _ = self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, ConnectionClosed):
                pass

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                try:
                    data = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                # 响应：有 echo + retcode
                if 'retcode' in data and isinstance(data.get('echo'), str):
                    echo = data['echo']
                    fut = self._pending.pop(echo, None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                    continue
                # 事件
                if 'post_type' in data:
                    await self._events.put(data)
        except ConnectionClosed:
            pass
        finally:
            # 唤醒所有 pending
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionClosed(None, None))

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        echo = uuid.uuid4().hex
        payload = {'action': action, 'params': params or {}, 'echo': echo}
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send(orjson.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)

    async def wait_event(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 35.0,
    ) -> dict[str, Any]:
        """等待匹配 predicate 的事件（超时抛 TimeoutError）"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"等待事件超时 {timeout}s")
            evt = await asyncio.wait_for(self._events.get(), timeout=remaining)
            if predicate(evt):
                return evt


# ===== 各项测试 =====

async def test_get_version_info(client: OneBotClient) -> None:
    resp = await client.call('get_version_info')
    assert resp['status'] == 'ok', f"status={resp['status']}"
    assert resp['retcode'] == 0, f"retcode={resp['retcode']}"
    data = resp['data']
    assert isinstance(data, dict), f"data 不是 dict: {data!r}"
    for key in ('app_name', 'app_version', 'protocol_version'):
        assert key in data, f"data 缺少 {key}"
        assert isinstance(data[key], str), f"data[{key}] 不是 str"
    assert data['protocol_version'] == 'v11', (
        f"protocol_version={data['protocol_version']}, 期望 v11"
    )


async def test_get_status(client: OneBotClient) -> None:
    resp = await client.call('get_status')
    assert resp['status'] == 'ok', f"status={resp['status']}"
    assert resp['retcode'] == 0, f"retcode={resp['retcode']}"
    data = resp['data']
    assert isinstance(data, dict), f"data 不是 dict"
    assert 'online' in data and isinstance(data['online'], bool), (
        f"data.online 缺失或类型错误: {data}"
    )
    assert 'good' in data and isinstance(data['good'], bool), (
        f"data.good 缺失或类型错误: {data}"
    )


async def test_get_login_info(client: OneBotClient) -> None:
    resp = await client.call('get_login_info')
    assert resp['status'] == 'ok', f"status={resp['status']}"
    assert resp['retcode'] == 0, f"retcode={resp['retcode']}"
    data = resp['data']
    assert isinstance(data, dict)
    assert 'user_id' in data and isinstance(data['user_id'], int), (
        f"data.user_id 缺失或类型错误: {data}"
    )
    assert 'nickname' in data, f"data.nickname 缺失: {data}"
    # nickname 可以是 None / '' / 真实昵称，但 key 必须存在


async def test_can_send_image(client: OneBotClient) -> None:
    resp = await client.call('can_send_image')
    assert resp['retcode'] == 0
    assert resp['data'] == {'yes': True}, f"data={resp['data']}"


async def test_can_send_record(client: OneBotClient) -> None:
    resp = await client.call('can_send_record')
    assert resp['retcode'] == 0
    assert resp['data'] == {'yes': True}, f"data={resp['data']}"


async def test_unsupported_actions(client: OneBotClient) -> None:
    """iLink 不支持的动作应返回 retcode=1404"""
    cases = [
        ('delete_msg', {'message_id': 123}),
        ('get_friend_list', {}),
        ('get_stranger_info', {'user_id': 1}),
        ('set_group_kick', {'group_id': 1, 'user_id': 2}),
        ('totally_unknown_action_xyz', {}),
    ]
    for action, params in cases:
        resp = await client.call(action, params)
        assert resp['retcode'] == 1404, (
            f"action={action} retcode={resp['retcode']}, 期望 1404"
        )
        assert resp['status'] == 'failed', (
            f"action={action} status={resp['status']}"
        )


async def test_send_group_msg_rejected(client: OneBotClient) -> None:
    """send_group_msg 和 send_msg(message_type=group) 都应被拒绝"""
    resp1 = await client.call('send_group_msg', {'group_id': 1, 'message': 'x'})
    assert resp1['retcode'] == 1404, f"send_group_msg retcode={resp1['retcode']}"

    resp2 = await client.call('send_msg', {
        'message_type': 'group',
        'group_id': 1,
        'message': 'x',
    })
    assert resp2['retcode'] == 1404, f"send_msg(group) retcode={resp2['retcode']}"


async def test_send_private_msg_bad_request(client: OneBotClient) -> None:
    """缺少必要参数应返回 1400"""
    resp = await client.call('send_private_msg', {})  # 缺 user_id 和 message
    assert resp['retcode'] == 1400, f"retcode={resp['retcode']}, 期望 1400"
    assert resp['status'] == 'failed'


async def test_send_private_msg_unknown_user(client: OneBotClient) -> None:
    """向从未给 bot 发过消息的 user_id 发送应失败（无 context_token）"""
    resp = await client.call('send_private_msg', {
        'user_id': 999999999,  # 几乎不可能存在的 user_id
        'message': [{'type': 'text', 'data': {'text': 'should fail'}}],
    })
    # 期望 failed 但不一定是特定 retcode（具体由实现决定，可能是 1400 业务错误）
    assert resp['status'] == 'failed', (
        f"向陌生 user_id 发送应失败，但 status={resp['status']}"
    )


async def test_action_echo_preserved(client: OneBotClient) -> None:
    """echo 字段应原样返回"""
    resp = await client.call('get_status')  # 内部会发 echo
    assert 'echo' in resp, "响应缺少 echo 字段"
    assert isinstance(resp['echo'], str), f"echo 类型错误: {type(resp['echo']).__name__}"


async def test_heartbeat_received(client: OneBotClient, timeout: float) -> None:
    """在 timeout 秒内应收到至少一次 heartbeat meta_event"""
    def is_heartbeat(evt: dict[str, Any]) -> bool:
        return (
            evt.get('post_type') == 'meta_event'
            and evt.get('meta_event_type') == 'heartbeat'
        )
    evt = await client.wait_event(is_heartbeat, timeout=timeout)

    # 验证字段
    assert isinstance(evt.get('time'), int), f"time 类型错误: {evt}"
    assert isinstance(evt.get('self_id'), int), f"self_id 类型错误: {evt}"
    assert isinstance(evt.get('interval'), int), f"interval 类型错误: {evt}"
    assert evt['interval'] > 0, f"interval 应 > 0，实际 {evt['interval']}"
    status = evt.get('status')
    assert isinstance(status, dict), f"status 不是 dict: {evt}"
    assert isinstance(status.get('online'), bool)
    assert isinstance(status.get('good'), bool)


async def test_send_private_msg_real(
    client: OneBotClient,
    user_id: int,
    text: str,
) -> None:
    """向真实 user_id 发送消息（用户须事先用微信向 bot 发过至少一条消息）"""
    resp = await client.call('send_private_msg', {
        'user_id': user_id,
        'message': [{'type': 'text', 'data': {'text': text}}],
    })
    assert resp['retcode'] == 0, (
        f"send_private_msg failed: retcode={resp['retcode']}, data={resp.get('data')}"
    )
    data = resp['data']
    assert isinstance(data, dict), f"data 不是 dict: {data}"
    assert 'message_id' in data and isinstance(data['message_id'], int), (
        f"data.message_id 缺失或类型错误: {data}"
    )


# ===== 主程序 =====

async def run_tests(args: argparse.Namespace) -> bool:
    runner = TestRunner()

    headers: dict[str, str] = {}
    if args.access_token is not None:
        headers['Authorization'] = f"Bearer {args.access_token}"

    print(f"Connecting to {args.url} ...")
    try:
        async with connect(
            args.url,
            additional_headers=headers,
            open_timeout=5.0,
        ) as ws:
            print(f"Connected. (subprotocol={ws.subprotocol})")
            async with OneBotClient(ws) as client:
                await runner.run('get_version_info', lambda: test_get_version_info(client))
                await runner.run('get_status', lambda: test_get_status(client))
                await runner.run('get_login_info', lambda: test_get_login_info(client))
                await runner.run('can_send_image', lambda: test_can_send_image(client))
                await runner.run('can_send_record', lambda: test_can_send_record(client))
                await runner.run(
                    'unsupported actions return 1404',
                    lambda: test_unsupported_actions(client),
                )
                await runner.run(
                    'send_group_msg / send_msg(group) rejected',
                    lambda: test_send_group_msg_rejected(client),
                )
                await runner.run(
                    'send_private_msg bad request → 1400',
                    lambda: test_send_private_msg_bad_request(client),
                )
                await runner.run(
                    'send_private_msg unknown user → failed',
                    lambda: test_send_private_msg_unknown_user(client),
                )
                await runner.run('echo preserved', lambda: test_action_echo_preserved(client))

                # heartbeat 是异步事件，最坏要等一个 interval
                await runner.run(
                    f'heartbeat event within {args.heartbeat_timeout}s',
                    lambda: test_heartbeat_received(client, args.heartbeat_timeout),
                )

                if args.send_to is not None:
                    text = args.send_text or f"e2e test {int(time.time())}"
                    await runner.run(
                        f'send_private_msg real to {args.send_to}',
                        lambda: test_send_private_msg_real(client, args.send_to, text),
                    )

    except OSError as e:
        print(f"无法连接 {args.url}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"连接出错: {type(e).__name__}: {e}", file=sys.stderr)
        return False

    return runner.summary()


def main() -> None:
    parser = argparse.ArgumentParser(description='OneBot v11 E2E test for ilink-onebot')
    _ = parser.add_argument(
        '--url', default='ws://127.0.0.1:6700/',
        help='WebSocket URL (default: ws://127.0.0.1:6700/)',
    )
    _ = parser.add_argument(
        '--access-token', default=os.environ.get('ONEBOT_ACCESS_TOKEN'),
        help='Authorization Bearer token (or env ONEBOT_ACCESS_TOKEN)',
    )
    _ = parser.add_argument(
        '--heartbeat-timeout', type=float, default=35.0,
        help='等待 heartbeat 事件的最大秒数（默认 35s，ServerConfig 默认 interval 30s）',
    )
    _ = parser.add_argument(
        '--send-to', type=int, default=None,
        help='可选：发一条真实消息到此 OneBot user_id（须先在微信侧给 bot 发过消息）',
    )
    _ = parser.add_argument(
        '--send-text', type=str, default=None,
        help='与 --send-to 配套的消息文本（默认带时间戳）',
    )
    args = parser.parse_args()

    ok = asyncio.run(run_tests(args))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
