#!/usr/bin/env python3
"""Spec-driven bug detector (the "review" stage).

Pipeline, per the artifact design:

  spec file ──> for each spec:
      1. trigger_pattern (regex)  ──> match suspicious files in the codebase
      2. for each matched file    ──> Claude Code applies detection_logic
                                       and reports concrete bugs (or none)

Usage:
    python detect.py \
        --spec   ../spec-db/spec-033.json \
        --codebase /path/to/repo \
        --model  claude-sonnet-4-5 \
        --output bugs.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from claude_runner import ClaudeRunError, extract_json, run_claude
from prompts import render
from specs import Spec, load_specs, match_files

# Cap embedded file content so a single huge file can't blow the context window.
# The agent can still Read the full file with its tools when it needs more.
MAX_EMBED_CHARS = 60_000


def _render_detection_logic(steps: list[str]) -> str:
    return "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))


def build_review_prompt(spec: Spec, rel_path: str, source: str) -> str:
    """Fill the review.md template with the concrete spec + file values."""
    if len(source) > MAX_EMBED_CHARS:
        source = (
            source[:MAX_EMBED_CHARS]
            + "\n\n... [truncated; use the Read tool to view the full file] ..."
        )

    return render(
        "review.md",
        file_path=rel_path,
        source_content=source,
        spec_name=spec.name,
        spec_description=spec.description,
        detection_logic=_render_detection_logic(spec.detection_logic),
    )


def detect_in_file(
    spec: Spec,
    codebase: Path,
    rel_path: str,
    model: str,
    *,
    timeout: int,
) -> list[dict]:
    """Run Claude Code on one matched file and return the reported bugs."""
    source = (codebase / rel_path).read_text(errors="ignore")
    prompt = build_review_prompt(spec, rel_path, source)

    result = run_claude(prompt, cwd=codebase, model=model, timeout=timeout)
    parsed = extract_json(result.text)

    bugs = parsed.get("bugs", []) if isinstance(parsed, dict) else []
    # Stamp provenance so downstream (validator) has everything it needs.
    for bug in bugs:
        bug.setdefault("file", rel_path)
        bug.setdefault("spec_id", spec.id)
        bug["_detected_by_spec"] = spec.id
    return bugs


def run_detection(
    spec_path: str,
    codebase: str,
    model: str,
    *,
    extensions: list[str],
    max_files: int | None,
    include_tests: bool,
    timeout: int,
    verbose: bool = True,
) -> dict:
    codebase_path = Path(codebase).resolve()
    specs = load_specs(spec_path)

    all_bugs: list[dict] = []
    stats = []

    for spec in specs:
        matches = match_files(
            spec,
            codebase_path,
            extensions=extensions,
            include_tests=include_tests,
        )
        if max_files is not None:
            matches = matches[:max_files]

        if verbose:
            print(
                f"[{spec.id}] {len(matches)} file(s) matched "
                f"trigger_pattern {spec.syntactic_patterns}",
                file=sys.stderr,
            )

        spec_bugs: list[dict] = []
        for i, fm in enumerate(matches, 1):
            if verbose:
                print(
                    f"  ({i}/{len(matches)}) reviewing {fm.rel_path} ...",
                    file=sys.stderr,
                )
            try:
                bugs = detect_in_file(
                    spec, codebase_path, fm.rel_path, model, timeout=timeout
                )
            except ClaudeRunError as exc:
                print(f"    ! skipped ({exc})", file=sys.stderr)
                continue
            if bugs and verbose:
                print(f"    -> {len(bugs)} issue(s)", file=sys.stderr)
            spec_bugs.extend(bugs)

        all_bugs.extend(spec_bugs)
        stats.append(
            {
                "spec_id": spec.id,
                "files_matched": len(matches),
                "bugs_found": len(spec_bugs),
            }
        )

    return {
        "model": model,
        "codebase": str(codebase_path),
        "spec_file": str(Path(spec_path).resolve()),
        "stats": stats,
        "bugs": all_bugs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Spec-driven bug detector")
    ap.add_argument("--spec", required=True, help="path to a spec JSON file")
    ap.add_argument("--codebase", required=True, help="path to the codebase root")
    ap.add_argument("--model", required=True, help="model name, e.g. claude-sonnet-4-5")
    ap.add_argument("--output", help="write the full report JSON here")
    ap.add_argument(
        "--extensions",
        default=".go",
        help="comma-separated file extensions to scan (default: .go)",
    )
    ap.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="cap files reviewed per spec (debugging)",
    )
    ap.add_argument(
        "--no-tests",
        action="store_true",
        help="skip *_test.go files during screening",
    )
    ap.add_argument("--timeout", type=int, default=1200, help="per-file claude timeout (s)")
    args = ap.parse_args()

    report = run_detection(
        args.spec,
        args.codebase,
        args.model,
        extensions=[e.strip() for e in args.extensions.split(",") if e.strip()],
        max_files=args.max_files,
        include_tests=not args.no_tests,
        timeout=args.timeout,
    )

    out = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(out)
        print(
            f"\nWrote {len(report['bugs'])} bug(s) to {args.output}",
            file=sys.stderr,
        )
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
