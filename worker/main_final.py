"""Final synthesis step — combines today's cached ai-analysis:{date} (quant)
and ai-analysis-news:{date} (news) into one final recommendation set per
ticker. Requires both to already exist for today.

Not scheduled on its own — worker.main_news calls this automatically at the
end of its own successful run (see main_news.main()), since a final synthesis
only makes sense immediately after a fresh news check. Can still be run
standalone for testing:
    python -m worker.main_final
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from . import redis_client as R
from .claude_cli import ClaudeCLIError, get_final_recommendations
from .logging_setup import get_logger
from .prompt_final import build_final_prompt

load_dotenv()

log = get_logger("main_final")


def main() -> int:
    start = time.monotonic()
    now = datetime.now(timezone.utc)
    log.info("Run started")

    today = now.strftime("%Y-%m-%d")
    redis_conn = R.get_client()
    if redis_conn is None:
        log.error("REDIS_URL is not set — this step needs Redis to read today's analyses")
        return 1

    final_key = R.final_cache_key(today)
    try:
        if redis_conn.get(final_key):
            log.info("Final recommendations for %s already cached — skipping Claude call.", today)
            return 0
    except Exception:
        log.warning("Redis unreachable, proceeding without cache check", exc_info=True)

    analysis_raw = redis_conn.get(R.cache_key(today))
    if not analysis_raw:
        log.error(
            "No cached analysis at '%s' — run `python -m worker.main` for today first.",
            R.cache_key(today),
        )
        return 1
    analysis = json.loads(analysis_raw)

    news_raw = redis_conn.get(R.news_cache_key(today))
    if not news_raw:
        log.error(
            "No cached news at '%s' — run `python -m worker.main_news` for today first.",
            R.news_cache_key(today),
        )
        return 1
    news = json.loads(news_raw)

    prompt = build_final_prompt(analysis, news, today)
    try:
        final, usage = get_final_recommendations(prompt)
    except ClaudeCLIError:
        log.error("Claude CLI error", exc_info=True)
        return 1

    result = {
        **final,
        "model": os.environ.get("CLAUDE_MODEL_LABEL", "claude (subscription CLI)"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "tokenUsage": usage,
    }
    log.info(
        "Final recommendations for %s: %s tokens(in=%s out=%s thinking=%s) "
        "cost=$%.4f (subscription usage, not billed) duration=%.1fs",
        today,
        [(r["ticker"], r["stance"]) for r in result.get("recommendations", [])],
        usage.get("inputTokens"), usage.get("outputTokens"), usage.get("thinkingTokens"),
        usage.get("totalCostUsd", 0.0), time.monotonic() - start,
    )

    try:
        redis_conn.set(final_key, json.dumps(result), ex=R.CACHE_TTL_SECONDS)
        log.info("Wrote final recommendations for %s to Redis key '%s'.", today, final_key)
    except Exception:
        log.error("Failed to write to Redis", exc_info=True)
        return 1

    prompt_key = R.final_prompt_cache_key(today)
    try:
        redis_conn.set(prompt_key, prompt, ex=R.CACHE_TTL_SECONDS)
        log.info("Wrote final prompt for %s to Redis key '%s'.", today, prompt_key)
    except Exception:
        log.warning("Failed to write final prompt to Redis", exc_info=True)

    log.info("Run finished in %.1fs", time.monotonic() - start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
