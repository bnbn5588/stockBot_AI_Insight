# stockBot AI Insight — standalone analysis worker

Fetches a Google Sheet of algorithmic trading signal history, computes portfolio
analytics (streaks, win rate, expectancy, forward returns, portfolio simulation vs.
buy-and-hold), and generates an AI analysis of the current signal environment via the
locally-authenticated **Claude Code CLI** — billed against a Claude subscription, not
pay-per-use API calls. Results are written to Redis.

See [ai-analysis-prompt-logic.md](ai-analysis-prompt-logic.md) for the exact prompt
template and JSON response contract.

## Project layout

```
worker/
  analytics.py      CSV parsing, streaks, trade stats, forward returns, portfolio
                     simulation.
  prompt.py          Builds the signal-only prompt. _build_data_sections() is shared
                     with prompt_news.py.
  prompt_news.py     News-only prompt — does NOT re-derive marketSummary/topPicks/
                     riskWatch/portfolioNote. Takes ticker+reason candidates already
                     flagged by main.py's cached output and asks only for news on those.
  claude_cli.py      Shells out to the `claude` CLI instead of the Anthropic SDK.
  redis_client.py    Redis key names + TTL.
  logging_setup.py   Shared logger — writes to log/worker.log (rotated) + stdout.
  main.py            Production entry point — signal-only analysis, writes to
                     ai-analysis:{date} and ai-analysis-prompt:{date}.
  main_news.py       News-only follow-on step — independently run/scheduled from
                     main.py. Reads today's cached ai-analysis:{date}, searches news
                     only for its topPicks/riskWatch/signal-change tickers, writes
                     newsHighlights to ai-analysis-news:{date} and the prompt to
                     ai-analysis-news-prompt:{date}. Requires main.py to have already
                     run for today.
requirements.txt
.env.example
run.sh               Native-path cron wrapper: loads .env, runs `python3 -m worker.main`.
Dockerfile            Docker-path image: Python worker + Node.js + the `claude` CLI.
.dockerignore
log/                  Generated at runtime by logging_setup.py — gitignored.
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
- `SHEET_ID` — Google Sheet ID containing the signal history (7-column, no header row —
  see `analytics.py`'s `parse_csv` for the exact format).
- `REDIS_URL` — Redis instance to read/write results.
- `CLAUDE_MODEL_LABEL` — optional, cosmetic only (stored in the result's `model` field).
- `NEWS_LOOKBACK_DAYS` — `worker.main_news` only, default `2`. How far back its web
  search looks for news. Keep it short if you're running that step several times a
  day — a wide window just re-returns the same older headlines on every run.

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

Adjust the schedule time to whenever the sheet actually updates for you (currently set
to 5 minutes after the assumed 08:30 update).

### News-only step

Independent of `worker.main` — its own command, its own schedule, its own container.
Native:
```
python -m worker.main_news
```
Docker (same mounts as above, overriding the default `CMD`):
```
docker run --rm -v /path/to/.env:/app/.env:ro -v ~/.claude:/home/worker/.claude -v ~/.claude.json:/home/worker/.claude.json ai-analysis-worker python -m worker.main_news
```
Add a separate crontab line for whatever cadence you want it on (independent of
`worker.main`'s schedule). Unlike `worker.main`, this step has no cache-skip — every
run always regenerates and overwrites `ai-analysis-news:{date}` (resetting its TTL),
so it's safe to run several times a day for fresher news; it just always calls Claude,
there's no "already done for today" shortcut.

## Logging

Every run logs to `log/worker.log` (relative to the project root) *and* stdout, so
existing `>> /var/log/ai-analysis.log` cron redirection keeps working unchanged while
also getting a persistent, structured file. Rotated at midnight, 14 days kept. Override
the location with the `LOG_DIR` env var.

Each run logs: start/finish with duration, ticker count parsed from the sheet,
cache hit/skip, `topPicks`/`riskWatch` tickers (or news candidates) produced, token
usage and cost per call, and full tracebacks on failure (`exc_info=True`) — enough to
diagnose a bad run without re-running it.

On the Docker path, `log/` is inside the container's ephemeral filesystem by default and
disappears with `--rm`. Mount it to persist across runs:
```
docker run --rm \
  -v /path/to/stockBot_AI_Insight/.env:/app/.env:ro \
  -v ~/.claude:/home/worker/.claude \
  -v ~/.claude.json:/home/worker/.claude.json \
  -v /path/to/stockBot_AI_Insight/log:/app/log \
  ai-analysis-worker
