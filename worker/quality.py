"""Checks each Claude response against the data it was given, before it is
cached or broadcast. A response with issues is sent back once with the list
of problems (claude_cli.run_checked); whatever remains after that is logged
and stored alongside the result as `qualityIssues`, never silently dropped.

The checks only catch things that can be verified mechanically — a ticker
that isn't in the data, a topPick that isn't a BUY, a figure that doesn't
match the snapshot, forecast language, an earnings report left unmentioned.
They say nothing about whether the reasoning is any good.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, Iterable, List, Optional

from . import analytics as A

EARNINGS_WARN_DAYS = 7
ADX_TOLERANCE = 1.5
PRICE_TOLERANCE = 0.02  # 2%
# Round numbers the model uses as thresholds ("ADX above 20"), not as a
# ticker's value — don't check them against the data.
_ADX_THRESHOLDS = {20.0, 25.0, 40.0}

_FORECAST_RE = re.compile(
    r"\bwill\s+(?:likely\s+)?(?:rise|fall|climb|drop|rally|surge|soar|plunge|rebound|recover|"
    r"go\s+up|go\s+down|outperform|underperform)\b"
    r"|\bguarantee[sd]?\b|\bprice\s+target\b|\btarget\s+price\b|\bcertain\s+to\b|\bsure\s+to\b",
    re.IGNORECASE,
)
_ADX_RE = re.compile(r"\bADX\s*(?:of|at|is|=|:|near|around|~)?\s*~?(\d+(?:\.\d+)?)", re.IGNORECASE)
_AGREE_RE = re.compile(r"\bagree(?:ment)?%?\s*(?:of|at|is|=|:)?\s*(\d+)\s*%?", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
_TRADE_STATS_RE = re.compile(r"win\s*rate|\bwr\b|expectancy|\bexp\b", re.IGNORECASE)
_EARNINGS_RE = re.compile(r"earning|report", re.IGNORECASE)


def earnings_days(row: A.Row, today: str) -> Optional[int]:
    if not row.earnings:
        return None
    return (date.fromisoformat(row.earnings) - date.fromisoformat(today)).days


def _earnings_soon(row: A.Row, today: str) -> bool:
    d = earnings_days(row, today)
    return d is not None and 0 <= d <= EARNINGS_WARN_DAYS


# The news step reports analyst actions, and "X raised its price target to $Y" is a fact
# about the news there, not a forecast by the model.
_REPORTED_TARGET_RE = re.compile(r"price\s+target|target\s+price", re.IGNORECASE)


def _forecast_issues(where: str, text: str, allow_reported_targets: bool = False) -> List[str]:
    return [f'{where}: forecast/certainty wording "{m.group(0)}" — describe trend and risk instead'
            for m in _FORECAST_RE.finditer(text or "")
            if not (allow_reported_targets and _REPORTED_TARGET_RE.fullmatch(m.group(0)))]


def _figure_issues(where: str, text: str, row: A.Row) -> List[str]:
    """Figures quoted in a ticker's own reason must match that ticker's row."""
    issues = []
    for m in _ADX_RE.finditer(text):
        v = float(m.group(1))
        if v not in _ADX_THRESHOLDS and abs(v - row.adx) > ADX_TOLERANCE:
            issues.append(f"{where}: says ADX {m.group(1)} but the data has {row.adx:.0f}")
    for m in _AGREE_RE.finditer(text):
        v = float(m.group(1))
        if abs(v - row.confidence) > 0.5:
            issues.append(f"{where}: says agree {m.group(1)}% but the data has {row.confidence:.0f}%")
    for m in _PRICE_RE.finditer(text):
        v = float(m.group(1).replace(",", ""))
        if row.price > 0 and abs(v / row.price - 1) > PRICE_TOLERANCE:
            issues.append(f"{where}: says ${m.group(1)} but the latest price is ${row.price:.2f}")
    return issues


def check_analysis(result: dict, all_data: A.AllData, today: str, min_trades: int,
                   picks: List[str], risks: List[str]) -> List[str]:
    """picks/risks: the tickers selection.select fixed for each list."""
    issues: List[str] = []
    for field, expected in (("topPicks", picks), ("riskWatch", risks)):
        items = result.get(field, [])
        got = [i.get("ticker", "") for i in items]
        if got != expected:
            issues.append(f"{field}: tickers {got} must be exactly {expected}, in that order")
        for item in items:
            t, reason = item.get("ticker", ""), item.get("reason", "")
            where = f"{field} {t}"
            rows = all_data.get(t)
            if not rows:
                issues.append(f"{where}: ticker is not in the snapshot")
                continue
            row = rows[-1]
            issues += _figure_issues(where, reason, row)
            if A.trade_stats(rows).total_trades < min_trades and _TRADE_STATS_RE.search(reason):
                issues.append(f"{where}: cites win rate/expectancy, but it is marked too few to judge")
            if _earnings_soon(row, today) and not _EARNINGS_RE.search(reason):
                issues.append(f"{where}: earnings in {earnings_days(row, today)} days is not mentioned")
            issues += _forecast_issues(where, reason)
    for field in ("marketSummary", "portfolioNote"):
        issues += _forecast_issues(field, result.get(field, ""))
    return issues


def check_news(result: dict, headlines: Dict[str, List[dict]]) -> List[str]:
    issues: List[str] = []
    seen = set()
    for item in result.get("newsHighlights", []):
        t = item.get("ticker", "")
        where = f"newsHighlights {t}"
        if t not in headlines:
            issues.append(f"{where}: ticker is not in the flagged list")
            continue
        if t in seen:
            issues.append(f"{where}: more than one entry — merge them into one")
        seen.add(t)
        valid = {h["id"] for h in headlines[t]}
        ids = item.get("headlineIds", [])
        bad = [i for i in ids if i not in valid]
        if bad:
            issues.append(f"{where}: headline ids {bad} are not among {t}'s headlines")
        if not ids:
            issues.append(f"{where}: no headlineIds — every entry must cite at least one headline")
        issues += _forecast_issues(where, item.get("summary", "") + " " + item.get("recommendation", ""),
                                   allow_reported_targets=True)
    return issues


def check_final(result: dict, tickers: Iterable[str], fixed: Dict[str, str],
                all_data: A.AllData, today: str) -> List[str]:
    """fixed: {ticker: stance} from prompt_final.stances."""
    issues: List[str] = []
    expected = list(tickers)
    got = [r.get("ticker") for r in result.get("recommendations", [])]
    if sorted(got) != sorted(expected):
        issues.append(f"recommendations: tickers {got} must be exactly {expected}, once each")
    for r in result.get("recommendations", []):
        t, stance, reason = r.get("ticker"), r.get("stance"), r.get("reason", "")
        where = f"recommendations {t}"
        if t in fixed and stance != fixed[t]:
            issues.append(f"{where}: stance {stance} must be the fixed stance {fixed[t]}")
        rows = all_data.get(t)
        if rows and _earnings_soon(rows[-1], today) and not _EARNINGS_RE.search(reason):
            issues.append(f"{where}: earnings in {earnings_days(rows[-1], today)} days is not mentioned")
        issues += _forecast_issues(where, reason)
    issues += _forecast_issues("summary", result.get("summary", ""))
    return issues
