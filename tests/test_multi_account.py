import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from onebots.action_handler import dispatch_action, push_pending_bootstrap_self_id
from onebots.models import OneBotAction
from sqlmodels.bridge import Bridge
from sqlmodels.bridge.context_token import ContextToken
from sqlmodels.bridge.enums import BridgeStateEnum, ILinkQRStatusEnum
from sqlmodels.bridge.ilink_models import QRCodeResponse, QRCodeStatusResponse
from sqlmodels.bridge.polling import PollingLoop
from sqlmodels.database_manager import DatabaseManager
from sqlmodels.server_config import ServerConfig
from typers.login import _login_impl


@pytest.fixture
async def test_session_factory(tmp_path):
    await DatabaseManager.close()
    db_file = tmp_path / "test_bridge.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    await DatabaseManager.init(db_url)
    session_factory = DatabaseManager.get_session_factory()
    async with session_factory() as session:
        # 默认初始化
        _ = await Bridge.get_instance(session)
        _ = await ServerConfig.get_instance(session)
    yield session_factory
    await DatabaseManager.close()


@pytest.mark.asyncio
async def test_multi_account_login_and_persistence(test_session_factory):
    """测试多账号独立登录并持久化，不相互覆盖"""
    async with test_session_factory() as session:
        default_b = await Bridge.get_by_name(session, 'default')
        assert default_b is not None
        default_b.bot_token = "token_default_123"
        default_b.wechat_id = "wechat_default"
        default_b.state = BridgeStateEnum.running
        await default_b.save(session)

        # 创建第二个账号
        bot2 = Bridge(account_name='bot2', self_id=10001)
        bot2 = await bot2.save(session)
        bot2_id = bot2.id
        assert bot2_id is not None

    # 模拟 bot2 扫码登录成功
    mock_qr_resp = QRCodeResponse(qrcode="mock_qr_token", qrcode_img_content="mock_base64")
    mock_status_resp = QRCodeStatusResponse(
        status=ILinkQRStatusEnum.confirmed,
        bot_token="token_bot2_456",
        ilink_bot_id="wechat_bot2",
        ilink_user_id="user_test",
    )

    with (
        patch.object(Bridge, "request_qrcode", new_callable=AsyncMock, return_value=mock_qr_resp),
        patch.object(Bridge, "check_qrcode_status", new_callable=AsyncMock, return_value=mock_status_resp),
        patch("sqlmodels.bridge.bridge.render_qrcode_to_terminal", return_value=None),
    ):
        success = await bot2.run_qrcode_login(test_session_factory)
        assert success is True

    # 验证 DB 中两个账号的数据互不影响
    async with test_session_factory() as session:
        b1 = await Bridge.get_by_name(session, 'default')
        b2 = await Bridge.get_by_name(session, 'bot2')

        assert b1 is not None and b2 is not None
        assert b1.bot_token == "token_default_123"
        assert b1.wechat_id == "wechat_default"

        assert b2.bot_token == "token_bot2_456"
        assert b2.wechat_id == "wechat_bot2"
        assert b2.state == BridgeStateEnum.running

        active = await Bridge.get_all_active(session)
        assert len(active) == 2
        active_names = {b.account_name for b in active}
        assert active_names == {'default', 'bot2'}


@pytest.mark.asyncio
async def test_auto_allocate_new_account(test_session_factory):
    """测试当 default 已登录时，不带 account 参数自动分配 bot2"""
    async with test_session_factory() as session:
        default_b = await Bridge.get_by_name(session, 'default')
        assert default_b is not None
        default_b.bot_token = "token_default_123"
        default_b.state = BridgeStateEnum.running
        await default_b.save(session)

    mock_qr_resp = QRCodeResponse(qrcode="mock_qr_token", qrcode_img_content="mock_base64")
    mock_status_resp = QRCodeStatusResponse(
        status=ILinkQRStatusEnum.confirmed,
        bot_token="token_bot2_auto",
        ilink_bot_id="wechat_bot2_auto",
    )

    with (
        patch("typers.login.infra_lifespan") as mock_lifespan,
        patch.object(Bridge, "request_qrcode", new_callable=AsyncMock, return_value=mock_qr_resp),
        patch.object(Bridge, "check_qrcode_status", new_callable=AsyncMock, return_value=mock_status_resp),
        patch("sqlmodels.bridge.bridge.render_qrcode_to_terminal", return_value=None),
    ):
        # mock infra_lifespan 上下文管理器返回 test_session_factory
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def dummy_lifespan():
            yield test_session_factory
        mock_lifespan.side_effect = dummy_lifespan

        await _login_impl(account=None, self_id=None, force=False)

    async with test_session_factory() as session:
        bot2 = await Bridge.get_by_name(session, 'bot2')
        assert bot2 is not None
        assert bot2.self_id == 10001
        assert bot2.bot_token == "token_bot2_auto"
        assert bot2.state == BridgeStateEnum.running


