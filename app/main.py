"""FastAPI 入口：挂 API 路由 + 托管前端静态文件 + 管理渠道后台任务生命周期。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import config
from .log import setup_logging
from .routes import router

# 初始化日志（控制台 INFO + 文件 DEBUG 滚动），必须在导入路由前完成
setup_logging()

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时拉起启用的渠道后台任务，关闭时停止。

    微信渠道（channels.wechat.enabled=true）作为常驻 asyncio 任务长轮询收消息。
    未启用或登录失败时打日志跳过，不影响 WebUI。
    """
    wechat_task: asyncio.Task | None = None
    ch = config.get_channel("wechat")
    if ch and ch.enabled:
        from .wechat import WechatService  # 延迟导入，避免未启用时也加载
        svc = WechatService()
        wechat_task = asyncio.create_task(svc.start())
        logger.info("微信渠道已启用，后台任务已启动")
    else:
        logger.info("微信渠道未启用")

    yield

    # 关闭：停止后台任务
    if wechat_task:
        wechat_task.cancel()
        try:
            await wechat_task
        except asyncio.CancelledError:
            pass
        logger.info("微信渠道后台任务已停止")


app = FastAPI(title="个人 Agent", lifespan=lifespan)

app.include_router(router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
