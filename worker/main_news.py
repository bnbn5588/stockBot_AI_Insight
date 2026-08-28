"""News-only follow-on step (prompt_news.py + claude_cli.get_news_highlights).
Does NOT re-derive marketSummary/topPicks/riskWatch/portfolioNote — it reads
those from today's already-cached ai-analysis:{date} (main.py's output) and
searches news only for the tickers that analysis already flagged (topPicks +
riskWatch + any signal change not already covered). Writes newsHighlights to
its own ai-analysis-news:{date} key, separate from the key the frontend
reads.

Requires main.py to have already run for today — this is a follow-on step,
not an independent analysis. Unlike main.py, there's no cache-skip: every
invocation regenerates and overwrites ai-analysis-news:{date}, so this can be
run — and scheduled — more than once a day for fresher news. Run manually:
    python -m worker.main_news

At the end of a successful run, this calls main_final.main() directly —
final synthesis (see main_final.py) only makes sense immediately after a
fresh news check, so it's chained here rather than given its own schedule;
whatever cadence this runs at, the final recommendation refreshes with it.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List

from dotenv import load_dotenv

from . import analytics as A
from . import redis_client as R
from .claude_cli import ClaudeCLIError, get_news_highlights
from .logging_setup import get_logger
from .main import fetch_sheet_csv
from .main_final import main as run_final_synthesis
from .prompt_news import NewsCandidate, build_news_only_prompt

load_dotenv()

log = get_logger("main_news")


def _collect_candidates(analysis: dict, all_data: A.AllData) -> List[NewsCandidate]:
    candidates: Dict[str, NewsCandidate] = {}

    for item in analysis.get("topPicks", []):
        candidates[item["ticker"]] = {
            "ticker": item["ticker"], "signal": "BUY", "reason": item["reason"],
        }
    for item in analysis.get("riskWatch", []):
        # riskWatch can include contradiction flags on either signal; the
        # per-ticker current signal is more informative than assuming SELL.
        rows = all_data.get(item["ticker"])
        signal = rows[-1].signal if rows else "?"
        candidates[item["ticker"]] = {
            "ticker": item["ticker"], "signal": signal, "reason": item["reason"],
        }

    for ticker, rows in all_data.items():
        if ticker in candidates or len(rows) < 2:
            continue
        last, prev = rows[-1], rows[-2]
        if last.signal != prev.signal:
            candidates[ticker] = {
                "ticker": ticker, "signal": last.signal,
                "reason": f"Signal changed {prev.signal}→{last.signal} today.",
            }

    return list(candidates.values())


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
    if redis_conn is None:
        log.error("REDIS_URL is not set — this step needs Redis to read today's analysis")
        return 1

    news_key = R.news_cache_key(today)
    # No cache-skip here, unlike main.py — this step is meant to be run more
    # than once a day for fresher news; each run always regenerates and
    # overwrites news_key (and the TTL resets with it).

    analysis_raw = redis_conn.get(R.cache_key(today))
    if not analysis_raw:
        log.error(
            "No cached analysis at '%s' — run `python -m worker.main` for today first; "
            "this step depends on its topPicks/riskWatch output.",
            R.cache_key(today),
        )
        return 1
    analysis = json.loads(analysis_raw)

    csv_text = fetch_sheet_csv(sheet_id)
    all_data = A.parse_csv(csv_text)
    if not all_data:
        log.error("No data parsed from sheet")
        return 1

    candidates = _collect_candidates(analysis, all_data)
    log.info("Collected %d news candidates: %s", len(candidates), [c["ticker"] for c in candidates])
    prompt = None
    if not candidates:
        log.info("No topPicks/riskWatch/signal-change tickers to search news for today.")
        result = {"generatedAt": today, "newsHighlights": []}
    else:
        lookback_days = int(os.environ.get("NEWS_LOOKBACK_DAYS", "2"))
        prompt = build_news_only_prompt(candidates, today, lookback_days=lookback_days)
        try:
            news, usage = get_news_highlights(prompt)
        except ClaudeCLIError:
            log.error("Claude CLI error", exc_info=True)
            return 1
        result = {
            **news,
            "model": os.environ.get("CLAUDE_MODEL_LABEL", "claude (subscription CLI)"),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "tokenUsage": usage,
        }
        log.info(
            "Found %d news highlights: %s tokens(in=%s out=%s thinking=%s) "
            "cost=$%.4f (subscription usage, not billed) duration=%.1fs",
            len(result.get("newsHighlights", [])),
            [h["ticker"] for h in result.get("newsHighlights", [])],
            usage.get("inputTokens"), usage.get("outputTokens"), usage.get("thinkingTokens"),
            usage.get("totalCostUsd", 0.0), time.monotonic() - start,
        )

    try:
        redis_conn.set(news_key, json.dumps(result), ex=R.CACHE_TTL_SECONDS)
        log.info("Wrote news highlights for %s to Redis key '%s'.", today, news_key)
    except Exception:
        log.error("Failed to write to Redis", exc_info=True)
        return 1

    if prompt is not None:
        prompt_key = R.news_prompt_cache_key(today)
        try:
            redis_conn.set(prompt_key, prompt, ex=R.CACHE_TTL_SECONDS)
            log.info("Wrote news prompt for %s to Redis key '%s'.", today, prompt_key)
        except Exception:
            log.warning("Failed to write news prompt to Redis", exc_info=True)

    log.info("Run finished in %.1fs", time.monotonic() - start)

    log.info("Chaining into final synthesis (worker.main_final)")
    return run_final_synthesis()


if __name__ == "__main__":
    raise SystemExit(main())
