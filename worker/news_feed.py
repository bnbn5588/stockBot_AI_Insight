"""Fetches recent headlines per ticker from Yahoo Finance, so the news step
summarizes real, dated articles instead of asking the model to web-search.

Two keyless sources, merged and deduplicated:
- the per-ticker RSS feed (feeds.finance.yahoo.com) — about 20 items for
  every ticker tried, with a short description; the primary source
- the search endpoint (query2.finance.yahoo.com/v1/finance/search) — more
  items for busy tickers but sometimes returns none, so only a supplement

Every headline gets a stable id ("NVDA-1", ...). The model refers to
headlines only by id and the code fills in publisher/date/url from here, so
a source or date in the output can never be invented.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, TypedDict

import requests

from .logging_setup import get_logger

log = get_logger("news_feed")

RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT_SECONDS = 15
MAX_PER_TICKER = 12
DESCRIPTION_CHARS = 280

_TAG_RE = re.compile(r"<[^>]+>")


class Headline(TypedDict):
    id: str
    title: str
    publisher: str
    published: str  # ISO 8601, UTC
    url: str
    description: str


def _clean(text: Optional[str]) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())


def _from_rss(ticker: str) -> List[dict]:
    res = requests.get(RSS_URL, params={"s": ticker, "region": "US", "lang": "en-US"},
                       headers=HEADERS, timeout=TIMEOUT_SECONDS)
    res.raise_for_status()
    items = []
    for it in ET.fromstring(res.content).iter("item"):
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        items.append({
            "title": _clean(it.findtext("title")),
            "publisher": "",  # the feed carries no publisher
            "published": published.astimezone(timezone.utc),
            "url": (it.findtext("link") or "").strip(),
            "description": _clean(it.findtext("description")),
        })
    return items


def _from_search(ticker: str) -> List[dict]:
    res = requests.get(SEARCH_URL, params={"q": ticker, "newsCount": 50, "quotesCount": 0},
                       headers=HEADERS, timeout=TIMEOUT_SECONDS)
    res.raise_for_status()
    items = []
    for n in res.json().get("news", []):
        # The search matches any article that merely lists the ticker among
        # many; keep the ones where it is one of at most three.
        related = n.get("relatedTickers") or []
        if ticker not in related or len(related) > 3:
            continue
        items.append({
            "title": _clean(n.get("title")),
            "publisher": n.get("publisher", ""),
            "published": datetime.fromtimestamp(n["providerPublishTime"], timezone.utc),
            "url": n.get("link", ""),
            "description": "",
        })
    return items


def fetch_headlines(ticker: str, since: datetime, limit: Optional[int] = MAX_PER_TICKER) -> List[Headline]:
    """Headlines published at or after `since`, newest first, at most
    `limit` (None: all). Raises only when both sources fail."""
    merged: Dict[str, dict] = {}
    errors = []
    for source in (_from_rss, _from_search):
        try:
            for item in source(ticker):
                key = item["title"].lower()
                if not key or item["published"] < since:
                    continue
                prev = merged.get(key)
                if prev is None:
                    merged[key] = item
                else:  # same story from both: keep whichever fields are filled
                    prev["publisher"] = prev["publisher"] or item["publisher"]
                    prev["description"] = prev["description"] or item["description"]
        except Exception as exc:  # network, HTTP, parse
            errors.append(f"{source.__name__}: {exc}")
    if len(errors) == 2:
        raise RuntimeError(f"no news source reachable for {ticker}: {'; '.join(errors)}")
    for e in errors:
        log.warning("%s headline source failed: %s", ticker, e)

    items = sorted(merged.values(), key=lambda i: i["published"], reverse=True)[:limit]
    return [
        {
            "id": f"{ticker}-{n}",
            "title": i["title"],
            "publisher": i["publisher"],
            "published": i["published"].isoformat(timespec="minutes"),
            "url": i["url"],
            "description": i["description"][:DESCRIPTION_CHARS],
        }
        for n, i in enumerate(items, start=1)
    ]


def lookback_start(now: datetime, lookback_days: int) -> datetime:
    """Window start. On Monday it reaches back over the weekend to Friday,
    so weekend news isn't skipped by a 2-day window."""
    days = max(lookback_days, 3) if now.weekday() == 0 else lookback_days
    return now - timedelta(days=days)
