"""API 路由：SSE 聊天、会话管理、模型列表、配置管理。"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agent as agent_mod
from . import approvals
from . import attachments as attachments_mod
from . import providers as providers_mod
from . import storage
from .config import CONFIG_PATH, PROJECT_ROOT, AppConfig, config, reload_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    conversation_id: str
    message: str
    # 已上传附件的元数据 [{id, name, type, mime, path}]，内容拼进当前这条消息
    attachments: list[dict] = []


class RenameRequest(BaseModel):
    title: str


def build_user_message(conversation_id: str, text: str, attachments: list[dict]) -> dict:
    """把用户文字 + 附件组装成 OpenAI 格式的 user message。

    - 只有文字（或无附件）：content 为字符串，兼容所有模型。
    - 含文本附件：在文字后追加一段「--- 附件: name ---」的文本块。
    - 含图片：content 必须用 parts 数组，图片作为 image_url part（base64 data URL）。
    """
    text_parts: list[str] = []      # 文本段落
    image_parts: list[dict] = []    # 图片 parts
    att_meta: list[dict] = []       # 落盘/前端展示用的元数据

    for att in attachments:
        att_id = att.get("id")
        meta = attachments_mod.find_meta(conversation_id, att_id)
        if meta is None:
            logger.warning("附件元数据缺失，跳过 id=%s", att_id)
            continue
        att_meta.append({
            "id": meta["id"], "name": meta["name"],
            "type": meta["type"], "size": meta["size"],
        })
        if meta["type"] == "text":
            content = attachments_mod.read_text_content(meta)
            text_parts.append(f"\n\n--- 附件: {meta['name']} ---\n{content}")
        elif meta["type"] == "image":
            data_url = attachments_mod.read_image_data_url(meta)
            if data_url:
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": data_url},
                })

    combined_text = text + "".join(text_parts)

    if image_parts:
        # 有图片：必须用 parts 数组；文字作为第一个 part（若无文字也要有个空占位）
        parts: list[dict] = []
        if combined_text.strip():
            parts.append({"type": "text", "text": combined_text})
        parts.extend(image_parts)
        content = parts
    else:
        content = combined_text

    msg = {"role": "user", "content": content}
    if att_meta:
        # 存一份不含 base64 的元数据，前端渲染气泡时展示附件标记
        msg["attachments"] = att_meta
    return msg


@router.get("/models")
async def list_models():
    """返回当前有优先级的 model 候选列表。"""
    return {
        "model_priority": [c.label() for c in config.get_model_candidates()]
    }


# ── 配置管理 ────────────────────────────────────────────────
# 支持热更新：保存后原地改 config 单例 + 清 provider 客户端缓存 + 落盘，
# 无需重启。密钥不写进 config.yaml（保留 ${ENV_VAR} 占位符），改密钥走 .env。

_ENV_PATH = PROJECT_ROOT / ".env"
_KNOWN_ENV_KEYS = ["OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LOCAL_API_KEY", "TAVILY_API_KEY"]


def _is_placeholder(value: str) -> bool:
    """判断一个 api_key 值是否还是 ${ENV_VAR} 占位符（而非明文密钥）。"""
    return "${" in value


def _mask(value: str) -> str:
    """密钥脱敏：只露末 4 位，用于回显给前端。"""
    if not value:
        return ""
    if _is_placeholder(value):
        return value  # 占位符本身不敏感，原样返回
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


def _provider_view(p) -> dict:
    """序列化一个 provider 给前端：base_url 明文，api_key 脱敏，api_key_ref 是原始占位符。"""
    return {
        "name": p.name,
        "base_url": p.base_url,
        "api_key": _mask(p.api_key),
        # 原始占位符（如 ${OPENAI_API_KEY}）；若 config 未用占位符则回退到掩码值，
        # 但 PUT 端会把非占位符的 ref 重新推断成占位符，所以不会把明文写回 config.yaml。
        "api_key_ref": p.api_key_ref or _mask(p.api_key),
    }


def _set_env_key(name: str, value: str) -> None:
    """更新一个环境变量：os.environ 立即生效 + 拼接写回 .env 持久化。

    用逐行拼接保留 .env 里其它行和注释，不整文件重生成。
    """
    os.environ[name] = value
    lines: list[str] = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{name}=") or stripped.startswith(f"export {name}="):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_config_yaml(raw: dict) -> None:
    """把 config 结构写回 config.yaml（保留占位符），加固定头注释。"""
    import yaml
    header = "# Auto-managed by WebUI. Edits here are overwritten on save.\n"
    CONFIG_PATH.write_text(header + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")


class ProviderIn(BaseModel):
    name: str
    base_url: str
    # api_key_ref：GET 回显的原始占位符（如 ${OPENAI_API_KEY}），保存时原样传回，
    # 避免掩码值覆盖 config.yaml 里的占位符。留空表示该 provider 无密钥（如本地 ollama）。
    api_key_ref: str = ""


class ModelCandidateIn(BaseModel):
    provider: str
    model: str


class ChannelIn(BaseModel):
    enabled: bool = False
    allowed_users: list[str] = []
    model_priority: str = "default"
    settings: dict = {}


class ConfigUpdate(BaseModel):
    providers: list[ProviderIn]
    model_priority: list[ModelCandidateIn]
    channels: dict[str, ChannelIn] = {}
    # 需要更新的密钥明文 {ENV_NAME: 新值}，只处理非空项；空表示不改。
    keys: dict[str, str] = {}


@router.get("/config")
async def get_config():
    """返回当前配置，密钥脱敏回显。

    api_key: 脱敏值（仅展示）。
    api_key_ref: config.yaml 里的原始占位符（如 ${OPENAI_API_KEY}），保存时原样传回，
                 以免被掩码值覆盖丢失。
    """
    env_keys = {k: _mask(os.getenv(k, "")) for k in _KNOWN_ENV_KEYS}
    return {
        "providers": [_provider_view(p) for p in config.providers.values()],
        "model_priority": [
            {"provider": c.provider, "model": c.model}
            for c in config.model_priority
        ],
        "channels": {name: ch.model_dump() for name, ch in config.channels.items()},
        "env_keys": env_keys,
    }


@router.put("/config")
async def update_config(req: ConfigUpdate):
    """保存配置并热更新：改 .env 密钥 → 重写 config.yaml → 重新加载单例 → 清客户端缓存。"""
    # 1. 更新非空密钥到环境变量 + .env（Tavily 这类直接读 env 的立即生效）
    for k, v in req.keys.items():
        if v and k in _KNOWN_ENV_KEYS:
            _set_env_key(k, v)

    # 2. 重建 config.yaml 原始结构。api_key 用回传的 api_key_ref（原始占位符）；
    #    若为空则该 provider 无密钥（如 local/ollama），写空串。不把明文密钥写进 config.yaml。
    providers_raw: dict[str, dict] = {}
    for p in req.providers:
        key_val = p.api_key_ref or ""
        providers_raw[p.name] = {"base_url": p.base_url, "api_key": key_val}

    raw = {
        "providers": providers_raw,
        "model_priority": [c.model_dump() for c in req.model_priority],
        "channels": {name: ch.model_dump() for name, ch in req.channels.items()},
    }

    # 3. 校验 + 原地重载单例（render 占位符 → pydantic 校验 → reload）
    try:
        rendered = _render_raw(raw)
        parsed = parse_config(rendered, raw=raw)
        # 交叉校验：model_priority 引用的 provider 必须已定义，否则拒绝保存
        missing = [c.model for c in parsed.model_priority if c.provider not in parsed.providers]
        if missing:
            raise ValueError(f"model_priority 引用了未定义的 provider：{missing}")
        reload_config(parsed)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"配置无效：{e}")

    # 4. 清 provider 客户端缓存（base_url/api_key 可能变了）
    providers_mod.reset_clients()

    # 5. 微信渠道热启停：按最新 config 同步后台任务（改 enabled/白名单/连接设置无需重启进程）
    try:
        from . import wechat as wechat_mod
        await wechat_mod.reload_channel()
    except Exception:
        logger.exception("微信渠道热更新失败")

    # 6. 落盘 config.yaml
    _write_config_yaml(raw)

    logger.info("配置已更新：%d 个 provider，%d 个 model 候选",
                len(req.providers), len(req.model_priority))
    return await get_config()


def _render_raw(raw: dict) -> dict:
    """对内存中的 config 结构渲染 ${ENV_VAR} 占位符。复用 config 模块的渲染逻辑。"""
    from .config import _render
    return _render(raw)


def parse_config(rendered: dict, raw: dict | None = None) -> AppConfig:
    """把渲染后的 dict 解析为 AppConfig（含 channels 校验）。

    raw 为渲染前的原始结构，用于保留 provider api_key 的占位符模板（api_key_ref），
    使热重载后 GET 回显仍不泄漏明文密钥。
    """
    from .config import ProviderConfig, ModelCandidate, ChannelConfig
    raw = raw or {}
    raw_providers = raw.get("providers", {}) or {}
    providers = {}
    for name, cfg in (rendered.get("providers", {}) or {}).items():
        ref = (raw_providers.get(name, {}) or {}).get("api_key", "")
        providers[name] = ProviderConfig(name=name, api_key_ref=ref, **cfg)
    priority = [ModelCandidate(**item) for item in (rendered.get("model_priority", []) or [])]
    channels = {name: ChannelConfig(**cfg) for name, cfg in (rendered.get("channels", {}) or {}).items()}
    return AppConfig(providers=providers, model_priority=priority, channels=channels)


@router.get("/conversations")
async def list_conversations(status: str = "active"):
    """按状态列出会话：active / archived / trashed。"""
    convs = await storage.list_all(status=status)
    return [
        {
            "id": c.id,
            "title": c.title,
            "status": c.status,
            "updated_at": c.updated_at,
            "deleted_at": c.deleted_at,
            "last_model": c.last_model,
        }
        for c in convs
    ]


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = await storage.get(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations")
async def create_conversation():
    conv = await storage.create()
    return conv.model_dump()


@router.post("/conversations/{conv_id}/rename")
async def rename_conversation(conv_id: str, req: RenameRequest):
    conv = await storage.rename(conv_id, req.title)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations/{conv_id}/archive")
async def archive_conversation(conv_id: str):
    conv = await storage.set_status(conv_id, "archived")
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations/{conv_id}/trash")
async def trash_conversation(conv_id: str):
    """软删除：移入回收站。"""
    conv = await storage.set_status(conv_id, "trashed")
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.post("/conversations/{conv_id}/restore")
async def restore_conversation(conv_id: str):
    """从回收站恢复为 active。"""
    conv = await storage.set_status(conv_id, "active")
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.model_dump()


@router.delete("/conversations/{conv_id}")
async def delete_conversation_forever(conv_id: str):
    """永久删除（仅回收站内可调）：直接删掉磁盘文件。"""
    ok = await storage.delete_forever(conv_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


class ApprovalRequest(BaseModel):
    approved: bool


@router.post("/command-confirm/{req_id}")
async def confirm_command(req_id: str, req: ApprovalRequest):
    """用户对高风险命令做出允许/拒绝的决定，唤醒等待中的 agent。"""
    ok = approvals.resolve(req_id, req.approved)
    if not ok:
        raise HTTPException(status_code=404, detail="确认请求不存在或已处理")
    return {"ok": True}


@router.post("/upload")
async def upload_attachment(
    file: UploadFile = File(...),
    conversation_id: str = Form(""),
):
    """上传一个附件，返回元数据。文本/图片白名单，大小上限见 attachments.MAX_FILE_SIZE。"""
    cid = conversation_id or "tmp"
    data = await file.read()
    try:
        meta = attachments_mod.save_upload(cid, file.filename or "file", data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 前端只需 id/name/type/size/mime；path 留在服务端即可（这里一并返回无妨）
    return meta


@router.post("/chat")
async def chat(req: ChatRequest):
    """接收一条用户消息，返回 SSE 事件流。"""

    conv = await storage.get(req.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if conv.status == "trashed":
        raise HTTPException(status_code=400, detail="该会话已在回收站，请先恢复再继续对话")

    # 落盘用户消息，并用返回的最新会话组装消息（带上历史）
    logger.info("收到用户消息 [会话 %s]：%s", req.conversation_id, req.message)
    user_msg = build_user_message(req.conversation_id, req.message, req.attachments)
    conv = await storage.append_message(req.conversation_id, user_msg)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    messages = list(conv.messages)
    # 首轮对话（标题还是「新会话」）时，结束后自动生成标题
    is_first_exchange = conv.title in ("", "新会话")

    async def event_stream():
        assistant_text_parts: list[str] = []
        used_model = None
        used_provider = None

        try:
            async for event in agent_mod.run_agent(messages, conversation_id=req.conversation_id):
                etype = event.get("type")

                if etype == "text_delta":
                    assistant_text_parts.append(event["content"])
                elif etype == "done":
                    used_model = event.get("model")
                elif etype == "model_fallback":
                    logger.warning("模型回退：失败=%s 原因=%s",
                                   event.get("failed"), event.get("reason"))

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # 落盘 assistant 消息
            full = "".join(assistant_text_parts)
            if full:
                logger.info("assistant 回复 [会话 %s, 模型 %s]：%s",
                            req.conversation_id, used_model, full)
                await storage.append_message(
                    req.conversation_id, {"role": "assistant", "content": full}
                )

            # 首轮对话：后台生成标题并推送（不阻塞已结束的响应流）
            if is_first_exchange:
                title = await agent_mod.generate_title(req.message)
                if title:
                    await storage.update_title(req.conversation_id, title)
                    yield f"data: {json.dumps({'type': 'title_update', 'title': title}, ensure_ascii=False)}\n\n"
        except Exception as e:  # 兜底：不要让连接挂死
            logger.exception("chat 处理异常 [会话 %s]", req.conversation_id)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 个人微信渠道：扫码登录状态（二维码在 WebUI 配置面板展示）
# ---------------------------------------------------------------------------

class WechatLoginRequest(BaseModel):
    force: bool = False  # 预留：暂未用（token 存在时 start_login 直接返回已登录）


@router.get("/wechat/status")
async def wechat_status():
    """微信渠道状态：是否启用、是否已登录、当前登录流程状态。"""
    from . import wechat as wechat_mod
    ch = config.get_channel("wechat")
    enabled = bool(ch and ch.enabled)
    svc = wechat_mod.get_wechat()
    if not svc:
        return {"enabled": enabled, "logged_in": False, "status": "idle", "qr": "", "hint": ""}
    return {"enabled": enabled, **svc.login_state()}


@router.post("/wechat/login")
async def wechat_login(req: WechatLoginRequest):
    """发起扫码登录，返回二维码（PNG data URL）供 WebUI 展示。"""
    from . import wechat as wechat_mod
    svc = wechat_mod.get_wechat()
    if not svc:
        raise HTTPException(status_code=400, detail="微信渠道未启用，请先在配置里开启并重启服务")
    if req.force and svc.login_state()["logged_in"]:
        svc.logout()
    state = await svc.start_login()
    return state


@router.get("/wechat/login/poll")
async def wechat_login_poll():
    """轮询扫码登录进度（二维码刷新 / 已扫码 / 成功 / 失败）。"""
    from . import wechat as wechat_mod
    svc = wechat_mod.get_wechat()
    if not svc:
        return {"enabled": False, "logged_in": False, "status": "idle", "qr": "", "hint": ""}
    return svc.login_state()


@router.post("/wechat/login/cancel")
async def wechat_login_cancel():
    """放弃本次扫码登录。"""
    from . import wechat as wechat_mod
    svc = wechat_mod.get_wechat()
    if svc:
        svc.cancel_login()
    return {"ok": True}


@router.post("/wechat/logout")
async def wechat_logout():
    """退出登录，清除本地凭证。"""
    from . import wechat as wechat_mod
    svc = wechat_mod.get_wechat()
    if not svc:
        raise HTTPException(status_code=400, detail="微信渠道未启用")
    svc.logout()
    return {"ok": True, **svc.login_state()}
