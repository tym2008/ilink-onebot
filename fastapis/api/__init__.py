"""FastAPI /api 子路由聚合层"""
from fastapi import APIRouter

from fastapis.api.v1 import router as v1_router

router = APIRouter(prefix='/api')
router.include_router(v1_router)
