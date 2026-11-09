"""Permanent, append-only record of every run, kept as training/evaluation
data. Redis holds the analysis, news and prompts for only 25h, and the
headlines Yahoo serves are gone within days, so anything not written here
can't be recovered later.

One JSONL file per day, data/archive/YYYY-MM-DD.jsonl, one line per step run:
    {"step": "analysis" | "news" | "final", "writtenAt": ..., "today": ..., ...}
A step re-run on the same day appends another line; readers take the last
line per step as what was cached (and, for "final", broadcast).

Records are point-in-time: the inputs exactly as the step saw them (sheet
rows, earnings dates as known that day, headlines), the prompt, the model's
answer and the model name. Outcomes (forward returns) are not stored
here — they come later from the sheet's own price history.

Writing never fails a run: a missing mount or a full disk logs a warning.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from . import analytics as A
from .logging_setup import get_logger

log = get_logger("archive")

SCHEMA_VERSION = 1
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")


def snapshot_rows(all_data: A.AllData) -> dict:
    """Each ticker's latest sheet row, as the step saw it."""
    return {t: asdict(rows[-1]) for t, rows in all_data.items() if rows}


def write(step: str, today: str, record: dict, written_at: Optional[str] = None) -> None:
    # The data dir is a host mount (see README). If it isn't mounted, a write
    # would land in the container and vanish with it — skip loudly instead.
    if not os.path.isdir(DATA_DIR):
        log.warning("%s not mounted — %s archive for %s skipped", DATA_DIR, step, today)
        return
    line = {
        "schema": SCHEMA_VERSION,
        "step": step,
        "today": today,
        "writtenAt": written_at or datetime.now(timezone.utc).isoformat(),
        **record,
    }
    path = os.path.join(DATA_DIR, "archive", f"{today}.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        log.info("Archived %s for %s to %s", step, today, path)
    except OSError:
        log.warning("Failed to archive %s for %s", step, today, exc_info=True)
