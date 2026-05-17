# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — ilink-onebot 单文件 exe 打包配置

打包后输出 ``dist/ilink-onebot.exe``，所有依赖、迁移文件、配置文件都嵌入二进制。

设计要点：
- **入口**：``typers/__main__.py``（``python -m typers`` 的等价入口），让打包出的
  exe 直接 ``ilink-onebot.exe login`` / ``run`` / ``status`` 工作
- **datas**：``alembic/`` 整个目录（迁移文件由 Alembic 在运行时通过文件系统路径
  动态加载，PyInstaller 不会跟踪它们）+ ``alembic.ini`` 配置
- **hiddenimports**：声明所有运行时通过字符串 / 反射加载的模块。重点：
  - ``aiosqlite``：通过 URL ``sqlite+aiosqlite://`` 字符串解析，不会被静态 import 跟踪
  - ``sqlalchemy.dialects.sqlite``：同上
  - ``cryptography.hazmat.backends.openssl``：通过 cffi 后端延迟加载
  - ``sqlmodel_ext.*``：项目通过 dot-path 引用部分子模块
- **excludes**：剔除 tk / PySide / IPython 等开发期才需要的可选依赖，减小体积

构建命令::

    pyinstaller ilink-onebot.spec

或通过项目 build script::

    python scripts/build.py
"""
from pathlib import Path

# spec 文件运行时 cwd 是项目根
PROJECT_ROOT = Path('.').absolute()


# ===== 数据文件（运行时通过文件系统路径访问，非 Python import） =====

datas: list[tuple[str, str]] = [
    # Alembic 迁移：env.py / script.py.mako / versions/*.py 都被 Alembic
    # 通过 importlib.util.spec_from_file_location 动态加载，PyInstaller 不跟踪
    (str(PROJECT_ROOT / 'alembic'), 'alembic'),
    # Alembic 主配置（DatabaseManager 在 frozen 模式下从 sys._MEIPASS 读取）
    (str(PROJECT_ROOT / 'alembic.ini'), '.'),
    # .env 示例：给用户参考，不含敏感数据
    (str(PROJECT_ROOT / '.env.example'), '.'),
]


# ===== 显式声明的延迟 import =====

hiddenimports: list[str] = [
    # SQLite 异步驱动：通过 URL "sqlite+aiosqlite://" 字符串选择，非静态 import
    'aiosqlite',
    'sqlalchemy.dialects.sqlite',
    'sqlalchemy.dialects.sqlite.aiosqlite',
    # cryptography 的 OpenSSL 后端通过 cffi 延迟加载
    'cryptography.hazmat.backends.openssl',
    'cryptography.hazmat.bindings._rust',
    # sqlmodel-ext 子模块：项目部分代码通过 dot-path 引用
    'sqlmodel_ext.base',
    'sqlmodel_ext.cond',
    'sqlmodel_ext.field_types',
    'sqlmodel_ext.mixins',
    'sqlmodel_ext.mixins.table',
    'sqlmodel_ext.mixins.cached_table',
    'sqlmodel_ext.mixins.polymorphic',
    'sqlmodel_ext.mixins.constants',
    'sqlmodel_ext.pagination',
    'sqlmodel_ext.relation_load_checker',
    # uvicorn 默认走 asyncio loop（避免 uvloop / httptools 缺失警告）
    'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.lifespan.on',
    # qrcode 子模块（部分通过 module path 字符串加载）
    'qrcode.image.pil',
    'qrcode.image.pure',
    # email_validator（pydantic 在某些验证场景下可能 lazy import）
    'email_validator',
]


# ===== 不需要打包的模块（减小体积） =====

excludes: list[str] = [
    # GUI 框架：本项目是 CLI / WS / HTTP 服务，不需要图形界面
    'tkinter',
    'PySide6',
    'PyQt5',
    'PyQt6',
    'matplotlib',
    # Jupyter 生态：开发期工具，运行时不需要
    'IPython',
    'jupyter',
    'notebook',
    # 测试框架：仅 dev 依赖
    'pytest',
    'pytest_asyncio',
]


# ===== PyInstaller Analysis（依赖分析阶段） =====

a = Analysis(
    [str(PROJECT_ROOT / 'typers' / '__main__.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ===== 字节码压缩 =====

pyz = PYZ(a.pure)

# ===== 单文件 EXE 输出（onefile 模式） =====

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ilink-onebot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # UPX 压缩（如系统安装了 upx，会显著减小体积）
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # CLI 应用，需要控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
