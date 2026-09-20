"""Broadcasts today's final recommendation (ai-analysis-final:{date}) to
every follower of a LINE Official Account via the Messaging API's Broadcast
endpoint:
https://developers.line.biz/en/reference/messaging-api/#send-broadcast-message

This is a true broadcast — every current follower gets it, there's no
per-message targeting — and it counts against the channel's monthly message
quota. Chained automatically at the end of a successful main_final.py run
(see main_final.main()); a failure here is logged but doesn't fail that run,
since the actual analysis was already safely written to Redis by that point.

Requires LINE_CHANNEL_ACCESS_TOKEN — a channel access token issued from a
Messaging API channel in the LINE Developers Console (not a LINE Login
channel; those tokens don't work here).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import requests

from .logging_setup import get_logger

log = get_logger("line_broadcast")

BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"

# LINE Flex Message hard limits — altText is truncated to fit, not just
# guarded, since going over it is a hard API rejection, not a warning.
_MAX_ALT_TEXT_CHARS = 400
_MAX_REASON_CHARS = 300  # keeps a ticker card readable without scrolling on a phone
_MAX_SUMMARY_CHARS = 320  # summary card is "mega"-sized, a bit more room than a ticker card

_STANCE_COLORS = {"favor": "#1DB446", "caution": "#E67E22"}
_SUMMARY_HEADER_COLOR = "#2C3E50"


class LineBroadcastError(RuntimeError):
    pass


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _truncate_at_sentence(text: str, limit: int) -> str:
    """Like _truncate, but prefers cutting at the end of a sentence within
    the limit rather than mid-word — used for the summary card, which reads
    as a standalone blurb rather than a reason clause, so an abrupt mid-word
    cutoff looks worse than it does on the shorter ticker cards."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut > limit * 0.4:  # don't cut so early it loses most of the content
        return window[: cut + 1].rstrip()
    return _truncate(text, limit)


def _summary_bubble(today: str, summary: str) -> Dict[str, Any]:
    return {
        "type": "bubble",
        "size": "mega",  # every bubble in the carousel must share this — see _ticker_bubble
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": _SUMMARY_HEADER_COLOR,
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "Final Recommendation", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                {"type": "text", "text": today, "color": "#BDC3C7", "size": "xs"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": _truncate_at_sentence(summary, _MAX_SUMMARY_CHARS) if summary else "(no summary)",
                    "wrap": True,
                    "size": "sm",
                },
            ],
        },
    }


def _ticker_bubble(rec: Dict[str, str]) -> Dict[str, Any]:
    color = _STANCE_COLORS.get(rec.get("stance", ""), "#7F8C8D")
    return {
        "type": "bubble",
        "size": "mega",  # LINE rejects a carousel with mixed bubble sizes — must match _summary_bubble's size
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": color,
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": rec.get("ticker", "?"), "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": rec.get("stance", "").upper(), "color": "#FFFFFF", "size": "sm"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": _truncate(rec.get("reason", ""), _MAX_REASON_CHARS), "wrap": True, "size": "xs"},
            ],
        },
    }


def build_flex_message(result: Dict[str, Any]) -> Dict[str, Any]:
    """Builds the LINE Flex Message payload — a carousel with one summary
    card followed by one card per ticker recommendation. Pure function, no
    network access, so it's fully unit-testable without a real token."""
    today = result.get("generatedAt", "")
    recommendations: List[Dict[str, str]] = result.get("recommendations", [])

    bubbles = [_summary_bubble(today, result.get("summary", ""))]
    bubbles += [_ticker_bubble(r) for r in recommendations]

    alt_text = f"Final recommendation {today}: " + ", ".join(
        f"{r.get('ticker', '?')} {r.get('stance', '?')}" for r in recommendations
    )

    return {
        "type": "flex",
        "altText": _truncate(alt_text, _MAX_ALT_TEXT_CHARS),
        "contents": {"type": "carousel", "contents": bubbles},
    }


def broadcast(result: Dict[str, Any]) -> None:
    """POSTs the built Flex Message to LINE's Broadcast endpoint. Raises
    LineBroadcastError on any failure (missing token, non-200 response,
    network error) — callers decide whether that's fatal."""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        raise LineBroadcastError("LINE_CHANNEL_ACCESS_TOKEN environment variable is not set")

    message = build_flex_message(result)
    try:
        res = requests.post(
            BROADCAST_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"messages": [message]},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise LineBroadcastError(f"LINE broadcast request failed: {exc}") from exc

    if res.status_code != 200:
        raise LineBroadcastError(f"LINE broadcast failed ({res.status_code}): {res.text}")
