#!/usr/bin/env python3
"""Spec initialization (the "generation" stage).

Given a bug report (code location + bug description) and a codebase, produce a
detection spec. The procedure mirrors the paper's three-step design:

  Phase 1 — two independent Claude Code agents analyze the case (in parallel):
    * Agent A (BLIND):    told only the code location, NOT the bug. Tries to find
                          a bug on its own. Reveals what a generic reviewer sees
                          and what domain knowledge it lacks.   (skill step 2)
    * Agent B (INFORMED): told the bug description + location. Analyzes root
                          cause, reachability, severity, and the external domain
                          knowledge required to recognize it.   (skill step 1)

  Phase 2 — a third Claude Code call reads BOTH agents' trajectories and distills
            a single spec (trigger_pattern + detection_logic).  (skill step 3)

Usage:
    python spec_init.py \
        --bug-report report.json \
        --codebase   /path/to/repo \
        --model      claude-opus-4-6 \
        --spec-db    ../spec-db/spec-033.json \
        --output     new-spec.json

`report.json`: {"file": "...", "line": 15, "description": "..."}
(`line_start`/`line_end` accepted instead of `line`.)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude_runner import ClaudeResult, ClaudeRunError, extract_json, run_claude
from prompts import render
from specs import _to_spec, match_files

CONTEXT_RADIUS = 40  # lines of source shown around the reported location


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _parse_report(report: dict) -> tuple[str, int, str]:
    file = report.get("file") or report.get("file_path")
    if not file:
        raise ValueError("bug report must include 'file'")
    line = report.get("line") or report.get("line_start") or 1
    description = (
        report.get("description")
        or report.get("comment")
        or report.get("bug")
        or ""
    )
    if not description:
        raise ValueError("bug report must include 'description'")
    return file, int(line), description


def _read_snippet(codebase: Path, rel_file: str, line: int) -> str:
    path = codebase / rel_file
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return "(could not read file; use the Read tool)"
    lo = max(0, line - 1 - CONTEXT_RADIUS)
    hi = min(len(lines), line + CONTEXT_RADIUS)
    out = []
    for i in range(lo, hi):
        marker = ">>" if (i + 1) == line else "  "
        out.append(f"{marker}{i + 1:5d}| {lines[i]}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #

def run_phase1(
    codebase: Path,
    file: str,
    line: int,
    description: str,
    snippet: str,
    model: str,
    timeout: int,
) -> tuple[ClaudeResult, ClaudeResult]:
    """Run the BLIND and INFORMED agents in parallel, capturing trajectories."""
    blind_prompt = render("blind_detect.md", file=file, line=line, snippet=snippet)
    informed_prompt = render(
        "informed_analysis.md",
        file=file, line=line, description=description, snippet=snippet,
    )

    def _blind() -> ClaudeResult:
        return run_claude(
            blind_prompt, cwd=codebase, model=model,
            timeout=timeout, capture_trajectory=True,
        )

    def _informed() -> ClaudeResult:
        return run_claude(
            informed_prompt, cwd=codebase, model=model,
            timeout=timeout, capture_trajectory=True,
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(_blind)
        fb = ex.submit(_informed)
        return fa.result(), fb.result()


def synthesize_spec(
    codebase: Path,
    file: str,
    line: int,
    description: str,
    trajectory_a: str,
    trajectory_b: str,
    model: str,
    timeout: int,
) -> dict:
    prompt = render(
        "synthesis.md",
        file=file,
        line=line,
        description=description,
        trajectory_a=trajectory_a or "(no trajectory captured)",
        trajectory_b=trajectory_b or "(no trajectory captured)",
    )
    result = run_claude(prompt, cwd=codebase, model=model, timeout=timeout)
    spec = extract_json(result.text)
    if not isinstance(spec, dict):
        raise ClaudeRunError("synthesis did not return a spec object")
    return spec


def validate_trigger(spec: dict, codebase: Path, buggy_file: str) -> dict:
    """Light recall/precision check on the synthesized syntactic patterns.

    Recall: at least one pattern must match the buggy file.
    Precision: number of files in the codebase matched by ANY pattern.
    """
    spec_obj = _to_spec(spec)
    syntactic = spec_obj.syntactic_patterns

    # Recall against the buggy file.
    buggy_path = codebase / buggy_file
    recall_hits = []
    try:
        content = buggy_path.read_text(errors="ignore")
        for p in syntactic:
            try:
                if re.search(p, content):
                    recall_hits.append(p)
            except re.error:
                pass
    except OSError:
        pass

    # Precision: count matched files across the codebase.
    matched = match_files(spec_obj, codebase)
    return {
        "syntactic": syntactic,
        "recall_ok": bool(recall_hits),
        "recall_matched_patterns": recall_hits,
        "precision_files_matched": len(matched),
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def initialize_spec(
    report: dict,
    codebase: str,
    model: str,
    *,
    spec_db: str | None = None,
    timeout: int = 1200,
    verbose: bool = True,
) -> dict:
    codebase_path = Path(codebase).resolve()
    file, line, description = _parse_report(report)
    snippet = _read_snippet(codebase_path, file, line)

    if verbose:
        print(f"[phase 1] blind + informed agents on {file}:{line} ...", file=sys.stderr)
    blind, informed = run_phase1(
        codebase_path, file, line, description, snippet, model, timeout
    )
    if verbose:
        print(
            f"  blind: {len(blind.trajectory)} events | "
            f"informed: {len(informed.trajectory)} events",
            file=sys.stderr,
        )

    if verbose:
        print("[phase 2] synthesizing spec ...", file=sys.stderr)
    spec = synthesize_spec(
        codebase_path, file, line, description,
        blind.transcript, informed.transcript, model, timeout,
    )

    trigger_check = validate_trigger(spec, codebase_path, file)
    if verbose:
        ok = "ok" if trigger_check["recall_ok"] else "FAILED (no pattern matches buggy file)"
        print(
            f"  trigger recall: {ok} | "
            f"precision: {trigger_check['precision_files_matched']} files",
            file=sys.stderr,
        )

    return {
        "spec": spec,
        "trigger_validation": trigger_check,
        "agent_trajectories": {
            "blind_detect": blind.transcript,
            "informed_analysis": informed.transcript,
        },
        "input": {"file": file, "line": line, "description": description},
        "model": model,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Initialize a detection spec from a bug report")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bug-report", help="path to a bug-report JSON file")
    src.add_argument("--bug-json", help="inline bug report as a JSON string")
    ap.add_argument("--codebase", required=True, help="path to the codebase root")
    ap.add_argument("--model", required=True, help="model name, e.g. claude-opus-4-6")
    ap.add_argument("--spec-db", help="spec DB JSON (append target for --append-db)")
    ap.add_argument("--output", help="write the result JSON here")
    ap.add_argument("--spec-only", action="store_true", help="print only the spec object")
    ap.add_argument("--append-db", action="store_true",
                    help="append the new spec to --spec-db (DB must be a JSON array)")
    ap.add_argument("--timeout", type=int, default=1200, help="per-agent claude timeout (s)")
    args = ap.parse_args()

    report = (
        json.loads(args.bug_json)
        if args.bug_json
        else json.loads(Path(args.bug_report).read_text())
    )

    result = initialize_spec(
        report, args.codebase, args.model,
        spec_db=args.spec_db, timeout=args.timeout,
    )

    if args.append_db and args.spec_db:
        db_path = Path(args.spec_db)
        db = json.loads(db_path.read_text()) if db_path.exists() else []
        if isinstance(db, dict):
            db = [db]
        db.append(result["spec"])
        db_path.write_text(json.dumps(db, indent=2))
        print(f"Appended '{result['spec'].get('name', 'spec')}' to {args.spec_db}",
              file=sys.stderr)

    payload = result["spec"] if args.spec_only else result
    out = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).write_text(out)
        print(f"\nWrote '{result['spec'].get('name', 'spec')}' to {args.output}",
              file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
