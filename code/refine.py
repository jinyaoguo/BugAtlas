#!/usr/bin/env python3
"""Spec refinement (the "refine" stage).

Input:  a spec + a codebase (+ model). Output: a tightened spec + report.

Goal: raise precision (drop false positives) WITHOUT losing recall (keep
detecting real bugs), by iterating:

  dry-run detect+validate a sample -> find FPs -> tighten spec -> re-check

Reuses detect.py (detector) and validator.py (validator). A third "refiner"
Claude Code call tightens the spec given the FP evidence.

Two pools (skill Step 4 invariants):
  * TP pool  (recall guard, grow-only): bug reports the validator labels TP.
               Every tightening must keep all of them detected, else it overshot.
  * FP pool  (precision target, rotating): bug reports labeled FP. Resolved ones
               drop out; a fresh sample of 10 is added each round.

Usage:
    python refine.py \
        --spec     ../spec-db/spec-033.json \
        --codebase /path/to/repo \
        --model    claude-opus-4-6 \
        --output   refined.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude_runner import ClaudeRunError, extract_json, run_claude
from detect import detect_in_file
from prompts import render
from specs import _syntactic_patterns, _to_spec, match_files
from validator import validate_bug

DEFAULT_SAMPLE_SIZE = 10
DEFAULT_MAX_ROUNDS = 3
LINE_TOLERANCE = 15           # a TP is "still detected" if within this many lines
REFINE_RETRIES = 2            # re-asks when a tightening drops a TP case


# --------------------------------------------------------------------------- #
# Refiner prompt (prompt/refine.md)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Spec / matching helpers
# --------------------------------------------------------------------------- #

def _spec_obj(spec: dict):
    return _to_spec(spec)


def _syntactic(spec: dict) -> list[str]:
    """Syntactic pre-screen patterns (new `syntactic_specification`, with
    backward-compat for old `trigger_pattern`)."""
    return _syntactic_patterns(spec)


def _invalid_patterns(spec: dict) -> list[str]:
    """Syntactic patterns that fail to compile as Python `re` regexes."""
    bad = []
    for p in _syntactic(spec):
        try:
            re.compile(p)
        except re.error:
            bad.append(p)
    return bad


def file_matches_trigger(spec: dict, codebase: Path, rel: str) -> bool:
    """True if the file still matches ANY of the spec's syntactic patterns."""
    try:
        content = (codebase / rel).read_text(errors="ignore")
    except OSError:
        return False
    for p in _syntactic(spec):
        try:
            if re.search(p, content):
                return True
        except re.error:
            continue
    return False


def sample_files(
    spec: dict,
    codebase: Path,
    n: int,
    exclude: set[str],
    rng: random.Random,
) -> list[str]:
    """Randomly sample up to n trigger-matching files not already tested."""
    rels = [m.rel_path for m in match_files(_spec_obj(spec), codebase)]
    rels = [r for r in rels if r not in exclude]
    rng.shuffle(rels)
    return rels[:n]


def _bug_matches(tp: dict, bug: dict) -> bool:
    """Whether a freshly-detected bug corresponds to a TP-pool case (same file,
    nearby line)."""
    if tp.get("file") != bug.get("file"):
        return False
    tl, bl = tp.get("line"), bug.get("line")
    if isinstance(tl, int) and isinstance(bl, int):
        return abs(tl - bl) <= LINE_TOLERANCE
    return True  # no line info -> file-level match


# --------------------------------------------------------------------------- #
# Detect + validate over a file set
# --------------------------------------------------------------------------- #

def detect_and_validate(
    spec: dict,
    codebase: Path,
    rel_paths: list[str],
    model: str,
    timeout: int,
    workers: int,
) -> list[dict]:
    """Run detector then validator over each file; return [{file, bug, validation}]."""
    spec_obj = _spec_obj(spec)

    def work(rel: str) -> list[dict]:
        out: list[dict] = []
        try:
            bugs = detect_in_file(spec_obj, codebase, rel, model, timeout=timeout)
        except ClaudeRunError:
            return out
        for bug in bugs:
            bug.setdefault("file", rel)
            try:
                verdict = validate_bug(bug, codebase, model, timeout=timeout)
            except ClaudeRunError as exc:
                verdict = {"verdict": "ERROR", "reason": str(exc)}
            out.append({"file": rel, "bug": bug, "validation": verdict})
        return out

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for chunk in ex.map(work, rel_paths):
            results.extend(chunk)
    return results


def _partition(dv: list[dict]) -> tuple[list[dict], list[dict]]:
    tps = [r for r in dv if r["validation"].get("verdict") == "TP"]
    fps = [r for r in dv if r["validation"].get("verdict") == "FP"]
    return tps, fps


def _tp_entry(r: dict) -> dict:
    return {**r["bug"], "file": r["file"]}


