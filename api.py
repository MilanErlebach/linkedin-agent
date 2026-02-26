"""
FastAPI Wrapper für den LinkedIn Agent.
Läuft als Docker-Container im n8n-Netzwerk.

Endpoints sind ASYNC: sie starten die Arbeit im Hintergrund und antworten
sofort mit 202 Accepted. Das Ergebnis wird direkt via Slack Bot Token gepostet.
"""

import os
import sys
import logging
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, Request, HTTPException

sys.path.insert(0, str(Path(__file__).parent / "agent"))

from main import run_agent
from post_generator import run_agent as run_post_agent
from slack_formatter import post_ideas_to_slack, post_result_to_slack, post_error_to_slack

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("linkedin_api")

app = FastAPI(title="autofyn LinkedIn Agent API", version="2.0.0")

API_SECRET = os.environ.get("AGENT_API_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")


def _check_auth(request: Request) -> None:
    if API_SECRET and request.headers.get("X-Api-Secret", "") != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Background Tasks ──────────────────────────────────────────────────────────

def _bg_generate_ideas(payload: dict) -> None:
    try:
        ideas = run_agent(payload)
        post_ideas_to_slack(ideas, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID)
    except Exception as exc:
        logger.exception("Background idea generation failed")
        post_error_to_slack(
            f"Ideen-Generierung fehlgeschlagen: {exc}",
            SLACK_BOT_TOKEN,
            SLACK_CHANNEL_ID,
        )


def _bg_generate_post(idea: dict, response_url: str, channel_id: str) -> None:
    channel = channel_id or SLACK_CHANNEL_ID
    try:
        post_text = run_post_agent(idea)
        post_result_to_slack(post_text, idea, response_url, SLACK_BOT_TOKEN, channel)
    except Exception as exc:
        logger.exception("Background post generation failed")
        error_msg = f"Post-Generierung fehlgeschlagen für '{idea.get('title', '?')}': {exc}"
        if response_url:
            import requests as req
            try:
                req.post(response_url, json={"text": f"❌ {error_msg}"}, timeout=10)
                return
            except Exception:
                pass
        post_error_to_slack(error_msg, SLACK_BOT_TOKEN, channel)


# ─── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate-ideas")
async def generate_ideas(request: Request, background_tasks: BackgroundTasks):
    """
    Starts idea generation in the background.
    Returns 202 immediately; posts 10 ideas to Slack when done.
    """
    _check_auth(request)
    payload = await request.json()
    logger.info(f"generate-ideas accepted | subject='{payload.get('email_subject', '')}'")
    background_tasks.add_task(_bg_generate_ideas, payload)
    return {"status": "accepted"}


@app.post("/generate-post")
async def generate_post(request: Request, background_tasks: BackgroundTasks):
    """
    Starts post generation in the background.
    Returns 202 immediately; posts finished post to Slack when done.

    Body: {idea: {...}, response_url: "https://...", channel_id: "C..."}
    """
    _check_auth(request)
    body = await request.json()
    idea = body.get("idea", {})
    if not idea:
        raise HTTPException(status_code=400, detail="Missing 'idea' in request body")

    response_url = body.get("response_url", "")
    channel_id = body.get("channel_id", "")

    logger.info(f"generate-post accepted | idea='{idea.get('title', '?')}'")
    background_tasks.add_task(_bg_generate_post, idea, response_url, channel_id)
    return {"status": "accepted"}
