"""Forward-test scoring — how did the live signals and AI stances actually do?

    python -m worker.track

Scores two things against SPY over 5/10/20 trading days, using only
decisions made after go-live (TRACK_LIVE_SINCE) — the rebuilt pre-go-live
history in the sheet is a backtest, not a track record:

- Engine signals: every history-sheet row (session S) is known after S's
  close and delivered the next morning, so it is entered at the next
  session's open. Scored two ways: every daily row, and "events" only (rows
  where the ticker's signal changed — far less overlap between windows).
- AI final stances: archived daily by main_final.py under
  ai-analysis-final-archive:{date} (no TTL). Broadcast before the US open on
  UTC date D, so entered at the open of the first session on/after D.

A call is "right" when BUY/favor beats SPY, or SELL/caution trails it.
Writes the summary to Redis `track:summary` (no TTL) for the dashboard.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from statistics import mean
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from . import analytics as A
from . import redis_client as R
from .logging_setup import get_logger
from .main import fetch_sheet_csv

load_dotenv()

log = get_logger("track")

HORIZONS = [5, 10, 20]
BENCH = "SPY"
LIVE_SINCE = os.environ.get("TRACK_LIVE_SINCE", "2026-11-09")
SUMMARY_KEY = "track:summary"
BULLISH = {"BUY", "favor"}
BEARISH = {"SELL", "caution"}

# date -> (open, close), split/dividend-adjusted
Bars = Dict[str, Tuple[float, float]]


def fetch_bars(ticker: str, attempts: int = 3) -> Bars:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "2y", "interval": "1d"}
    for i in range(attempts):
        try:
            res = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            res.raise_for_status()
            r = res.json()["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
            bars: Bars = {}
            for ts, o, c, ac in zip(r["timestamp"], q["open"], q["close"], adj):
                if None in (o, c, ac) or c == 0:
                    continue
                day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
                f = ac / c  # adjust the open by the same factor as the close
                bars[day] = (o * f, ac)
            return bars
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))
    return {}


def _entry_index(days: List[str], day: str, strictly_after: bool) -> Optional[int]:
    for i, d in enumerate(days):
        if d > day or (d == day and not strictly_after):
            return i
    return None


def forward_excess(bars: Bars, bench: Bars, entry_day_idx: int, days: List[str], h: int) -> Optional[float]:
    """% return from open of days[i] to close of days[i+h-1], minus SPY's."""
    j = entry_day_idx + h - 1
    if j >= len(days):
        return None
    d0, d1 = days[entry_day_idx], days[j]
    if d0 not in bars or d1 not in bars or d0 not in bench or d1 not in bench:
        return None
    r = bars[d1][1] / bars[d0][0] - 1
    rb = bench[d1][1] / bench[d0][0] - 1
    return (r - rb) * 100


def _score(calls: List[dict], prices: Dict[str, Bars], days: List[str], strictly_after: bool) -> List[dict]:
    """Attach entry date and per-horizon excess to each call."""
    bench = prices[BENCH]
    out = []
    for c in calls:
        bars = prices.get(c["ticker"])
        i = _entry_index(days, c["date"], strictly_after)
        row = {**c, "entry": days[i] if i is not None else None}
        for h in HORIZONS:
            row[f"x{h}"] = forward_excess(bars, bench, i, days, h) if (bars and i is not None) else None
        out.append(row)
    return out


def _aggregate(scored: List[dict], key: str) -> dict:
    groups: Dict[str, dict] = {}
    for label in sorted({s[key] for s in scored}):
        g = [s for s in scored if s[key] == label]
        stats = {"calls": len(g)}
        for h in HORIZONS:
            xs = [s[f"x{h}"] for s in g if s[f"x{h}"] is not None]
            hit = None
            if xs and label in BULLISH:
                hit = sum(x > 0 for x in xs) / len(xs) * 100
            elif xs and label in BEARISH:
                hit = sum(x < 0 for x in xs) / len(xs) * 100
            stats[f"h{h}"] = {
                "n": len(xs),
                "meanExcess": round(mean(xs), 3) if xs else None,
                "hitRate": round(hit, 1) if hit is not None else None,
            }
        groups[label] = stats
    return groups


def engine_calls(all_data: A.AllData) -> Tuple[List[dict], List[dict]]:
    rows, events = [], []
    for ticker, history in all_data.items():
        for prev, cur in zip([None] + history[:-1], history):
            if cur.date < LIVE_SINCE:
                continue
            call = {"date": cur.date, "ticker": ticker, "label": cur.signal}
            rows.append(call)
            if prev is None or prev.signal != cur.signal:
                events.append(call)
    return rows, events


def ai_calls(redis_conn) -> List[dict]:
    calls = []
    for key in redis_conn.scan_iter(match=R.final_archive_key("*")):
        key = key.decode() if isinstance(key, bytes) else key
        date = key.rsplit(":", 1)[1]
        if date < LIVE_SINCE:
            continue
        final = json.loads(redis_conn.get(key))
        for rec in final.get("recommendations", []):
            calls.append({"date": date, "ticker": rec["ticker"], "label": rec["stance"]})
    return calls


def main() -> int:
    log.info("Run started (live since %s)", LIVE_SINCE)
    redis_conn = R.get_client()
    if redis_conn is None:
        log.error("REDIS_URL is not set")
        return 1

    all_data = A.parse_csv(fetch_sheet_csv(os.environ["SHEET_ID"]))
    rows, events = engine_calls(all_data)
    ai = ai_calls(redis_conn)

    tickers = sorted({c["ticker"] for c in rows + ai} | {BENCH})
    prices: Dict[str, Bars] = {}
    for t in tickers:
        try:
            prices[t] = fetch_bars(t)
        except Exception:
            log.warning("price fetch failed for %s — its calls stay unscored this run", t, exc_info=True)
        time.sleep(0.5)
    if BENCH not in prices:
        log.error("benchmark %s prices unavailable", BENCH)
        return 1
    days = sorted(prices[BENCH])

    s_rows = _score(rows, prices, days, strictly_after=True)
    s_events = _score(events, prices, days, strictly_after=True)
    s_ai = _score(ai, prices, days, strictly_after=False)

    def pending(scored):
        return sum(1 for s in scored if s[f"x{HORIZONS[0]}"] is None)

    summary = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "liveSince": LIVE_SINCE,
        "benchmark": BENCH,
        "horizons": HORIZONS,
        "engine": {
            "daily": _aggregate(s_rows, "label"),
            "events": _aggregate(s_events, "label"),
            "pending": pending(s_rows),
        },
        "ai": {
            "stances": _aggregate(s_ai, "label"),
            "pending": pending(s_ai),
            "days": len({c["date"] for c in ai}),
        },
        "recentAi": sorted(s_ai, key=lambda s: (s["date"], s["ticker"]), reverse=True)[:30],
    }
    redis_conn.set(SUMMARY_KEY, json.dumps(summary))
    log.info(
        "Scored engine rows=%d events=%d (pending %d), AI calls=%d over %d day(s) (pending %d) -> %s",
        len(s_rows), len(s_events), summary["engine"]["pending"],
        len(s_ai), summary["ai"]["days"], summary["ai"]["pending"], SUMMARY_KEY,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
