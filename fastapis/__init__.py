"""
FastAPI 媒体 HTTP 服务（薄传输层）

职责：通过 HTTP URL 暴露已解密的 iLink 媒体文件，供 OneBot 框架（NoneBot2 等）
下载图片/语音/视频/文件。

为什么需要本地媒体服务：
- iLink CDN 上的媒体用 AES-128-ECB 加密，OneBot 框架无法直接下载
- 必须由桥接下载 → 解密 → 通过本地 HTTP 服务暴露明文 URL → 写入 OneBot 段

URL 模式：`http://{host}:{port}/api/v1/media/{file_id}`，由 `MediaFile.url_suffix`
property 生成相对路径，调用方拼接 `meta_config.MEDIA_SERVE_PUBLIC_BASE_URL`。

依赖注入：
- `SessionDep`：每个请求独立的 AsyncSession（由 DatabaseManager.get_session 提供）
- `BridgeDep`：Bridge 单例（每次请求查询，命中 sqlmodel-ext 缓存层）

API 结构遵循"目录即路径"惯例：`fastapis/api/v1/<resource>/__init__.py` 定义路由，
相对位置即 URL 路径。
"""
from fastapis.app import create_app
