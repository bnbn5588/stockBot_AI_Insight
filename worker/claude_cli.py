"""Invokes the locally-authenticated `claude` CLI instead of the Anthropic API,
so generation rides on the Claude subscription already logged into this
machine/container rather than pay-per-use API billing.

Two non-obvious things this depends on (found by trial and error against CLI
v2.1.241 — verify again if the CLI version changes):

1. The prompt MUST be piped via stdin, not passed as the positional `prompt`
   argument. With the positional argument, Claude Code's headless routing only
   sees the opening sentence of a multi-paragraph prompt and responds asking
   for the "missing" data — even though the full text was passed. Piping the
   identical text via stdin makes it see the whole message.
2. The subprocess is run from an isolated, empty `cwd` (not this project's
   directory). Even with tool access disabled, Claude Code's default system
   prompt includes static directory-listing/env context, which otherwise
   leaks into the response (e.g. it name-drops random files it "noticed").

`get_analysis` disables tool access entirely (`--tools ""`) — otherwise
Claude Code runs its normal agentic coding-assistant loop. `get_news_highlights`
allows only WebSearch/WebFetch, for the news-only prompt (prompt_news.py) —
that reintroduces multi-turn tool-use behavior, so treat it as less
predictable/slower than the signal-only path until proven out.

`--json-schema` forces the reply to match the given schema exactly, returned
pre-parsed in the envelope's `structured_output` field.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Tuple

DEFAULT_TIMEOUT_SECONDS = 300
NEWS_TIMEOUT_SECONDS = 480  # web search adds turns/latency

_TICKER_ITEM_SCHEMA = {
    "type": "object",
    "properties": {"ticker": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["ticker", "reason"],
}

ANALYSIS_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "generatedAt": {"type": "string"},
        "marketSummary": {"type": "string"},
        "topPicks": {"type": "array", "items": _TICKER_ITEM_SCHEMA},
        "riskWatch": {"type": "array", "items": _TICKER_ITEM_SCHEMA},
        "portfolioNote": {"type": "string"},
    },
    "required": ["generatedAt", "marketSummary", "topPicks", "riskWatch", "portfolioNote"],
}

_NEWS_HIGHLIGHT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string"},
        "summary": {"type": "string"},
        "source": {"type": "string"},
        "publishedDate": {"type": "string"},
        "recommendation": {"type": "string"},
    },
    "required": ["ticker", "summary", "source", "publishedDate", "recommendation"],
}

NEWS_ONLY_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "generatedAt": {"type": "string"},
        "newsHighlights": {"type": "array", "items": _NEWS_HIGHLIGHT_ITEM_SCHEMA},
    },
    "required": ["generatedAt", "newsHighlights"],
}

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class ClaudeCLIError(RuntimeError):
    pass


def _find_claude_binary() -> str:
    path = shutil.which("claude")
    if not path:
        raise ClaudeCLIError(
            "`claude` CLI not found on PATH. Install Claude Code and log in with an "
            "active subscription on this machine/container before running this job."
        )
    return path


def _clean_env() -> Dict[str, str]:
    """Strips Claude-Code-session markers (CLAUDECODE, CLAUDE_CODE_*, CLAUDE_*)
    so the child process never detects itself as a child/subagent of whatever
    process happens to be running this script — a real scenario if this worker
    is ever run manually from inside a Claude Code terminal, and otherwise a
    harmless no-op on a clean cron-launched process."""
    return {
        k: v for k, v in os.environ.items()
        if not (k == "CLAUDECODE" or k.startswith("CLAUDE_"))
    }


def _extract_usage(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Token/cost accounting from the CLI's JSON envelope. totalCostUsd is the
    equivalent API-billed value Claude Code always reports — it does not mean
    this call was actually billed per-token; subscription usage counts
    against the plan instead."""
    usage = envelope.get("usage") or {}
    return {
        "inputTokens": usage.get("input_tokens", 0),
        "outputTokens": usage.get("output_tokens", 0),
        "cacheCreationInputTokens": usage.get("cache_creation_input_tokens", 0),
        "cacheReadInputTokens": usage.get("cache_read_input_tokens", 0),
        "thinkingTokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
        "totalCostUsd": envelope.get("total_cost_usd", 0.0),
    }


def _run_cli(
    prompt: str,
    schema: Dict[str, Any],
    tools: str,
    timeout_seconds: int,
    bypass_permissions: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    binary = _find_claude_binary()

    cmd = [
        binary, "-p",
        "--output-format", "json",
        "--tools", tools,
        "--json-schema", json.dumps(schema),
    ]
    if bypass_permissions:
        # Safe here only because `tools` is restricted to non-destructive
        # tools (WebSearch/WebFetch) by the caller — never pass this with
        # Bash/Edit/Write allowed.
        cmd += ["--permission-mode", "bypassPermissions"]

    with tempfile.TemporaryDirectory(prefix="ai-analysis-cli-") as isolated_cwd:
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                env=_clean_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
                cwd=isolated_cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCLIError(f"claude CLI timed out after {timeout_seconds}s") from exc

    if proc.returncode != 0:
        raise ClaudeCLIError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}")

    stdout = proc.stdout.strip()
    if not stdout:
        raise ClaudeCLIError("claude CLI returned empty output")

    try:
        envelope: Dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCLIError(f"claude CLI output was not valid JSON: {exc}") from exc

    if envelope.get("is_error"):
        raise ClaudeCLIError(f"claude CLI reported an error: {envelope.get('result')}")

    usage = _extract_usage(envelope)

    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured, usage

    # Fall back to extracting JSON from the plain result text — older/newer
    # CLI versions, or --json-schema not honored for some reason.
    text = envelope.get("result")
    if not isinstance(text, str) or not text:
        raise ClaudeCLIError("No structured_output or result text in claude CLI response")

    match = _JSON_RE.search(text)
    if not match:
        raise ClaudeCLIError("Could not extract JSON from Claude response")
    return json.loads(match.group(0)), usage


def get_analysis(prompt: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Runs prompt through `claude -p` (headless, no tool access, schema-
    constrained output) and returns (analysis dict, token usage dict)."""
    return _run_cli(prompt, ANALYSIS_JSON_SCHEMA, tools="", timeout_seconds=timeout_seconds)


def get_news_highlights(prompt: str, timeout_seconds: int = NEWS_TIMEOUT_SECONDS) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Runs prompt through `claude -p` with WebSearch/WebFetch allowed (no
    filesystem/Bash access), returning ({generatedAt, newsHighlights}, token
    usage dict). Slower and less predictable than get_analysis — tool use
    reintroduces multi-turn behavior."""
    return _run_cli(
        prompt, NEWS_ONLY_JSON_SCHEMA, tools="WebSearch,WebFetch",
        timeout_seconds=timeout_seconds, bypass_permissions=True,
    )
