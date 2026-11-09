"""FastAPI 入口：挂 API 路由 + 托管前端静态文件。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .log import setup_logging
from .routes import router

# 初始化日志（控制台 INFO + 文件 DEBUG 滚动），必须在导入路由前完成
setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI(title="个人 Agent")

app.include_router(router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
