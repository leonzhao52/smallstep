"""执行 shell 命令：风险分级 + 校验 + 子进程执行。

风险策略（白名单制，宁枉勿纵）：
- SAFE：只读、无副作用的命令（git status / ls / cat / python xxx.py ...）→ 自动执行。
- 其它一切命令（写、删、装包、管道、重定向、未知命令...）→ 需用户确认。
理由：黑名单永远列不全危险命令，漏一条就是事故；白名单宁可多确认几次也不漏放。

执行细节：
- 子进程在 PROJECT_ROOT 下运行（不允许 cwd 参数，避免逃逸）。
- 有超时，超时 kill 进程。
- 输出有大小上限，避免刷爆上下文。
- 合并 stdout/stderr 返回。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0
MAX_OUTPUT_CHARS = 10000

# 只读/无副作用命令白名单：这些自动执行，不需确认。
# 只匹配命令名（第一个 token），不含参数校验——参数里仍可能藏重定向等，见 _has_shell_meta。
# 注意：pip/npm/cargo/go 等包管理器**不在**白名单——它们有 install/uninstall 等
# 变异子命令会改环境，一律走确认（宁可多确认，不漏放）。
SAFE_COMMANDS = {
    # 文件查看
    "ls", "dir", "cat", "type", "head", "tail", "less", "more", "wc",
    "stat", "file", "find", "pwd", "echo", "which", "where", "whoami",
    "tree", "du", "df",
    # 搜索
    "grep", "rg", "ag", "findstr",
    # git 只读（进一步看子命令，见 SAFE_GIT_SUBCOMMANDS）
    "git",
    # 解释器/运行时：直接跑脚本/REPL 是只读的（脚本内部行为由用户在确认环节把关，
    # 且脚本需已存在；真正的危险在于 pip install 这类显式改环境的命令）
    "python", "python3", "py", "node", "java", "javac",
    # 系统信息
    "uname", "hostname", "date", "env", "printenv", "systeminfo",
    "tasklist", "ps",
}

# git 的只读子命令（第一个参数）。其它 git 子命令（commit/push/checkout...）需确认。
SAFE_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "tag", "remote", "config",
    "blame", "ls-files", "rev-parse", "describe", "shortlog", "reflog",
}


def _has_shell_meta(command: str) -> bool:
    """检测命令里是否含 shell 元字符（重定向/管道/命令替换等）。

    有这些符号说明可能产生写副作用或执行任意后续命令，一律需确认。
    """
    meta = ["|", ">", "<", "&", "`", "$(", ";", "\n"]
    return any(m in command for m in meta)


def _first_token(command: str) -> str:
    """取命令的第一个 token（命令名），转小写、去掉路径前缀。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    base = os.path.basename(tokens[0]).lower()
    # Windows 下 python.exe -> python
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def classify_risk(command: str) -> str:
    """返回 'safe'（自动执行）或 'risky'（需确认）。"""
    cmd = command.strip()
    if not cmd:
        return "risky"
    if _has_shell_meta(cmd):
        return "risky"

    base = _first_token(cmd)
    if base == "git":
        # git 需进一步看子命令
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        sub = tokens[1].lower() if len(tokens) > 1 else ""
        return "safe" if sub in SAFE_GIT_SUBCOMMANDS else "risky"

    if base in SAFE_COMMANDS:
        return "safe"

    return "risky"


def _decode(data: bytes) -> str:
    """解码子进程输出：优先 utf-8，失败退回本地编码（Windows 常见 gbk），替换错误。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk", errors="replace")
        except Exception:
            return data.decode("utf-8", errors="replace")


async def execute(command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """在 PROJECT_ROOT 下执行命令，返回合并的输出（截断）。不抛异常。"""
    timeout = max(1.0, min(float(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    logger.info("执行命令 [timeout=%.0fs]：%s", timeout, command)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        logger.warning("启动命令失败：%s", e)
        return f"命令启动失败：{e}"

    try:
        stdout_data, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        logger.warning("命令超时被终止：%s", command)
        return f"命令超时（>{timeout:.0f}s），已终止：{command}"

    output = _decode(stdout_data or b"")
    rc = proc.returncode
    header = f"[退出码 {rc}]\n"
    body = output if output.strip() else "[无输出]"

    result = header + body
    if len(result) > MAX_OUTPUT_CHARS:
        result = result[:MAX_OUTPUT_CHARS] + f"\n...[输出过长，已截断，共 {len(result)} 字符]"
    return result
