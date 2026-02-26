#!/usr/bin/env python3
"""
LinkedIn Agent - Morning Idea Generator
Aufgerufen von n8n via Execute Command Node.

Usage:
    python3 main.py /path/to/input.json
    python3 main.py '{"email_content": "...", ...}'   # inline JSON

Input JSON:
    {
        "email_content": "Full newsletter text...",
        "email_subject": "Startup Insider Daily - ...",
        "rss_openai": [{"title": "...", "link": "...", "summary": "..."}],
        "rss_anthropic": [{"title": "...", "link": "...", "summary": "..."}]
    }

Output (stdout, JSON):
    {
        "status": "success",
        "ideas": [...10 idea objects...],
        "generated_at": "2026-02-25T09:00:00+01:00",
        "model": "claude-opus-4-6"
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

from prompts import IDEA_GENERATION_SYSTEM_PROMPT
from tools import TOOL_DEFINITIONS, execute_tool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr  # logs go to stderr, output goes to stdout
)
logger = logging.getLogger("linkedin_agent")

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 10
FORCE_OUTPUT_AFTER = 6  # Inject "jetzt ausgeben" nudge after this many tool-call iterations


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


def build_user_message(data: dict) -> str:
    """Format the input data into a clear prompt for Claude."""
    berlin = ZoneInfo("Europe/Berlin")
    today = datetime.now(berlin).strftime("%A, %d. %B %Y")

    lines = [f"Heute ist {today}. Hier sind die Content-Quellen für heute:\n"]

    # Email Newsletter
    email_content = data.get("email_content", "").strip()
    email_subject = data.get("email_subject", "").strip()
    if email_content:
        lines.append(f"## Newsletter: {email_subject or 'Startup Insider Daily'}")
        lines.append(email_content[:4000])  # Cap at 4k chars
        lines.append("")

    # OpenAI RSS
    rss_openai = data.get("rss_openai", [])
    if rss_openai:
        lines.append("## OpenAI Blog (neueste Artikel)")
        for item in rss_openai[:6]:
            lines.append(f"- [{item.get('title', '')}]({item.get('link', '')})")
            if item.get("summary"):
                lines.append(f"  {item['summary'][:200]}")
        lines.append("")

    # Anthropic RSS
    rss_anthropic = data.get("rss_anthropic", [])
    if rss_anthropic:
        lines.append("## Anthropic Blog (neueste Artikel)")
        for item in rss_anthropic[:6]:
            lines.append(f"- [{item.get('title', '')}]({item.get('link', '')})")
            if item.get("summary"):
                lines.append(f"  {item['summary'][:200]}")
        lines.append("")

    lines.append(
        "Analysiere die Quellen. Nutze fetch_article um interessante Artikel vollständig zu lesen "
        "und web_search für deutschen Kontext oder aktuelle Reaktionen.\n"
        "Erstelle dann genau 10 LinkedIn-Post-Ideen als JSON-Array. "
        "Denke immer: Was ist der autofyn-Winkel? Nicht reporten – Standpunkt nehmen."
    )

    return "\n".join(lines)


def parse_ideas_from_response(response) -> list:
    """Extract the JSON array from Claude's response."""
    # Collect all text blocks
    full_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text

    # Try to extract JSON array
    # First: look for ```json ... ``` fences
    fence_match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", full_text)
    if fence_match:
        return json.loads(fence_match.group(1))

    # Second: find the outermost [ ... ] array
    array_match = re.search(r"(\[[\s\S]*\])", full_text)
    if array_match:
        return json.loads(array_match.group(1))

    raise ValueError(f"Could not parse JSON array from response. Raw text (first 500 chars):\n{full_text[:500]}")


def _create_with_retry(client, kwargs: dict, max_retries: int = 3):
    """Call client.messages.create with exponential backoff on 529 Overloaded."""
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < max_retries - 1:
                wait = 20 * (2 ** attempt)  # 20, 40s – max 60s total wait
                logger.warning(f"Anthropic API overloaded, retrying in {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                raise


def run_agent(input_data: dict) -> dict:
    """Main agentic loop: generate 10 LinkedIn post ideas."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_message = build_user_message(input_data)
    messages = [{"role": "user", "content": user_message}]

    logger.info(f"Starting idea generation agent ({MODEL})")
    tool_call_count = 0
    nudge_sent = False

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info(f"Iteration {iteration}/{MAX_ITERATIONS}")

        force_output = tool_call_count >= FORCE_OUTPUT_AFTER

        if force_output and not nudge_sent:
            logger.info("Forcing output: injecting nudge message, removing tools")
            messages.append({
                "role": "user",
                "content": (
                    "Du hast genug recherchiert. Erstelle jetzt die 10 LinkedIn-Post-Ideen "
                    "als JSON-Array. Nur das Array, kein Text drumherum."
                )
            })
            nudge_sent = True

        # Omit tools entirely when forcing output (empty list causes API issues)
        create_kwargs = dict(
            model=MODEL,
            max_tokens=4096,
            system=IDEA_GENERATION_SYSTEM_PROMPT,
            messages=messages
        )
        if not force_output:
            create_kwargs["tools"] = TOOL_DEFINITIONS

        response = _create_with_retry(client, create_kwargs)

        logger.info(f"Stop reason: {response.stop_reason}, "
                    f"Input tokens: {response.usage.input_tokens}, "
                    f"Output tokens: {response.usage.output_tokens}")

        if response.stop_reason == "end_turn":
            ideas = parse_ideas_from_response(response)
            logger.info(f"Generated {len(ideas)} ideas successfully")
            return ideas

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(f"Tool call: {block.name}({json.dumps(block.input)[:100]})")
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
                    tool_call_count += 1

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        logger.warning(f"Unexpected stop reason: {response.stop_reason}")
        break

    raise RuntimeError(f"Agent did not produce output after {MAX_ITERATIONS} iterations")


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
            "model": MODEL
        }
        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        logger.exception("Agent failed")
        print(json.dumps({
            "status": "error",
            "message": str(e)
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