# --------------------------------------------------------------------------- #
# Recall guard
# --------------------------------------------------------------------------- #

def recall_lost(
    spec: dict,
    codebase: Path,
    tp_pool: list[dict],
    model: str,
    timeout: int,
    workers: int,
) -> list[dict]:
    """Return TP-pool cases the tightened spec would no longer catch.

    A TP is preserved iff its file still matches the syntactic trigger AND the
    detector still reports a matching bug in that file.
    """
    spec_obj = _spec_obj(spec)

    def check(tp: dict) -> dict | None:
        rel = tp["file"]
        if not file_matches_trigger(spec, codebase, rel):
            return tp  # would no longer be scanned in production
        try:
            bugs = detect_in_file(spec_obj, codebase, rel, model, timeout=timeout)
        except ClaudeRunError:
            return tp
        for b in bugs:
            b.setdefault("file", rel)
            if _bug_matches(tp, b):
                return None
        return tp

    lost: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for res in ex.map(check, tp_pool):
            if res is not None:
                lost.append(res)
    return lost


# --------------------------------------------------------------------------- #
# Refiner call (with recall-preserving retries)
# --------------------------------------------------------------------------- #

def _render_fp_block(fps: list[dict]) -> str:
    if not fps:
        return "(none)"
    out = []
    for i, r in enumerate(fps, 1):
        bug, val = r["bug"], r["validation"]
        out.append(
            f"{i}. {r['file']}:{bug.get('line', '?')} — {bug.get('comment', '')}\n"
            f"   validator: {val.get('verdict')} ({val.get('confidence', '?')}) "
            f"{val.get('reason', '')}\n"
            f"   evidence: {val.get('evidence', '')}"
        )
    return "\n".join(out)


def _render_tp_block(tp_pool: list[dict]) -> str:
    if not tp_pool:
        return "(none captured yet — preserve general detection ability)"
    out = []
    for i, tp in enumerate(tp_pool, 1):
        tag = " [anchor]" if tp.get("_anchor") else ""
        out.append(
            f"{i}. {tp.get('file')}:{tp.get('line', '?')}{tag} — "
            f"{tp.get('comment', '')}"
        )
    return "\n".join(out)


