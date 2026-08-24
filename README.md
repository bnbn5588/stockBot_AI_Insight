# stockBot AI Insight — standalone analysis worker

Replaces the Claude call inside `stock-dashboard`'s `GET /api/ai-analysis` route with a
standalone Python worker. Same Google Sheet, same computed metrics, same prompt — but
generation runs through the locally-authenticated **Claude Code CLI** (billed against a
Claude subscription) instead of the pay-per-use Anthropic API, and the result is written
into the same Redis cache the Next.js frontend already reads from. No frontend changes
required.

See [ai-analysis-prompt-logic.md](ai-analysis-prompt-logic.md) for the original
route.ts prompt/API spec this was ported from.

## How it fits with `stock-dashboard`

```
Google Sheet (SHEET_ID)                 stock-dashboard (Next.js)
        │                                        │
        ▼                                        │
┌───────────────────┐     writes      ┌──────────▼──────────┐
│  this worker       │ ───────────────▶│  Redis               │◀── reads (GET /api/ai-analysis)
│  (cron, daily)      │                │  ai-analysis:{date}  │
└───────────────────┘                 └──────────────────────┘
```

`stock-dashboard`'s `route.ts` still gates its Redis read behind a truthy
`ANTHROPIC_API_KEY` check (it happens *before* the cache check). Until that route is
simplified to a pure Redis reader, keep `ANTHROPIC_API_KEY` set to any non-empty dummy
string in that app's env, or the frontend will 503 before ever looking at what this
worker wrote.

## Project layout

```
worker/
  analytics.py      Port of stock-dashboard/src/lib/analytics.ts — CSV parsing, streaks,
                     trade stats, forward returns, portfolio simulation. Only the pieces
                     the prompt actually needs.
  prompt.py          Builds the exact signal-only prompt (byte-for-byte match to the
                     original route.ts template). _build_data_sections() is shared with
                     prompt_news.py.
  prompt_news.py     News-only prompt — does NOT re-derive marketSummary/topPicks/
                     riskWatch/portfolioNote. Takes ticker+reason candidates already
                     flagged by main.py's cached output and asks only for news on those.
  claude_cli.py      Shells out to the `claude` CLI instead of the Anthropic SDK.
  redis_client.py    Redis key names + TTL.
  main.py            Production entry point — signal-only analysis, writes to
                     ai-analysis:{date} (the key stock-dashboard reads).
  main_news.py       Manual/experimental follow-on step — reads today's cached
                     ai-analysis:{date}, searches news only for its topPicks/riskWatch/
                     signal-change tickers, writes newsHighlights to
                     ai-analysis-news:{date} and the prompt to
                     ai-analysis-news-prompt:{date} (separate keys, not read by the
                     frontend). Requires main.py to have already run for today.
requirements.txt
.env.example
run.sh               Native-path cron wrapper: loads .env, runs `python3 -m worker.main`.
Dockerfile            Docker-path image: Python worker + Node.js + the `claude` CLI.
.dockerignore
```

## Setup

Both deployment paths need `claude` logged in once with an active subscription — the
worker shells out to it, so it must be on `PATH` and authenticated for whichever
account actually runs the job:
```
npm install -g @anthropic-ai/claude-code
claude   # interactive login — must be an active Claude subscription (Pro/Max),
         # not an ANTHROPIC_API_KEY, or you're back to pay-per-use billing.
```

Then `cp .env.example .env` and fill in:
- `SHEET_ID` — same Google Sheet ID as `stock-dashboard`.
- `REDIS_URL` — same Redis instance as `stock-dashboard`.
- `CLAUDE_MODEL_LABEL` — optional, cosmetic only (stored in the result's `model` field;
  the frontend just displays it verbatim).

From here, pick one of the two paths below.

### Native (Python installed directly on the host)

1. `pip install -r requirements.txt` (a venv is recommended).
2. Manual run (writes to Redis if `REDIS_URL` is set, otherwise prints the result):
   ```
   python -m worker.main
   ```
