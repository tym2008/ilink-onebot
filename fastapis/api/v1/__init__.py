"""FastAPI /api/v1 子路由聚合层"""
from fastapi import APIRouter

from fastapis.api.v1.media import router as media_router

router = APIRouter(prefix='/v1')
router.include_router(media_router)
