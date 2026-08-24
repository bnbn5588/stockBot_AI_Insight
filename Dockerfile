FROM python:3.12-slim

# Node.js is needed only to install the `claude` CLI — the worker itself is
# pure Python. This keeps the host's Python/pip completely untouched; the
# only thing installed on the host is Docker (and Node, separately, just for
# the one-time `claude` login — see README "Docker deployment").
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker ./worker
COPY run_all.sh .
RUN chmod +x run_all.sh

# The news-only step needs --permission-mode bypassPermissions (see
# claude_cli.py) to actually execute WebSearch/WebFetch — Claude Code refuses
# that combination when running as root, as a safety guardrail. Run as a
# non-root user so both worker.main and worker.main_news work the same way.
RUN useradd --create-home --shell /bin/bash worker
ENV HOME=/home/worker
USER worker

# .env, ~/.claude, and ~/.claude.json are mounted at runtime, not baked into
# the image — see README for the `docker run` command. CMD runs the
# production analysis followed by the news-only follow-on (run_all.sh);
# override with `python -m worker.main` alone to skip the news step.
CMD ["./run_all.sh"]
