"""Thai versions of the model-written text, for the dashboard's TH/EN switch.
Each step adds a `th` field to its own Redis payload:

    analysis: {"marketSummary", "portfolioNote", "topPicks": {ticker: reason},
               "riskWatch": {ticker: reason}}
    news:     {ticker: {"summary", "recommendation"}}
    final:    {"summary", "recommendations": {ticker: reason}}

One extra Claude call per step. A failed or incomplete translation leaves `th`
out (the dashboard then shows English) and never fails the run. The LINE
broadcast stays English.
"""
from __future__ import annotations

from typing import Dict, Optional

from .claude_cli import ClaudeCLIError, run_checked
from .logging_setup import get_logger

log = get_logger("translate")

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "th": {"type": "string", "minLength": 1}},
                "required": ["id", "th"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _prompt(texts: Dict[str, str]) -> str:
    lines = "\n".join(f"[{k}] {v}" for k, v in texts.items())
    return f"""Translate each text below from English into natural Thai for Thai retail investors.
They are notes on stock-trading signals and news.

Rules:
- Keep as written: ticker symbols, numbers and % figures, dates, BUY / SELL / HOLD, ADX, +DI / -DI,
  agree, regime UP / DOWN, SPY, favor / caution, company and publisher names.
- Translate the meaning, not word by word; plain everyday Thai, no slang.
- Do not add, drop or soften anything — the same facts, caveats and level of certainty.
- Return one item per id, with the same ids.

Texts:
{lines}"""


def translate_texts(texts: Dict[str, str]) -> Optional[Dict[str, str]]:
    """{id: Thai} for every id in `texts`, or None on failure."""
    texts = {k: v for k, v in texts.items() if v}
    if not texts:
        return {}

    def check(r: dict) -> list:
        got = {i["id"] for i in r.get("items", [])}
        missing = sorted(set(texts) - got)
        return [f"items: missing ids {missing}"] if missing else []

    try:
        result, usage, issues = run_checked(_prompt(texts), _SCHEMA, check=check)
    except ClaudeCLIError:
        log.warning("Thai translation failed — leaving it out", exc_info=True)
        return None
    if issues:
        log.warning("Thai translation incomplete (%s) — leaving it out", issues)
        return None
    log.info("Translated %d texts to Thai, cost=$%.4f (subscription usage, not billed)",
             len(texts), usage.get("totalCostUsd", 0.0))
    return {i["id"]: i["th"] for i in result["items"] if i["id"] in texts}


def analysis_th(result: dict) -> Optional[dict]:
    texts = {"marketSummary": result.get("marketSummary", ""),
             "portfolioNote": result.get("portfolioNote", "")}
    for field in ("topPicks", "riskWatch"):
        for item in result.get(field, []):
            texts[f"{field}.{item['ticker']}"] = item.get("reason", "")
    th = translate_texts(texts)
    if th is None:
        return None
    return {
        "marketSummary": th.get("marketSummary", ""),
        "portfolioNote": th.get("portfolioNote", ""),
        **{field: {i["ticker"]: th.get(f"{field}.{i['ticker']}", "") for i in result.get(field, [])}
           for field in ("topPicks", "riskWatch")},
    }


def news_th(result: dict) -> Optional[dict]:
    texts = {}
    for h in result.get("newsHighlights", []):
        texts[f"{h['ticker']}.summary"] = h.get("summary", "")
        texts[f"{h['ticker']}.recommendation"] = h.get("recommendation", "")
    th = translate_texts(texts)
    if th is None:
        return None
    return {h["ticker"]: {"summary": th.get(f"{h['ticker']}.summary", ""),
                          "recommendation": th.get(f"{h['ticker']}.recommendation", "")}
            for h in result.get("newsHighlights", [])}


def final_th(result: dict) -> Optional[dict]:
    texts = {"summary": result.get("summary", "")}
    for r in result.get("recommendations", []):
        texts[f"rec.{r['ticker']}"] = r.get("reason", "")
    th = translate_texts(texts)
    if th is None:
        return None
    return {"summary": th.get("summary", ""),
            "recommendations": {r["ticker"]: th.get(f"rec.{r['ticker']}", "")
                                for r in result.get("recommendations", [])}}
