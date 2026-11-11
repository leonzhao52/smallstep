"""配置加载：config.yaml + .env 环境变量注入，pydantic 校验。

密钥不写进 config.yaml，而是用 ${ENV_VAR} 或 ${ENV_VAR:-默认值} 占位符，
加载时从环境变量渲染。
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 占位符：${VAR} 或 ${VAR:-default}
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _render_value(value: str) -> str:
    """把字符串里的 ${VAR} / ${VAR:-default} 替换为环境变量值。"""

    def _sub(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        env = os.getenv(name)
        if env is not None and env != "":
            return env
        if default is not None:
            return default
        # 未设置且无默认值 -> 留空（pydantic 层面不强制，交给 provider 校验/回退处理）
        return ""

    return _PLACEHOLDER_RE.sub(_sub, value)


def _render(obj):
    """递归渲染 dict / list / str 中的占位符。"""
    if isinstance(obj, str):
        return _render_value(obj)
    if isinstance(obj, dict):
        return {k: _render(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render(v) for v in obj]
    return obj


class ProviderConfig(BaseModel):
    """单个 LLM provider 的连接信息。"""

    name: str
    base_url: str
    api_key: str = ""
    # api_key 字段在 config.yaml 里的原始模板（如 ${OPENAI_API_KEY}），未渲染环境变量。
    # 供 WebUI 配置回显用，避免把明文密钥泄漏到前端。不参与 provider 连接逻辑。
    api_key_ref: str = ""


class ModelCandidate(BaseModel):
    """优先级列表里的一项：某个 provider 下的某个 model。"""

    provider: str
    model: str

    def label(self) -> str:
        return f"{self.provider}/{self.model}"


class ChannelConfig(BaseModel):
    """一个接入渠道（webui / wechat ...）的配置。

    渠道无关：每个渠道可独立开关、设用户白名单、引用一套 model_priority。
    wechat 目前只是预留结构，尚无实现。
    """

    enabled: bool = False
    allowed_users: list[str] = Field(default_factory=list)
    model_priority: str = "default"   # 引用模型档名（当前只有 default 一套）
    settings: dict = Field(default_factory=dict)


class AppConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    model_priority: list[ModelCandidate] = Field(default_factory=list)
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)

    def get_provider(self, name: str) -> Optional[ProviderConfig]:
        return self.providers.get(name)

    def get_model_candidates(self) -> list[ModelCandidate]:
        """返回有优先级的 model 候选列表（从高到低）。"""
        return list(self.model_priority)

    def get_channel(self, name: str) -> Optional[ChannelConfig]:
        return self.channels.get(name)


def load_config() -> AppConfig:
    """加载 .env 并解析 config.yaml，产出 AppConfig。"""
    load_dotenv(PROJECT_ROOT / ".env")

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"未找到 {CONFIG_PATH}。请先复制 config.example.yaml 为 config.yaml。"
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    rendered = _render(raw)

    providers_rendered = rendered.get("providers", {}) or {}
    providers_raw = raw.get("providers", {}) or {}
    providers = {}
    for name, cfg in providers_rendered.items():
        # api_key_ref 取渲染前的原始模板（占位符），供 WebUI 回显，避免泄漏明文密钥
        ref = (providers_raw.get(name, {}) or {}).get("api_key", "")
        providers[name] = ProviderConfig(name=name, api_key_ref=ref, **cfg)

    priority_raw = rendered.get("model_priority", []) or []
    priority = [ModelCandidate(**item) for item in priority_raw]

    channels_raw = rendered.get("channels", {}) or {}
    channels = {name: ChannelConfig(**cfg) for name, cfg in channels_raw.items()}

    return AppConfig(
        providers=providers, model_priority=priority, channels=channels
    )


# 热更新锁：防止并发保存时对文件和单例的读-改-写交错
_reload_lock = threading.Lock()


def reload_config(new: AppConfig) -> None:
    """把新配置原地合并进模块级单例 config。

    其他模块用 `from .config import config` 绑定了这个名字，重新赋值不会传播，
    所以必须原地修改同一个 AppConfig 实例，改动才能在各模块生效。
    调用方负责在必要时另行调用 providers.reset_clients() 清客户端缓存。
    """
    with _reload_lock:
        config.providers.clear()
        config.providers.update(new.providers)
        config.model_priority = list(new.model_priority)
        config.channels.clear()
        config.channels.update(new.channels)


# 模块级单例，供其他模块导入使用
config = load_config()
