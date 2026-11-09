"""网络工具：网页搜索（Tavily，免 key 时降级 DuckDuckGo）+ 网页正文抓取。

- web_search：有 TAVILY_API_KEY 走 Tavily，否则用 ddgs 免 key 搜索。
- web_fetch：抓取 URL 并用 trafilatura 提取正文（markdown）。

出错都返回友好字符串，不抛异常，避免打断 agent 对话流。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .tools import register

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
FETCH_TIMEOUT = 20.0
MAX_CONTENT_CHARS = 8000


# ---------------- 搜索后端 ----------------

async def _tavily_search(query: str, max_results: int, api_key: str) -> Optional[str]:
    """Tavily 搜索。失败返回 None（交由上层降级/报错）。"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"query": query, "max_results": max_results, "search_depth": "basic"}
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.post(TAVILY_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Tavily 搜索失败：%s", e)
        return None

    results = data.get("results", [])
    if not results:
        return f"搜索 '{query}' 没有结果。"

    lines = [f"搜索 '{query}' 的结果（Tavily，共 {len(results)} 条）：\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        url = r.get("url", "").strip()
        content = (r.get("content") or "").strip()
        lines.append(f"{i}. {title}\n   链接: {url}\n   摘要: {content}\n")
    return "\n".join(lines)


def _ddg_search(query: str, max_results: int) -> Optional[str]:
    """DuckDuckGo 免 key 搜索（降级后端）。失败返回 None。"""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # 旧包名兜底
        except ImportError:
            return None

    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        logger.warning("DuckDuckGo 搜索失败：%s", e)
        return None

    if not hits:
        return f"搜索 '{query}' 没有结果。"

    lines = [f"搜索 '{query}' 的结果（DuckDuckGo，共 {len(hits)} 条）：\n"]
    for i, r in enumerate(hits, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("href") or r.get("url") or "").strip()
        body = (r.get("body") or r.get("snippet") or "").strip()
        lines.append(f"{i}. {title}\n   链接: {url}\n   摘要: {body}\n")
    return "\n".join(lines)


@register(
    name="web_search",
    description="联网搜索。返回与查询相关的网页标题、链接和摘要。配置了 TAVILY_API_KEY 时用 Tavily，否则用 DuckDuckGo。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "返回结果条数，默认 5"},
        },
        "required": ["query"],
    },
)
async def web_search(query: str, max_results: int = 5) -> str:
    max_results = max(1, min(int(max_results), 10))
    api_key = os.getenv("TAVILY_API_KEY", "").strip()

    if api_key:
        result = await _tavily_search(query, max_results, api_key)
        if result is not None:
            logger.info("web_search(Tavily): %s", query)
            return result
        # Tavily 失败，尝试降级到 DDG
        logger.info("Tavily 失败，降级到 DuckDuckGo: %s", query)

    result = _ddg_search(query, max_results)
    if result is not None:
        logger.info("web_search(DDG): %s", query)
        return result

    return f"搜索失败：'{query}'。Tavily 未配置或不可用，且 DuckDuckGo 也失败（可能需要网络代理）。"


# ---------------- 网页抓取 ----------------

def _extract_content(html: str, url: str) -> Optional[str]:
    """用 trafilatura 提取正文，优先 markdown。"""
    try:
        import trafilatura
        downloaded = html if html else trafilatura.fetch_url(url)
        if not downloaded:
            return None
        extracted = trafilatura.extract(
            downloaded, url=url, output_format="markdown",
            include_comments=False, include_tables=True,
        )
        return extracted
    except Exception as e:
        logger.warning("trafilatura 提取失败：%s", e)
        return None


@register(
    name="web_fetch",
    description="抓取一个网页并提取正文内容（markdown）。用于阅读搜索结果里的具体文章/文档。",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的网页 URL（以 http:// 或 https:// 开头）"},
        },
        "required": ["url"],
    },
)
async def web_fetch(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return f"错误：无效的 URL '{url}'，必须以 http:// 或 https:// 开头。"

    # 先用 httpx 拿 HTML（带 UA，减少被拒），再用 trafilatura 提正文
    html = None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; personal-agent/1.0)"}
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.warning("抓取 %s 失败，尝试 trafilatura 自带抓取：%s", url, e)

    content = _extract_content(html, url)
    if not content:
        return f"抓取失败：无法从 '{url}' 提取正文（页面可能为空、非文章类型或需要 JS 渲染）。"

    logger.info("web_fetch: %s (%d chars)", url, len(content))
    if len(content) > MAX_CONTENT_CHARS:
        return content[:MAX_CONTENT_CHARS] + f"\n\n[...内容过长，已截断，共约 {len(content)} 字符]"
    return content
