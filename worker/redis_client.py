"""Redis cache access — matches stock-dashboard's `ai-analysis:{date}` key and
25h TTL exactly, so the existing frontend (unmodified) keeps reading the same
cache this worker writes to.
"""
from __future__ import annotations

import os
from typing import Optional

import redis

CACHE_TTL_SECONDS = 90_000  # 25h — matches route.ts's `redis.set(cacheKey, ..., 'EX', 90000)`


def get_client() -> Optional[redis.Redis]:
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    return redis.from_url(url, socket_timeout=5, socket_connect_timeout=5, decode_responses=True)


def cache_key(today: str) -> str:
    return f"ai-analysis:{today}"


def prompt_cache_key(today: str) -> str:
    return f"ai-analysis-prompt:{today}"


def news_cache_key(today: str) -> str:
    return f"ai-analysis-news:{today}"


def news_prompt_cache_key(today: str) -> str:
    return f"ai-analysis-news-prompt:{today}"


def final_cache_key(today: str) -> str:
    return f"ai-analysis-final:{today}"


def final_prompt_cache_key(today: str) -> str:
    return f"ai-analysis-final-prompt:{today}"
