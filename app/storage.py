"""会话存储：每个会话一个 JSON 文件，放 data/conversations/。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONV_DIR = PROJECT_ROOT / "data" / "conversations"


class Conversation(BaseModel):
    id: str
    title: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    messages: list[dict] = Field(default_factory=list)
    # 记录最近一次实际使用的 provider/model，便于排查
    last_provider: Optional[str] = None
    last_model: Optional[str] = None


def _path(conv_id: str) -> Path:
    return CONV_DIR / f"{conv_id}.json"


def _ensure_dir() -> None:
    CONV_DIR.mkdir(parents=True, exist_ok=True)


async def create(title: str = "新会话") -> Conversation:
    conv = Conversation(id=uuid.uuid4().hex, title=title[:30] or "新会话")
    await save(conv)
    return conv


async def save(conv: Conversation) -> None:
    _ensure_dir()
    conv.updated_at = time.time()
    _path(conv.id).write_text(
        conv.model_dump_json(indent=2), encoding="utf-8"
    )


async def get(conv_id: str) -> Optional[Conversation]:
    p = _path(conv_id)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return Conversation(**data)


async def list_all() -> list[Conversation]:
    """返回所有会话，按更新时间倒序。"""
    _ensure_dir()
    convs: list[Conversation] = []
    for p in CONV_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            convs.append(Conversation(**data))
        except (json.JSONDecodeError, Exception):
            continue  # 跳过损坏文件
    convs.sort(key=lambda c: c.updated_at, reverse=True)
    return convs


async def append_message(conv_id: str, message: dict) -> Optional[Conversation]:
    conv = await get(conv_id)
    if conv is None:
        return None
    conv.messages.append(message)
    # 首条用户消息作为标题
    if not conv.title or conv.title == "新会话":
        if message.get("role") == "user":
            conv.title = (message.get("content") or "")[:30] or "新会话"
    await save(conv)
    return conv
