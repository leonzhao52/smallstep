"""文件读写工具（沙箱限制在项目目录内）。

- read_file：读文本文件，超长截断。
- write_file：写文件（自动建父目录）——唯一有写副作用的工具。
- list_dir：列目录。

安全（两层）：
1. 沙箱：所有路径先解析为绝对路径，校验落在 PROJECT_ROOT 内，拒绝越界。
2. 敏感文件黑名单：拒绝读写含密钥/隐私的文件（.env、config.yaml、日志、会话数据），
   防止模型被诱导泄露真实 API key。模板文件（.env.example 等）保留可读。
越界/受保护/出错都返回友好字符串，不抛异常。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .tools import register

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_CHARS = 20000

# 受保护路径（相对 PROJECT_ROOT）。
# - 文件名模式用 fnmatch（支持 .env、.env.*）
# - 目录前缀用前缀匹配（data/、data/logs/）
# 注意：.example 模板不保护，保留可读以便模型理解配置结构。
PROTECTED_FILES = [".env", ".env.*", "config.yaml"]
PROTECTED_DIRS = ["data/"]


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


def _is_protected(resolved: Path) -> bool:
    """判断解析后的绝对路径是否命中敏感黑名单。

    resolved 必须已通过 _resolve_sandboxed 校验（在 PROJECT_ROOT 内）。
    """
    try:
        rel = resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return False
    rel_str = rel.as_posix()

    # .example 模板（如 .env.example / config.example.yaml）不受保护
    if rel_str.endswith(".example") or rel.name.endswith(".example"):
        return False

    # 目录前缀匹配
    for d in PROTECTED_DIRS:
        if rel_str == d.rstrip("/") or rel_str.startswith(d):
            return True

    # 文件名模式匹配（.env、.env.local、config.yaml）
    import fnmatch
    for pattern in PROTECTED_FILES:
        if fnmatch.fnmatch(rel.name, pattern):
            return True

    return False


def _guard(path_str: str) -> tuple[Path | None, str | None]:
    """沙箱 + 敏感黑名单二合一校验。返回 (resolved, 错误消息)。"""
    resolved = _resolve_sandboxed(path_str)
    if resolved is None:
        return None, f"错误：路径 '{path_str}' 超出项目目录范围，已拒绝访问。"
    if _is_protected(resolved):
        logger.warning("已拦截对受保护路径的访问：%s", path_str)
        return None, f"拒绝访问：'{path_str}' 是受保护的敏感文件（可能含密钥或隐私），模型不可读写。"
    return resolved, None


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
    resolved, err = _guard(path)
    if err:
        return err
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
    resolved, err = _guard(path)
    if err:
        return err
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
    resolved, err = _guard(path)
    if err:
        return err
    if not resolved.exists():
        return f"错误：目录不存在 '{path}'。"
    if not resolved.is_dir():
        return f"错误：'{path}' 不是目录。"
    lines = []
    hidden = 0
    try:
        for item in sorted(resolved.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            # 隐藏受保护条目（如 .env、config.yaml），不暴露其存在
            if _is_protected(item.resolve()):
                hidden += 1
                continue
            if item.is_dir():
                lines.append(f"[目录] {item.name}/")
            else:
                size = item.stat().st_size
                lines.append(f"[文件] {item.name}  ({size} bytes)")
    except Exception as e:
        return f"错误：列目录失败：{e}"
    if hidden:
        lines.append(f"[隐藏 {hidden} 个受保护文件]")
    logger.info("list_dir: %s (%d 项, 隐藏 %d)", path, len(lines), hidden)
    return "\n".join(lines) or "[空目录]"
