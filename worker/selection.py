"""Picks topPicks and riskWatch by rule, so the same sheet always gives the
same tickers. The model only explains the picks (prompt.py); left to choose,
it picked different tickers from identical data on different runs.

topPicks — BUY tickers in an uptrend, best first, at most 3:
  "clean" = regime=UP, +DI at least 5 above -DI, ADX >= 20, agree >= 60
  "weak"  = regime=UP, +DI above -DI, but short of one of those
Clean ones come before weak ones. A BUY with +DI at or below -DI, or outside
regime=UP, is not a pick. topPicks can be empty.

riskWatch — the 3 highest-scoring non-pick tickers (score > 0), where:
  +3 signal changed today
  +2 SELL that started within the last 5 sessions (fresh break)
  +2 earnings report within 7 days
  +1 regime=DOWN with -DI above +DI (established downtrend)
  +1 unreliable read: agree <= 20 and ADX < 20
  +1 3 or more signal flips in the last 10 sessions
Ties go to the wider -DI over +DI gap, then the ticker name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from . import analytics as A

MAX_PICKS = 3
MAX_RISKS = 3
CLEAN_ADX = 20
CLEAN_AGREE = 60
CLEAN_DI_GAP = 5
FRESH_SELL_DAYS = 5
EARNINGS_WARN_DAYS = 7
FLIP_LOOKBACK = 10
FLIP_COUNT = 3


@dataclass
class Pick:
    ticker: str
    why: List[str] = field(default_factory=list)  # the rule hits, shown to the model


def _earnings_days(row: A.Row, today: str) -> Optional[int]:
    if not row.earnings:
        return None
    return (date.fromisoformat(row.earnings) - date.fromisoformat(today)).days


def _di_gap(row: A.Row) -> float:
    """+DI minus -DI; 0 when unknown."""
    if row.plus_di is None or row.minus_di is None:
        return 0.0
    return row.plus_di - row.minus_di


def _flips(rows: List[A.Row]) -> int:
    recent = rows[-FLIP_LOOKBACK:]
    return sum(1 for a, b in zip(recent, recent[1:]) if a.signal != b.signal)


def top_picks(all_data: A.AllData) -> List[Pick]:
    ranked = []
    for t, rows in all_data.items():
        last = rows[-1]
        if last.signal != "BUY" or last.regime != "UP" or _di_gap(last) <= 0:
            continue
        short = ([f"ADX {last.adx:.0f} < {CLEAN_ADX}"] if last.adx < CLEAN_ADX else []) + \
                ([f"agree {last.confidence:.0f} < {CLEAN_AGREE}"] if last.confidence < CLEAN_AGREE else []) + \
                ([f"DI gap {_di_gap(last):.1f} < {CLEAN_DI_GAP}"] if _di_gap(last) < CLEAN_DI_GAP else [])
        clean = not short
        why = ["clean uptrend" if clean else f"weak uptrend ({', '.join(short)})"]
        key = (not clean, -last.confidence, -_di_gap(last), -last.adx, t)
        ranked.append((key, Pick(t, why)))
    return [p for _, p in sorted(ranked, key=lambda x: x[0])[:MAX_PICKS]]


def risk_watch(all_data: A.AllData, today: str, exclude: List[str]) -> List[Pick]:
    ranked = []
    for t, rows in all_data.items():
        if t in exclude:
            continue
        last = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None
        score, why = 0, []
        if prev and prev.signal != last.signal:
            score += 3
            why.append(f"signal changed {prev.signal}→{last.signal} today")
        streak = A.current_streak(rows)
        if last.signal == "SELL" and streak["days"] <= FRESH_SELL_DAYS:
            score += 2
            why.append(f"fresh SELL ({streak['days']}d)")
        d = _earnings_days(last, today)
        if d is not None and 0 <= d <= EARNINGS_WARN_DAYS:
            score += 2
            why.append(f"earnings in {d}d")
        if last.regime == "DOWN" and _di_gap(last) < 0:
            score += 1
            why.append("downtrend (regime=DOWN, -DI above +DI)")
        if last.confidence <= 20 and last.adx < CLEAN_ADX:
            score += 1
            why.append("unreliable read (low agree, weak ADX)")
        if _flips(rows) >= FLIP_COUNT:
            score += 1
            why.append(f"{_flips(rows)} flips in {FLIP_LOOKBACK} sessions")
        if score > 0:
            ranked.append(((-score, _di_gap(last), t), Pick(t, why)))
    return [p for _, p in sorted(ranked, key=lambda x: x[0])[:MAX_RISKS]]


def select(all_data: A.AllData, today: str) -> tuple[List[Pick], List[Pick]]:
    picks = top_picks(all_data)
    return picks, risk_watch(all_data, today, exclude=[p.ticker for p in picks])