```

## Redis keys

All four share the same 25h TTL (`CACHE_TTL_SECONDS` in `redis_client.py`) so nothing
bleeds into the next trading day.

| Key                             | Written by     | Contents                                                              |
|-----------------------------------|----------------|-------------------------------------------------------------------------|
| `ai-analysis:{date}`             | `main.py`      | `generatedAt`, `marketSummary`, `topPicks`, `riskWatch`, `portfolioNote`, `model`, `fetchedAt`, `tokenUsage`. No `prompt` field — it lives only in `ai-analysis-prompt:{date}` instead of being duplicated. |
| `ai-analysis-prompt:{date}`      | `main.py`      | The prompt string, stored on its own key for easy inspection without pulling the whole result. |
| `ai-analysis-news:{date}`        | `main_news.py` | `generatedAt`, `newsHighlights` (each with a per-item `recommendation`), `model`, `fetchedAt`, `tokenUsage`. No `prompt` field, same reasoning — lives in `ai-analysis-news-prompt:{date}` instead. |
| `ai-analysis-news-prompt:{date}` | `main_news.py` | The news-only prompt string, stored on its own key. |

`tokenUsage` (both result keys): `inputTokens`, `outputTokens`,
`cacheCreationInputTokens`, `cacheReadInputTokens`, `thinkingTokens`, `totalCostUsd`.
`totalCostUsd` is the equivalent API-billed value Claude Code always reports in its
output — it does not mean the call was actually billed per-token; subscription usage
counts against the plan instead (see `claude_cli._extract_usage`).

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
tool-free signal-only path and has a different reliability profile. Run it manually a
few times and compare output before putting it on any schedule.

## Sample prompts and output

Real captures from a live run against the actual sheet — not hand-written examples.

### Signal-only (`prompt.py` → `ai-analysis:{date}`)

Prompt sent to Claude:
```
You are reviewing algorithmic stock signals generated by a systematic trading bot. Today is 2026-08-23.

Portfolio: 13 tickers | BUY: 2 | SELL: 2 | HOLD: 9
No signal changes since yesterday.

Portfolio performance (equal-weight simulation):
Strategy +24.4% vs buy-and-hold +34.2% (equal-weight across all tickers)

Current snapshot (ticker | signal | price | streak | period_return | confidence% | ADX | win_rate/expectancy):
AAPL  | HOLD | $  309.35 | streak=HOLD×4d | period=+22.6%  | conf= 32 | adx= 25 | wr=50% exp=+1.1%
AMD   | HOLD | $  473.25 | streak=HOLD×24d | period=+103.0% | conf= 28 | adx= 15 | wr=64% exp=+6.9%
AMZN  | HOLD | $  258.63 | streak=HOLD×10d | period=+21.4%  | conf= 36 | adx= 20 | wr=20% exp=-4.3%
ASML  | HOLD | $ 1763.76 | streak=HOLD×4d | period=-8.6%   | conf= 24 | adx= 19 | no_completed_trades
CDE   | BUY  | $   20.97 | streak=BUY×3d  | period=-3.9%   | conf= 47 | adx= 40 | wr=50% exp=-2.3%
GOOGL | HOLD | $  344.82 | streak=HOLD×25d | period=+36.1%  | conf= 36 | adx= 11 | wr=75% exp=+7.9%
META  | SELL | $  621.71 | streak=SELL×1d | period=+0.0%   | conf=  0 | adx=  0 | no_completed_trades
MSFT  | BUY  | $  483.24 | streak=BUY×3d  | period=-5.9%   | conf= 42 | adx= 35 | wr=33% exp=-4.9%
MU    | HOLD | $  966.78 | streak=HOLD×5d | period=+306.3% | conf= 32 | adx= 16 | wr=83% exp=+19.3%
NVDA  | HOLD | $  214.72 | streak=HOLD×5d | period=+17.2%  | conf= 32 | adx= 26 | wr=14% exp=-2.6%
ORCL  | HOLD | $  146.47 | streak=HOLD×6d | period=-49.7%  | conf= 32 | adx= 25 | wr=50% exp=+3.3%
RXT   | SELL | $    3.32 | streak=SELL×6d | period=-40.7%  | conf= 47 | adx= 28 | wr=0% exp=-48.7%
TSM   | HOLD | $  418.95 | streak=HOLD×2d | period=+46.2%  | conf= 32 | adx= 19 | wr=67% exp=+5.6%

