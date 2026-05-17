"""
DatabaseManager — SQLite + aiosqlite 连接管理

纯 classmethod 单例模式：
- 启动时自动执行 `alembic upgrade head` 应用所有迁移
- StaticPool 允许异步连接复用（SQLite + aiosqlite 的标准做法）
- SQLite 专用 PRAGMA：WAL 模式 + 外键约束启用

PyInstaller 兼容：``_resolve_alembic_assets`` 在 frozen 环境下从 ``sys._MEIPASS``
解析 ``alembic.ini`` 和 ``alembic/`` 目录路径（PyInstaller onefile 模式的临时
解压目录）；源代码运行时回退到项目根目录。

:author: Foxerine <https://github.com/Foxerine>
"""
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, ClassVar

from alembic import command
from alembic.config import Config as AlembicConfig
from anyio import to_thread
from loguru import logger as l
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel.ext.asyncio.session import AsyncSession

DB_NOT_INITIALIZED_MSG = "数据库引擎未初始化，请先调用 DatabaseManager.init()"


def _resolve_alembic_assets() -> tuple[Path, Path]:
    """
    解析 ``alembic.ini`` 文件路径 + ``alembic/`` 脚本目录路径。

    - PyInstaller frozen 模式：从 ``sys._MEIPASS`` 临时解压目录读取（spec 文件已
      把 alembic.ini 和 alembic/ 加入 datas）
    - 普通源码运行：从项目根目录（``database_manager.py`` 的祖父目录）读取

    返回 ``(alembic_ini_path, alembic_script_dir)`` 元组。
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        base = Path(sys._MEIPASS)  # pyright: ignore[reportAttributeAccessIssue]
    else:
        # database_manager.py 位于 <project_root>/sqlmodels/database_manager.py
        base = Path(__file__).resolve().parent.parent
    return base / 'alembic.ini', base / 'alembic'


@event.listens_for(Engine, 'connect')
def _sqlite_set_pragma(dbapi_connection: Any, connection_record: Any) -> None:
    """
    SQLite 连接时设置关键 PRAGMA：
    - WAL 模式：写时复制日志，允许并发读 + 单写
    - foreign_keys: SQLite 默认不强制外键，必须显式启用
    - synchronous=NORMAL: WAL 下的推荐值，平衡性能与耐久性
    """
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA foreign_keys=ON')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.close()


class DatabaseManager:
    """
    纯 classmethod 单例模式。

    此类永不实例化。通过 init() 初始化全局引擎，通过 get_session() 或
    get_session_factory() 访问会话。
    """

    engine: ClassVar[AsyncEngine | None] = None
    _session_factory: ClassVar[async_sessionmaker[AsyncSession] | None] = None

    def __new__(cls) -> 'DatabaseManager':
        raise RuntimeError("DatabaseManager 是纯 classmethod 单例，禁止实例化")

    @classmethod
    async def init(cls, database_url: str, echo_sql: bool = False) -> None:
        """
        初始化数据库引擎并应用 Alembic 迁移到最新版本。

        :param database_url: SQLite 连接字符串（如 `sqlite+aiosqlite:///data/bridge.db`）
        :param echo_sql: 是否打印 SQL 语句（调试用）
        """
        if cls.engine is not None:
            l.debug("DatabaseManager 已初始化，幂等返回")
            return

        # SQLite 文件路径：确保父目录存在
        if database_url.startswith('sqlite+aiosqlite:///'):
            db_path_str = database_url.replace('sqlite+aiosqlite:///', '', 1)
            if db_path_str and db_path_str != ':memory:':
                db_path = Path(db_path_str)
                db_path.parent.mkdir(parents=True, exist_ok=True)

        cls.engine = create_async_engine(
            database_url,
            echo=echo_sql,
            future=True,
            poolclass=StaticPool,
            connect_args={'check_same_thread': False},
        )

        cls._session_factory = async_sessionmaker(
            cls.engine,
            class_=AsyncSession,
            expire_on_commit=True,
        )

        # 应用 Alembic 迁移到最新版本
        await cls._run_alembic_upgrade(database_url)

        l.info(f"DatabaseManager 初始化完成: {database_url}")

    @classmethod
    async def _run_alembic_upgrade(cls, database_url: str) -> None:
        """
        异步执行 ``alembic upgrade head``。

        Alembic 的 ``command.upgrade`` 是同步 API，通过 ``anyio.to_thread`` 在工作
        线程中运行，避免阻塞 asyncio 事件循环。

        路径解析：通过 ``_resolve_alembic_assets`` 兼容 PyInstaller frozen 模式
        （从 ``sys._MEIPASS`` 读取打包进 exe 的迁移文件）和源代码运行（从项目根读取）。
        """
        alembic_ini, alembic_script_dir = _resolve_alembic_assets()

        def _upgrade() -> None:
            alembic_cfg = AlembicConfig(str(alembic_ini))
            # frozen 模式下 alembic.ini 的 script_location 是相对工作目录的，需要
            # 显式覆盖为打包后的实际位置
            alembic_cfg.set_main_option('script_location', str(alembic_script_dir))
            # 动态注入 URL（alembic.ini 中留空，从 meta_config 读取）
            alembic_cfg.set_main_option('sqlalchemy.url', database_url)
            command.upgrade(alembic_cfg, 'head')

        l.info(f"开始执行 Alembic 迁移（ini={alembic_ini}）...")
        await to_thread.run_sync(_upgrade)
        l.info("Alembic 迁移完成")

    @classmethod
    async def get_session(cls) -> AsyncGenerator[AsyncSession, None]:
        """获取数据库会话（FastAPI Depends 风格）"""
        if cls._session_factory is None:
            raise RuntimeError(DB_NOT_INITIALIZED_MSG)
        async with cls._session_factory() as session:
            yield session

    @classmethod
    def get_session_factory(cls) -> async_sessionmaker[AsyncSession]:
        """获取 session factory（用于手动创建 session）"""
        if cls._session_factory is None:
            raise RuntimeError(DB_NOT_INITIALIZED_MSG)
        return cls._session_factory

    @classmethod
    async def close(cls) -> None:
        """关闭数据库引擎（幂等）"""
        if cls.engine is None:
            return
        await cls.engine.dispose()
        cls.engine = None
        cls._session_factory = None
        l.info("DatabaseManager 已关闭")
