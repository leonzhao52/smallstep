"""文件读写工具（沙箱限制在项目目录内）。

- read_file：读文本文件，超长截断。
- write_file：写文件（自动建父目录）——唯一有写副作用的工具。
- list_dir：列目录。

安全：所有路径先解析为绝对路径，再校验是否落在 PROJECT_ROOT 内，
拒绝绝对路径外和 `..` 越界访问。越界/出错都返回友好字符串，不抛异常。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .tools import register

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_CHARS = 20000


def _resolve_sandboxed(path_str: str) -> Path | None:
    """把请求路径解析为绝对路径并校验在沙箱内。越界返回 None。"""
    p = Path(path_str)
    # 相对路径以 PROJECT_ROOT 为基准
    resolved = (PROJECT_ROOT / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return resolved


@register(
    name="read_file",
    description="读取项目目录内某个文本文件的内容。path 为相对项目根的路径（如 'README.md'、'app/main.py'）。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根目录的文件路径"},
        },
        "required": ["path"],
    },
)
def read_file(path: str) -> str:
    resolved = _resolve_sandboxed(path)
    if resolved is None:
        return f"错误：路径 '{path}' 超出项目目录范围，已拒绝访问。"
    if not resolved.exists():
        return f"错误：文件不存在 '{path}'。"
    if not resolved.is_file():
        return f"错误：'{path}' 不是文件。"
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"错误：读取文件失败：{e}"
    logger.info("read_file: %s (%d chars)", path, len(text))
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n\n[...文件过长，已截断，共 {len(text)} 字符]"
    return text or "[空文件]"


@register(
    name="write_file",
    description="把内容写入项目目录内的某个文件（覆盖写，自动创建父目录）。仅限项目目录内。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根目录的文件路径"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        "required": ["path", "content"],
    },
)
def write_file(path: str, content: str) -> str:
    resolved = _resolve_sandboxed(path)
    if resolved is None:
        return f"错误：路径 '{path}' 超出项目目录范围，已拒绝写入。"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"错误：写入文件失败：{e}"
    logger.info("write_file: %s (%d chars)", path, len(content))
    return f"已写入 {path}（{len(content)} 字符）。"


@register(
    name="list_dir",
    description="列出项目目录内某个目录的内容（文件名、类型、大小）。path 省略时列出项目根目录。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根目录的目录路径，默认 '.'"},
        },
        "required": [],
    },
)
def list_dir(path: str = ".") -> str:
    resolved = _resolve_sandboxed(path)
    if resolved is None:
        return f"错误：路径 '{path}' 超出项目目录范围。"
    if not resolved.exists():
        return f"错误：目录不存在 '{path}'。"
    if not resolved.is_dir():
        return f"错误：'{path}' 不是目录。"
    lines = []
    try:
        for item in sorted(resolved.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.is_dir():
                lines.append(f"[目录] {item.name}/")
            else:
                size = item.stat().st_size
                lines.append(f"[文件] {item.name}  ({size} bytes)")
    except Exception as e:
        return f"错误：列目录失败：{e}"
    logger.info("list_dir: %s (%d 项)", path, len(lines))
    return "\n".join(lines) or "[空目录]"
