"""命令执行确认管理器。

当 execute_command 工具遇到非 safe 命令时，不能直接执行，要等用户在界面点
「允许 / 拒绝」。本模块用一个进程内的 pending 表 + asyncio.Future 实现：

- create()：agent 侧调用，登记一个待确认请求并返回 (request_id, future)。
  agent 之后 await 这个 future，用户决定前一直挂起（带超时）。
- resolve()：HTTP 端点侧调用，用户点了允许/拒绝后，set future 的结果唤醒 agent。

注意：状态存在内存里，服务重启会丢（pending 的 agent 会因 future 超时而放弃，
视为拒绝），对单用户本地应用可接受。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# 等待用户确认的超时（秒）。超时视为拒绝，避免 agent 永久挂起。
APPROVAL_TIMEOUT = 60.0


@dataclass
class PendingRequest:
    id: str
    conversation_id: str
    command: str
    risk: str
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


_pending: dict[str, PendingRequest] = {}


def create(conversation_id: str, command: str, risk: str) -> tuple[str, asyncio.Future]:
    """登记一个待确认请求，返回 (request_id, future)。"""
    req_id = uuid.uuid4().hex
    pending = PendingRequest(
        id=req_id,
        conversation_id=conversation_id,
        command=command,
        risk=risk,
    )
    _pending[req_id] = pending
    logger.info("创建确认请求 [%s] 会话=%s 命令=%s", req_id, conversation_id, command)
    return req_id, pending.future


def get(req_id: str) -> Optional[PendingRequest]:
    return _pending.get(req_id)


def resolve(req_id: str, approved: bool) -> bool:
    """用户做出决定，唤醒等待中的 agent。返回是否成功（请求存在且未决）。"""
    pending = _pending.pop(req_id, None)
    if pending is None:
        logger.warning("确认请求不存在或已处理：%s", req_id)
        return False
    if pending.future.done():
        return False
    pending.future.set_result(bool(approved))
    logger.info("确认请求 [%s] 已决定：%s", req_id, "允许" if approved else "拒绝")
    return True


def cleanup_expired(max_age: float = 300.0) -> None:
    """清理长时间未决的请求（future 会因超时自行结束，这里只移除表项）。"""
    now = time.time()
    expired = [rid for rid, p in _pending.items() if now - p.created_at > max_age]
    for rid in expired:
        pending = _pending.pop(rid, None)
        if pending and not pending.future.done():
            pending.future.set_result(False)
    if expired:
        logger.info("清理过期确认请求 %d 个", len(expired))