3. Schedule it: `run.sh` loads `.env` (cron's own environment is otherwise empty) and
   runs the worker. Add a crontab entry for whichever user is logged into `claude`:
   ```
   35 8 * * * /path/to/stockBot_AI_Insight/run.sh >> /var/log/ai-analysis.log 2>&1
   ```

### Docker (host Python/pip stays untouched)

The worker's Python dependencies live entirely inside the image. `claude` still has to
run *inside* the container too (it's a subprocess of the worker), so the image bundles
Node.js + the CLI — but its login is interactive and can't happen at build time, so the
container reuses the login already completed on the host by bind-mounting `~/.claude`
in at runtime.

The container runs as a non-root `worker` user (`HOME=/home/worker`), not root —
required because the news-only step's `--permission-mode bypassPermissions` (needed to
actually execute WebSearch/WebFetch, see `claude_cli.py` point 4 below) is refused by
the CLI when running as root, as a safety guardrail. That means credentials mount to
`/home/worker/...`, not `/root/...`.

1. Run `npm install -g @anthropic-ai/claude-code` and `claude` (the login step above)
   directly on the server — this only touches Node/npm, not Python.
2. Build the image:
   ```
   docker build -t ai-analysis-worker .
   ```
3. Manual run. Three things to get right:
   - Mount `.env` as a file, not `--env-file` — Docker's `--env-file` doesn't strip
     quotes, and `.env` values like `REDIS_URL="redis://..."` need that; the worker's
     `load_dotenv()` handles it correctly when `.env` is mounted as a file instead.
   - Mount **both** `~/.claude` (directory) and `~/.claude.json` (a separate file at
     the home directory root) — Claude Code's config isn't only in the directory.
   - Replace `~` below with whichever host user actually ran `claude` login (e.g.
     `/home/ubuntu/.claude` if not root).
   ```
   docker run --rm \
     -v /path/to/stockBot_AI_Insight/.env:/app/.env:ro \
     -v ~/.claude:/home/worker/.claude \
     -v ~/.claude.json:/home/worker/.claude.json \
     ai-analysis-worker
   ```
   If the mounted files aren't readable/writable by the container (permission denied,
   or `.claude.json` "not found" despite the mount) — the bind-mounted files keep the
   host's UID/GID, which may not match the container's `worker` user. Add
   `--user $(id -u):$(id -g)` to the `docker run` command to run the container as the
   same host user that owns those files instead.
4. Schedule it — same `docker run` command in crontab:
   ```
   35 8 * * * docker run --rm -v /path/to/stockBot_AI_Insight/.env:/app/.env:ro -v /home/ubuntu/.claude:/home/worker/.claude -v /home/ubuntu/.claude.json:/home/worker/.claude.json ai-analysis-worker >> /var/log/ai-analysis.log 2>&1
   ```
5. Rebuild (`docker build -t ai-analysis-worker .`) whenever `worker/*.py` or
   `requirements.txt` changes — the running container won't pick up code changes on
   its own.

Adjust the schedule time in either path to whenever the sheet actually updates for you
(currently set to 5 minutes after the assumed 08:30 update).

### News-only step (experimental, not scheduled)

Run *after* the production analysis for today exists — this step reads its
topPicks/riskWatch tickers rather than re-deriving them. Native:
```
python -m worker.main_news
```
Docker (same mounts as above, overriding the default `CMD`):
```
docker run --rm -v /path/to/.env:/app/.env:ro -v ~/.claude:/home/worker/.claude -v ~/.claude.json:/home/worker/.claude.json ai-analysis-worker python -m worker.main_news
```
Same cache-skip behavior, writing to its own `ai-analysis-news:{date}` key — never
touches the key the frontend reads. Errors out if today's `ai-analysis:{date}` isn't
cached yet. See "Two analyses, not overlapping" below before deciding whether to chain
this into the daily schedule.

## Redis keys