Signal effectiveness — avg next-trading-day % return across all historical data:
BUY: +0.148% (n=1293) | SELL: +0.146% (n=944) | HOLD: -0.002% (n=806)

Respond with ONLY this JSON object (no markdown fences, no text outside the JSON):
{
  "generatedAt": "2026-08-23",
  "marketSummary": "2-3 sentences on the overall signal environment — reference the BUY/SELL/HOLD split, whether the strategy is beating buy-and-hold, and any notable signal changes today",
  "topPicks": [{"ticker": "TICK", "reason": "specific, data-backed reason in one sentence"}],
  "riskWatch": [{"ticker": "TICK", "reason": "specific, data-backed concern in one sentence"}],
  "portfolioNote": "1-2 sentences on portfolio health, strategy vs buy-and-hold, or actionable guidance"
}

Rules:
- topPicks: 1–3 tickers with the strongest BUY conviction (long BUY streak, high confidence, positive expectancy, strong ADX).
- riskWatch: 1–3 tickers on SELL signals, weak confidence, negative expectancy, or contradictions (BUY signal with negative expectancy, or SELL with positive expectancy — flag explicitly).
- Highlight tickers that just changed signal today as momentum shifts worth watching.
- Be specific: cite actual ticker names, streak lengths, confidence scores, or % figures.
- Do not invent data not present above.
```

Resulting `ai-analysis:{date}` (the prompt above is stored separately at
`ai-analysis-prompt:{date}` — not duplicated inside this object):
```json
{
  "generatedAt": "2026-08-23",
  "marketSummary": "Signals are split 2 BUY / 2 SELL / 9 HOLD across the 13-ticker portfolio, with no changes since yesterday, though META's SELL streak is only 1 day old and carries zero confidence — worth watching for a possible re-flip. The strategy is underperforming buy-and-hold (+24.4% vs +34.2%), and in aggregate HOLD signals have essentially no next-day edge (-0.002% avg) while BUY (+0.148%) and SELL (+0.146%) retain modest positive skew historically.",
  "topPicks": [
    {
      "ticker": "CDE",
      "reason": "Highest confidence (47%) and strongest trend strength (ADX 40) of any current signal on a 3-day BUY streak, though its own historical expectancy is -2.3% (wr=50%), so conviction is trend-based rather than statistically proven."
    }
  ],
  "riskWatch": [
    {
      "ticker": "RXT",
      "reason": "6-day SELL streak with a 0% historical win rate and -48.7% expectancy on a stock already down 40.7% over the period — the weakest signal quality in the portfolio."
    },
    {
      "ticker": "MSFT",
      "reason": "Currently a 3-day BUY streak despite a 33% win rate and -4.9% expectancy, a direct contradiction between the active signal and its historical performance."
    },
    {
      "ticker": "META",
      "reason": "Fresh 1-day SELL signal with 0% confidence, 0 ADX, and no completed trades to validate it — essentially an unproven, low-conviction flip."
    }
  ],
  "portfolioNote": "The strategy is trailing simple buy-and-hold by roughly 9.8 percentage points (+24.4% vs +34.2%), and both current BUY signals (CDE, MSFT) carry negative historical expectancy, so avoid treating the BUY count as a strong entry signal right now — HOLD names like MU (wr=83%, exp=+19.3%) and GOOGL (wr=75%, exp=+7.9%) show far better track records despite not being actionable signals today.",
  "model": "claude (subscription CLI)",
  "fetchedAt": "2026-08-23T03:47:13.017333+00:00",
  "tokenUsage": {
    "inputTokens": 2,
    "outputTokens": 5027,
    "cacheCreationInputTokens": 7195,
    "cacheReadInputTokens": 3900,
    "thinkingTokens": 4136,
    "totalCostUsd": 0.082057
  }
}
```

### News-only (`prompt_news.py` → `ai-analysis-news:{date}` / `ai-analysis-news-prompt:{date}`)

Prompt sent to Claude, built from that same run's `topPicks`/`riskWatch` (this is the
entire prompt — it's deliberately short, no data dump, no re-derivation). Note the
`lookback_days`-controlled window (default 2 days, `NEWS_LOOKBACK_DAYS` env var to
change it) — kept short because this step has no cache-skip and is meant to be run
several times a day; a wide window would just re-return the same older headlines on
every run instead of surfacing what's actually new since the last one:
```
Today is 2026-08-24. The tickers below were already flagged by a separate,
data-driven analysis of algorithmic trading signals (shown with why each was flagged).
Your only job here is to check for recent news on each — the quant reasoning is already
done elsewhere and is not yours to repeat or second-guess.

