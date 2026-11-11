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
    # 状态：active 正常 / archived 归档 / trashed 已进回收站（软删除）
    status: str = "active"
    # 软删除时间，用于回收站展示与「30 天后自动清理」
    deleted_at: Optional[float] = None
    # 记录最近一次实际使用的 provider/model，便于排查
    last_provider: Optional[str] = None
    last_model: Optional[str] = None
    # 来源渠道：webui（默认）/ wechat ...，用于区分不同渠道的会话
    channel: str = "webui"
    # 渠道内的发送者标识（如微信 from_user_id），webui 渠道为空
    sender_id: Optional[str] = None


def _path(conv_id: str) -> Path:
    return CONV_DIR / f"{conv_id}.json"


def _ensure_dir() -> None:
    CONV_DIR.mkdir(parents=True, exist_ok=True)


async def create(title: str = "新会话") -> Conversation:
    conv = Conversation(id=uuid.uuid4().hex, title=title[:30] or "新会话")
    await save(conv)
    return conv


async def get_or_create_by_sender(channel: str, sender_id: str) -> Conversation:
    """按「渠道 + 发送者」取或建会话（微信等非 webui 渠道用）。

    每个渠道内的每个发送者对应一个独立会话。遍历 active 会话匹配
    channel + sender_id；没有就新建（标题先用发送者标识占位，首轮后由
    agent.generate_title 覆盖）。单用户本地应用，遍历文件量小，可接受。
    """
    _ensure_dir()
    for p in CONV_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (
            data.get("status") == "active"
            and data.get("channel") == channel
            and data.get("sender_id") == sender_id
        ):
            return Conversation(**data)

    conv = Conversation(
        id=uuid.uuid4().hex,
        title=f"{channel}:{sender_id}"[:30],
        channel=channel,
        sender_id=sender_id,
    )
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


async def list_all(status: Optional[str] = None) -> list[Conversation]:
    """返回会话，按更新时间倒序。

    status=None 返回全部（含归档/回收站）；指定 'active'/'archived'/'trashed' 则只返回该状态。
    """
    _ensure_dir()
    convs: list[Conversation] = []
    for p in CONV_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            convs.append(Conversation(**data))
        except (json.JSONDecodeError, Exception):
            continue  # 跳过损坏文件
    if status is not None:
        convs = [c for c in convs if c.status == status]
    convs.sort(key=lambda c: c.updated_at, reverse=True)
    return convs


async def append_message(conv_id: str, message: dict) -> Optional[Conversation]:
    conv = await get(conv_id)
    if conv is None:
        return None
    conv.messages.append(message)
    # 首条用户消息作为兜底标题（后续可能被 LLM 生成的标题覆盖）
    if not conv.title or conv.title == "新会话":
        if message.get("role") == "user":
            conv.title = (message.get("content") or "")[:30] or "新会话"
    await save(conv)
    return conv


async def update_title(conv_id: str, title: str) -> Optional[Conversation]:
    """更新会话标题（用于自动生成的标题覆盖兜底标题）。"""
    conv = await get(conv_id)
    if conv is None:
        return None
    conv.title = title.strip()[:50] or conv.title
    await save(conv)
    return conv


async def rename(conv_id: str, title: str) -> Optional[Conversation]:
    """用户手动重命名会话。"""
    conv = await get(conv_id)
    if conv is None:
        return None
    conv.title = title.strip()[:50] or conv.title
    await save(conv)
    return conv


async def set_status(conv_id: str, status: str) -> Optional[Conversation]:
    """切换会话状态：active / archived / trashed。

    进回收站时记录 deleted_at；离开回收站（恢复）时清空 deleted_at。
    """
    if status not in ("active", "archived", "trashed"):
        return None
    conv = await get(conv_id)
    if conv is None:
        return None
    conv.status = status
    conv.deleted_at = time.time() if status == "trashed" else None
    await save(conv)
    return conv


async def delete_forever(conv_id: str) -> bool:
    """从回收站永久删除：直接删掉磁盘文件。返回是否成功。"""
    p = _path(conv_id)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False
