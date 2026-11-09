"""单次流式请求：给定一个 client + model，流式拉取 chunk。

本模块不决定用哪个 provider/model、不做回退——那些是 agent.py 的职责。
这里只把 OpenAI SDK 的异常原样抛出，交给上层判断是否回退。
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Optional

from openai import AsyncOpenAI


async def stream_once(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
) -> AsyncGenerator[Any, None]:
    """发起一次流式 chat completion，逐个 yield SDK 的 chunk。

    连接/认证/限流等错误会在拿到首块之前由 SDK 抛出，向上传播。
    """
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools or None,
        stream=True,
    )
    async for chunk in stream:
        yield chunk
