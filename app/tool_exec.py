"""执行命令工具（execute_command）。

风险说明：执行 shell 命令是高风险操作。真正的「风险分级 + 用户确认 + 执行」
编排在 agent.run_single_model 里（因为它要往 SSE 流推 approval_request 事件、
await 用户决定）。本模块只负责：
- 注册工具 schema，让 LLM 知道有这个工具、怎么调；
- 提供一个兜底 handler（正常情况下不会被走到，agent 会拦截 execute_command
  做确认流程；万一走到，拒绝执行，避免绕过确认）。
"""
from __future__ import annotations

from .tools import register


@register(
    name="execute_command",
    description=(
        "在项目根目录下执行一条 shell 命令并返回输出（合并 stdout/stderr 和退出码）。"
        "只读命令（如 git status、ls、cat、python 脚本）会自动执行；"
        "其它可能产生副作用的命令（写文件、删除、安装包、管道等）需要用户在界面确认后才会执行。"
        "命令有超时限制。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "number", "description": "超时秒数，默认 30，最大 120"},
        },
        "required": ["command"],
    },
)
async def execute_command(command: str, timeout: float = 30.0) -> str:
    # 兜底：正常情况下 agent 会拦截 execute_command 走确认流程，不会调到这里。
    # 若走到（例如未来重构遗漏），拒绝执行，绝不绕过确认机制。
    return (
        f"错误：命令 '{command}' 未能通过确认流程执行。"
        "execute_command 必须经由 agent 的用户确认机制运行。"
    )
