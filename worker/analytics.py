"""Port of stock-dashboard/src/lib/analytics.ts — only the functions needed to
build the AI-analysis prompt (streaks, consensus, forward returns, trade stats,
portfolio simulation). Keep in sync with the TS source if that changes.
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ADX_RE = re.compile(r"ADX:\s*([\d.]+)")


@dataclass
class Row:
    date: str
    signal: str
    price: float
    strength: str
    confidence: float
    adx: float


AllData = Dict[str, List[Row]]


# ── CSV parsing ──────────────────────────────────────────────────────────────
#
# Sheet format (7 columns, no header row):
#   "2025-10-19 8:00:07","AAPL","BUY","Weak","47.4","Strong (ADX: 60.63)","$252.29"
#
# The bot runs daily so the same trading-day row appears multiple times;
# keep only the first occurrence per (date, ticker).

def parse_csv(csv_text: str) -> AllData:
    data: AllData = {}
    seen = set()

    for parts in csv.reader(io.StringIO(csv_text.strip())):
        if len(parts) < 7:
            continue

        date = parts[0].strip()[:10]
        ticker = parts[1].strip()
        signal = parts[2].strip()
        strength = parts[3].strip()

        try:
            confidence = float(parts[4].strip())
        except ValueError:
            confidence = 0.0

        adx_match = _ADX_RE.search(parts[5].strip())
        adx = float(adx_match.group(1)) if adx_match else 0.0

        try:
            price = float(parts[6].strip().replace("$", "").replace(",", ""))
        except ValueError:
            price = math.nan

        if not _DATE_RE.match(date):
            continue
        if not ticker or not signal or math.isnan(price) or price <= 0:
            continue

        key = f"{date}|{ticker}"
        if key in seen:
            continue
        seen.add(key)

        data.setdefault(ticker, []).append(
            Row(date=date, signal=signal, price=price, strength=strength, confidence=confidence, adx=adx)
        )

    for rows in data.values():
        rows.sort(key=lambda r: r.date)

    return data


# ── Streak ───────────────────────────────────────────────────────────────────

def current_streak(rows: List[Row]) -> Dict[str, object]:
    """Current signal and how many trailing days share that signal."""
    if not rows:
        return {"signal": "", "days": 0}
    sig = rows[-1].signal
    n = 0
    for r in reversed(rows):
        if r.signal != sig:
            break
        n += 1
    return {"signal": sig, "days": n}


# ── Simulated P&L ────────────────────────────────────────────────────────────

@dataclass
class PnLPoint:
    date: str
    strategy: float  # % return, $10,000 starting capital, BUY->enter SELL->exit
    bah: float       # buy-and-hold % return from same start


def simulate_pnl(rows: List[Row]) -> List[PnLPoint]:
    if not rows:
        return []
    k = 10_000.0
    cash = k
    shares = 0.0
    in_pos = False
    bah_shares = k / rows[0].price

    result: List[PnLPoint] = []
    for row in rows:
        if row.signal == "BUY" and not in_pos:
            shares = cash / row.price
            cash = 0.0
            in_pos = True
        elif row.signal == "SELL" and in_pos:
            cash = shares * row.price
            shares = 0.0
            in_pos = False

        cur = shares * row.price if in_pos else cash
        result.append(PnLPoint(
            date=row.date,
            strategy=((cur - k) / k) * 100,
            bah=((bah_shares * row.price - k) / k) * 100,
        ))
    return result


# ── Today's snapshot ─────────────────────────────────────────────────────────

@dataclass
class SnapshotRow:
    ticker: str
    price: float
    signal: str
    date: str
    first_date: str
    trading_days: int
    day_change_pct: float
    period_change_pct: float
    streak: Dict[str, object]


def latest_snapshot(all_data: AllData) -> List[SnapshotRow]:
    out: List[SnapshotRow] = []
    for ticker, rows in all_data.items():
        if not rows:
            continue
        last = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None
        first = rows[0]
        out.append(SnapshotRow(
            ticker=ticker,
            price=last.price,
            signal=last.signal,
            date=last.date,
            first_date=first.date,
            trading_days=len(rows),
            day_change_pct=((last.price - prev.price) / prev.price) * 100 if prev else 0.0,
            period_change_pct=((last.price - first.price) / first.price) * 100 if first else 0.0,
            streak=current_streak(rows),
        ))
    out.sort(key=lambda r: r.ticker)
    return out


# ── Market consensus ─────────────────────────────────────────────────────────

def market_consensus(all_data: AllData) -> Dict[str, int]:
    """Count of tickers currently on each signal (latest row per ticker)."""
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for rows in all_data.values():
        if not rows:
            continue
        sig = rows[-1].signal
        if sig in counts:
            counts[sig] += 1
    return counts


# ── Forward returns ──────────────────────────────────────────────────────────

def aggregate_forward_returns(all_data: AllData) -> Dict[str, Dict[str, float]]:
    """For each signal type, avg next-trading-day price change, aggregated
    across all tickers."""
    sums = {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0}
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for rows in all_data.values():
        for i in range(len(rows) - 1):
            sig = rows[i].signal
            if sig in sums:
                sums[sig] += ((rows[i + 1].price - rows[i].price) / rows[i].price) * 100
                counts[sig] += 1
    return {
        sig: {"avg": (sums[sig] / counts[sig]) if counts[sig] else 0.0, "count": counts[sig]}
        for sig in ("BUY", "SELL", "HOLD")
    }


# ── Trade log / stats ────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    duration_days: int


def compute_trades(rows: List[Row]) -> List[Trade]:
    """Every completed BUY->SELL round trip, chronological order."""
    trades: List[Trade] = []
    entry_idx = -1
    for i, row in enumerate(rows):
        if row.signal == "BUY" and entry_idx == -1:
            entry_idx = i
        elif row.signal == "SELL" and entry_idx != -1:
            entry = rows[entry_idx]
            trades.append(Trade(
                entry_date=entry.date,
                exit_date=row.date,
                entry_price=entry.price,
                exit_price=row.price,
                return_pct=((row.price - entry.price) / entry.price) * 100,
                duration_days=i - entry_idx,
            ))
            entry_idx = -1
    return trades


@dataclass
class TradeStats:
    total_trades: int
    win_rate: float   # 0-100
    expectancy: float  # weighted avg return per trade


def trade_stats(rows: List[Row]) -> TradeStats:
    trades = compute_trades(rows)
    wins = [t for t in trades if t.return_pct > 0]
    losses = [t for t in trades if t.return_pct <= 0]

    win_rate = (len(wins) / len(trades)) * 100 if trades else 0.0
    avg_win = (sum(t.return_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.return_pct for t in losses) / len(losses)) if losses else 0.0
    expectancy = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss

    return TradeStats(total_trades=len(trades), win_rate=win_rate, expectancy=expectancy)


# ── Portfolio simulation ─────────────────────────────────────────────────────

def all_unique_dates(all_data: AllData) -> List[str]:
    dates = set()
    for rows in all_data.values():
        for r in rows:
            dates.add(r.date)
    return sorted(dates)


def portfolio_simulation(all_data: AllData) -> List[PnLPoint]:
    """Equal-weight portfolio: averages each ticker's strategy and
    buy-and-hold % returns across all dates. Each ticker is included from its
    own first data date onward; missing dates are forward-filled."""
    tickers = list(all_data.keys())
    if not tickers:
        return []

    strat_maps: List[Dict[str, float]] = []
    bah_maps: List[Dict[str, float]] = []
    first_dates: List[str] = []

    for ticker in tickers:
        rows = all_data[ticker]
        sm: Dict[str, float] = {}
        bm: Dict[str, float] = {}
        for p in simulate_pnl(rows):
            sm[p.date] = p.strategy
            bm[p.date] = p.bah
        strat_maps.append(sm)
        bah_maps.append(bm)
        first_dates.append(rows[0].date if rows else "9999-12-31")

    dates = all_unique_dates(all_data)
    n = len(tickers)
    last_strat = [0.0] * n
    last_bah = [0.0] * n
    result: List[PnLPoint] = []

    for date in dates:
        sum_s = sum_b = 0.0
        count = 0
        for i in range(n):
            if date < first_dates[i]:
                continue
            count += 1
            if date in strat_maps[i]:
                last_strat[i] = strat_maps[i][date]
                last_bah[i] = bah_maps[i][date]
            sum_s += last_strat[i]
            sum_b += last_bah[i]
        if count:
            result.append(PnLPoint(date=date, strategy=sum_s / count, bah=sum_b / count))

    return result
