#!/usr/bin/env python3
"""Bug-report validator (the "validator" stage).

Given a bug report (from detect.py or any compatible source), Claude Code reads
the real source in the codebase and decides whether the report is a true
positive (would actually trigger in production) or a false positive, following
the rubric in prompt/validator.md.

Usage:
    # validate a whole detector report
    python validator.py \
        --report bugs.json \
        --codebase /path/to/repo \
        --model claude-sonnet-4-5 \
        --output validated.json

    # validate a single inline bug report
    python validator.py --bug '{"file":"x.go","line":42,"comment":"..."}' \
        --codebase /path/to/repo --model claude-sonnet-4-5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from claude_runner import ClaudeRunError, extract_json, run_claude
from prompts import load_raw


def build_validation_prompt(bug: dict) -> str:
    """Compose the validator instruction with the concrete bug report.

    validator.md is a pure rubric (no placeholders); we append the report
    under test and tell the agent to read the cited source before deciding.
    """
    instruction = load_raw("validator.md")
    report_json = json.dumps(bug, indent=2)

    return f"""{instruction}

---

## Bug Report Under Validation

```json
{report_json}
```

The codebase is your current working directory. Use the Read and Grep tools to
open `{bug.get("file", "<unknown file>")}` around line {bug.get("line", "?")},
trace callers and data flow as the rubric requires, then output your verdict in
the exact JSON format specified above.
"""


def validate_bug(
    bug: dict,
    codebase: Path,
    model: str,
    *,
    timeout: int,
) -> dict:
    """Return the validator verdict dict for a single bug report."""
    prompt = build_validation_prompt(bug)
    result = run_claude(prompt, cwd=codebase, model=model, timeout=timeout)
    verdict = extract_json(result.text)
    if not isinstance(verdict, dict):
        raise ClaudeRunError("validator did not return a JSON object")
    return verdict


def _load_reports(args) -> list[dict]:
    """Normalize CLI inputs into a flat list of bug-report dicts."""
    if args.bug:
        return [json.loads(args.bug)]

    data = json.loads(Path(args.report).read_text())
    if isinstance(data, dict):
        if isinstance(data.get("bugs"), list):  # detect.py report shape
            return data["bugs"]
        return [data]  # a single bug object
    if isinstance(data, list):
        return data
    raise ValueError("unrecognized --report shape")


def run_validation(
    reports: list[dict],
    codebase: str,
    model: str,
    *,
    timeout: int,
    verbose: bool = True,
) -> dict:
    codebase_path = Path(codebase).resolve()
    validated: list[dict] = []
    counts = {"TP": 0, "FP": 0, "ERROR": 0}

    for i, bug in enumerate(reports, 1):
        loc = f"{bug.get('file', '?')}:{bug.get('line', '?')}"
        if verbose:
            print(f"({i}/{len(reports)}) validating {loc} ...", file=sys.stderr)
        try:
            verdict = validate_bug(bug, codebase_path, model, timeout=timeout)
        except ClaudeRunError as exc:
            verdict = {"verdict": "ERROR", "reason": str(exc)}
        label = verdict.get("verdict", "ERROR")
        counts[label] = counts.get(label, 0) + 1
        if verbose:
            print(
                f"    -> {label} ({verdict.get('confidence', '?')}): "
                f"{verdict.get('reason', '')}",
                file=sys.stderr,
            )
        validated.append({"bug": bug, "validation": verdict})

    return {
        "model": model,
        "codebase": str(codebase_path),
        "summary": counts,
        "results": validated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Bug-report TP/FP validator")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--report", help="detect.py report JSON, or a list/single bug")
    src.add_argument("--bug", help="a single inline bug report as a JSON string")
    ap.add_argument("--codebase", required=True, help="path to the codebase root")
    ap.add_argument("--model", required=True, help="model name")
    ap.add_argument("--output", help="write the validation report JSON here")
    ap.add_argument("--timeout", type=int, default=1200, help="per-report claude timeout (s)")
    args = ap.parse_args()

    reports = _load_reports(args)
    report = run_validation(reports, args.codebase, args.model, timeout=args.timeout)

    out = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(out)
        print(f"\nSummary: {report['summary']} -> {args.output}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