All four share the same 25h TTL (`CACHE_TTL_SECONDS` in `redis_client.py`, matching
route.ts's original `EX 90000`) so nothing bleeds into the next trading day.

| Key                             | Written by     | Contents                                                              |
|-----------------------------------|----------------|-------------------------------------------------------------------------|
| `ai-analysis:{date}`             | `main.py`      | The production result — `generatedAt`, `marketSummary`, `topPicks`, `riskWatch`, `portfolioNote`, `model`, `fetchedAt`, `prompt`. **This is what `stock-dashboard` reads.** |
| `ai-analysis-prompt:{date}`      | `main.py`      | The same prompt string, stored on its own key for easy inspection without pulling the whole result. |
| `ai-analysis-news:{date}`        | `main_news.py` | `generatedAt`, `newsHighlights` (each with a per-item `recommendation`), `model`, `fetchedAt`, `prompt`. Not read by the frontend. |
| `ai-analysis-news-prompt:{date}` | `main_news.py` | The news-only prompt string, stored on its own key. |

## Two analyses, not overlapping

Each analysis has exactly one job:

**Signal-only (`prompt.py`, production, `main.py`).** Pure quant analysis over the
sheet's historical signals — streaks, win rate, expectancy, forward returns, portfolio
simulation vs. buy-and-hold. Explicitly forbidden from using outside information ("Do
not invent data not present above"). No tool access at all (`claude_cli.get_analysis`)
— fast, no external dependencies beyond the sheet. Produces `topPicks`/`riskWatch` —
the single source of truth for which tickers matter today and why.

**News-only (`prompt_news.py`, experimental, `main_news.py`).** Runs *after* the
signal-only analysis and depends on its output: reads `topPicks` + `riskWatch` +
any signal-change ticker from the cached `ai-analysis:{date}`, and asks Claude to
search news for exactly those tickers — nothing else, and it's explicitly told the
quant reasoning is already done and not its job to repeat. Returns only
`newsHighlights` (`ticker`, `summary`, `source`, `publishedDate`, `recommendation`).
`recommendation` is scoped to that one ticker's news — whether it reinforces, tempers,
or contradicts the specific flagged reason (expectancy, streak, confidence) — not a
standalone buy/sell call or a re-derived `portfolioNote`. Rules against fabrication:
only tickers from the flagged list, real source/date from an actual search result, omit
(don't stub) a ticker with no relevant news, no new quantitative figures invented in the
recommendation.

This keeps candidate selection deterministic (Python-computed from the already-cached
analysis) rather than having the model re-guess which tickers matter, so the two layers
can't drift apart or contradict each other, and neither call repeats the other's work.

Tradeoff: `claude_cli.get_news_highlights` allows `WebSearch`/`WebFetch` (never
Bash/Edit/Write) and runs several turns instead of 1, so it's noticeably slower than the
tool-free signal-only path and has a different reliability profile. Compare a few days
of output before deciding whether to chain it into the daily schedule.

## `claude_cli.py` — non-obvious CLI behavior

Found by trial and error against Claude Code CLI v2.1.241 running on Windows; re-verify
if the CLI version changes.

1. **Prompt must go via stdin, not the positional argument.** Passing a multi-paragraph
   prompt as `claude -p "<prompt>"` makes the CLI's headless routing see only the
   opening sentence and respond asking for the "missing" data — even though the full
   text was passed. Piping the identical text via stdin (`subprocess.run(..., input=prompt)`)
   fixes it.
2. **Run from an isolated, empty `cwd`.** Even with all tools disabled, Claude Code's
   default system prompt includes static directory-listing/environment context, which
   leaks into responses (e.g. it name-drops unrelated files it "noticed" in the working
   directory). Each call runs in a fresh `tempfile.TemporaryDirectory()`.
3. **Strip `CLAUDE_*`/`CLAUDECODE` env vars before spawning.** If this worker is ever
   run manually from inside an active Claude Code session/terminal, the subprocess
   inherits session markers (`CLAUDECODE=1`, `CLAUDE_CODE_CHILD_SESSION=1`, etc.) and
   detects itself as a child/subagent, producing noticeably lazier, lower-effort output.
   `_clean_env()` filters these out. Harmless no-op on a clean cron-launched process.
4. **`--tools "X,Y"` allows tools but doesn't grant execution permission.** In headless
   print mode, tool calls still go through a permission check and get silently denied
   without a human present to approve them (visible via `permission_denials` in the raw
   JSON envelope). `get_news_highlights` adds `--permission-mode bypassPermissions` —
   safe here specifically because `tools` is restricted to non-destructive
   `WebSearch`/`WebFetch`; never combine that flag with Bash/Edit/Write access. The CLI
   also refuses this flag outright when running as root — the Docker image runs as a
   non-root `worker` user for exactly this reason.
5. **`--json-schema` + `--output-format json`** returns a pre-parsed, schema-validated
   dict in the envelope's `structured_output` field — no markdown fences, no prose to
   regex out. `_run_cli()` falls back to regex-extracting JSON from the `result` text
   field if `structured_output` isn't present, for CLI-version robustness.

## Known gaps / follow-ups

- `stock-dashboard/route.ts` still needs simplifying to a pure Redis reader (drop the
  Anthropic SDK call and the `ANTHROPIC_API_KEY` gate).
- News-only step isn't wired into any schedule; it's a manual follow-on tool for now
  (`python -m worker.main_news`, run after `worker.main`).
- Docker path requires `claude`'s login to happen on the host first (Node/npm on the
  host, separate from the containerized Python) — the OAuth flow is interactive and
  can't run inside a container build step, so the container reuses the host's
  credentials via a bind mount rather than logging in itself.
- Docker image isn't auto-rebuilt on code changes — `docker build` has to be rerun
  manually after editing `worker/*.py` or `requirements.txt`.
