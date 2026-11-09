"""API 路由：SSE 聊天、会话管理、模型列表。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agent as agent_mod
from . import storage
from .config import config

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


@router.get("/models")
async def list_models():
    """返回当前有优先级的 model 候选列表。"""
    return {
        "model_priority": [c.label() for c in config.get_model_candidates()]
    }


@router.get("/conversations")
async def list_conversations():
    convs = await storage.list_all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "updated_at": c.updated_at,
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


@router.post("/chat")
async def chat(req: ChatRequest):
    """接收一条用户消息，返回 SSE 事件流。"""

    conv = await storage.get(req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 落盘用户消息，并用返回的最新会话组装消息（带上历史）
    user_msg = {"role": "user", "content": req.message}
    conv = await storage.append_message(req.conversation_id, user_msg)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    messages = list(conv.messages)

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
                    used_provider = None  # 回退后由后续 done 决定

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # 落盘 assistant 消息
            full = "".join(assistant_text_parts)
            if full:
                await storage.append_message(
                    req.conversation_id, {"role": "assistant", "content": full}
                )
            # 记录本次使用的 model（从 done 事件拿，简单起见在下面补写）
        except Exception as e:  # 兜底：不要让连接挂死
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
