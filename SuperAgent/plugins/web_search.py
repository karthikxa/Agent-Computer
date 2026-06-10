"""Example plugin: web search tool for the PluginRegistry.

Feature #64 — plug this file into a plugins/ directory and the
PluginRegistry will auto-discover it via load_from_directory().

The registry looks for a PLUGIN_MANIFEST dict at module level.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web using DuckDuckGo Instant Answer API."""
    try:
        import aiohttp
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
                results = []
                if data.get("AbstractText"):
                    results.append({
                        "title": data.get("Heading", ""),
                        "snippet": data["AbstractText"],
                        "url": data.get("AbstractURL", ""),
                    })
                for topic in data.get("RelatedTopics", [])[:max_results - len(results)]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        results.append({
                            "title": topic.get("Text", "")[:80],
                            "snippet": topic.get("Text", ""),
                            "url": topic.get("FirstURL", ""),
                        })
                return results[:max_results]
    except Exception as exc:
        logger.warning("web_search plugin error: %s", exc)
        return []


# Required: plugin manifest discovered by PluginRegistry.load_from_directory()
PLUGIN_MANIFEST = {
    "name": "web_search",
    "description": "Search the web using DuckDuckGo and return top results",
    "fn": web_search,
    "input_schema": {
        "query": "string — search terms",
        "max_results": "int — maximum number of results to return (default 5)",
    },
    "timeout_seconds": 20.0,
    "version": "1.0.0",
    "tags": ["web", "search", "information"],
}
