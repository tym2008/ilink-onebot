# ilink-onebot

![status](https://img.shields.io/badge/status-WIP-orange) ![python](https://img.shields.io/badge/python-3.14%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

> [!WARNING]
> **🚧 开发中（Work In Progress）**
> 本项目仍处于早期开发阶段，API、配置项、数据库 schema 与运行行为**可能随时发生不兼容变更**，
> 尚未在生产环境充分验证。请勿用于关键生产场景。欢迎试用、提 Issue 与 PR——但请自行承担风险。

**让你的 NoneBot2、AstrBot 等 OneBot v11 兼容框架直接收发微信消息。**

微信于 2026 年 3 月通过 **ClawBot 插件**正式开放了首个官方个人号 Bot API（iLink
协议），但官方生态目前只提供了 OpenClaw 与 SDK 直连方式，**没有桥接器把它接入
OneBot v11 生态**。本项目填补这一空白——你可以继续用熟悉的 OneBot 框架开发对话
机器人，桥接器透明处理底层协议差异。

> **同类参考**：本项目在性质上等同于 `napcat`（QQ→OneBot v11）、`lagrange`
> （QQ→OneBot v11）、`gocqhttp` 历史版本——把某个 IM 平台的协议格式转换为 OneBot v11
> 标准协议；区别在于本项目调用的是腾讯官方开放的 iLink HTTP API，**不涉及任何客户端
> hook、协议逆向、模拟器自动化**。

---

## 它能做什么

- ✅ **直接复用 OneBot v11 框架**：写一次插件，QQ 和微信都能用
- ✅ **支持多账号同时在线**：单进程支持配置与并发运行多个微信 Bot，共用一条 WS 连接，各自独立维护 `self_id`
- ✅ **支持全部 iLink 消息类型**：文本 / 图片 / 语音 / 视频 / 文件 / 引用回复
- ✅ **正向与反向 WebSocket**：支持框架连入（正向 WS 模式）与主动连出到框架（反向 WS 客户端模式）
- ✅ **会话持久化**：进程重启后自动恢复所有账号的登录状态和长轮询游标，不丢消息
- ✅ **优雅扫码登录**：终端直接渲染 QR 码，远程 SSH 也能扫
- ✅ **零侵入部署**：你的 OneBot 框架不用改一行代码，把它当 NapCat / Lagrange 一样接入
- ✅ **自动重登**：iLink 会话过期（errcode -14）后自动引导扫码重登

## 它**不**能做什么

- ❌ **群聊**：iLink 协议本身不支持群聊，仅支持 1:1 私聊
- ❌ **撤回消息**：iLink 无撤回 API
- ❌ **主动发消息**：所有发送必须由用户先向对应账号发消息（建立 `context_token`）后触发——这是
  iLink 协议层面的安全与风控设计，非桥接器限制

---

## 快速开始

### 1. 安装

需要 Python 3.14+ 和 [uv](https://docs.astral.sh/uv/) 包管理器。

```bash
git clone https://github.com/Foxerine/ilink-onebot.git
cd ilink-onebot
uv sync
cp .env.example .env       # 端口、缓存路径等可在此处调整
```

### 2. 扫码登录（支持多账号）

**登录首个/默认账号（默认 self_id=10000）：**
```bash
uv run ilink-onebot login
```

**登录第二个账号（指定别名及可选的 self_id）：**
```bash
uv run ilink-onebot login bot2 --self-id 10001
```

终端会渲染一个 QR 码，用对应微信"扫一扫"即可。登录凭据自动持久化至本地 SQLite。若需重置会话重新扫码，追加 `--force` 参数即可。

### 3. 账号管理

```bash
# 查看所有已登录账号、self_id、微信昵称与状态
uv run ilink-onebot account list

# 移除指定的账号及其会话数据
uv run ilink-onebot account remove bot2
```

### 4. 启动主服务

```bash
uv run ilink-onebot run
```

主服务启动后会自动为所有已就绪账号拉起并发长轮询，并通过 WebSocket 提供 OneBot 服务：

| 服务 | 模式 / 协议 | 说明 |
|------|-----------|------|
| `ws://0.0.0.0:6700/` | WS 服务端（正向） | 监听端口，等待 OneBot 框架连接接入 |
| `ws://<框架地址>/...` | WS 客户端（反向） | 主动连接到框架（通过 `.env` 中的 `ONEBOT_REVERSE_WS_URL` 启用） |
| `http://0.0.0.0:5701/` | HTTP 媒体服务 | 媒体下载中继代理（框架在此获取解密后的图片/音频/视频） |
| `https://ilinkai.weixin.qq.com` | HTTPS 出站 | 各账号独立的腾讯 iLink 消息长轮询通道 |

### 5. 连接你的 OneBot 框架

- **正向连接（框架主动连接 ilink-onebot）**：以 NoneBot2 为例，在 `.env.dev` 中配置：
  ```env
  ONEBOT_WS_URLS=["ws://127.0.0.1:6700/"]
  ```
- **反向连接（ilink-onebot 主动连接框架）**：以 TRSS-Yunzai 或 NoneBot2 反向端点为例，在 `ilink-onebot/.env` 中配置：
  ```env
  ONEBOT_REVERSE_WS_URL=ws://127.0.0.1:2536/OneBotv11
  ```
  连接握手时，所有已登录的 Bot 将会在同一条连接上广播各自的 `lifecycle: connect` / `lifecycle: enable` 事件并同时上线（如 TRSS-Yunzai 中 `Bot[10000]` 与 `Bot[10001]` 同时点亮）。

### 6. 查看运行状态

```bash
uv run ilink-onebot status
```

输出所有 Bridge 账号状态、微信昵称、配置详情及媒体缓存统计。

---

## 部署提示

- **生产环境**：建议把 OneBot WS 配置 `ONEBOT_ACCESS_TOKEN` 防止未授权连接
- **NAT / 反向代理**：媒体 URL 需要公网可达。在 `.env` 设置
  `MEDIA_SERVE_PUBLIC_BASE_URL=https://your-domain.com`
- **媒体缓存**：默认 1 小时 TTL，可通过 `MEDIA_CACHE_TTL_SECONDS` 调整。
  日志默认 2 MB 滚动 + 保留 5 份压缩存档，避免磁盘膨胀
- **数据目录**：默认 `data/`（SQLite 数据库 + 媒体缓存）、`logs/`（日志轮转）。
  这两个目录已经 `.gitignore`，可挂载为 Docker volume / K8s PVC 持久化

---

## 单文件 exe 打包

项目支持通过 PyInstaller 打包为单个可执行文件，免去用户安装 Python / 创建虚拟
环境的麻烦。

```bash
# 1. 同步开发依赖（含 PyInstaller）
uv sync --all-groups

# 2. 一键打包（产物 dist/ilink-onebot.exe）
.venv/Scripts/python.exe scripts/build.py

# 3. 直接运行 exe
dist/ilink-onebot.exe --help
dist/ilink-onebot.exe login
dist/ilink-onebot.exe run
```

打包产物特性：
- **单文件**：所有 Python 解释器 + 依赖 + 迁移文件 + 配置全部嵌入 ≈ 30–50 MB
- **自包含 Alembic 迁移**：`alembic/versions/*.py` 通过 `sys._MEIPASS` 解压，
  新机器首次运行会自动建表
- **`.env` 仍从工作目录读**：把 `.env` 放在 exe 旁边（参考 `.env.example`）

---

## 协议支持矩阵

### OneBot v11 动作（actions）

| 动作 | 支持 | 说明 |
|------|------|------|
| `send_private_msg` / `send_msg`(private) | ✅ | 文本 / reply / 媒体段（支持多账号根据 user_id 或 self_id 智能路由） |
| `get_login_info` | ✅ | 动态返回对应账号的 `self_id` 及微信昵称 |
| `get_status` / `get_version_info` | ✅ | 元信息查询 |
| `can_send_image` / `can_send_record` | ✅ | 始终返回 true |
| `get_friend_list` / `get_stranger_info` | ✅ | 从数据库会话记录返回已交互联系人（兼容 TRSS-Yunzai 等框架初始化） |
| `get_group_list` / `get_group_member_list` | ✅ | 返回空列表（个人号无群聊，防报错兼容） |
| `_set_model_show` / `get_guild_*` / `get_online_clients` | ✅ | 框架兼容性占位响应 |
| `send_group_msg` / 群管理类 | ❌ | iLink 不支持群聊，返回 retcode=1404 |
| `delete_msg` | ❌ | iLink 无撤回 API，返回 retcode=1404 |

### 消息段映射

| iLink 类型 | OneBot v11 段 | 备注 |
|-----------|--------------|------|
| `TEXT`    | `text`       |      |
| `IMAGE`   | `image`      | 自动解密 + 通过本地媒体服务暴露 |
| `VOICE`   | `record`     | 同上 |
| `FILE`    | `file`       | NapCat 扩展段 |
| `VIDEO`   | `video`      | 同上 |
| `ref_msg` | `reply`      | 引用回复 |

入站的 `GENERATING` 状态消息会被跳过（OneBot v11 无流式消息概念，等 `FINISH` 再发）。

---

## 项目架构

```
                    OneBot 框架（NoneBot2 / AstrBot / Koishi 等）
                              ▲
                              │ WebSocket
                              ▼
    ┌─────────── onebots/  (OneBot v11 协议层) ───────────┐
    │  ws_server.py   action_handler.py   heartbeat.py   │
    └────────────────────────┬───────────────────────────┘
                             │
              ┌──────────────▼───────────────┐
              │  sqlmodels/bridge/  (领域核心) │
              │   Bridge 充血模型（编排中枢）    │
              │   converters / polling / etc. │
              └──────────────┬───────────────┘
                             │ HTTP
    ┌───── fastapis/ ────────┘   ┌──────  typers/ ──────┐
    │  /api/v1/media/{id}        │  login / run / status │
    │  （媒体下载代理）              │  （CLI 端点）           │
    └─────────────────────────   └───────────────────────┘
                             ▲
                             │ HTTP 长轮询 + CDN
                  iLink 服务器（ilinkai.weixin.qq.com）
```

| 层 | 职责 |
|----|------|
| `typers/`         | CLI 入口，用 typer + `asyncio.run()` |
| `onebots/`        | OneBot v11 协议层（WS + action + heartbeat），调用 Bridge 业务方法 |
| `fastapis/`       | HTTP 媒体服务（FastAPI + uvicorn 嵌入式运行） |
| `sqlmodels/bridge/` | Bridge 充血模型，编排中枢，聚合 iLink API、消息转换、轮询循环 |
| `sqlmodels/*`     | 其他领域模型：`ContextToken` / `MediaFile` / `OneBotMessageMapping` / `ServerConfig` |
| `utils/`          | 共享 HTTP 会话、加密、日志、HTTP 异常 helpers |

## 配置三分层

| 层 | 何时确定 | 在哪修改 | 示例 |
|----|---------|---------|------|
| **`meta_config`** | 部署时 | `.env` | 网络端口、磁盘路径、外部 URL、超时阈值 |
| **`ServerConfig`** | 运行时可调 | DB 单例表 | 轮询间隔、心跳频率、重试策略 |
| **`Bridge`** | 协议产生 | 由桥接自动维护 | bot_token、扫码状态、wechat_nickname |

详见 [CLAUDE.md](CLAUDE.md)。

---

## 兼容的 OneBot v11 框架

只要遵循 [OneBot v11 标准](https://github.com/botuniverse/onebot-11)，通过正向 WS（`ws://127.0.0.1:6700/`）或反向 WS（主动连向框架端点）都可无缝对接：

- [TRSS-Yunzai](https://github.com/TimeRainStarSky/Yunzai)（搭配 `OneBotv11` 适配器插件，已实测反向 WS 与单连接多账号 `Bot[self_id]` 同时上线）
- [NoneBot2](https://github.com/nonebot/nonebot2)（已实测正向与反向 WS）
- [AstrBot](https://github.com/Soulter/AstrBot)
- [Koishi](https://koishi.chat/)（搭配 [onebot 适配器](https://github.com/koishijs/koishi-plugin-adapter-onebot)）
- 其他兼容框架欢迎在 Issues 区报告兼容性

---

## 故障排查

### 启动时报 `iLink session expired (-14)`

iLink bot_token 有有效期（具体 TTL 未公开，经验值数天到数周）。重新跑
`ilink-onebot login` 扫码即可。`ServerConfig.auto_logout_on_session_expired`
默认为 `true`，桥接会自动引导。

### OneBot 框架收不到消息

按顺序排查：
1. `ilink-onebot status` 看 Bridge 是否 `running`
2. WebSocket 是否真的连上：`ss -tnlp | grep 6700`
3. `tail -f logs/ilink-onebot.log` 看是否有 `[iLink] getupdates` 流水
4. 是否被 `ONEBOT_ACCESS_TOKEN` 拦截（连接时框架须发 `Authorization: Bearer ...`
   或 query 参数 `?access_token=...`）

### 发图/视频报 `404` 或 `Connection refused`

OneBot 框架试图通过媒体 URL 下载，但 URL 不可达。检查：
- `MEDIA_SERVE_PUBLIC_BASE_URL` 是否配置正确（NAT/反代场景必填）
- 媒体服务端口 `5701` 是否对框架进程可达（容器化部署须显式开端口）

### iLink 上传/发送报 `errcode != 0`

iLink 业务错误码（如 `1404` 频率限制、`1502` 内容审核拦截）会原样透传到日志。
常见原因：
- 单日发消息量过大触发腾讯风控
- 内容被腾讯安全审核拦截（条款赋予腾讯此权利）
- 媒体上传时计算的 `aes_key` 编码方式错（IMAGE 与其他类型规则不同，见 `utils/crypto.py`）

---

## 免责声明

**本项目是基于公开协议的合规互操作性实现。**

1. **使用官方授权 API**：本项目仅通过腾讯于 2026 年 3 月正式开放的官方 iLink Bot
   API 接入微信。该接口由微信通过 ClawBot 插件主动开放给开发者使用，**不涉及任何
   逆向工程、协议破解、客户端 hook、模拟器自动化或其他未授权技术手段**。腾讯官方
   仓库 [`Tencent/openclaw-weixin`](https://github.com/Tencent/openclaw-weixin)
   以 MIT 协议公开了该 API 的客户端实现，本项目遵循同一协议规范。

2. **协议格式转换的合规边界**：本项目把 iLink 协议**格式**转换为 OneBot v11
   **格式**，**不改变 iLink 协议的实质用途**——所有消息收发的实质都是把外部内容
   传递给微信用户。OneBot v11 协议本身只是一个标准化的消息事件格式（同
   Telegram Bot API、Matrix Client-Server API 性质相同），不规定消息的最终处理者
   必须是 AI。最终连接到 OneBot 框架的服务是不是 AI 由使用者自行决定。**使用者
   须自行判断其具体使用场景是否符合《微信 ClawBot 功能使用条款》关于"第三方 AI
   服务"的定义边界**；如腾讯认定使用方式不符合，最直接的后果是 bot_token 被吊销
   或速率限制，与本项目源码的开源合法性无关。

3. **互操作性的合理应用**：本项目把 iLink 协议转换为 OneBot v11 标准协议，是
   软件互操作性（Interoperability）的常见实践——同 NapCat、Lagrange 等将其他即
   时通讯协议接入 OneBot v11 的开源桥接器在性质上完全一致。互操作性受多数司法
   管辖区版权法的明确豁免（如美国 DMCA §1201(f)、欧盟 Software Directive 2009/24/EC
   第 6 条等）保护。

4. **用户责任**：使用者须自行确保使用方式符合腾讯微信《软件许可及服务协议》、
   《微信 ClawBot 功能使用条款》及当地法律法规。本项目作者对使用者的具体使用
   行为不承担任何责任。**请勿用于自动化营销骚扰、违法内容传播、未经授权的信息
   收集或任何会损害他人权益的用途**——这类滥用是 2020 年杭州互联网法院相关
   反不正当竞争判决的核心要件，与桥接器本身的工具属性无关，但使用者将承担直接
   法律责任。

5. **数据隐私**：本桥接器在用户自有的设备上运行，所有数据（消息、媒体、会话
   凭据）均存储在用户本地 SQLite。项目本身不收集、上传或分发任何用户数据。
   使用者应自行评估在本地存储微信会话凭据的安全风险，特别是在多用户共享主机
   或公开环境部署时。

6. **无关联声明**：本项目与腾讯公司、微信团队、NoneBot 团队、Anthropic（Claude AI
   的提供者）等任何商业实体**无任何附属、合作或背书关系**。"iLink" / "微信" /
   "ClawBot" / "OpenClaw" / "OneBot" / "NoneBot" 等名词仅作为协议标识符或框架
   名称使用，相关商标归各自所有者所有。

7. **学习与研究用途**：本项目以学习、研究和个人合理使用为目的发布。作者不为
   任何商业部署、规模化运营或违反第三方服务条款的行为提供支持。

---

## 贡献

欢迎 Issue 与 PR。提交代码前请阅读 [CLAUDE.md](CLAUDE.md) 中的代码规范——
项目对类型安全、异步编程、SQLModel 充血模型有较严格的约定。

## 许可

[MIT License](LICENSE) © 2026 Foxerine

MIT 许可证仅适用于本项目源代码本身，不构成对任何第三方协议（如微信《软件许可
及服务协议》、《微信 ClawBot 功能使用条款》）使用方式的授权。
