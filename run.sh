#!/bin/sh
# Wrapper for cron: cron runs with a near-empty environment, so load .env
# (SHEET_ID, REDIS_URL, etc.) before invoking the worker. Requires `claude`
# to already be installed and logged into an active subscription for
# whichever user account cron runs this as.
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

exec python3 -m worker.main
