#!/usr/bin/env python3
"""
LinkedIn Post Generator
Aufgerufen wenn User in Slack auf "Ausarbeiten" klickt.

Usage:
    python3 post_generator.py /path/to/input.json
    python3 post_generator.py '{"idea": {...}}'

Input JSON:
    {
        "idea": {
            "id": 3,
            "title": "Warum dein ERP kein KI-Problem hat",
            "hook": "Dein ERP ist kein KI-Problem. Es ist ein Datenproblem.",
            "angle": "Unternehmen kaufen KI-Add-ons bevor ihre Daten sauber sind.",
            "source": "rss_anthropic",
            "source_url": "https://..."
        }
    }

Output (stdout, JSON):
    {
        "status": "success",
        "post": "Fertiger LinkedIn Post Text...",
        "idea_id": 3,
        "word_count": 187
    }
"""

import os
import sys
import json
import logging
import re
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from prompts import POST_GENERATION_SYSTEM_PROMPT
from tools import TOOL_DEFINITIONS, execute_tool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("post_generator")

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 6
FORCE_OUTPUT_AFTER = 3  # After 3 tool calls, force the post to be written


def load_input(arg: str) -> dict:
    arg = arg.strip()
    if arg.startswith("/") or arg.startswith("./"):
        path = Path(arg)
        if not path.exists():
            raise FileNotFoundError(f"Input file not found: {arg}")
        return json.loads(path.read_text())
    else:
        return json.loads(arg)


def build_user_message(idea: dict) -> str:
    lines = [
        "Schreibe einen vollständigen LinkedIn-Post basierend auf dieser Idee:\n",
        f"**Titel**: {idea.get('title', '')}",
        f"**Hook (erste Zeile)**: {idea.get('hook', '')}",
        f"**Winkel / Kernaussage**: {idea.get('angle', '')}",
    ]

    if idea.get("source_url"):
        lines.append(f"**Quell-URL**: {idea['source_url']}")
        lines.append(f"**Quelle**: {idea.get('source_title', idea.get('source', ''))}")
        lines.append(
            "\nNutze fetch_article um den Quell-Artikel zu lesen und konkrete Details "
            "(Zahlen, Zitate, spezifische Features) in den Post einzubauen. "
            "Dann schreibe den fertigen Post."
        )
    elif idea.get("source") in ("web_research",):
        lines.append(
            "\nNutze web_search um aktuelle Details zu diesem Thema zu finden. "
            "Dann schreibe den fertigen Post."
        )
    else:
        lines.append("\nSchreibe direkt den fertigen Post basierend auf dem Hook und dem Winkel.")

    return "\n".join(lines)


def extract_post_text(response) -> str:
    """Extract the post text from Claude's final response."""
    full_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text
    return full_text.strip()


def count_words(text: str) -> int:
    return len(text.split())


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


def run_agent(idea: dict) -> str:
    """Generate a full LinkedIn post for one idea."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_message = build_user_message(idea)
    messages = [{"role": "user", "content": user_message}]

    logger.info(f"Generating post for idea: {idea.get('title', '?')} ({MODEL})")
    tool_call_count = 0
    nudge_sent = False

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info(f"Iteration {iteration}/{MAX_ITERATIONS}")

        force_output = tool_call_count >= FORCE_OUTPUT_AFTER

        if force_output and not nudge_sent:
            logger.info("Forcing post output: injecting nudge, removing tools")
            messages.append({
                "role": "user",
                "content": (
                    "Du hast genug recherchiert. Schreibe jetzt den vollständigen LinkedIn-Post. "
                    "Nur den fertigen Post-Text, kein JSON, keine Erklärungen."
                )
            })
            nudge_sent = True

        create_kwargs = dict(
            model=MODEL,
            max_tokens=2048,
            system=POST_GENERATION_SYSTEM_PROMPT,
            messages=messages
        )
        if not force_output:
            create_kwargs["tools"] = TOOL_DEFINITIONS

        response = _create_with_retry(client, create_kwargs)

        logger.info(f"Stop reason: {response.stop_reason}, "
                    f"Tokens: {response.usage.input_tokens}in / {response.usage.output_tokens}out")

        if response.stop_reason == "end_turn":
            return extract_post_text(response)

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

        logger.warning(f"Unexpected stop reason: {response.stop_reason}")
        break

    raise RuntimeError(f"Post generator did not produce output after {MAX_ITERATIONS} iterations")


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "message": "Usage: post_generator.py <input_json_or_path>"}))
        sys.exit(1)

    try:
        input_data = load_input(sys.argv[1])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(json.dumps({"status": "error", "message": f"Input error: {e}"}))
        sys.exit(1)

    idea = input_data.get("idea", {})
    if not idea:
        print(json.dumps({"status": "error", "message": "Missing 'idea' in input"}))
        sys.exit(1)

    try:
        post_text = run_agent(idea)
        result = {
            "status": "success",
            "post": post_text,
            "idea_id": idea.get("id"),
            "idea_title": idea.get("title", ""),
            "word_count": count_words(post_text)
        }
        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        logger.exception("Post generation failed")
        print(json.dumps({
            "status": "error",
            "message": str(e)
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
