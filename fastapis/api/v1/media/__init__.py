"""
媒体文件 HTTP 服务端点

`GET /api/v1/media/{file_id}` 返回已解密的明文媒体文件。

行为：
- 缓存命中：``MediaFile.ensure_decrypted`` 检测到 ``local_path`` 已存在直接返回
- 缓存未命中：``MediaFile.ensure_decrypted`` 触发流式下载 + AES 解密 + （VOICE 自动
  silk → wav 转码）
- DB 中无此 ID：MediaFileDep 自动抛 HTTP 404
- 解密失败：500（通过 ``raise_internal_error`` 助手）

Content-Type 优先使用 `MediaFile.mime_type`，缺失时由 FastAPI 根据扩展名推断。

设计要点：
- `MediaFileDep` 提取为独立模块 deps.py，路由签名直接声明意图
- 异常通过 `utils/http_exceptions` 中的 helpers 抛出，禁止裸 `raise HTTPException`
- 不做认证：媒体 URL 含整数 ID 可枚举。公网部署应在反向代理添加签名 URL 或 IP 白名单
- 不设 cache header：交给 OneBot 框架处理重复下载（NoneBot2 默认会缓存）
"""
from fastapi import APIRouter
from fastapi.responses import FileResponse
from loguru import logger as l

from fastapis.api.v1.media.deps import MediaFileDep
from fastapis.deps import BridgeDep, SessionDep
from utils.http_exceptions import raise_internal_error

router = APIRouter(prefix='/media', tags=['media'])


@router.get('/{file_id}', response_class=FileResponse)
async def get_media(
    media: MediaFileDep,
    session: SessionDep,
    bridge: BridgeDep,
) -> FileResponse:
    """
    返回解密后的媒体文件内容。

    :raises HTTPException 404: 文件 ID 不存在（由 MediaFileDep 自动抛出）
    :raises HTTPException 500: 下载或解密失败
    """
    try:
        local_path = await media.ensure_decrypted(
            bridge=bridge,
            session=session,
            commit=True,
        )
    except Exception as e:
        l.exception(f"MediaFile {media.id} 解密/下载失败: {e}")
        raise_internal_error(f"Media decryption failed: {e}")

    return FileResponse(
        path=local_path,
        media_type=media.mime_type,
        filename=media.file_name,
    )
