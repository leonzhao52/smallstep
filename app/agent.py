"""Agent 核心：跨 model 优先级回退 + 流式对话 + 工具调用扩展点。

两层循环：
- 外层：遍历 model_priority 做优先级回退，当前 model 不可用就换下一个。
- 内层：单个 model 上的工具调用循环（骨架阶段注册表为空，实际不触发）。

流式期间出错的处理原则（见 run_single_model 里的注释）：
首块之前出错 -> 干净回退；已吐文本后断流 -> 不静默回退，避免内容重复。
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)

from . import llm, providers, tools
from .config import config

logger = logging.getLogger(__name__)

# 触发回退的异常：连接失败、认证失败、限流、模型不存在、服务端 5xx。
FALLBACK_EXCEPTIONS = (
    APIConnectionError,
    AuthenticationError,
    RateLimitError,
    NotFoundError,
    InternalServerError,
)


class FallbackTriggered(Exception):
    """首块之前出错，应当回退到下一个候选 model。"""


async def run_agent(messages: list[dict]) -> AsyncGenerator[dict, None]:
    """对外入口：产出 SSE 事件流。

    依次尝试 model_priority 里的候选，第一个跑通的就返回；全部失败则 yield error。
    """
    candidates = config.get_model_candidates()
    if not candidates:
        yield {"type": "error", "message": "model_priority 为空，请在 config.yaml 配置至少一个候选模型。"}
        return

    last_error: Exception | None = None
    logger.info("开始处理请求，候选模型: %s", [c.label() for c in candidates])
    for cand in candidates:
        try:
            client = providers.get_client(cand.provider)
        except KeyError as e:
            last_error = e
            logger.warning("provider %s 未配置，跳过：%s", cand.provider, e)
            yield {
                "type": "model_fallback",
                "failed": cand.label(),
                "reason": f"provider 未配置：{e}",
            }
            continue

        try:
            logger.info("尝试候选模型 [%s]", cand.label())
            used_model = None
            async for event in run_single_model(client, cand.model, messages):
                # 记录实际使用的 model，随 done 事件带出
                if event.get("type") == "done":
                    used_model = event.get("model")
                yield event
            logger.info("候选模型 [%s] 成功完成", cand.label())
            return  # 成功跑完
        except FallbackTriggered as e:
            last_error = e
            logger.warning("候选模型 [%s] 失败，回退到下一个：%s", cand.label(), e)
            yield {
                "type": "model_fallback",
                "failed": cand.label(),
                "reason": str(e.__cause__ or e),
            }
            continue

    logger.error("所有候选模型均不可用，最后一个错误：%s", last_error)
    yield {"type": "error", "message": f"所有候选模型均不可用，最后一个错误：{last_error}"}


async def generate_title(user_message: str) -> str | None:
    """用 LLM 根据首条用户消息生成一个简短会话标题（非流式）。

    复用 model_priority 的回退链：依次尝试候选模型，第一个成功就返回。
    生成失败返回 None，由调用方保留「截断首条消息」的兜底标题。
    """
    candidates = config.get_model_candidates()
    prompt = (
        "请根据下面的用户消息，生成一个不超过 16 个字的简短会话标题。"
        "只输出标题本身，不要任何引号、标点或解释。\n\n"
        f"用户消息：{user_message[:200]}"
    )
    messages = [{"role": "user", "content": prompt}]

    for cand in candidates:
        try:
            client = providers.get_client(cand.provider)
        except KeyError:
            continue
        try:
            resp = await client.chat.completions.create(
                model=cand.model, messages=messages, max_tokens=30, temperature=0.3,
            )
            title = (resp.choices[0].message.content or "").strip().strip('"「」【】')
            # 取第一行，去掉多余空白，限制长度
            title = title.splitlines()[0].strip()[:30] if title else ""
            if title:
                logger.info("生成会话标题 [%s]：%s", cand.label(), title)
                return title
        except Exception as e:
            logger.warning("生成标题 [%s] 失败：%s", cand.label(), e)
            continue

    logger.warning("所有候选模型生成标题均失败，保留兜底标题")
    return None


async def run_single_model(
    client, model: str, messages: list[dict]
) -> AsyncGenerator[dict, None]:
    """在单个 model 上跑：流式输出文本，必要时执行工具并循环。

    抛出 FallbackTriggered 表示该 model 首块前失败，由 run_agent 换下一个。
    """
    working = list(messages)
    tool_schemas = tools.openai_tool_schemas()

    while True:
        accumulated_text: list[str] = []
        # tool_calls 按 index 累积（流式里每个 tool_call 分多块到达）
        tool_calls_acc: dict[int, dict] = {}
        emitted_any_text = False

        # 记录本次发送给 LLM 的完整消息（含历史与工具结果）和是否带工具
        logger.debug("发送给 LLM [%s]，消息数=%d，tools=%s",
                     model, len(working), bool(tool_schemas))
        logger.debug("发送内容: %s", working)

        try:
            async for chunk in llm.stream_once(client, model, working, tool_schemas):
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # 文本增量
                if delta.content:
                    emitted_any_text = True
                    accumulated_text.append(delta.content)
                    yield {"type": "text_delta", "content": delta.content}

                # 工具调用增量
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        idx = tc.index
                        slot = tool_calls_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments

        except FALLBACK_EXCEPTIONS as e:
            if emitted_any_text:
                # 已经输出过部分文本，静默回退会导致内容重复，改为直接报错收尾。
                logger.warning("LLM [%s] 流式中断（已输出部分文本，不回退）：", model, exc_info=e)
                yield {
                    "type": "text_delta",
                    "content": f"\n\n[流式中断，未回退：{e}]",
                }
                yield {"type": "done", "model": model}
                return
            # 首块之前失败 -> 干净回退
            logger.warning("LLM [%s] 首块前失败，将回退：", model, exc_info=e)
            raise FallbackTriggered(str(e)) from e

        full_text = "".join(accumulated_text)

        # 记录 LLM 的完整回复
        logger.debug("LLM [%s] 回复长度=%d", model, len(full_text))
        logger.debug("回复内容: %s", full_text)

        # 没有工具调用 -> 本轮结束
        if not tool_calls_acc:
            logger.info("LLM [%s] 完成本轮回复（无工具调用）", model)
            yield {"type": "done", "model": model}
            return

        # 有工具调用：先把 assistant 消息（含 tool_calls）append 回 working
        assistant_msg = {
            "role": "assistant",
            "content": full_text or None,
            "tool_calls": [
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"]},
                }
                for slot in (tool_calls_acc[i] for i in sorted(tool_calls_acc))
            ],
        }
        working.append(assistant_msg)

        # 逐个执行工具，yield 结果并 append 回 working
        for i in sorted(tool_calls_acc):
            slot = tool_calls_acc[i]
            logger.info("LLM [%s] 请求工具调用: %s(%s)", model, slot["name"], slot["arguments"])
            yield {"type": "tool_call", "name": slot["name"], "arguments": slot["arguments"]}
            result = await tools.execute(slot["name"], slot["arguments"])
            logger.debug("工具 %s 返回: %s", slot["name"], result)
            yield {"type": "tool_result", "name": slot["name"], "content": result}
            working.append(
                {"role": "tool", "tool_call_id": slot["id"], "content": result}
            )
        # 继续内层循环，把工具结果喂回模型
