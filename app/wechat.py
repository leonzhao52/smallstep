"""个人微信渠道：iLink 纯 HTTP 协议（文本收发 MVP）。

参考 C:\\workspace\\nanobot-0.3.0\\nanobot\\channels\\weixin\\runtime.py，
移植其逆向的 iLink 协议（来自 @tencent-weixin/openclaw-weixin v1.0.3），
但**剥离** nanobot 的 BaseChannel/MessageBus/OutboundMessage 抽象——
smallstep 的 agent.run_agent 是纯异步生成器，微信渠道直接驱动它即可。

协议要点：
- 扫码登录：get_bot_qrcode → 轮询 get_qrcode_status（confirmed 取 bot_token）。
- 收消息：POST getupdates 长轮询，带游标 get_updates_buf，按 message_id 去重。
- 发消息：POST sendmessage，必须带 context_token（从收到的消息缓存，过期约 90s，
  发前用 getconfig 刷新）。
- 第一版只处理文本项（item_list 里 type==1），媒体留后续。

已知风险：iLink 是第三方逆向协议（非官方），无稳定性/封号保证，可能随时失效。
token 属敏感凭证，落在 gitignore 的 data/wechat/ 目录。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from . import approvals, agent as agent_mod, storage
from .config import PROJECT_ROOT, config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 协议常量（来自 openclaw-weixin types.ts）
# ---------------------------------------------------------------------------
ITEM_TEXT = 1          # item_list 里的文本项
MESSAGE_TYPE_BOT = 2   # message_type 2 = 机器人自己发的（要跳过）
WEIXIN_MAX_MESSAGE_LEN = 4000

ILINK_APP_ID = "bot"
ILINK_CHANNEL_VERSION = "2.1.1"
BASE_INFO = {"channel_version": ILINK_CHANNEL_VERSION}

ERRCODE_SESSION_EXPIRED = -14
SESSION_PAUSE_S = 60 * 60          # 会话过期后暂停 1 小时
CONTEXT_TOKEN_MAX_AGE_S = 60       # context_token 超过这个年龄就主动刷新

MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30
RETRY_DELAY_S = 2
MAX_QR_REFRESH = 3
DEFAULT_POLL_TIMEOUT_S = 35

STATE_DIR = PROJECT_ROOT / "data" / "wechat"

# 进程内单例：main.py 启动渠道时设置，/api/wechat/* 路由通过 get_wechat() 访问。
# 这样 WebUI 触发的扫码登录和后台长轮询跑在同一个 service 实例上（共享 token/状态）。
_instance: Optional["WechatService"] = None


_task: Optional[asyncio.Task] = None


def get_wechat() -> Optional["WechatService"]:
    return _instance


def set_wechat(svc: Optional["WechatService"]) -> None:
    global _instance
    _instance = svc


# ---------------------------------------------------------------------------
# 渠道生命周期（热启停）：供 main.py lifespan 和 PUT /api/config 调用
# ---------------------------------------------------------------------------

async def start_channel() -> None:
    """按当前 config 启动微信渠道（已启动则忽略）。enabled=false 时不做任何事。"""
    global _task
    ch = config.get_channel("wechat")
    if not (ch and ch.enabled):
        logger.info("微信渠道未启用，跳过启动")
        return
    if _instance is not None and _task and not _task.done():
        logger.info("微信渠道已在运行，忽略重复启动")
        return
    svc = WechatService()
    set_wechat(svc)
    _task = asyncio.create_task(svc.start())
    logger.info("微信渠道已启动（未登录时在 WebUI 扫码）")


async def stop_channel() -> None:
    """停止微信渠道并清掉单例。幂等——未运行时安全调用。"""
    global _task
    if _instance is not None:
        await _instance.stop()
    set_wechat(None)
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


async def reload_channel() -> None:
    """配置热更新后同步渠道状态：按 enabled 决定启停；配置项变了则重启。

    - enabled=false：停掉（若在跑）。
    - enabled=true 且未跑：启动。
    - enabled=true 且在跑，但 base_url/poll_timeout/白名单变了：重启以应用新配置。
    """
    ch = config.get_channel("wechat")
    enabled = bool(ch and ch.enabled)
    running = _instance is not None and _task is not None and not _task.done()

    if not enabled:
        if running or _instance is not None:
            await stop_channel()
        return

    # 需要重启吗：对比运行中实例的关键配置和最新 config
    if running:
        svc = _instance
        settings = (ch.settings if ch else {}) or {}
        new_base = settings.get("base_url") or "https://ilinkai.weixin.qq.com"
        new_timeout = int(settings.get("poll_timeout") or DEFAULT_POLL_TIMEOUT_S)
        new_users = list(ch.allowed_users or [])
        new_token = settings.get("token") or ""
        cfg_changed = (
            svc._base_url != new_base
            or svc._poll_timeout_s != new_timeout
            or svc._allowed_users != new_users
            or svc._boot_token != new_token  # 对比启动时的 settings token，避免扫码后的真实 token 触发误重启
        )
        if cfg_changed:
            logger.info("微信渠道配置变更，重启以应用")
            await stop_channel()
            await start_channel()
        else:
            logger.info("微信渠道配置未变，保持运行")
        return

    await start_channel()


def _qr_png_data_url(content: str) -> str:
    """把扫码内容渲染成 PNG 的 data URL，供 WebUI 直接 <img> 展示。

    iLink 返回的 qrcode_img_content 是一段可被微信识别的内容/链接，
    用 qrcode 库本地重绘成二维码图片（比终端 ASCII 友好得多）。
    """
    import io
    import qrcode

    img = qrcode.make(content, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _build_client_version(version: str) -> int:
    """把语义化版本编码成 0x00MMNNPP。"""
    parts = version.split(".")
    def _int(i):
        try:
            return int(parts[i])
        except Exception:
            return 0
    return ((_int(0) & 0xFF) << 16) | ((_int(1) & 0xFF) << 8) | (_int(2) & 0xFF)


ILINK_APP_CLIENT_VERSION = _build_client_version(ILINK_CHANNEL_VERSION)


def _random_wechat_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 → 十进制字符串 → base64。每请求新生成。"""
    uint32 = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(uint32).encode()).decode()