Flagged tickers:
- CDE (BUY): Strongest technical setup among current BUY signals — 4-day streak, highest confidence in the book at 47%, and ADX=40 signaling a firm trend, though its expectancy is still negative (-2.3%) so size accordingly.
- RXT (SELL): 7-day SELL streak with a 0% historical win rate and -48.7% expectancy alongside a -40.7% period return — the weakest signal quality in the portfolio.
- MSFT (BUY): Active BUY signal (4-day streak, conf=42) contradicts its own historical performance: 33% win rate and -4.9% expectancy, plus a -5.9% period return.
- META (SELL): SELL signal is only 1 day old with 0% confidence, ADX=0, and no completed trades to validate it — low-conviction signal that could reverse quickly.

For each ticker, use web search to check for recent news from the last 2 days only:
earnings, guidance changes, analyst actions, regulatory or legal events, and major
price-moving headlines. Skip anything older than that window, and skip routine market
commentary.

Respond with ONLY this JSON object (no markdown fences, no text outside the JSON):
{
  "generatedAt": "2026-08-24",
  "newsHighlights": [{"ticker": "TICK", "summary": "one sentence on what happened", "source": "publisher name", "publishedDate": "YYYY-MM-DD", "recommendation": "one sentence on whether this news reinforces, tempers, or contradicts the flagged signal, and why"}]
}

Rules:
- Only include tickers from the flagged list above — do not search for or add others.
- Each entry must cite a real source and an approximate date from an actual search
  result. Never fabricate a headline, source, or date.
- If a ticker has no relevant recent news, omit it — do not include a placeholder entry.
- Return an empty newsHighlights array if none of the flagged tickers have relevant news.
- recommendation must reference the specific flagged reason (e.g. its expectancy,
  streak, or confidence) alongside the news — say explicitly whether the news makes the
  existing signal more or less trustworthy. Do not invent new quantitative figures; only
  the reason text above and the news you found are available to you.
- recommendation is not a buy/sell instruction — frame it as how this changes conviction
  in the already-flagged signal, not as new standalone advice.
```

Resulting `ai-analysis-news:{date}` (the prompt above is stored separately at
`ai-analysis-news-prompt:{date}` — not duplicated inside this object). Only META had
news inside the 2-day window — CDE/RXT/MSFT's news from a few days earlier correctly
got excluded rather than re-surfaced:
```json
{
  "generatedAt": "2026-08-24",
  "newsHighlights": [
    {
      "ticker": "META",
      "summary": "Meta's stock has been pressured by a combination of massive AI capex guidance ($130-145B for 2026) and escalating legal risk, including a federal trial over child safety/addictive design where a Los Angeles jury already found the company liable and a separate $375M New Mexico penalty.",
      "source": "TipRanks",
      "publishedDate": "2026-08-22",
      "recommendation": "This is real, escalating negative news that adds substance to the SELL thesis, but the flagged signal is already only 1 day old with 0% confidence, ADX=0, and no completed trades to validate it — so while the news direction aligns with a bearish stance, it doesn't fix the signal's fundamental lack of statistical backing, meaning conviction should remain low despite the coincidental alignment."
    }
  ],
  "model": "claude (subscription CLI)",
  "fetchedAt": "2026-08-24T02:44:37.365536+00:00",
  "tokenUsage": {
    "inputTokens": 10,
    "outputTokens": 2800,
    "cacheCreationInputTokens": 30335,
    "cacheReadInputTokens": 95289,
    "thinkingTokens": 1503,
    "totalCostUsd": 0.4631168
  }
}
```

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
   field if `structured_output` isn't present, for CLI-version robustness. The same
   envelope also carries `usage`/`total_cost_usd`, captured by `_extract_usage()` into
   each result's `tokenUsage` field.

## Known gaps / follow-ups

- News-only step isn't wired into any schedule; it's a manual/independently-scheduled
  tool for now (`python -m worker.main_news`, run after `worker.main` has cached
  today's analysis).
- Docker path requires `claude`'s login to happen on the host first (Node/npm on the
  host, separate from the containerized Python) — the OAuth flow is interactive and
  can't run inside a container build step, so the container reuses the host's
  credentials via a bind mount rather than logging in itself.
- Docker image isn't auto-rebuilt on code changes — `docker build` has to be rerun
  manually after editing `worker/*.py` or `requirements.txt`.
