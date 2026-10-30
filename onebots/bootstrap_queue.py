"""
OneBot v11 多账号启动握手队列

用于多账号连接时将无 self_id 的 get_login_info 顺序映射到对应账号。
独立成模块避免 ws_client / ws_server 与 action_handler 之间的循环导入。
"""

_pending_bootstrap_self_ids: list[int] = []


def push_pending_bootstrap_self_id(self_id: int) -> None:
    """记录刚刚发送了 bootstrap lifecycle 的 self_id，供后续 get_login_info 匹配"""
    _pending_bootstrap_self_ids.append(self_id)


def pop_pending_bootstrap_self_id() -> int | None:
    """提取最早待获取 login_info 的 self_id"""
    if _pending_bootstrap_self_ids:
        return _pending_bootstrap_self_ids.pop(0)
    return None
