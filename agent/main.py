#!/usr/bin/env python3
"""
LinkedIn Agent - Morning Idea Generator
Aufgerufen von n8n via HTTP Request.

Zweistufiger Pipeline:
  Phase 1 (Synthese):  Claude fetcht 15+ RSS-Feeds, filtert auf 48h, dedupliziert Topics
  Phase 2 (Ideen):     Claude wählt 10 beste Topics, erstellt Post-Ideen mit autofyn-Winkel

Input JSON:
    {
        "email_content": "Full newsletter text...",
        "email_subject": "Startup Insider Daily - ...",
        "rss_openai": [{"title": "...", "link": "...", "summary": "...", "pubDate": "..."}],
        "rss_anthropic": [{"title": "...", "link": "...", "summary": "...", "pubDate": "..."}]
    }

Output (stdout, JSON):
    {
        "status": "success",
        "ideas": [...10 idea objects...],
        "generated_at": "2026-02-25T09:00:00+01:00",
        "model": "claude-sonnet-4-6"
    }
"""

import os
import sys
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv

# Load .env from parent directory of this script
load_dotenv(Path(__file__).parent.parent / ".env")

from prompts import SYNTHESIS_SYSTEM_PROMPT, IDEA_GENERATION_SYSTEM_PROMPT
from tools import TOOL_DEFINITIONS, execute_tool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr  # logs go to stderr, output goes to stdout
)
logger = logging.getLogger("linkedin_agent")

MODEL = "claude-sonnet-4-6"

# Phase 1 – Synthesis constants
SYNTHESIS_MAX_ITERATIONS = 20
SYNTHESIS_FORCE_OUTPUT_AFTER = 15  # allow many RSS fetches before forcing output

# Phase 2 – Idea generation constants
IDEAS_MAX_ITERATIONS = 10
IDEAS_FORCE_OUTPUT_AFTER = 5

# Additional RSS feeds for Claude to fetch in synthesis phase
RSS_FEEDS_FOR_SYNTHESIS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("t3n", "https://t3n.de/rss.xml"),
    ("Heise Online", "https://www.heise.de/rss/heise-atom.xml"),
    ("Eric Hartford Blog", "https://erichartford.com/rss.xml"),
    ("NYT Technology", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml"),
    ("Sifted EU", "https://sifted.eu/feed?post_type=article"),
    ("Crunchbase News", "https://news.crunchbase.com/feed/"),
    ("Bloomberg Technology", "https://feeds.bloomberg.com/technology/news.rss"),
    ("Business Insider", "https://feeds.businessinsider.com/custom/all"),
    ("Reddit r/artificial", "https://www.reddit.com/r/artificial/.rss"),
]


def load_input(arg: str) -> dict:
    """Load input JSON from file path or inline JSON string."""
    arg = arg.strip()
    if arg.startswith("/") or arg.startswith("./"):
        path = Path(arg)
        if not path.exists():
            raise FileNotFoundError(f"Input file not found: {arg}")
        return json.loads(path.read_text())
    else:
        return json.loads(arg)