@pytest.mark.asyncio
async def test_concurrent_polling_workers(test_session_factory):
    """测试 PollingLoop.start 为所有 active 账号启动独立的 async 轮询任务"""
    async with test_session_factory() as session:
        b1 = await Bridge.get_by_name(session, 'default')
        assert b1 is not None
        b1.bot_token = "t1"
        b1.state = BridgeStateEnum.running
        await b1.save(session)

        b2 = Bridge(account_name='bot2', self_id=10001, bot_token="t2", state=BridgeStateEnum.running)
        await b2.save(session)

    # 启动轮询
    with patch.object(PollingLoop, "_tick", new_callable=AsyncMock) as mock_tick:
        from sqlmodels.bridge.polling import _TickOutcome
        mock_tick.return_value = _TickOutcome(error=False, next_interval_seconds=10.0)

        await PollingLoop.start(test_session_factory)
        try:
            assert len(PollingLoop._workers) == 2
            assert b1.id in PollingLoop._workers
            assert b2.id in PollingLoop._workers
            # 确认都是正在运行的 Task
            for tid, t in PollingLoop._workers.items():
                assert isinstance(t, asyncio.Task)
                assert not t.done()
        finally:
            await PollingLoop.stop()
            assert len(PollingLoop._workers) == 0


@pytest.mark.asyncio
async def test_action_routing_by_self_id_and_user_id(test_session_factory):
    """测试 OneBot Action 能依据 self_id 或 user_id 正确路由到对应 Bridge"""
    async with test_session_factory() as session:
        b1 = await Bridge.get_by_name(session, 'default')
        assert b1 is not None
        b1.bot_token = "t1"
        b1.self_id = 10000
        b1.state = BridgeStateEnum.running
        await b1.save(session)

        b2 = Bridge(account_name='bot2', self_id=10001, bot_token="t2", state=BridgeStateEnum.running)
        b2 = await b2.save(session)
        assert b2.id is not None

        # 给 bot2 写入一个 context_token
        ct = ContextToken(bridge_id=b2.id, user_name="wx_user_bot2", token="ctx_token_123")
        await ct.save(session)

        config = await ServerConfig.get_instance(session)

    # 1. 显式指定 self_id = 10001 -> 路由到 bot2
    action_with_self_id = OneBotAction(
        action="get_login_info",
        params={"self_id": 10001},
        echo="test_echo_1",
    )
    resp1 = await dispatch_action(b1, test_session_factory, config, action_with_self_id)
    assert resp1.retcode == 0
    assert resp1.data["user_id"] == 10001

    # 2. 通过 user_id 反查 -> 路由到拥有该 context_token 的 bot2
    reg = b2.get_id_registry()
    onebot_uid = reg.resolve_user_id("wx_user_bot2")

    with patch.object(Bridge, "send_text", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "msg_id_123"
        action_send = OneBotAction(
            action="send_msg",
            params={
                "user_id": onebot_uid,
                "message": [{"type": "text", "data": {"text": "hello"}}],
            },
            echo="test_echo_2",
        )
        resp2 = await dispatch_action(b1, test_session_factory, config, action_send)
        assert resp2.retcode == 0
        mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_account_image_sending_routes_to_second_bot(test_session_factory):
    """测试第二个账号出站发送图片时，必须正确路由到第二个账号并调用其专属 send_media"""
    from sqlmodels.media_file.media_file import MediaFile

    async with test_session_factory() as session:
        b1 = await Bridge.get_by_name(session, 'default')
        assert b1 is not None
        b1.bot_token = "token_b1"
        b1.self_id = 10000
        b1.state = BridgeStateEnum.running
        await b1.save(session)

        b2 = Bridge(account_name='bot2', self_id=10001, bot_token="token_b2", state=BridgeStateEnum.running)
        b2 = await b2.save(session)
        assert b2.id is not None

        # 仅为 bot2 写入 context_token（模拟用户向 bot2 发送消息）
        ct = ContextToken(bridge_id=b2.id, user_name="wx_user_bot2_img", token="ctx_token_b2")
        await ct.save(session)

        config = await ServerConfig.get_instance(session)

    reg = b2.get_id_registry()
    onebot_uid = reg.resolve_user_id("wx_user_bot2_img")

    # 构造发送图片的 OneBot Action
    action_image = OneBotAction(
        action="send_msg",
        params={
            "user_id": onebot_uid,
            "message": [
                {
                    "type": "image",
                    "data": {"file": "base64://aGVsbG8="},  # 模拟 base64 图片
                }
            ],
        },
        echo="test_img_echo",
    )

    called_send_media_bridges: list[Bridge] = []

    async def mock_send_media(self, *args, **kwargs):
        called_send_media_bridges.append(self)

    with (
        patch.object(MediaFile, "prepare_and_upload", new_callable=AsyncMock) as mock_prepare,
        patch.object(Bridge, "send_media", mock_send_media),
    ):
        mock_prepare.return_value = None

        resp = await dispatch_action(b1, test_session_factory, config, action_image)
        assert resp.retcode == 0

        # 关键断言 1：prepare_and_upload 必须使用 bot2 实例，而不是 b1
        mock_prepare.assert_awaited_once()
        assert mock_prepare.await_args is not None
        called_bridge = mock_prepare.await_args.args[0]
        assert called_bridge.id == b2.id
        assert called_bridge.account_name == 'bot2'
        assert called_bridge.bot_token == 'token_b2'

        # 关键断言 2：send_media 必须由 bot2 实例执行
        assert len(called_send_media_bridges) == 1
        assert called_send_media_bridges[0].id == b2.id
        assert called_send_media_bridges[0].account_name == 'bot2'


@pytest.mark.asyncio
async def test_reply_segment_with_mapped_id_and_fallback_on_failure(test_session_factory):
    """测试出站带有 reply 段时，如果支持则携带 ref_msg 发送；如果 iLink 发送失败则丢弃 reply 段回退重发"""
    from sqlmodels.bridge.exceptions import ILinkAPIError
    from sqlmodels.onebot_message_mapping import OneBotMessageMapping

    async with test_session_factory() as session:
        b1 = await Bridge.get_by_name(session, 'default')
        assert b1 is not None
        b1.bot_token = "token_b1"
        b1.self_id = 10000
        b1.state = BridgeStateEnum.running
        await b1.save(session)

        ct = ContextToken(bridge_id=b1.id, user_name="wx_user_reply", token="ctx_token_reply")
        await ct.save(session)

        # 预置消息映射：onebot 消息 14 -> ilink 消息 "wx_msg_ilink_14"
        mapping = OneBotMessageMapping(onebot_message_id=14, ilink_msg_id="wx_msg_ilink_14")
        await mapping.save(session)

        config = await ServerConfig.get_instance(session)

    reg = b1.get_id_registry()
    onebot_uid = reg.resolve_user_id("wx_user_reply")

    action_reply = OneBotAction(
        action="send_msg",
        params={
            "user_id": onebot_uid,
            "message": [
                {"type": "reply", "data": {"id": "14"}},
                {"type": "text", "data": {"text": "hello with reply"}},
            ],
        },
        echo="test_reply_echo",
    )

    # 场景 1：首次尝试携带 ref_msg 发送成功
    calls_scenario1: list[dict[str, Any]] = []

    async def mock_send_text_success(self, *args, **kwargs):
        calls_scenario1.append(kwargs)
        return "msg_123"

    with patch.object(Bridge, "send_text", mock_send_text_success):
        resp = await dispatch_action(b1, test_session_factory, config, action_reply)
        assert resp.retcode == 0
        assert len(calls_scenario1) == 1
        ref_msg = calls_scenario1[0].get("ref_msg")
        assert ref_msg is not None
        assert ref_msg.message_item is not None
        assert ref_msg.message_item.msg_id == "wx_msg_ilink_14"

    # 场景 2：iLink 不支持 reply（报错），自动回退丢弃 reply 段重发
    calls_scenario2: list[dict[str, Any]] = []

    async def mock_send_text_fallback(self, *args, **kwargs):
        calls_scenario2.append(kwargs)
        if kwargs.get("ref_msg") is not None:
            raise ILinkAPIError(errcode=10001, errmsg="reply not supported", endpoint="sendmessage")
        return "msg_124"

    with patch.object(Bridge, "send_text", mock_send_text_fallback):
        resp = await dispatch_action(b1, test_session_factory, config, action_reply)
        assert resp.retcode == 0
        # 应该调用 2 次：第 1 次带 ref_msg 失败，第 2 次不带 ref_msg 成功
        assert len(calls_scenario2) == 2
        assert calls_scenario2[0].get("ref_msg") is not None
        assert calls_scenario2[1].get("ref_msg") is None
        assert calls_scenario2[1].get("content") == "hello with reply"


@pytest.mark.asyncio
async def test_reply_segment_with_unmapped_id_falls_back_directly(test_session_factory):
    """测试未映射的 reply 段不会抛 NotImplementedError，而是丢弃 reply 段直接发送"""
    async with test_session_factory() as session:
        b1 = await Bridge.get_by_name(session, 'default')
        assert b1 is not None
        b1.bot_token = "token_b1"
        b1.self_id = 10000
        b1.state = BridgeStateEnum.running
        await b1.save(session)

        ct = ContextToken(bridge_id=b1.id, user_name="wx_user_unmapped", token="ctx_token_unmapped")
        await ct.save(session)

        config = await ServerConfig.get_instance(session)

    reg = b1.get_id_registry()
    onebot_uid = reg.resolve_user_id("wx_user_unmapped")

    action_unmapped = OneBotAction(
        action="send_msg",
        params={
            "user_id": onebot_uid,
            "message": [
                {"type": "reply", "data": {"id": "99999"}},  # 未映射的 ID
                {"type": "text", "data": {"text": "hello direct"}},
            ],
        },
        echo="test_unmapped_echo",
    )

    calls: list[dict[str, Any]] = []

    async def mock_send_text(self, *args, **kwargs):
        calls.append(kwargs)
        return "msg_direct"

    with patch.object(Bridge, "send_text", mock_send_text):
        resp = await dispatch_action(b1, test_session_factory, config, action_unmapped)
        assert resp.retcode == 0
        assert len(calls) == 1
        assert calls[0].get("ref_msg") is None
        assert calls[0].get("content") == "hello direct"


