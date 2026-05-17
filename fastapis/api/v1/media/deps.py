"""
媒体端点的依赖注入

把 "按 ID 查找已存在的 MediaFile" 提取为路由依赖，让路由签名直接声明
"我需要一个已存在的媒体文件"，404 处理消失到依赖层。

`MediaFile.get_exist_one()` 在 FastAPI 已安装的环境下会自动抛 HTTPException(404)。
"""
from typing import Annotated

from fastapi import Depends

from fastapis.deps import SessionDep
from sqlmodels.media_file import MediaFile


async def _get_media_file(file_id: int, session: SessionDep) -> MediaFile:
    """
    按整数路径参数 `file_id` 加载 MediaFile。

    未找到时 `get_exist_one` 自动抛出 HTTPException(404)，路由函数无需手动处理。
    """
    return await MediaFile.get_exist_one(
        session, file_id, detail=f"MediaFile {file_id} not found",
    )


MediaFileDep = Annotated[MediaFile, Depends(_get_media_file)]
"""为路由注入一个按 `{file_id}` 加载的 MediaFile（404 已在依赖层抛出）"""
