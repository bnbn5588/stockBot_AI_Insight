"""Final synthesis prompt — combines the already-computed signal-only
analysis (ai-analysis:{date}) and news-only analysis (ai-analysis-news:{date})
into one final, reconciled recommendation per ticker. Does NOT re-derive the
quant numbers or re-search news — pure synthesis over what both prior steps
already produced, so this stays fast (no tool access, see
claude_cli.get_final_recommendations) and can't contradict either source; it
just resolves them into one bottom-line stance.
"""
from __future__ import annotations

from typing import Dict, List


def _news_line(ticker: str, news_by_ticker: Dict[str, dict]) -> str:
    nh = news_by_ticker.get(ticker)
    if not nh:
        return "news: no relevant recent news found"
    return f"news: {nh['summary']} (source: {nh['source']}, {nh['publishedDate']}) — read: {nh['recommendation']}"


def build_final_prompt(analysis: dict, news: dict, today: str) -> str:
    news_by_ticker: Dict[str, dict] = {h["ticker"]: h for h in news.get("newsHighlights", [])}

    # A ticker can appear in both topPicks and riskWatch (a contradiction flag
    # cuts both ways) — merge into one line per ticker instead of two
    # redundant blocks with the same news line repeated.
    quant_by_ticker: Dict[str, List[str]] = {}
    for item in analysis.get("topPicks", []):
        quant_by_ticker.setdefault(item["ticker"], []).append(f"[topPick] {item['reason']}")
    for item in analysis.get("riskWatch", []):
        quant_by_ticker.setdefault(item["ticker"], []).append(f"[riskWatch] {item['reason']}")

    lines: List[str] = [
        f"- {ticker}: {' '.join(reasons)} | {_news_line(ticker, news_by_ticker)}"
        for ticker, reasons in quant_by_ticker.items()
    ]
    tickers_block = "\n".join(lines)

    return f"""Today is {today}. Two independent analyses of the same portfolio already ran:
1) A data-driven analysis of algorithmic trading signals (quant reasoning below, already final).
2) A news check on the tickers that analysis flagged (news findings below, where found).

Your only job is to reconcile these two into one final stance per ticker below. Do not
recompute the quant numbers, do not search for new information, and do not add tickers
not listed below — use only what's provided.

Quant portfolio note: {analysis.get('portfolioNote', '')}

Flagged tickers:
{tickers_block}

Respond with ONLY this JSON object (no markdown fences, no text outside the JSON):
{{
  "generatedAt": "{today}",
  "summary": "2-3 sentence final takeaway synthesizing both the quant picture and the news read",
  "recommendations": [{{"ticker": "TICK", "stance": "favor", "reason": "one sentence combining the quant signal and the news finding"}}]
}}

Rules:
- Include every ticker listed above, exactly once each.
- stance defaults to "favor" for a topPick and "caution" for a riskWatch ticker; if a
  ticker is flagged as both (a contradiction), weigh both quant angles plus the news to
  decide. Flip the default only when the news read materially changes the picture, and
  say why in reason.
- reason must reference both the quant angle and the news angle (or explicitly note no
  news was found) — don't drop either source.
- Do not invent data not present above."""