def attempt_refine(
    spec: dict,
    fps: list[dict],
    tp_pool: list[dict],
    codebase: Path,
    model: str,
    timeout: int,
    workers: int,
    verbose: bool,
) -> tuple[dict | None, list[dict]]:
    """Ask the refiner to tighten the spec; retry if it drops a TP case.

    Returns (accepted_spec, lost_tps_of_last_attempt). accepted_spec is None if
    no recall-preserving tightening was found within REFINE_RETRIES.
    """
    fp_block = _render_fp_block(fps)
    extra = ""
    last_lost: list[dict] = []

    for attempt in range(1, REFINE_RETRIES + 2):
        prompt = render(
            "refine.md",
            spec_json=json.dumps(spec, indent=2, ensure_ascii=False),
            fp_block=fp_block,
            tp_block=_render_tp_block(tp_pool),
        ) + extra
        try:
            result = run_claude(prompt, cwd=codebase, model=model, timeout=timeout)
            candidate = extract_json(result.text)
        except ClaudeRunError as exc:
            if verbose:
                print(f"    refiner error: {exc}", file=sys.stderr)
            return None, last_lost
        if not isinstance(candidate, dict):
            return None, last_lost
        # Preserve the spec name across the refinement.
        candidate.setdefault("name", spec.get("name"))

        bad = _invalid_patterns(candidate)
        if bad:
            if verbose:
                print(f"    refine attempt {attempt}: {len(bad)} non-Python regex — retrying",
                      file=sys.stderr)
            extra = (
                "\n\n## RETRY — these syntactic patterns are NOT valid Python `re` "
                f"regexes: {bad}. Rewrite them using basic regex only (no "
                "variable-width look-behind / possessive quantifiers / atomic groups)."
            )
            continue

        last_lost = recall_lost(candidate, codebase, tp_pool, model, timeout, workers)
        if not last_lost:
            if verbose:
                print(f"    refine attempt {attempt}: recall preserved", file=sys.stderr)
            return candidate, []
        if verbose:
            print(
                f"    refine attempt {attempt}: dropped {len(last_lost)} TP — retrying",
                file=sys.stderr,
            )
        lost_desc = "; ".join(f"{t.get('file')}:{t.get('line', '?')}" for t in last_lost)
        extra = (
            f"\n\n## RETRY — your previous tightening LOST these true positives: "
            f"{lost_desc}. Make a MORE SURGICAL change that still flags them."
        )

    return None, last_lost


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def refine_spec(
    spec: dict,
    codebase: str,
    model: str,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 0,
    anchor: tuple[str, int] | None = None,
    timeout: int = 1200,
    workers: int = 4,
    verbose: bool = True,
) -> dict:
    codebase_path = Path(codebase).resolve()
    rng = random.Random(seed)
    tested: set[str] = set()
    tp_pool: list[dict] = []
    rounds: list[dict] = []

    if anchor:
        af, al = anchor
        tp_pool.append({"file": af, "line": al, "comment": "(anchor case)", "_anchor": True})

    # ---- Round 0: baseline dry-run ----------------------------------------- #
    sample = sample_files(spec, codebase_path, sample_size, tested, rng)
    tested |= set(sample)
    if verbose:
        print(f"[round 0] baseline dry-run on {len(sample)} sampled file(s)", file=sys.stderr)
    dv = detect_and_validate(spec, codebase_path, sample, model, timeout, workers)
    tps, fps = _partition(dv)
    tp_pool.extend(_tp_entry(r) for r in tps)
    rounds.append({
        "round": 0, "sampled": sample, "detected": len(dv),
        "tp": len(tps), "fp": len(fps),
    })
    if verbose:
        print(f"  detected {len(dv)} | TP {len(tps)} | FP {len(fps)}", file=sys.stderr)

    if not fps:
        return _result(spec, "converged", rounds, tp_pool, refined=False)

    current = spec
    status = "fp_persist"

    # ---- Refinement rounds -------------------------------------------------- #
    for rnd in range(1, max_rounds + 1):
        if verbose:
            print(f"[round {rnd}] refining to remove {len(fps)} FP(s)", file=sys.stderr)

        accepted, lost = attempt_refine(
            current, fps, tp_pool, codebase_path, model, timeout, workers, verbose
        )
        if accepted is None:
            rounds.append({"round": rnd, "action": "refine_failed",
                           "reason": "could not tighten without losing recall",
                           "lost_tp": len(lost)})
            if verbose:
                print("  refinement could not preserve recall — stopping", file=sys.stderr)
            break
        current = accepted

        # Re-check old FP files (only those still matching the trigger) + fresh sample.
        old_fp_files = sorted({r["file"] for r in fps
                               if file_matches_trigger(current, codebase_path, r["file"])})
        fresh = sample_files(current, codebase_path, sample_size, tested, rng)
        tested |= set(fresh)
        recheck_files = sorted(set(old_fp_files) | set(fresh))
        if verbose:
            print(
                f"  re-checking {len(old_fp_files)} old-FP + {len(fresh)} fresh file(s)",
                file=sys.stderr,
            )

        dv = detect_and_validate(current, codebase_path, recheck_files, model, timeout, workers)
        tps, fps = _partition(dv)
        tp_pool.extend(_tp_entry(r) for r in tps)
        resolved = len(old_fp_files) - len({r["file"] for r in fps} & set(old_fp_files))
        rounds.append({
            "round": rnd, "action": "refined",
            "rechecked": recheck_files, "fresh_sampled": fresh,
            "old_fp_resolved": resolved, "tp": len(tps), "fp_remaining": len(fps),
        })
        if verbose:
            print(f"  after refine: TP {len(tps)} | FP {len(fps)}", file=sys.stderr)

        if not fps:
            status = "converged"
            break

    refined = current is not spec
    return _result(current, status, rounds, tp_pool, refined=refined)


def _result(spec: dict, status: str, rounds: list[dict], tp_pool: list[dict], refined: bool) -> dict:
    return {
        "spec": spec,
        "refinement": {
            "status": status,            # converged | fp_persist
            "refined": refined,
            "rounds": rounds,
            "tp_pool_size": len(tp_pool),
            "tp_pool": tp_pool,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Refine a spec to remove false positives")
    ap.add_argument("--spec", required=True, help="path to a single-spec JSON file")
    ap.add_argument("--codebase", required=True, help="path to the codebase root")
    ap.add_argument("--model", required=True, help="model name, e.g. claude-opus-4-6")
    ap.add_argument("--output", help="write the result JSON here")
    ap.add_argument("--spec-only", action="store_true", help="print only the refined spec")
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    ap.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--anchor-case", help="file:line of a guaranteed TP to preserve")
    ap.add_argument("--workers", type=int, default=4, help="parallel detect/validate workers")
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    if isinstance(spec, list):
        if len(spec) != 1:
            ap.error("--spec must contain exactly one spec (got a list of "
                     f"{len(spec)}); refine operates on one spec at a time")
        spec = spec[0]

    anchor = None
    if args.anchor_case:
        f, _, ln = args.anchor_case.rpartition(":")
        anchor = (f, int(ln))

    result = refine_spec(
        spec, args.codebase, args.model,
        max_rounds=args.max_rounds, sample_size=args.sample_size,
        seed=args.seed, anchor=anchor, timeout=args.timeout, workers=args.workers,
    )

    payload = result["spec"] if args.spec_only else result
    out = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(out)
        print(f"\n[{result['refinement']['status']}] wrote refined spec to {args.output}",
              file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