def build_synthesis_message(data: dict) -> str:
    """Build user message for Phase 1: includes pre-fetched sources + list of feeds to fetch."""
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime.now(berlin)
    today = now.strftime("%A, %d. %B %Y, %H:%M Uhr")

    lines = [
        f"Heute ist {today}. Analysiere alle News-Quellen und sammle Stories der letzten 48 Stunden.\n"
    ]

    # Pre-fetched sources from n8n
    email_content = data.get("email_content", "").strip()
    email_subject = data.get("email_subject", "").strip()
    if email_content:
        lines.append(f"## Bereits vorhanden – Newsletter: {email_subject or 'Startup Insider Daily'}")
        lines.append(email_content[:4000])
        lines.append("")

    rss_openai = data.get("rss_openai", [])
    if rss_openai:
        lines.append("## Bereits vorhanden – OpenAI Blog")
        for item in rss_openai[:6]:
            date_str = f" ({item['pubDate']})" if item.get("pubDate") else ""
            lines.append(f"- [{item.get('title', '')}]({item.get('link', '')}){date_str}")
        lines.append("")

    rss_anthropic = data.get("rss_anthropic", [])
    if rss_anthropic:
        lines.append("## Bereits vorhanden – Anthropic News")
        for item in rss_anthropic[:6]:
            date_str = f" ({item['pubDate']})" if item.get("pubDate") else ""
            lines.append(f"- [{item.get('title', '')}]({item.get('link', '')}){date_str}")
        lines.append("")

    # Additional feeds for Claude to fetch
    lines.append("## Diese RSS-Feeds musst du jetzt fetchen (via fetch_rss Tool):")
    for name, url in RSS_FEEDS_FOR_SYNTHESIS:
        lines.append(f"- {name}: {url}")
    lines.append("")
    lines.append(
        "Fetche ALLE Feeds in der Liste oben. Dann filtere auf letzte 48h, "
        "cluster gleiche Stories zu einem Eintrag, und gib das JSON-Array zurück."
    )

    return "\n".join(lines)


def build_ideas_message(topics: list) -> str:
    """Build user message for Phase 2: formatted deduplicated topic list."""
    berlin = ZoneInfo("Europe/Berlin")
    today = datetime.now(berlin).strftime("%A, %d. %B %Y")

    lines = [
        f"Heute ist {today}. Hier sind die aktuellen, deduplizierten News-Topics der letzten 48h:\n",
        "## Aktuelle Topics (bereits aus 15+ Quellen gefiltert und dedupliziert)\n"
    ]

    for topic in topics:
        age = topic.get("age_hours", "?")
        age_str = f"vor {age}h" if isinstance(age, (int, float)) else str(age)
        sources = ", ".join(topic.get("sources", []))
        lines.append(f"### {topic.get('topic_id', '')}. {topic.get('title', '')}")
        lines.append(f"**Alter:** {age_str} | **Quellen:** {sources}")
        lines.append(f"**URL:** {topic.get('primary_url', '')}")
        lines.append(f"**Zusammenfassung:** {topic.get('summary', '')}")
        lines.append("")

    lines.append(
        "Nutze fetch_article um interessante Artikel vollständig zu lesen "
        "und web_search für deutschen Kontext oder aktuelle Reaktionen.\n"
        "Wähle die 10 besten Topics aus und erstelle für jeden eine Post-Idee. "
        "Denke immer: Was ist der autofyn-Winkel? Nicht reporten – Standpunkt nehmen."
    )

    return "\n".join(lines)


def parse_json_array_from_response(response) -> list:
    """Extract a JSON array from Claude's response text."""
    full_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text

    # First: look for ```json ... ``` fences
    fence_match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", full_text)
    if fence_match:
        return json.loads(fence_match.group(1))

    # Second: find the outermost [ ... ] array
    array_match = re.search(r"(\[[\s\S]*\])", full_text)
    if array_match:
        return json.loads(array_match.group(1))

    raise ValueError(
        f"Could not parse JSON array from response. Raw text (first 500 chars):\n{full_text[:500]}"
    )


def _create_with_retry(client, kwargs: dict, max_retries: int = 3):
    """Call client.messages.create with exponential backoff on 529 Overloaded."""
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < max_retries - 1:
                wait = 20 * (2 ** attempt)  # 20, 40s – max 60s total wait
                logger.warning(
                    f"Anthropic API overloaded, retrying in {wait}s (attempt {attempt+1}/{max_retries})"
                )
                time.sleep(wait)
            else:
                raise


