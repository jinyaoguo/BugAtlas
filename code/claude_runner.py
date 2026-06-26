"""Thin wrapper around the Claude Code CLI for headless (non-interactive) runs.

The detector, validator, and spec initializer all drive Claude Code the same
way: spawn `claude -p <prompt>` inside the target codebase so the agent can use
its Read / Grep / Bash tools to inspect the real source, then parse the result.

Two output modes:
  * default (`--output-format json`)      -> just the final answer (run_claude)
  * trajectory (`--output-format stream-json --verbose`) -> the full sequence of
    tool calls + reasoning, needed by spec initialization (capture_trajectory=True)
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Tools the agent is allowed to use. The prompts only ask the model to *read*
# the codebase, so we keep the surface read-only.
DEFAULT_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash"]

# Truncation limits when rendering a trajectory into a human-readable transcript.
_MAX_TOOL_RESULT_CHARS = 1500
_MAX_TEXT_CHARS = 4000


class ClaudeRunError(RuntimeError):
    """Raised when the Claude Code CLI fails or returns unusable output."""


@dataclass
class ClaudeResult:
    text: str                                   # the assistant's final text
    raw: dict[str, Any]                         # the final result envelope
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    trajectory: list[dict] = field(default_factory=list)  # raw stream events
    transcript: str = ""                        # rendered, human-readable trace


def _base_cmd(prompt: str, model: str, allowed_tools: Optional[list[str]]) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--allowedTools",
        ",".join(allowed_tools or DEFAULT_ALLOWED_TOOLS),
    ]


def run_claude(
    prompt: str,
    *,
    cwd: str | Path,
    model: str,
    allowed_tools: Optional[list[str]] = None,
    timeout: int = 1200,
    extra_args: Optional[list[str]] = None,
    capture_trajectory: bool = False,
) -> ClaudeResult:
    """Run Claude Code headless inside `cwd` and return the parsed result.

    `cwd` should be the codebase root so the agent's Read/Grep tools resolve
    relative paths against the project under analysis. When `capture_trajectory`
    is True, the full tool-call/reasoning trace is captured in `.trajectory`
    (raw events) and `.transcript` (rendered text).
    """
    cwd = Path(cwd).resolve()
    if not cwd.is_dir():
        raise ClaudeRunError(f"codebase path is not a directory: {cwd}")

    cmd = _base_cmd(prompt, model, allowed_tools)
    if capture_trajectory:
        cmd += ["--output-format", "stream-json", "--verbose"]
    else:
        cmd += ["--output-format", "json"]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ClaudeRunError(
            "`claude` CLI not found on PATH. Install Claude Code first."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeRunError(f"claude timed out after {timeout}s") from exc

    if proc.returncode != 0:
        raise ClaudeRunError(
            f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )

    if capture_trajectory:
        events, envelope = _parse_stream(proc.stdout)
    else:
        events = []
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeRunError(
                f"could not parse claude JSON output: {proc.stdout[:500]}"
            ) from exc

    text = envelope.get("result", "")
    if envelope.get("is_error"):
        raise ClaudeRunError(f"claude reported an error: {text[:500]}")

    return ClaudeResult(
        text=text,
        raw=envelope,
        cost_usd=envelope.get("total_cost_usd"),
        duration_ms=envelope.get("duration_ms"),
        trajectory=events,
        transcript=render_transcript(events) if capture_trajectory else "",
    )


def _parse_stream(stdout: str) -> tuple[list[dict], dict]:
    """Parse stream-json (JSONL) output into (events, final_result_envelope)."""
    events: list[dict] = []
    envelope: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(obj)
        if obj.get("type") == "result":
            envelope = obj
    if not envelope:
        raise ClaudeRunError("stream-json output contained no result event")
    return events, envelope


def _truncate(s: str, limit: int) -> str:
    s = s if isinstance(s, str) else str(s)
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} chars]"


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", "") or block.get("content", ""))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content)


def render_transcript(events: list[dict]) -> str:
    """Render stream events into a compact, readable agent transcript.

    Captures the agent's reasoning (text), the tools it invoked (tool_use with
    inputs), and truncated tool results — i.e. what the agent looked at and
    concluded. This is what the spec-synthesis phase reads.
    """
    lines: list[str] = []
    for ev in events:
        etype = ev.get("type")
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    lines.append("[assistant] " + _truncate(block["text"].strip(), _MAX_TEXT_CHARS))
                elif btype == "tool_use":
                    inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                    lines.append(f"[tool_use] {block.get('name')}({_truncate(inp, 600)})")
        elif etype == "user":
            for block in ev.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    txt = _tool_result_text(block.get("content", ""))
                    lines.append("[tool_result] " + _truncate(txt, _MAX_TOOL_RESULT_CHARS))
        elif etype == "result":
            lines.append("[final] " + _truncate(ev.get("result", ""), _MAX_TEXT_CHARS))
    return "\n".join(lines)


_JSON_BLOCK = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Extract the first ```json ...``` block (or a bare JSON object) from text."""
    for block in _JSON_BLOCK.findall(text):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ClaudeRunError("no parseable JSON found in claude response")
