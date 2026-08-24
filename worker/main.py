"""Standalone replacement for stock-dashboard's GET /api/ai-analysis.

Fetches the same Google Sheet, runs the same analytics, builds the same
prompt, but generates the answer via the locally-authenticated `claude` CLI
(subscription) instead of the billed Anthropic API, then writes the result
into the same Redis cache key the Next.js frontend already reads from.

Intended to run once per invocation (no-ops if today's key is already
cached) — schedule it externally (cron, Task Scheduler, etc.) to run daily
shortly after the sheet updates.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from . import analytics as A
from . import redis_client as R
from .claude_cli import ClaudeCLIError, get_analysis
from .logging_setup import get_logger
from .prompt import build_prompt

load_dotenv()

SHEET_NAME = "history"
log = get_logger("main")


def fetch_sheet_csv(sheet_id: str) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={SHEET_NAME}"
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    return res.text


def generate_analysis(all_data: A.AllData, today: str) -> tuple[dict, str]:
    prompt = build_prompt(all_data, today)
    analysis, usage = get_analysis(prompt)
    result = {
        **analysis,
        "model": os.environ.get("CLAUDE_MODEL_LABEL", "claude (subscription CLI)"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "tokenUsage": usage,
    }
    return result, prompt


def main() -> int:
    start = time.monotonic()
    now = datetime.now(timezone.utc)
    log.info("Run started")

    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        log.error("SHEET_ID environment variable is not set")
        return 1

    today = now.strftime("%Y-%m-%d")
    redis_conn = R.get_client()
    key = R.cache_key(today)

    if redis_conn is not None:
        try:
            if redis_conn.get(key):
                log.info("Analysis for %s already cached — skipping Claude call.", today)
                return 0
        except Exception:
            log.warning("Redis unreachable, proceeding without cache check", exc_info=True)

    csv_text = fetch_sheet_csv(sheet_id)
    all_data = A.parse_csv(csv_text)
    if not all_data:
        log.error("No data parsed from sheet")
        return 1
    log.info("Parsed %d tickers from sheet", len(all_data))

    try:
        result, prompt = generate_analysis(all_data, today)
    except ClaudeCLIError:
        log.error("Claude CLI error", exc_info=True)
        return 1

    usage = result.get("tokenUsage", {})
    log.info(
        "Generated analysis for %s — topPicks=%s riskWatch=%s tokens(in=%s out=%s "
        "thinking=%s) cost=$%.4f (subscription usage, not billed) duration=%.1fs",
        today,
        [p["ticker"] for p in result.get("topPicks", [])],
        [p["ticker"] for p in result.get("riskWatch", [])],
        usage.get("inputTokens"), usage.get("outputTokens"), usage.get("thinkingTokens"),
        usage.get("totalCostUsd", 0.0), time.monotonic() - start,
    )

    if redis_conn is not None:
        try:
            redis_conn.set(key, json.dumps(result), ex=R.CACHE_TTL_SECONDS)
            log.info("Wrote analysis for %s to Redis key '%s'.", today, key)
        except Exception:
            log.error("Failed to write to Redis", exc_info=True)
            return 1

        prompt_key = R.prompt_cache_key(today)
        try:
            redis_conn.set(prompt_key, prompt, ex=R.CACHE_TTL_SECONDS)
            log.info("Wrote prompt for %s to Redis key '%s'.", today, prompt_key)
        except Exception:
            log.warning("Failed to write prompt to Redis", exc_info=True)
    else:
        log.warning("REDIS_URL not set — analysis generated but not cached")
        print(json.dumps(result, indent=2))
        print(f"\nPrompt used:\n{prompt}")

    log.info("Run finished in %.1fs", time.monotonic() - start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
