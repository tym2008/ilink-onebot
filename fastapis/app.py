"""
FastAPI 应用工厂

`create_app()` 构造完整 ASGI 应用，挂载所有路由。

不写 lifespan：FastAPI 媒体服务嵌入在 typer CLI 的 asyncio 事件循环中运行
（`typers/run.py` 用 `uvicorn.Server(Config(app)).serve()` 作为 task），
DB / HTTP session 等基础设施由外层 CLI 主程序统一初始化。
"""
from fastapi import FastAPI

from fastapis.api import router as api_router


def create_app() -> FastAPI:
    """
    构造 FastAPI 应用并挂载所有路由。

    :return: 可直接传给 uvicorn.Server 的 ASGI 应用
    """
    app = FastAPI(
        title='ilink-onebot Media Service',
        description='Local HTTP service for serving decrypted iLink media files',
        version='0.1.0',
    )
    app.include_router(api_router)
    return app
