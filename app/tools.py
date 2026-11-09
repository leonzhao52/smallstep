"""工具注册表（骨架）。

骨架阶段没有任何具体工具，但注册机制、schema 转换、执行分发的代码都写好，
下一轮加工具只需用 @register(...) 装饰一个函数。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional


# name -> {"schema": {...}, "handler": callable}
TOOLS: dict[str, dict[str, Any]] = {}


def register(name: str, description: str, parameters: dict):
    """把一个函数注册为工具。

    parameters 是 JSON Schema 的 parameters 对象（OpenAI function calling 格式）。
    handler 约定：同步或异步函数，接收 **kwargs（即 LLM 给出的参数），返回任意可序列化结果。
    """

    def deco(fn: Callable):
        TOOLS[name] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            "handler": fn,
        }
        return fn

    return deco


def openai_tool_schemas() -> list[dict]:
    """把所有已注册工具转成 OpenAI chat.completions 的 tools 参数格式。"""
    return [entry["schema"] for entry in TOOLS.values()]


async def execute(name: str, args_json: str) -> str:
    """执行一个工具调用，返回字符串结果（会作为 tool message 喂回模型）。"""
    entry = TOOLS.get(name)
    if entry is None:
        return f"错误：未注册的工具 '{name}'"

    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        return f"错误：工具 '{name}' 的参数不是合法 JSON：{e}"

    handler = entry["handler"]
    result = handler(**args)
    # 支持 async handler
    if hasattr(result, "__await__"):
        result = await result

    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, default=str)
    return result


def get_tool(name: str) -> Optional[dict]:
    return TOOLS.get(name)
