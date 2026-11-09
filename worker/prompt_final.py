"""Final synthesis prompt — combines the already-computed signal-only
analysis (ai-analysis:{date}) and news-only analysis (ai-analysis-news:{date})
into one final, reconciled recommendation per ticker. Does NOT re-derive the
quant numbers or re-search news — pure synthesis over what both prior steps
already produced, so this stays fast (no tool access, see
claude_cli.get_final_recommendations) and can't contradict either source; it
just resolves them into one bottom-line stance.
"""
from __future__ import annotations

from typing import Dict, List, Tuple


def _news_line(ticker: str, news_by_ticker: Dict[str, dict]) -> str:
    nh = news_by_ticker.get(ticker)
    if not nh:
        return "news: no relevant recent news found"
    effect = f" [{nh['effect']}, {nh['materiality']}]" if nh.get("effect") else ""
    return (f"news{effect}: {nh['summary']} (source: {nh['source']}, {nh['publishedDate']}) "
            f"— read: {nh['recommendation']}")


def stances(analysis: dict, news: dict) -> Dict[str, Tuple[str, str]]:
    """{ticker: (stance, why)} by rule: favor for a topPick, caution for a
    riskWatch ticker (or one in both). A favor turns to caution when the news
    step rated the news "contradicts" + "high"; a caution never turns to
    favor — good news alone doesn't override a broken or unclear trend (since
    2026-11-10: a single analyst upgrade had flipped a fresh SELL to favor).
    The model writes the reasons; it doesn't pick the stance — run to run it
    flipped stances on identical input."""
    news_by_ticker = {h["ticker"]: h for h in news.get("newsHighlights", [])}
    out: Dict[str, Tuple[str, str]] = {}
    for ticker, reasons in _quant_by_ticker(analysis).items():
        is_pick = any(r.startswith("[topPick]") for r in reasons)
        is_risk = any(r.startswith("[riskWatch]") for r in reasons)
        default = "favor" if is_pick and not is_risk else "caution"
        nh = news_by_ticker.get(ticker, {})
        major_against = nh.get("effect") == "contradicts" and nh.get("materiality") == "high"
        if default == "favor" and major_against:
            out[ticker] = ("caution", "topPick, turned to caution by major news against the signal")
        elif default == "caution" and major_against:
            out[ticker] = ("caution", "riskWatch; major news goes against the signal, but good news "
                                      "alone doesn't override the trend")
        else:
            out[ticker] = (default, "topPick" if default == "favor" else
                           "riskWatch" if not is_pick else "flagged as both topPick and riskWatch")
    return out


def _quant_by_ticker(analysis: dict) -> Dict[str, List[str]]:
    # A ticker can appear in both topPicks and riskWatch (a contradiction flag
    # cuts both ways) — merge into one entry per ticker instead of two
    # redundant blocks with the same news line repeated.
    quant_by_ticker: Dict[str, List[str]] = {}
    for item in analysis.get("topPicks", []):
        quant_by_ticker.setdefault(item["ticker"], []).append(f"[topPick] {item['reason']}")
    for item in analysis.get("riskWatch", []):
        quant_by_ticker.setdefault(item["ticker"], []).append(f"[riskWatch] {item['reason']}")
    return quant_by_ticker


def candidate_tickers(analysis: dict) -> List[str]:
    """The deduped ticker list build_final_prompt will include, in the same
    order. main_final.py uses this to size the response schema's exact
    array length (claude_cli.get_final_recommendations' candidate_count) —
    keeping the two in sync here means they can't drift apart."""
    return list(_quant_by_ticker(analysis).keys())


def build_final_prompt(analysis: dict, news: dict, today: str) -> str:
    news_by_ticker: Dict[str, dict] = {h["ticker"]: h for h in news.get("newsHighlights", [])}
    quant_by_ticker = _quant_by_ticker(analysis)

    fixed = stances(analysis, news)
    lines: List[str] = [
        f"- {ticker} — stance: {fixed[ticker][0]} ({fixed[ticker][1]}): {' '.join(reasons)} | "
        f"{_news_line(ticker, news_by_ticker)}"
        for ticker, reasons in quant_by_ticker.items()
    ]
    tickers_block = "\n".join(lines)

    return f"""Today is {today}. Two independent analyses of the same portfolio already ran:
1) A data-driven analysis of algorithmic trading signals (quant reasoning below, already final).
2) A news check on the tickers that analysis flagged (news findings below, where found).

Your job is to explain one final stance per ticker below. The stance is already fixed by
rule (shown after each ticker) — do not change it. Do not recompute the quant numbers, do not
search for new information, and do not add tickers not listed below — use only what's provided.

Quant portfolio note: {analysis.get('portfolioNote', '')}

Flagged tickers:
{tickers_block}

Your response fields (the output schema enforces the exact structure):
- generatedAt: "{today}"
- recommendations: one entry per flagged ticker, each with ticker, stance (the fixed one),
  and reason (one sentence combining the quant signal and the news finding).
  This is its own top-level field — never put recommendation data inside summary.
- summary: a plain-prose 2-3 sentence final takeaway synthesizing both the quant picture
  and the news read. No JSON, brackets, or per-ticker lists in it.

Rules:
- Include every ticker listed above, exactly once each.
- stance: copy the fixed stance shown for each ticker. "favor" = trend to follow, reasonable
  to hold or buy; "caution" = avoid, reduce or wait. The rule: favor for a topPick, caution for
  a riskWatch ticker; a topPick turns to caution on major news against it, while a riskWatch
  ticker stays caution whatever the news.
- When a topPick was turned to caution by news, reason must say what the news was and why it
  outweighs the trend. When major news goes against a riskWatch ticker's signal, mention it as
  a reason to watch, while the stance stays caution until the trend itself recovers. When news
  contradicts but was rated low, say it was noted but not enough to change the stance.
- reason must reference both the quant angle and the news angle (or explicitly note no
  news was found) — don't drop either source.
- Describe trend and risk, not certainty: no "will rise", "guaranteed", or price targets. These signals
  help manage risk; they are not forecasts of which stocks will beat the market.
- If the quant angle mentions an upcoming earnings report, keep that in the reason: the stance
  carries event risk until the report is out.
- Do not invent data not present above."""
