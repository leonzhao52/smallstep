"""附件上传存储：文本类文件读出内容内联，图片转 base64 data URL。

存储位置：data/uploads/{conversation_id}/{safe_filename}
- data/ 已在 gitignore + tool_files 敏感黑名单，不会被 git 跟踪、不会被 read_file 读到。

安全：
- 文件名消毒（防路径穿越、Windows 保留名、超长）。
- 扩展名白名单（文本类 / 图片类），不在名单内一律拒绝。
- 单文件大小上限。

消息组装：
- 纯文本附件 -> 追加文本 part（兼容所有模型时 content 保持字符串）。
- 图片 -> {"type":"image_url","image_url":{"url":"data:{mime};base64,..."}} part。
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_ROOT = PROJECT_ROOT / "data" / "uploads"

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_TEXT_CHARS = 20000            # 内联文本附件的最大字符数

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".html", ".htm", ".css",
    ".scss", ".csv", ".tsv", ".xml", ".sh", ".bash", ".ps1", ".bat", ".sql",
    ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".swift", ".kt", ".log",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Windows 保留设备名
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def classify_type(ext: str) -> Optional[str]:
    """按扩展名返回 'text' / 'image' / None（不支持）。ext 含点、小写。"""
    ext = ext.lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def sanitize_filename(name: str) -> str:
    """消毒上传文件名：去目录成分、处理保留名、限长、去危险字符。"""
    name = (name or "").strip()
    # 只取最后一段，去掉任何目录成分
    p = Path(name)
    base = p.name or "file"
    stem = p.stem or "file"
    suffix = p.suffix

    # 去掉文件名里的危险字符（保留常见分隔如 - _ . 空格）
    cleaned = "".join(ch for ch in stem if ch.isalnum() or ch in "-_. ")
    cleaned = cleaned.strip() or "file"

    # Windows 保留名
    if cleaned.upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    # 限长（保留扩展名空间）
    if len(cleaned) > 120:
        cleaned = cleaned[:110]

    result = f"{cleaned}{suffix}"
    # 最后再兜底：确保没有残留路径分隔符
    return Path(result).name or "file"


def _unique_path(conv_dir: Path, filename: str) -> Path:
    """若目标已存在，追加 -1/-2 后缀避免覆盖。"""
    target = conv_dir / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    for i in range(1, 1000):
        candidate = conv_dir / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    # 极端情况：用 uuid
    return conv_dir / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"


def _conv_dir(conversation_id: str) -> Path:
    d = UPLOAD_ROOT / conversation_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_upload(conversation_id: str, original_name: str, data: bytes) -> dict:
    """保存一个上传文件，返回附件元数据 {id, name, type, size, mime}。失败抛 ValueError。"""
    if len(data) > MAX_FILE_SIZE:
        raise ValueError(f"文件过大（>{MAX_FILE_SIZE // (1024 * 1024)}MB）")

    ext = Path(original_name).suffix.lower()
    ftype = classify_type(ext)
    if ftype is None:
        raise ValueError(f"不支持的文件类型 '{ext or '(无扩展名)'}'，仅支持文本类和常见图片。")

    safe = sanitize_filename(original_name)
    conv_dir = _conv_dir(conversation_id)
    target = _unique_path(conv_dir, safe)
    target.write_bytes(data)

    mime = mimetypes.guess_type(target.name)[0] or (
        "image/png" if ftype == "image" else "text/plain"
    )
    meta = {
        "id": uuid.uuid4().hex,
        "name": target.name,
        "type": ftype,
        "size": len(data),
        "mime": mime,
        # 存储相对路径，供 chat 组装时读回内容
        "path": str(target.relative_to(PROJECT_ROOT)),
    }
    # 把元数据落盘，chat 组装时按 id 精确读回，避免扫描目录
    (conv_dir / f"{meta['id']}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("附件已保存 [%s] %s (%d bytes, %s)", conversation_id, target.name, len(data), ftype)
    return meta


def find_meta(conversation_id: str, att_id: str) -> Optional[dict]:
    """按 id 读取已上传附件的元数据。找不到返回 None。"""
    if not att_id:
        return None
    # 只接受 hex id，防止路径穿越
    if not re.fullmatch(r"[0-9a-f]{32}", att_id):
        return None
    meta_path = UPLOAD_ROOT / conversation_id / f"{att_id}.json"
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_text_content(meta: dict) -> str:
    """读文本附件内容（截断到 MAX_TEXT_CHARS）。"""
    path = PROJECT_ROOT / meta["path"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[附件 {meta['name']} 读取失败：{e}]"
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n...[附件过长已截断，共 {len(text)} 字符]"
    return text


def read_image_data_url(meta: dict) -> Optional[str]:
    """读图片附件，返回 data:{mime};base64,... 形式的 data URL。失败返回 None。"""
    path = PROJECT_ROOT / meta["path"]
    try:
        data = path.read_bytes()
    except Exception as e:
        logger.warning("图片附件读取失败 %s：%s", meta["name"], e)
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{meta.get('mime', 'image/png')};base64,{b64}"
