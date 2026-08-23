"""News-only prompt — deliberately does NOT re-derive marketSummary/topPicks/
riskWatch/portfolioNote (that's prompt.py's job, already cached in
ai-analysis:{date}). Takes the ticker+reason candidates already flagged by
that analysis and asks Claude to search news for exactly those, returning
only newsHighlights. Keeps the two analyses non-overlapping: one source of
truth for the quant picks, one source of truth for news.

Requires the CLI call to allow the WebSearch/WebFetch tools (see
claude_cli.get_news_highlights) — NOT wired into the daily cron path by
default; main_news.py runs it against whatever the production analysis
already flagged.
"""
from __future__ import annotations

from typing import List, TypedDict


class NewsCandidate(TypedDict):
    ticker: str
    signal: str
    reason: str  # why it was flagged: topPicks/riskWatch reason text, or a signal-change note


def build_news_only_prompt(candidates: List[NewsCandidate], today: str) -> str:
    candidate_lines = "\n".join(
        f"- {c['ticker']} ({c['signal']}): {c['reason']}" for c in candidates
    )

    return f"""Today is {today}. The tickers below were already flagged by a separate,
data-driven analysis of algorithmic trading signals (shown with why each was flagged).
Your only job here is to check for recent news on each — the quant reasoning is already
done elsewhere and is not yours to repeat or second-guess.

Flagged tickers:
{candidate_lines}

For each ticker, use web search to check for recent news (last 5 trading days): earnings,
guidance changes, analyst actions, regulatory or legal events, and major price-moving
headlines. Skip routine market commentary.

Respond with ONLY this JSON object (no markdown fences, no text outside the JSON):
{{
  "generatedAt": "{today}",
  "newsHighlights": [{{"ticker": "TICK", "summary": "one sentence on what happened", "source": "publisher name", "publishedDate": "YYYY-MM-DD"}}]
}}

Rules:
- Only include tickers from the flagged list above — do not search for or add others.
- Each entry must cite a real source and an approximate date from an actual search
  result. Never fabricate a headline, source, or date.
- If a ticker has no relevant recent news, omit it — do not include a placeholder entry.
- Return an empty newsHighlights array if none of the flagged tickers have relevant news."""
