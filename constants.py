"""
项目级常量（跨模块共享）

最大公约数原则：只有被多个模块同时使用的常量才放在这里。
模块私有常量定义在对应的 `{module}/constants.py` 中。
"""

BRIDGE_SINGLETON_ID: int = 1
"""Bridge 单例固定 ID（sqlmodels、typers、fastapis 都会用到）"""
