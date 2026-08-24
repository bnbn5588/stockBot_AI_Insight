"""Shared logging config. Writes to log/worker.log (rotated at midnight, 14
days kept) plus stdout, so cron/docker output redirection keeps working
exactly as before while also getting a persistent, structured log file.

Log directory defaults to <project root>/log; override with LOG_DIR (e.g. to
point at a Docker volume mount, since the container's filesystem is
otherwise ephemeral).
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(os.environ.get("LOG_DIR", Path(__file__).resolve().parent.parent / "log"))
_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured

    root = logging.getLogger("worker")
    if not _configured:
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            _LOG_DIR / "worker.log", when="midnight", backupCount=14, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)

        _configured = True

    return logging.getLogger(f"worker.{name}")
