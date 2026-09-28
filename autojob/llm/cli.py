"""Subprocess wrapper around the local `claude` CLI.

Every call runs with `cwd` set to a throwaway directory (never this repo)
so the CLI does not pick up auto-job's own CLAUDE.md, AGENTS.md, or
.claude/ hooks while it screens jobs or researches companies. The prompt
goes in on stdin and the result comes back as `--output-format json`,
which wraps the model's text in a `"result"` field alongside metadata.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class ClaudeCliError(Exception):
    """Raised on any claude CLI failure: bad exit code, timeout, or output
    that can't be parsed. Callers should catch this and fail soft."""


def run_claude(
    prompt: str,
    *,
    model: str,
    cwd: Path,
    timeout: int,
    allowed_tools: str | None = None,
) -> str:
    """Run `claude -p --model <model> --output-format json`, prompt on
    stdin, and return the text in the response's "result" field."""
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude CLI timed out after {timeout}s") from exc
    except OSError as exc:
        raise ClaudeCliError(f"claude CLI failed to start: {exc}") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()[:500]
        raise ClaudeCliError(f"claude CLI exited {completed.returncode}: {stderr}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(f"claude CLI returned non-JSON output: {exc}") from exc
    result = payload.get("result")
    if not isinstance(result, str):
        raise ClaudeCliError("claude CLI JSON response has no 'result' string field")
    return result


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str):
    """Pull the first JSON object or array out of `text`.

    Robust to the model wrapping its answer in prose or a markdown code
    fence, both of which the "result" text sometimes contains even when
    asked for JSON only.
    """
    stripped = text.strip()
    fence_match = _FENCE_RE.search(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    start = None
    for i, ch in enumerate(stripped):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ClaudeCliError("no JSON object or array found in claude CLI output")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(stripped[start:])
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(f"could not parse JSON from claude CLI output: {exc}") from exc
    return obj