def _run_agentic_loop(
    client,
    system_prompt: str,
    user_message: str,
    tools: list,
    max_iterations: int,
    force_output_after: int,
    phase_name: str,
    nudge_message: str,
) -> list:
    """Generic agentic loop. Returns parsed JSON array from Claude's final output."""
    messages = [{"role": "user", "content": user_message}]
    tool_call_count = 0
    nudge_sent = False

    for iteration in range(1, max_iterations + 1):
        logger.info(f"[{phase_name}] Iteration {iteration}/{max_iterations}")

        force_output = tool_call_count >= force_output_after

        if force_output and not nudge_sent:
            logger.info(f"[{phase_name}] Forcing output after {tool_call_count} tool calls")
            messages.append({"role": "user", "content": nudge_message})
            nudge_sent = True

        create_kwargs = dict(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
        )
        if not force_output:
            create_kwargs["tools"] = tools

        response = _create_with_retry(client, create_kwargs)

        logger.info(
            f"[{phase_name}] stop_reason={response.stop_reason}, "
            f"tokens={response.usage.input_tokens}/{response.usage.output_tokens}"
        )

        if response.stop_reason == "end_turn":
            result = parse_json_array_from_response(response)
            logger.info(f"[{phase_name}] Got {len(result)} items")
            return result

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"[{phase_name}] Tool: {block.name}({json.dumps(block.input)[:100]})")
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                    tool_call_count += 1

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        logger.warning(f"[{phase_name}] Unexpected stop reason: {response.stop_reason}")
        break

    raise RuntimeError(f"[{phase_name}] No output after {max_iterations} iterations")


def run_synthesis(client, input_data: dict) -> list:
    """Phase 1: Fetch all RSS feeds, filter to 48h, deduplicate topics."""
    synthesis_tools = [t for t in TOOL_DEFINITIONS if t["name"] == "fetch_rss"]
    user_message = build_synthesis_message(input_data)

    logger.info("=== Phase 1: Synthesis ===")
    return _run_agentic_loop(
        client=client,
        system_prompt=SYNTHESIS_SYSTEM_PROMPT,
        user_message=user_message,
        tools=synthesis_tools,
        max_iterations=SYNTHESIS_MAX_ITERATIONS,
        force_output_after=SYNTHESIS_FORCE_OUTPUT_AFTER,
        phase_name="Synthesis",
        nudge_message=(
            "Du hast genug Feeds gefetcht. Erstelle jetzt die deduplizierte Topic-Liste "
            "als JSON-Array. Nur das Array, kein Text drumherum."
        ),
    )


def run_idea_generation(client, topics: list) -> list:
    """Phase 2: Generate 10 LinkedIn ideas from deduplicated topics."""
    # fetch_article and web_search – topics already fetched, no need for fetch_rss
    idea_tools = [t for t in TOOL_DEFINITIONS if t["name"] in ("fetch_article", "web_search")]
    user_message = build_ideas_message(topics)

    logger.info("=== Phase 2: Idea Generation ===")
    return _run_agentic_loop(
        client=client,
        system_prompt=IDEA_GENERATION_SYSTEM_PROMPT,
        user_message=user_message,
        tools=idea_tools,
        max_iterations=IDEAS_MAX_ITERATIONS,
        force_output_after=IDEAS_FORCE_OUTPUT_AFTER,
        phase_name="Ideas",
        nudge_message=(
            "Du hast genug recherchiert. Erstelle jetzt die 10 LinkedIn-Post-Ideen "
            "als JSON-Array. Nur das Array, kein Text drumherum."
        ),
    )


def run_agent(input_data: dict) -> list:
    """Two-phase pipeline: synthesis → idea generation."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Phase 1: Collect, filter, deduplicate
    topics = run_synthesis(client, input_data)

    # Phase 2: Generate 10 ideas with autofyn angle
    ideas = run_idea_generation(client, topics)

    return ideas


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "message": "Usage: main.py <input_json_or_path>"}))
        sys.exit(1)

    try:
        input_data = load_input(sys.argv[1])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(json.dumps({"status": "error", "message": f"Input error: {e}"}))
        sys.exit(1)

    try:
        berlin = ZoneInfo("Europe/Berlin")
        ideas = run_agent(input_data)
        result = {
            "status": "success",
            "ideas": ideas,
            "generated_at": datetime.now(berlin).isoformat(),
            "model": MODEL,
        }
        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        logger.exception("Agent failed")
        print(json.dumps({
            "status": "error",
            "message": str(e),
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
