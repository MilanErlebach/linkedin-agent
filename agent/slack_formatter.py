"""
Slack posting helpers for the LinkedIn Agent.
Posts ideas and finished posts directly to Slack via Bot Token.
"""

import json
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("slack_formatter")

TONE_EMOJI = {"direkt": "🎯", "ironisch": "😏", "pragmatisch": "🔧", "thought_leader": "💡"}
FORMAT_EMOJI = {
    "story": "📖",
    "erklärer": "📚",
    "hot_take": "🔥",
    "zahlen_analyse": "📊",
    "mini_framework": "🔧",
}
SOURCE_LABELS = {
    "rss_openai": "OpenAI Blog",
    "rss_anthropic": "Anthropic News",
    "email_podcast": "Startup Insider",
    "web_research": "Web-Recherche",
}


def post_ideas_to_slack(ideas: list, token: str, channel: str) -> None:
    berlin = ZoneInfo("Europe/Berlin")
    today = datetime.now(berlin).strftime("%A, %d. %B %Y")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🟣 LinkedIn Post Ideen – {today}", "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{len(ideas)} Ideen · Klicke *Ausarbeiten* für einen vollständigen Post",
                }
            ],
        },
        {"type": "divider"},
    ]

    for idea in ideas:
        tone_emoji = TONE_EMOJI.get(idea.get("estimated_tone", ""), "🟣")
        fmt_emoji = FORMAT_EMOJI.get(idea.get("post_format", ""), "")
        source_label = SOURCE_LABELS.get(idea.get("source", ""), idea.get("source", ""))
        source_url = idea.get("source_url", "")
        source_text = f"📌 {source_label}"
        if source_url:
            source_text += f" · <{source_url}|Link>"

        idea_id = idea.get("id", 0)
        fmt_label = idea.get("post_format", "")
        prefix = f"{fmt_emoji} {tone_emoji}" if fmt_emoji else tone_emoji
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{prefix} *{idea_id}. {idea.get('title', '')}*"
                        + (f"  `{fmt_label}`" if fmt_label else "") + "\n"
                        f"> {idea.get('hook', '')}\n"
                        f"_{idea.get('angle', '')}_\n"
                        f"{source_text}"
                    ),
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Ausarbeiten ✍️", "emoji": True},
                    "value": json.dumps({"idea_id": idea_id, "idea": idea}),
                    "action_id": f"ausarbeiten_{idea_id}",
                    "style": "primary",
                },
            }
        )
        blocks.append({"type": "divider"})

    _post_blocks(blocks, token, channel)


def post_result_to_slack(
    post_text: str, idea: dict, response_url: str, token: str, channel: str
) -> None:
    word_count = len(post_text.split())
    idea_title = idea.get("title", "?")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "✍️ Dein LinkedIn Post ist fertig!", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Idee:* {idea_title} · {word_count} Wörter"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```\n{post_text}\n```"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "_Kopiere den Text oben und paste ihn direkt in LinkedIn_ 👆"}
            ],
        },
    ]

    # Prefer response_url (scoped to original message thread)
    if response_url:
        try:
            resp = requests.post(
                response_url,
                json={"blocks": blocks, "replace_original": False},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Post sent via response_url")
            return
        except Exception as exc:
            logger.warning(f"response_url failed ({exc}), falling back to Slack API")

    _post_blocks(blocks, token, channel)


def post_error_to_slack(message: str, token: str, channel: str) -> None:
    _post_blocks(
        [{"type": "section", "text": {"type": "mrkdwn", "text": f"❌ {message}"}}],
        token,
        channel,
    )


def _post_blocks(blocks: list, token: str, channel: str) -> None:
    if not token or not channel:
        logger.error("Cannot post to Slack: SLACK_BOT_TOKEN or SLACK_CHANNEL_ID missing")
        return

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": channel, "blocks": blocks},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        logger.error(f"Slack API error: {data.get('error', 'unknown')} | {data}")
    else:
        logger.info(f"Successfully posted to Slack channel {channel}")
