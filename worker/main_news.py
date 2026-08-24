"""Manual/experimental runner for the news-only step (prompt_news.py +
claude_cli.get_news_highlights). Does NOT re-derive marketSummary/topPicks/
riskWatch/portfolioNote — it reads those from today's already-cached
ai-analysis:{date} (main.py's output) and searches news only for the
tickers that analysis already flagged (topPicks + riskWatch + any signal
change not already covered). Writes newsHighlights to its own
ai-analysis-news:{date} key, separate from the key the frontend reads.

Requires main.py to have already run for today — this is a follow-on step,
not an independent analysis. Run manually:
    python -m worker.main_news
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

from dotenv import load_dotenv

from . import analytics as A
from . import redis_client as R
from .claude_cli import ClaudeCLIError, get_news_highlights
from .main import fetch_sheet_csv
from .prompt_news import NewsCandidate, build_news_only_prompt

load_dotenv()


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
    now = datetime.now(timezone.utc)

    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("SHEET_ID environment variable is not set", file=sys.stderr)
        return 1

    today = now.strftime("%Y-%m-%d")
    redis_conn = R.get_client()
    if redis_conn is None:
        print("REDIS_URL is not set — this step needs Redis to read today's analysis", file=sys.stderr)
        return 1

    news_key = R.news_cache_key(today)
    try:
        if redis_conn.get(news_key):
            print(f"News highlights for {today} already cached — skipping Claude call.")
            return 0
    except Exception as exc:
        print(f"Redis unreachable, proceeding without cache check: {exc}", file=sys.stderr)

    analysis_raw = redis_conn.get(R.cache_key(today))
    if not analysis_raw:
        print(
            f"No cached analysis at '{R.cache_key(today)}' — run `python -m worker.main` "
            "for today first; this step depends on its topPicks/riskWatch output.",
            file=sys.stderr,
        )
        return 1
    analysis = json.loads(analysis_raw)

    csv_text = fetch_sheet_csv(sheet_id)
    all_data = A.parse_csv(csv_text)
    if not all_data:
        print("No data parsed from sheet", file=sys.stderr)
        return 1

    candidates = _collect_candidates(analysis, all_data)
    prompt = None
    if not candidates:
        print("No topPicks/riskWatch/signal-change tickers to search news for today.")
        result = {"generatedAt": today, "newsHighlights": []}
    else:
        prompt = build_news_only_prompt(candidates, today)
        try:
            news, usage = get_news_highlights(prompt)
        except ClaudeCLIError as exc:
            print(f"Claude CLI error: {exc}", file=sys.stderr)
            return 1
        result = {
            **news,
            "model": os.environ.get("CLAUDE_MODEL_LABEL", "claude (subscription CLI)"),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "tokenUsage": usage,
        }

    try:
        redis_conn.set(news_key, json.dumps(result), ex=R.CACHE_TTL_SECONDS)
        print(f"Wrote news highlights for {today} to Redis key '{news_key}'.")
    except Exception as exc:
        print(f"Failed to write to Redis: {exc}", file=sys.stderr)
        return 1

    if prompt is not None:
        prompt_key = R.news_prompt_cache_key(today)
        try:
            redis_conn.set(prompt_key, prompt, ex=R.CACHE_TTL_SECONDS)
            print(f"Wrote news prompt for {today} to Redis key '{prompt_key}'.")
        except Exception as exc:
            print(f"Failed to write news prompt to Redis: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