class WechatService:
    """个人微信渠道服务：扫码登录 + 长轮询收消息 + 调 agent + 发回复。

    作为一个常驻 asyncio 任务跑在 FastAPI lifespan 里。
    """

    def __init__(self) -> None:
        ch = config.get_channel("wechat")
        settings = (ch.settings if ch else {}) or {}
        self._enabled = bool(ch and ch.enabled)
        self._allowed_users: list[str] = (ch.allowed_users if ch else []) or []
        self._base_url: str = settings.get("base_url") or "https://ilinkai.weixin.qq.com"
        self._poll_timeout_s: int = int(settings.get("poll_timeout") or DEFAULT_POLL_TIMEOUT_S)

        self._token: str = settings.get("token") or ""
        self._boot_token: str = self._token  # 启动时 settings 里的 token，用于热重载对比
        self._get_updates_buf: str = ""
        self._context_tokens: dict[str, str] = {}      # from_user_id -> context_token
        self._context_token_at: dict[str, float] = {}  # from_user_id -> 缓存时间
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._session_pause_until: float = 0.0
        self._next_poll_timeout_s: int = self._poll_timeout_s

        self._client: Optional[httpx.AsyncClient] = None
        self._running = False

        # WebUI 扫码登录会话状态（/api/wechat/* 读写，前端轮询展示）
        self._login_status = "idle"   # idle | waiting | confirmed | failed
        self._login_qr: str = ""       # 二维码 PNG data URL
        self._login_hint = ""          # 给用户看的提示（如"已扫码，请在手机确认"）
        self._login_task: Optional[asyncio.Task] = None
        self._login_done: asyncio.Event = asyncio.Event()  # confirmed/failed 时 set

    # ------------------------------------------------------------------
    # 状态持久化（token / 游标 / context_token 落盘，重启免扫码）
    # ------------------------------------------------------------------

    def _state_file(self) -> Path:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return STATE_DIR / "account.json"

    def _load_state(self) -> bool:
        f = self._state_file()
        if not f.exists():
            return False
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            self._token = data.get("token", "") or self._token
            self._get_updates_buf = data.get("get_updates_buf", "")
            ctx = data.get("context_tokens", {})
            if isinstance(ctx, dict):
                self._context_tokens = {str(k): str(v) for k, v in ctx.items() if v}
            if data.get("base_url"):
                self._base_url = data["base_url"]
            return bool(self._token)
        except Exception:
            logger.exception("加载微信账号状态失败")
            return False

    def _save_state(self) -> None:
        try:
            self._state_file().write_text(json.dumps({
                "token": self._token,
                "get_updates_buf": self._get_updates_buf,
                "context_tokens": self._context_tokens,
                "base_url": self._base_url,
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.exception("保存微信账号状态失败")

    # ------------------------------------------------------------------
    # HTTP 封装
    # ------------------------------------------------------------------

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        h = {
            "X-WECHAT-UIN": _random_wechat_uin(),
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if auth and self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    # ------------------------------------------------------------------
    # 扫码登录
    # ------------------------------------------------------------------

    async def _fetch_qr(self) -> tuple[str, str]:
        """取二维码，返回 (qrcode_id, 扫码 URL/内容)。"""
        data = await self._get("ilink/bot/get_bot_qrcode", params={"bot_type": "3"}, auth=False)
        qrcode_id = data.get("qrcode", "")
        if not qrcode_id:
            raise RuntimeError(f"获取微信二维码失败：{data}")
        return qrcode_id, (data.get("qrcode_img_content") or qrcode_id)

    def _print_qr(self, content: str) -> None:
        """终端打印二维码供扫码。

        Windows 控制台默认 GBK，编不了二维码用的块字符（U+2580 等），
        会抛 UnicodeEncodeError。先把 stdout 重配为 UTF-8；仍失败则降级为
        纯 ASCII（'#'/' '）自绘，保证任何终端都能扫。
        """
        import sys
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(content)
            qr.make(fit=True)
            qr.print_ascii(invert=True, out=sys.stdout)
        except UnicodeEncodeError:
            self._print_qr_ascii(content)
        except ImportError:
            print(f"\n微信登录链接（用微信扫码或打开）：{content}\n")

    @staticmethod
    def _print_qr_ascii(content: str) -> None:
        """纯 ASCII 二维码（'#'/' '），规避非 UTF-8 终端的编码问题。"""
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        print()
        for row in matrix:
            print("".join("#" if cell else " " for cell in row))
        print()

    async def login(self, force: bool = False) -> bool:
        """扫码登录并保存 token。force=True 丢弃已有凭证重新登录。返回是否成功。"""
        if force:
            self._token = ""
            self._get_updates_buf = ""
            sf = self._state_file()
            if sf.exists():
                sf.unlink()
        if self._token or self._load_state():
            logger.info("微信已登录（复用已存 token）")
            return True

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=30), follow_redirects=True)
        self._running = True
        try:
            return await self._qr_login()
        finally:
            self._running = False
            await self._client.aclose()
            self._client = None

    def login_state(self) -> dict:
        """给 WebUI 轮询的登录状态快照。"""
        return {
            "status": self._login_status,
            "qr": self._login_qr,
            "hint": self._login_hint,
            "logged_in": bool(self._token),
        }

    def _reset_login_state(self) -> None:
        self._login_status = "idle"
        self._login_qr = ""
        self._login_hint = ""

    async def start_login(self) -> dict:
        """WebUI 触发扫码登录：起一个后台任务跑二维码流程，立即返回当前状态。

        前端随后轮询 login_state() 拿二维码图和 confirmed/failed。重复调用会先取消上一次。
        """
        if self._token:
            self._login_status = "confirmed"
            self._login_done.set()
            return self.login_state()
        # 取消上一次还在跑的登录任务
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
            try:
                await self._login_task
            except (asyncio.CancelledError, Exception):
                pass
        self._login_done.clear()
        self._login_task = asyncio.create_task(self._qr_login())
        return self.login_state()

    def cancel_login(self) -> None:
        """放弃本次扫码（前端关闭弹窗/点取消）。"""
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
        self._reset_login_state()

    def logout(self) -> None:
        """清除已存 token，下次需重新扫码。"""
        self._token = ""
        self._get_updates_buf = ""
        self._context_tokens.clear()
        self._context_token_at.clear()
        sf = self._state_file()
        if sf.exists():
            sf.unlink()
        self._login_status = "idle"
        self._login_done.clear()
        logger.info("微信已退出登录，清除本地凭证")

    def _publish_qr(self, qr_content: str) -> None:
        """把二维码渲染成 PNG data URL 供 WebUI 展示；失败则退回终端打印。"""
        try:
            self._login_qr = _qr_png_data_url(qr_content)
        except Exception:
            logger.exception("生成二维码图片失败，降级到终端打印")
            self._login_qr = ""
            self._print_qr(qr_content)

    async def _qr_login(self) -> bool:
        self._login_status = "waiting"
        self._login_hint = "请用微信扫码"
        try:
            refresh = 0
            qrcode_id, qr_url = await self._fetch_qr()
            self._publish_qr(qr_url)
            poll_base = self._base_url
            while self._running:
                try:
                    status = await self._get(
                        "ilink/bot/get_qrcode_status", params={"qrcode": qrcode_id},
                        auth=False, base_url=poll_base,
                    )
                except (httpx.TimeoutException, httpx.TransportError):
                    await asyncio.sleep(1)
                    continue
                if not isinstance(status, dict):
                    await asyncio.sleep(1)
                    continue

                st = status.get("status", "")
                if st == "confirmed":
                    token = status.get("bot_token", "")
                    if token:
                        self._token = token
                        if status.get("baseurl"):
                            self._base_url = status["baseurl"]
                        self._save_state()
                        self._login_status = "confirmed"
                        self._login_hint = "登录成功"
                        self._login_done.set()
                        logger.info("微信登录成功 user_id=%s", status.get("ilink_user_id", ""))
                        return True
                    self._login_status = "failed"
                    self._login_hint = "扫码确认但未返回凭证，请重试"
                    logger.error("扫码确认但未返回 bot_token")
                    self._login_done.set()
                    return False
                if st == "scaned_but_redirect":
                    host = str(status.get("redirect_host", "") or "").strip()
                    if host:
                        poll_base = host if host.startswith(("http://", "https://")) else f"https://{host}"
                    self._login_hint = "已扫码，请在手机上确认登录"
                elif st == "expired":
                    refresh += 1
                    if refresh > MAX_QR_REFRESH:
                        self._login_status = "failed"
                        self._login_hint = "二维码过期次数过多，请重新发起登录"
                        self._login_done.set()
                        logger.warning("二维码过期次数过多，放弃登录")
                        return False
                    qrcode_id, qr_url = await self._fetch_qr()
                    self._publish_qr(qr_url)
                    self._login_hint = "二维码已刷新，请重新扫码"
                    poll_base = self._base_url
                # wait -> 继续轮询
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            self._reset_login_state()
            raise
        except Exception:
            logger.exception("微信扫码登录失败")
            self._login_status = "failed"
            self._login_hint = "登录出错，请查看日志或重试"
        self._login_done.set()
        return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _ensure_client(self) -> None:
        """惰性建 HTTP 客户端——登录流程可能在 start() 之前就被 WebUI 触发。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._poll_timeout_s + 10, connect=30),
                follow_redirects=True,
            )

    async def _get(self, endpoint: str, params: dict | None = None, *, auth: bool = True,
                   base_url: str | None = None) -> dict:
        self._ensure_client()
        url = f"{(base_url or self._base_url).rstrip('/')}/{endpoint}"
        resp = await self._client.get(url, params=params, headers=self._headers(auth=auth))
        resp.raise_for_status()
        return resp.json()

    async def _post(self, endpoint: str, body: dict) -> dict:
        self._ensure_client()
        if "base_info" not in body:
            body["base_info"] = BASE_INFO
        url = f"{self._base_url.rstrip('/')}/{endpoint}"
        resp = await self._client.post(url, json=body, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def start(self) -> None:
        """长轮询主循环。作为常驻 asyncio 任务运行。

        有 token（配置或落盘）就直接收消息；否则**不再在终端扫码**，
        而是等待用户在 WebUI 配置面板里发起扫码登录（start_login）。
        """
        if not self._enabled:
            logger.info("微信渠道未启用，跳过启动")
            return

        self._ensure_client()
        self._running = True

        if not (self._token or self._load_state()):
            logger.info("微信未登录，等待在 WebUI 配置面板扫码登录…")
            await self._login_done.wait()
            if not self._token:
                logger.error("微信登录未完成，渠道不收消息。可在配置面板重试或先填 token。")
                self._running = False
                return

        logger.info("微信渠道启动，开始长轮询收消息…")
        failures = 0
        while self._running:
            try:
                await self._poll_once()
                failures = 0
            except httpx.TimeoutException:
                continue  # 长轮询超时是正常的
            except Exception:
                if not self._running:
                    break
                logger.exception("微信轮询出错")
                failures += 1
                await asyncio.sleep(BACKOFF_DELAY_S if failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_S)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    failures = 0

    async def stop(self) -> None:
        self._running = False
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None
        self._save_state()
        logger.info("微信渠道已停止")

    # ------------------------------------------------------------------
    # 长轮询收消息
    # ------------------------------------------------------------------

    async def _poll_once(self) -> None:
        # 会话过期暂停中
        remaining = self._session_pause_until - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)
            return

        self._client.timeout = httpx.Timeout(self._next_poll_timeout_s + 10, connect=30)
        data = await self._post("ilink/bot/getupdates", {"get_updates_buf": self._get_updates_buf})

        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret not in (None, 0)) or (errcode not in (None, 0)):
            if errcode == ERRCODE_SESSION_EXPIRED or ret == ERRCODE_SESSION_EXPIRED:
                self._session_pause_until = time.time() + SESSION_PAUSE_S
                logger.warning("微信会话过期（errcode -14），暂停 %d 分钟", SESSION_PAUSE_S // 60)
                return
            raise RuntimeError(f"getupdates 失败：ret={ret} errcode={errcode} {data.get('errmsg', '')}")

        # 服务端建议的轮询超时
        server_ms = data.get("longpolling_timeout_ms")
        if server_ms and server_ms > 0:
            self._next_poll_timeout_s = max(server_ms // 1000, 5)

        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
            self._save_state()

        for msg in data.get("msgs", []) or []:
            try:
                await self._process_message(msg)
            except Exception:
                logger.exception("处理微信消息失败")

    async def _process_message(self, msg: dict) -> None:
        if msg.get("message_type") == MESSAGE_TYPE_BOT:
            return  # 跳过自己发的

        msg_id = str(msg.get("message_id", "") or msg.get("seq", ""))
        if not msg_id:
            msg_id = f"{msg.get('from_user_id', '')}_{msg.get('create_time_ms', '')}"
        from_user_id = msg.get("from_user_id", "") or ""
        if not from_user_id:
            return

        # 按 message_id 去重
        if msg_id in self._processed_ids:
            return
        self._processed_ids[msg_id] = None
        while len(self._processed_ids) > 1000:
            self._processed_ids.popitem(last=False)

        # 缓存 context_token（回复必需）
        ctx = msg.get("context_token", "")
        if ctx:
            self._context_tokens[from_user_id] = ctx
            self._context_token_at[from_user_id] = time.time()
            self._save_state()

        # 第一版只取文本项（type==1）
        text_parts = []
        for item in msg.get("item_list", []) or []:
            if item.get("type", 0) == ITEM_TEXT:
                t = (item.get("text_item") or {}).get("text", "")
                if t:
                    text_parts.append(t)
        content = "\n".join(text_parts).strip()
        if not content:
            return

        if not self._is_allowed(from_user_id):
            logger.warning("拒绝非白名单微信用户：%s", from_user_id)
            return

        logger.info("微信收消息 from=%s len=%d", from_user_id, len(content))
        await self._handle_text(from_user_id, content)

    def _is_allowed(self, sender_id: str) -> bool:
        if "*" in self._allowed_users:
            return True
        return str(sender_id) in self._allowed_users

    # ------------------------------------------------------------------
    # 消息 -> agent -> 回复
    # ------------------------------------------------------------------

    async def _handle_text(self, from_user_id: str, text: str) -> None:
        # 每个微信联系人一个独立会话（channel=wechat）
        conv = await storage.get_or_create_by_sender("wechat", from_user_id)
        user_msg = {"role": "user", "content": text}
        conv = await storage.append_message(conv.id, user_msg)

        is_first = conv.title in ("", "新会话")
        messages = list(conv.messages)

        reply_parts: list[str] = []
        used_model = None
        async for event in agent_mod.run_agent(messages, conversation_id=conv.id):
            # approval_request 是 {"event": {...}} 包裹结构，其余事件是平铺的；
            # 兼容两种形态，统一取内层 payload 做判断。
            payload = event.get("event") if isinstance(event.get("event"), dict) else event
            etype = payload.get("type")
            if event.get("type") == "text_delta" or etype == "text_delta":
                reply_parts.append(event.get("content") or payload.get("content") or "")
            elif etype == "done":
                used_model = payload.get("model")
            elif etype == "approval_request":
                # 微信渠道没有网页确认按钮：高风险命令一律自动拒绝
                req_id = payload.get("request_id") or payload.get("req_id")
                if req_id:
                    approvals.resolve(req_id, False)
                logger.info("微信渠道自动拒绝高风险命令：%s", payload.get("command", ""))
            elif etype == "model_fallback":
                logger.warning("微信渠道模型回退：%s", payload.get("failed"))

        reply = "".join(reply_parts).strip()
        if reply:
            logger.info("微信回复 from=%s model=%s len=%d", from_user_id, used_model, len(reply))
            await storage.append_message(conv.id, {"role": "assistant", "content": reply})
            await self._send_text(from_user_id, reply)

        # 首轮生成标题（不阻塞）
        if is_first:
            title = await agent_mod.generate_title(text)
            if title:
                await storage.update_title(conv.id, title)

    # ------------------------------------------------------------------
    # 发消息
    # ------------------------------------------------------------------

    async def _refresh_context_token(self, user_id: str) -> str:
        """context_token 过期约 90s，发前主动刷新。"""
        ctx = self._context_tokens.get(user_id, "")
        if not ctx:
            return ctx
        age = time.time() - self._context_token_at.get(user_id, 0)
        if age < CONTEXT_TOKEN_MAX_AGE_S:
            return ctx
        try:
            data = await self._post("ilink/bot/getconfig", {
                "ilink_user_id": user_id, "context_token": ctx,
            })
            new = str(data.get("context_token", "") or "")
            if new and new != ctx and data.get("ret", 0) == 0:
                self._context_tokens[user_id] = new
                self._context_token_at[user_id] = time.time()
                self._save_state()
                return new
        except Exception:
            logger.warning("刷新 context_token 失败（user=%s），用旧值", user_id)
        return ctx

    async def _send_text(self, to_user_id: str, text: str) -> None:
        if not self._client or not self._token:
            raise RuntimeError("微信客户端未初始化或未登录")
        ctx = await self._refresh_context_token(to_user_id)
        for chunk in _split_message(text, WEIXIN_MAX_MESSAGE_LEN):
            body = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": f"smallstep-{uuid.uuid4().hex[:12]}",
                    "message_type": MESSAGE_TYPE_BOT,
                    "message_state": 2,
                    "item_list": [{"type": ITEM_TEXT, "text_item": {"text": chunk}}],
                    "context_token": ctx,
                },
            }
            data = await self._post("ilink/bot/sendmessage", body)
            ret = data.get("ret", 0)
            errcode = data.get("errcode", 0)
            if (ret not in (None, 0)) or (errcode not in (None, 0)):
                raise RuntimeError(
                    f"微信发消息失败：ret={ret} errcode={errcode} {data.get('errmsg', '')}"
                )


def _split_message(text: str, limit: int) -> list[str]:
    """按 limit 切分长文本为多条。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
