"""API 路由：SSE 聊天、会话管理、模型列表。"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agent as agent_mod
from . import storage
from .config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class RenameRequest(BaseModel):
    title: str


@router.get("/models")
async def list_models():
    """返回当前有优先级的 model 候选列表。"""
    return {
        "model_priority": [c.label() for c in config.get_model_candidates()]
    }


@router.get("/conversations")
async def list_conversations(status: str = "active"):
    """按状态列出会话：active / archived / trashed。"""
    convs = await storage.list_all(status=status)
    return [
        {
            "id": c.id,
            "title": c.title,
            "status": c.status,
            "updated_at": c.updated_at,
            "deleted_at": c.deleted_at,
            "last_model": c.last_model,
        }
        for c in convs
    ]


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = await storage.get(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations")
async def create_conversation():
    conv = await storage.create()
    return conv.model_dump()


@router.post("/conversations/{conv_id}/rename")
async def rename_conversation(conv_id: str, req: RenameRequest):
    conv = await storage.rename(conv_id, req.title)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations/{conv_id}/archive")
async def archive_conversation(conv_id: str):
    conv = await storage.set_status(conv_id, "archived")
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations/{conv_id}/trash")
async def trash_conversation(conv_id: str):
    """软删除：移入回收站。"""
    conv = await storage.set_status(conv_id, "trashed")
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations/{conv_id}/restore")
async def restore_conversation(conv_id: str):
    """从回收站恢复为 active。"""
    conv = await storage.set_status(conv_id, "active")
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.delete("/conversations/{conv_id}")
async def delete_conversation_forever(conv_id: str):
    """永久删除（仅回收站内可调）：直接删掉磁盘文件。"""
    ok = await storage.delete_forever(conv_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


@router.post("/chat")
async def chat(req: ChatRequest):
    """接收一条用户消息，返回 SSE 事件流。"""

    conv = await storage.get(req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if conv.status == "trashed":
        raise HTTPException(status_code=400, detail="该会话已在回收站，请先恢复再继续对话")

    # 落盘用户消息，并用返回的最新会话组装消息（带上历史）
    logger.info("收到用户消息 [会话 %s]：%s", req.conversation_id, req.message)
    user_msg = {"role": "user", "content": req.message}
    conv = await storage.append_message(req.conversation_id, user_msg)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    messages = list(conv.messages)
    # 首轮对话（标题还是「新会话」）时，结束后自动生成标题
    is_first_exchange = conv.title in ("", "新会话")

    async def event_stream():
        assistant_text_parts: list[str] = []
        used_model = None
        used_provider = None

        try:
            async for event in agent_mod.run_agent(messages):
                etype = event.get("type")

                if etype == "text_delta":
                    assistant_text_parts.append(event["content"])
                elif etype == "done":
                    used_model = event.get("model")
                elif etype == "model_fallback":
                    logger.warning("模型回退：失败=%s 原因=%s",
                                   event.get("failed"), event.get("reason"))

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # 落盘 assistant 消息
            full = "".join(assistant_text_parts)
            if full:
                logger.info("assistant 回复 [会话 %s, 模型 %s]：%s",
                            req.conversation_id, used_model, full)
                await storage.append_message(
                    req.conversation_id, {"role": "assistant", "content": full}
                )

            # 首轮对话：后台生成标题并推送（不阻塞已结束的响应流）
            if is_first_exchange:
                title = await agent_mod.generate_title(req.message)
                if title:
                    await storage.update_title(req.conversation_id, title)
                    yield f"data: {json.dumps({'type': 'title_update', 'title': title}, ensure_ascii=False)}\n\n"
        except Exception as e:  # 兜底：不要让连接挂死
            logger.exception("chat 处理异常 [会话 %s]", req.conversation_id)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
