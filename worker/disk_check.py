"""Daily disk-space check for the host this worker runs on. Pushes a LINE
message to the admin (LINE_ADMIN_USER_ID only — never broadcast) when the
disk holding the mounted log/ directory is at or above DISK_WARN_PERCENT
(default 85). Reads usage through the log mount, so it measures the host's
filesystem, not the container's. Run daily from cron:
    python -m worker.disk_check
"""
from __future__ import annotations

import os
import shutil

from dotenv import load_dotenv

from .line_broadcast import LineBroadcastError, send_text_message
from .logging_setup import get_logger

load_dotenv()

log = get_logger("disk_check")

CHECK_PATH = os.environ.get("LOG_DIR", "/app/log")


def main() -> int:
    warn = float(os.environ.get("DISK_WARN_PERCENT", "85"))
    usage = shutil.disk_usage(CHECK_PATH)
    pct = usage.used / usage.total * 100
    free_gb = usage.free / 1024**3
    log.info("Disk %.0f%% used, %.1f GB free (warn at %.0f%%)", pct, free_gb, warn)
    if pct < warn:
        return 0

    admin = os.environ.get("LINE_ADMIN_USER_ID")
    if not (admin and os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")):
        log.warning("Disk above threshold but no LINE admin configured — not notified")
        return 0
    try:
        send_text_message(
            f"[stockBot] disk {pct:.0f}% full, {free_gb:.1f} GB free. "
            "Free space: `docker image prune` / remove old backup-* image tags.",
            to=admin,
        )
        log.info("Sent disk warning to admin")
    except LineBroadcastError:
        log.error("Failed to send disk warning", exc_info=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
