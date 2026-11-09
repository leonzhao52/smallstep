"""Provider 客户端池：按 provider name 懒加载并缓存 AsyncOpenAI 实例。

每个 provider 指向各自的 base_url，即可兼容 OpenAI / DeepSeek / Ollama 等
各类 OpenAI 兼容服务，无需不同的 SDK。
"""
from __future__ import annotations

from openai import AsyncOpenAI

from .config import ProviderConfig, config

_clients: dict[str, AsyncOpenAI] = {}


def get_client(provider_name: str) -> AsyncOpenAI:
    """按 name 取（或创建）一个 provider 的 AsyncOpenAI 客户端。"""
    if provider_name in _clients:
        return _clients[provider_name]

    cfg: ProviderConfig | None = config.get_provider(provider_name)
    if cfg is None:
        raise KeyError(f"未知的 provider: {provider_name}")

    client = AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "empty")
    _clients[provider_name] = client
    return client


def reset_clients() -> None:
    """清空缓存（主要用于测试）。"""
    _clients.clear()
