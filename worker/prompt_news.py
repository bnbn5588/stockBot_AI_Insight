"""News-only prompt — deliberately does NOT re-derive marketSummary/topPicks/
riskWatch/portfolioNote (that's prompt.py's job, already cached in
ai-analysis:{date}). Takes the ticker+reason candidates already flagged by
that analysis and asks Claude to search news for exactly those, returning
newsHighlights with a per-item recommendation (does this news reinforce,
temper, or contradict the already-flagged signal). Keeps the two analyses
non-overlapping: one source of truth for the quant picks, one source of
truth for news and its read on those picks.

lookback_days keeps the search window tight (default 2 days) rather than a
wide multi-day span — this step has no cache-skip and is meant to be run
several times a day, so a short window means each run surfaces what's
actually new since the last one instead of re-returning the same older
headlines every time.

Two variants:
- build_news_from_headlines_prompt (default): the headlines are fetched by
  the code (news_feed.py) and listed in the prompt with ids; the model only
  reads, filters and summarizes them and cites them by id. No tools needed.
- build_news_only_prompt: the model web-searches itself (WebSearch/WebFetch,
  see claude_cli.get_news_highlights). Kept as the fallback for when no
  headline source is reachable.
"""
from __future__ import annotations

from typing import Dict, List, TypedDict


class NewsCandidate(TypedDict):
    ticker: str
    signal: str
    reason: str  # why it was flagged: topPicks/riskWatch reason text, or a signal-change note
    earnings: str  # "next earnings 2026-11-17 (in 9 days)", or "" when unknown


def _candidate_line(c: NewsCandidate) -> str:
    extra = f" [{c['earnings']}]" if c.get("earnings") else ""
    return f"- {c['ticker']} ({c['signal']}){extra}: {c['reason']}"


_SIGNAL_CONTEXT = """The signals are trend-following (daily closes): BUY = an intact uptrend (above the 200-day
average, short-term trend and directional movement up); SELL = the trend is broken, meaning
"exit / stay out" rather than a forecast of how far the price will fall; HOLD = long-term uptrend
with a mixed short term. Their value is risk control, not predicting which stocks beat the market."""

_READ_RULES = """- recommendation must say whether the news makes the flagged trend read more or less
  trustworthy, referring to the flagged reason (trend, regime, directional movement, agree%,
  streak). Do not invent new quantitative figures — only the reason text and the news are
  available to you.
- recommendation is not a buy/sell instruction — frame it as how this changes conviction in
  the already-flagged signal, not as new standalone advice.
- If a ticker reports earnings soon (shown in brackets), say so: the trend read carries event
  risk until the report is out. An upcoming report is not bullish or bearish by itself.
- No forecast or certainty wording ("will rise", "guaranteed", price targets)."""


def _headline_line(h: dict) -> str:
    who = f"{h['publisher']}, " if h.get("publisher") else ""
    desc = f" — {h['description']}" if h.get("description") else ""
    return f"  [{h['id']}] ({who}{h['published'][:16].replace('T', ' ')} UTC) {h['title']}{desc}"


def build_news_from_headlines_prompt(
    candidates: List[NewsCandidate], headlines: Dict[str, List[dict]], today: str, window: str
) -> str:
    blocks = []
    for c in candidates:
        hs = headlines.get(c["ticker"], [])
        listed = "\n".join(_headline_line(h) for h in hs) if hs else "  (no headlines in the window)"
        blocks.append(f"{_candidate_line(c)}\n{listed}")
    body = "\n\n".join(blocks)

    return f"""Today is {today}. The tickers below were already flagged by a separate,
data-driven analysis of algorithmic trading signals (shown with why each was flagged).
Your only job here is to read the recent headlines listed under each ticker — the quant
reasoning is already done elsewhere and is not yours to repeat or second-guess.

{_SIGNAL_CONTEXT}

Flagged tickers and their headlines from the last {window} (newest first, each with an id):

{body}

Respond with newsHighlights: at most one entry per ticker, each with ticker, headlineIds (the
ids of the headlines it is based on), summary (one or two sentences on what happened) and
recommendation.

Rules:
- Use only the headlines listed above, cited by their ids. Do not add facts that are not in
  a headline's title or description.
- Keep news that is specifically about the company and could move the stock: earnings,
  guidance, analyst rating or target changes, deals, products, regulatory or legal events,
  management changes, large price moves with a stated cause.
- Skip opinion and listicle pieces ("Is X a buy?", "X vs Y", "stocks to watch"), articles
  that only mention the ticker in passing, and option-chain or quote pages.
- If none of a ticker's headlines qualify, leave the ticker out. Return an empty
  newsHighlights array if nothing qualifies.
{_READ_RULES}"""


def build_news_only_prompt(candidates: List[NewsCandidate], today: str, lookback_days: int = 2) -> str:
    candidate_lines = "\n".join(_candidate_line(c) for c in candidates)
    window = "24 hours" if lookback_days <= 1 else f"{lookback_days} days"

    return f"""Today is {today}. The tickers below were already flagged by a separate,
data-driven analysis of algorithmic trading signals (shown with why each was flagged).
Your only job here is to check for recent news on each — the quant reasoning is already
done elsewhere and is not yours to repeat or second-guess.

{_SIGNAL_CONTEXT}

Flagged tickers:
{candidate_lines}

For each ticker, use web search to check for recent news from the last {window} only:
earnings, guidance changes, analyst actions, regulatory or legal events, and major
price-moving headlines. Skip anything older than that window, and skip routine market
commentary.

Respond with ONLY this JSON object (no markdown fences, no text outside the JSON):
{{
  "generatedAt": "{today}",
  "newsHighlights": [{{"ticker": "TICK", "summary": "one sentence on what happened", "source": "publisher name", "publishedDate": "YYYY-MM-DD", "recommendation": "one sentence on whether this news reinforces, tempers, or contradicts the flagged signal, and why"}}]
}}

Rules:
- Only include tickers from the flagged list above — do not search for or add others.
- Each entry must cite a real source and an approximate date from an actual search
  result. Never fabricate a headline, source, or date.
- If a ticker has no relevant recent news, omit it — do not include a placeholder entry.
- Return an empty newsHighlights array if none of the flagged tickers have relevant news.
{_READ_RULES}"""
