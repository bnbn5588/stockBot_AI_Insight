"""Final synthesis step — combines today's cached ai-analysis:{date} (quant)
and ai-analysis-news:{date} (news) into one final recommendation set per
ticker. Requires both to already exist for today.

Not scheduled on its own — worker.main_news calls this automatically at the
end of its own successful run (see main_news.main()), since a final synthesis
only makes sense immediately after a fresh news check. Can still be run
standalone for testing:
    python -m worker.main_final

No cache-skip, same as main_news.py: every invocation regenerates and
overwrites ai-analysis-final:{date}. main_news.py has no cache-skip
specifically so it can be run several times a day for fresher news — if this
step still skipped once a day, chaining it after main_news.py would mean the
final recommendation never actually reflected any of those later refreshes.

If LINE_CHANNEL_ACCESS_TOKEN is set, also broadcasts the result to every
follower of a LINE Official Account (see line_broadcast.py) — skipped
entirely if the token isn't configured, and a broadcast failure doesn't fail
this run. Note this means every regeneration broadcasts again: if
main_news.py (and therefore this step) is ever scheduled more than once a
day, followers get a fresh broadcast each time too.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from . import analytics as A
from . import archive
from . import quality as Q
from . import redis_client as R
from .claude_cli import ClaudeCLIError, final_json_schema, model_name, run_checked
from .line_broadcast import LineBroadcastError, broadcast, notify_failure
from .logging_setup import get_logger
from .main import fetch_sheet_csv
from .prompt_final import build_final_prompt, candidate_tickers, stances

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
        notify_failure("worker.main_final", "REDIS_URL is not set")
        return 1

    final_key = R.final_cache_key(today)
    # No cache-skip here, unlike a once-a-day step — see module docstring:
    # this always regenerates and overwrites final_key (resetting its TTL).

    analysis_raw = redis_conn.get(R.cache_key(today))
    if not analysis_raw:
        log.error(
            "No cached analysis at '%s' — run `python -m worker.main` for today first.",
            R.cache_key(today),
        )
        notify_failure(
            "worker.main_final",
            f"No cached analysis at '{R.cache_key(today)}' — worker.main must run "
            "successfully for today before this step can.",
        )
        return 1
    analysis = json.loads(analysis_raw)

    news_raw = redis_conn.get(R.news_cache_key(today))
    if not news_raw:
        log.error(
            "No cached news at '%s' — run `python -m worker.main_news` for today first.",
            R.news_cache_key(today),
        )
        notify_failure(
            "worker.main_final",
            f"No cached news at '{R.news_cache_key(today)}' — worker.main_news must run "
            "successfully for today before this step can.",
        )
        return 1
    news = json.loads(news_raw)

    tickers = candidate_tickers(analysis)
    if not tickers:
        log.info("No topPicks/riskWatch tickers to synthesize for today.")
        final, usage, prompt = {"generatedAt": today, "summary": "", "recommendations": []}, {}, None
        issues = []
    else:
        prompt = build_final_prompt(analysis, news, today)
        # Signals and earnings dates for the checks; without the sheet the
        # checks that need them are skipped rather than failing the run.
        try:
            all_data = A.parse_csv(fetch_sheet_csv(os.environ["SHEET_ID"]))
        except Exception:
            log.warning("Sheet unavailable — final checks run without signal data", exc_info=True)
            all_data = {}
        fixed = {t: s for t, (s, _) in stances(analysis, news).items()}
        try:
            final, usage, issues = run_checked(
                prompt, final_json_schema(len(tickers)),
                check=lambda r: Q.check_final(r, tickers, fixed, all_data, today),
            )
        except ClaudeCLIError as exc:
            log.error("Claude CLI error", exc_info=True)
            notify_failure("worker.main_final", f"Claude CLI error: {exc}")
            return 1
        # The rule decides the stance even if the model still disagrees.
        for r in final.get("recommendations", []):
            if r.get("ticker") in fixed:
                r["stance"] = fixed[r["ticker"]]

    result = {
        **final,
        "qualityIssues": issues,
        "model": model_name(),
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
    archive.write("final", today, {"prompt": prompt, "result": result})

    try:
        redis_conn.set(final_key, json.dumps(result), ex=R.CACHE_TTL_SECONDS)
        log.info("Wrote final recommendations for %s to Redis key '%s'.", today, final_key)
        # Same payload, no TTL — the forward-test record (worker.track). A
        # re-run on the same day overwrites it, so the archive holds the
        # day's last (= broadcast) stance.
        redis_conn.set(R.final_archive_key(today), json.dumps(result))
    except Exception:
        log.error("Failed to write to Redis", exc_info=True)
        notify_failure("worker.main_final", "Failed to write final recommendations to Redis")
        return 1

    if prompt is not None:
        prompt_key = R.final_prompt_cache_key(today)
        try:
            redis_conn.set(prompt_key, prompt, ex=R.CACHE_TTL_SECONDS)
            log.info("Wrote final prompt for %s to Redis key '%s'.", today, prompt_key)
        except Exception:
            log.warning("Failed to write final prompt to Redis", exc_info=True)

    # Broadcast is opt-in (skipped entirely if no token is configured) and
    # non-fatal — the analysis itself is already safely written to Redis by
    # this point, so a LINE-side failure shouldn't fail the whole run.
    if result.get("recommendations") and os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"):
        try:
            broadcast(result)
            log.info("Broadcast final recommendations for %s to LINE OA.", today)
        except LineBroadcastError:
            log.error("LINE broadcast failed", exc_info=True)

    log.info("Run finished in %.1fs", time.monotonic() - start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
