"""Web search tool for research agents.

Uses the Brave Search API (BRAVE_SEARCH_API_KEY env var) via httpx.
Falls back to the DuckDuckGo Instant Answer API (no key required) for
basic lookups when Brave is not configured.

Registers as: web_search  (toolset="web")

Configuration:
  BRAVE_SEARCH_API_KEY  — Brave Search API key (free tier: 2000 req/month)
                          https://api.search.brave.com/
"""

from __future__ import annotations

import json
import logging
import os

from core.registry import registry, tool_error

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_DDG_ENDPOINT = "https://api.duckduckgo.com/"

_MAX_SNIPPET_CHARS = 300


def _search_brave(query: str, num_results: int, api_key: str) -> str:
    try:
        import httpx
    except ImportError:
        return tool_error("httpx is not installed. Run: pip install httpx")

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(num_results, 20)}

    try:
        response = httpx.get(_BRAVE_ENDPOINT, headers=headers, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return tool_error(f"Brave Search API error: {e}")

    results = []
    for item in data.get("web", {}).get("results", [])[:num_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("description") or "")[:_MAX_SNIPPET_CHARS],
        })

    return json.dumps({
        "query": query,
        "source": "brave",
        "results": results,
        "total_returned": len(results),
    }, ensure_ascii=False)


def _search_duckduckgo(query: str, num_results: int) -> str:
    try:
        import httpx
    except ImportError:
        return tool_error("httpx is not installed. Run: pip install httpx")

    params = {
        "q": query,
        "format": "json",
        "no_redirect": "1",
        "no_html": "1",
        "skip_disambig": "1",
    }

    try:
        response = httpx.get(_DDG_ENDPOINT, params=params, timeout=10.0)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return tool_error(f"DuckDuckGo API error: {e}")

    results = []

    # Abstract result (if any)
    abstract = data.get("AbstractText", "")
    abstract_url = data.get("AbstractURL", "")
    if abstract and abstract_url:
        results.append({
            "title": data.get("Heading", query),
            "url": abstract_url,
            "snippet": abstract[:_MAX_SNIPPET_CHARS],
        })

    # Related topics
    for topic in data.get("RelatedTopics", [])[:num_results]:
        if not isinstance(topic, dict):
            continue
        text = topic.get("Text", "")
        url = topic.get("FirstURL", "")
        if text and url:
            results.append({
                "title": text[:80],
                "url": url,
                "snippet": text[:_MAX_SNIPPET_CHARS],
            })
        if len(results) >= num_results:
            break

    if not results:
        return json.dumps({
            "query": query,
            "source": "duckduckgo",
            "results": [],
            "note": (
                "No results from DuckDuckGo instant answer API. "
                "Set BRAVE_SEARCH_API_KEY for full web search results."
            ),
        })

    return json.dumps({
        "query": query,
        "source": "duckduckgo",
        "results": results[:num_results],
        "total_returned": min(len(results), num_results),
    }, ensure_ascii=False)


def _web_search(query: str, num_results: int = 5) -> str:
    """Search the web and return a list of results.

    Args:
        query:       Search query string.
        num_results: Number of results to return (max 20). Default 5.
    """
    if not query or not query.strip():
        return tool_error("query must not be empty")

    num_results = max(1, min(num_results, 20))

    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if api_key:
        return _search_brave(query, num_results, api_key)

    logger.debug(
        "web_search: BRAVE_SEARCH_API_KEY not set; falling back to DuckDuckGo"
    )
    return _search_duckduckgo(query, num_results)


registry.register(
    name="web_search",
    handler=_web_search,
    schema={
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g. 'FastAPI authentication best practices')",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (1–20). Default 5.",
                    "default": 5,
                },
            },
        }
    },
    toolset="web",
    description=(
        "Search the web and return titles, URLs, and snippets. "
        "Uses Brave Search API if BRAVE_SEARCH_API_KEY is set, otherwise DuckDuckGo."
    ),
)
