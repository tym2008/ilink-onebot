# ilink-onebot 开发规范

## 项目定位
将微信 iLink 协议（ClawBot 插件开放的官方个人号 Bot API）桥接到 OneBot v11 标准协议，
使 NoneBot2 等 OneBot v11 兼容框架能够接收微信消息。

## 项目环境
- Python 3.14+
- 虚拟环境: `.venv/`
- 包管理: `uv`（`uv sync` / `uv add <package>`）
- 类型检查: `.venv/Scripts/basedpyright.exe`（零错误）
- 运行: `python -m typers` 或 `ilink-onebot <command>`

## 技术栈
| 用途 | 选型 |
|------|------|
| ORM | sqlmodel-ext（`SQLModelBase`, `TableBaseMixin`, 字段类型） |
| DB | SQLite + aiosqlite |
| HTTP 客户端 | aiohttp + `AioHttpClientSessionMixin` |
| WS 服务端 | websockets 库 |
| HTTP 服务端 | FastAPI + uvicorn（媒体服务） |
| CLI | typer + `asyncio.run()` |
| 配置 | pydantic-settings（`.env`） |
| 加密 | pycryptodome（AES-128-ECB） |
| JSON | orjson（禁止标准库 json） |
| 日志 | loguru（`from loguru import logger as l`） |

## 架构分层（DDD）
```
typers/    ← CLI 端点层（login/run/status）
onebots/   ← OneBot v11 协议层（WS 服务端）
fastapis/  ← HTTP 端点层（媒体服务）
           ↓ 都调用领域层方法
sqlmodels/ ← 领域核心层（Bridge 充血模型 + MediaFile）
utils/     ← 基础设施（HTTP/日志/加密）
```

**Bridge 是核心充血模型**：包含 iLink 会话状态 + 所有 API 业务方法 + 消息转换逻辑。
三个端点层都是薄适配器，最终调用 Bridge 的业务方法。

## 设计哲学
1. **充血模型**: SQLModel 实例同时含 DB 字段和业务方法
2. **会话参数化**: 方法接收 `session: AsyncSession`，不存储 session 引用
3. **运行时状态**: `model_post_init()` 初始化 `_` 前缀属性，不存 DB
4. **ClassVar 单例**: 需要生命周期管理的全局服务用 ClassVar 模式
5. **异步优先**: 所有 IO 操作异步（aiohttp, aiosqlite, aiofiles）
6. **严格类型安全**: Python 3.10+ 语法（`int | None`），禁用 Any
7. **错误快速失败**: 不返回 None 表示错误，抛异常
8. **单一真相源**: 运行时可变配置存 Bridge 模型字段，基础设施配置存 `.env`

## 代码规范
- UTF-8 编码，4 空格缩进，f-string 格式化
- 单引号 `'` 给机器看（dict key），双引号 `"` 给人看（log 消息）
- 字段描述用 docstring `"""..."""`，不用 `Field(description=...)`
- `from loguru import logger as l`
- **禁止裸 Pydantic**: 一律用 sqlmodel-ext 的 `SQLModelBase` / `ExtraIgnoreModelBase`
- **禁止 `global` 关键字**: 用 ClassVar + classmethod 单例模式
- **禁止 `assert` 做生产守卫**: `python -O` 会跳过，用 `raise`
- **禁止空容器代替 None**: `None` 语义是"不存在/不适用"，空容器（`{}`/`[]`/`''`）
  语义是"存在但为空"。不可互换。`payload: dict | None = None` 正确，`payload = {}` 再
  条件添加字段错误——这是用构造过程代替值的类型表达
- **依赖注入优于自取**: `session_factory` / `Bridge` / `ServerConfig` 等单例必须通过
  参数传递，不在方法内部调用 `DatabaseManager.get_session_factory()` 等自取。
  唯一允许自取的地方是系统入口（`typers/bootstrap.py`、`fastapis/deps.py`）。
  下游闭包用 `_make_xxx_callback(session_factory)` 工厂模式注入
- **禁止函数内 import**: 除 `TYPE_CHECKING` 块外
- **禁止 `__all__`**: 显式 `from .xxx import ...` 已表达导出意图
- **最大公约数原则**: 只在某个模块使用的常量定义在 `{module}/constants.py`，
  跨模块共享的才放项目级 `constants.py`

## 日志规范
- loguru 自动添加时间戳和函数名，禁止手动 `TAG = "[Module]"` 前缀
- 日志文件默认 2MB 轮转 + 保留 5 个 + gzip 压缩（保守设置防磁盘膨胀）
- 配置通过 `meta_config.LOG_ROTATION_SIZE` 等字段调整

## 数据库迁移（Alembic）
项目用 Alembic 管理 SQLite schema 变更，避免手动 create_all 导致的不可追溯。

日常工作流：
```bash
# 1. 修改 SQLModel 后生成迁移
.venv/Scripts/alembic.exe revision --autogenerate -m '描述变更'
# 2. 检查 alembic/versions/ 下生成的脚本（autogenerate 不完美）
# 3. 应用迁移（启动时自动运行）
.venv/Scripts/alembic.exe upgrade head
```

常用命令：
```bash
.venv/Scripts/alembic.exe current     # 查看当前版本
.venv/Scripts/alembic.exe history     # 查看迁移历史
.venv/Scripts/alembic.exe downgrade -1  # 回滚一个版本
```

**启动时自动迁移**: `DatabaseManager.init()` 会执行 `alembic upgrade head`。
**SQLite batch 模式**: `env.py` 设置 `render_as_batch=True`，因为 SQLite 的
ALTER TABLE 能力有限，Alembic 需要通过"复制表→删除→重命名"的 batch 模式处理。

**迁移文件命名规范**：
- 执行 `date` 获取系统时间，禁止凭记忆填写时间戳
- 文件名格式：`YYYYMMDD_HHMMSS_描述.py`
- revision ID：相同的 `YYYYMMDD_HHMMSS` 时间戳

## 关键模块
- `utils/aiohttp_mixin.py`: 共享 aiohttp.ClientSession + 自动重试（429/5xx）
- `utils/crypto.py`: iLink 的 AES-128-ECB 加解密（IMAGE 和其他类型密钥编码不同）
- `sqlmodels/database_manager.py`: SQLite 连接管理 + 启动时 Alembic upgrade
- `sqlmodels/bridge/bridge.py`: Bridge 充血模型（核心实体）
- `onebots/ws_server.py`: OneBot v11 Reverse WS 服务端
- `typers/run.py`: 主运行循环（编排 WS + FastAPI + 轮询 + 心跳）

## 参考
- OneBot v11 规范: https://github.com/botuniverse/onebot-11
- sqlmodel-ext: https://pypi.org/project/sqlmodel-ext/
