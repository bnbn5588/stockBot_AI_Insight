#!/bin/sh
# Docker default CMD: runs the production analysis, then the news-only
# follow-on. `set -e` means if worker.main fails, worker.main_news never
# runs (it needs main's cached output anyway); if worker.main succeeds but
# worker.main_news fails, this script's exit code reflects that failure.
set -e
python -m worker.main
python -m worker.main_news
