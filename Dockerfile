FROM python:3.12-slim

# Node.js is needed only to install the `claude` CLI — the worker itself is
# pure Python. This keeps the host's Python/pip completely untouched; the
# only thing installed on the host is Docker (and Node, separately, just for
# the one-time `claude` login — see README "Docker deployment").
#
# The CLI version is pinned deliberately — an earlier unpinned `npm install
# -g @anthropic-ai/claude-code` silently froze at whatever version was
# current the first time this layer built, since Docker's build cache reuses
# a layer whenever the instruction text is unchanged, regardless of how many
# times the image gets rebuilt for unrelated code changes afterward. That let
# production run an untested CLI version (2.1.197) for weeks while every
# behavior in claude_cli.py's docstring and the README's "non-obvious CLI
# behavior" section was verified against 2.1.241 — a real, confirmed
# contributor to at least one class of production failure. Bump this pin
# deliberately, and re-verify those documented behaviors still hold, rather
# than letting it drift silently again.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code@2.1.241 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker ./worker

# The news-only step needs --permission-mode bypassPermissions (see
# claude_cli.py) to actually execute WebSearch/WebFetch — Claude Code refuses
# that combination when running as root, as a safety guardrail. Run as a
# non-root user so both worker.main and worker.main_news work the same way.
RUN useradd --create-home --shell /bin/bash worker \
    && mkdir -p /app/log && chown -R worker:worker /app
ENV HOME=/home/worker
USER worker

# .env, ~/.claude, and ~/.claude.json are mounted at runtime, not baked into
# the image — see README for the `docker run` command. CMD runs the
# production analysis; override with `python -m worker.main_news` to run the
# news-only step independently (its own schedule, its own container).
CMD ["python", "-m", "worker.main"]
