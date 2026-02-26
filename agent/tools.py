"""
Tool-Implementierungen für den Claude Agent.
Jedes Tool hat: eine Python-Funktion + ein Anthropic-Schema für tool_use.
"""

import socket
import json
import re
import os
import logging
from urllib.parse import urlencode, quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Anthropic Tool Schemas
# ─────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "fetch_rss",
        "description": (
            "Fetches and parses an RSS feed, returning the latest articles "
            "with title, link, summary, and published date. Use this to get "
            "fresh content from OpenAI Blog, Anthropic Blog, or other feeds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The RSS feed URL"
                },
                "max_items": {
                    "type": "integer",
                    "description": "Maximum number of items to return (default: 8)",
                    "default": 8
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "fetch_article",
        "description": (
            "Fetches and extracts the main text content from a web article URL. "
            "Use this to get the full article text when the RSS summary is too short "
            "to understand what the article is about. Returns the first ~3000 characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The article URL to read"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web for current information. Use to find context, reactions, "
            "German market perspective, or additional details on a topic. "
            "Good for finding: recent stats, expert reactions, German-language coverage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query in German or English"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5)",
                    "default": 5
                }
            },
            "required": ["query"]
        }
    }
]


# ─────────────────────────────────────────────
# Tool Implementations
# ─────────────────────────────────────────────

def fetch_rss(url: str, max_items: int = 8) -> str:
    """Parse an RSS feed and return structured article data."""
    try:
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(15)
        try:
            feed = feedparser.parse(url)
        finally:
            socket.setdefaulttimeout(old_timeout)

        if feed.bozo and not feed.entries:
            return json.dumps({"error": f"Failed to parse RSS feed: {url}", "items": []})

        items = []
        for entry in feed.entries[:max_items]:
            # Clean summary from HTML tags
            summary_raw = entry.get("summary", entry.get("description", ""))
            summary_clean = BeautifulSoup(summary_raw, "lxml").get_text(separator=" ")[:500].strip()

            published = ""
            if hasattr(entry, "published"):
                published = entry.published
            elif hasattr(entry, "updated"):
                published = entry.updated

            items.append({
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", ""),
                "summary": summary_clean,
                "published": published,
                "author": entry.get("author", "")
            })

        return json.dumps({"feed_title": feed.feed.get("title", url), "items": items}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"fetch_rss error for {url}: {e}")
        return json.dumps({"error": str(e), "items": []})


def fetch_article(url: str) -> str:
    """Scrape main text content from an article URL."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")

        # Remove noise
        for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                          "form", "iframe", "noscript", "figure"]):
            tag.decompose()

        # Title
        title = ""
        if soup.title:
            title = soup.title.get_text().strip()

        # Try to find main content in order of preference
        content = None
        for selector in ["article", "main", '[role="main"]', ".post-content",
                          ".article-body", ".entry-content", "#content", "body"]:
            content = soup.select_one(selector)
            if content:
                break

        if not content:
            content = soup

        text = content.get_text(separator="\n")
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        text = text.strip()[:3000]

        return json.dumps({
            "url": url,
            "title": title,
            "text": text,
            "char_count": len(text)
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"fetch_article error for {url}: {e}")
        return json.dumps({"error": str(e), "url": url, "text": ""})


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web. Uses Brave API if key present, else DuckDuckGo."""
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()

    if brave_key:
        return _brave_search(query, max_results, brave_key)
    else:
        return _duckduckgo_search(query, max_results)


def _brave_search(query: str, max_results: int, api_key: str) -> str:
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results, "search_lang": "de"},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", "")
            })
        return json.dumps({"query": query, "results": results}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Brave search error: {e}")
        return _duckduckgo_search(query, max_results)


def _duckduckgo_search(query: str, max_results: int) -> str:
    """Fallback: scrape DuckDuckGo HTML results."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        }
        resp = requests.get(
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")
        results = []

        for result in soup.select(".result")[:max_results]:
            title_el = result.select_one(".result__title a")
            snippet_el = result.select_one(".result__snippet")

            if not title_el:
                continue

            href = title_el.get("href", "")
            # DuckDuckGo wraps URLs – extract real URL
            if "uddg=" in href:
                href = requests.utils.unquote(href.split("uddg=")[-1].split("&")[0])

            results.append({
                "title": title_el.get_text().strip(),
                "url": href,
                "snippet": snippet_el.get_text().strip() if snippet_el else ""
            })

        return json.dumps({"query": query, "results": results, "source": "duckduckgo"},
                          ensure_ascii=False)

    except Exception as e:
        logger.error(f"DuckDuckGo search error: {e}")
        return json.dumps({"error": str(e), "query": query, "results": []})


# ─────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Route a tool_use call from Claude to the right function."""
    if tool_name == "fetch_rss":
        return fetch_rss(
            url=tool_input["url"],
            max_items=tool_input.get("max_items", 8)
        )
    elif tool_name == "fetch_article":
        return fetch_article(url=tool_input["url"])
    elif tool_name == "web_search":
        return web_search(
            query=tool_input["query"],
            max_results=tool_input.get("max_results", 5)
        )
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
