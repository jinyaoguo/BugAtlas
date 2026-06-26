# BugAtlas Artifacts

This repository is the companion artifact for the paper. It comprises three
stages: **specification initialization**, **specification refinement**, and
**spec-driven bug detection**. Each stage runs **headless Claude Code**
(`claude -p`) inside the target codebase directory, so the agent can read the
real source via its Read / Grep tools.

## Method Overview

![workflow](figs/workflow.pdf)


## Requirements

- **Python 3.9+**; no third-party packages required (standard library only).
- **Claude Code CLI** installed and authenticated (the `claude` command is on PATH).
- The model name passed to `--model` must be **routable** on your inference
  gateway (e.g. `claude-opus-4-6`).

All commands are run from the `code/` directory (paths below are relative to it).

## Usage

### ① Specification initialization

Following the paper's three-step procedure, distill a single bug report (code
location + defect description) into a spec:

1. **Phase 1: two Claude Code agents analyze in parallel** (each agent's
   trajectory is fully recorded):
   - *Blind*: given only the code location, **not** the defect; it must decide
     on its own whether a bug exists — this exposes what a generic reviewer
     misses and which domain knowledge it lacks.
   - *Informed*: given the defect description; it analyzes the root cause,
     reachability, severity, and the **external domain knowledge** needed to
     recognize the bug that cannot be inferred from code alone.
2. **Phase 2: synthesis**: a third Claude Code call reads the two trajectories
   above and distills one spec (`syntactic_specification` +
   `semantic_specification`).

```bash
python spec_init.py \
  --bug-report report.json \
  --codebase   /path/to/repo \
  --model      claude-opus-4-6 \
  --output     new-spec.json
```

`report.json` looks like `{"file": "...", "line": 13, "description": "..."}`.
Common options: `--bug-json '<inline>'` (pass the report inline), `--spec-only`
(emit just the spec body), `--spec-db <path>` together with `--append-db`
(append the new spec to a JSON-array spec database). The full output includes:
the generated spec, a lightweight `trigger_validation` (recall check against the
buggy file + number of matched files across the codebase), and both agents'
transcripts.

### ② Specification refinement — tighten a spec (eliminate false positives)

Raise a spec's **precision** (drop FPs) **without losing recall** (still detect
real bugs), by iterating: "detect + validate a sample → find FPs → tighten the
spec → re-check". Reuses detect and validator, plus a `refiner` (Claude Code)
that performs the tightening.

- **Round 0**: randomly sample up to 10 files among those matched by the spec's
  trigger, run detect then validator on each; split the results into a
  **TP pool** (bugs validated as real — the recall guard, grow-only) and an
  **FP pool** (bugs validated as false — the precision target). If there are no
  FPs, return the spec unchanged.
- **Refinement rounds** (≤ `--max-rounds`, default 3): the refiner tightens the
  spec from the FP evidence; a **recall guard** rejects any tightening that drops
  a TP-pool case (and asks for a more surgical change); it then re-checks the old
  FP files + a fresh sample of 10. Converges once no FPs remain.

```bash
python refine.py \
  --spec     ../spec-db/spec-033.json \
  --codebase /path/to/repo \
  --model    claude-opus-4-6 \
  --output   refined.json
```

Common options: `--sample-size` (files sampled per round, default 10),
`--max-rounds` (default 3), `--seed` (sampling seed for reproducibility),
`--anchor-case file:line` (inject a guaranteed TP to preserve, e.g. the original
defect the spec was just generated from), `--workers` (parallel detect/validate
workers, default 4), `--spec-only`. Output:
`{ spec, refinement:{ status, refined, rounds[], tp_pool[] } }`, where `status`
is `converged` or `fp_persist` (FPs survived the round budget — usually means the
spec is too broad).


### ③ Spec-driven bug detection

For each spec: first use the `syntactic_specification` regexes to pre-screen the
codebase down to candidate files, then let Claude Code judge each file by the
`semantic_specification` and report defects.

```bash
python detect.py \
  --spec     ../spec-db/spec-033.json \
  --codebase /path/to/repo \
  --model    claude-opus-4-6 \
  --output   bugs.json
```

The input may be a single spec, an array of specs, or `{"specs": [...]}`. Common
options: `--extensions .go,.py` (file suffixes to scan, default `.go`),
`--max-files N` (max files checked per spec, for debugging), `--no-tests` (skip
`*_test.go`), `--timeout` (per-file timeout in seconds, default 1200).

Output: `{ model, codebase, spec_file, stats[], bugs[] }`; each bug carries
`file, line, severity, comment, how_to_fix, spec_id, confidence`.

## Quick Start

```bash
cd code

# ① Generate a spec from a single bug report
python spec_init.py --bug-report report.json --codebase /path/to/repo \
  --model claude-opus-4-6 --spec-only --output spec.json

# ② Refine the spec (inject the original defect as a must-keep TP)
python refine.py --spec spec.json --codebase /path/to/repo \
  --model claude-opus-4-6 --anchor-case path/to/file.go:13 --output spec.refined.json

# ③ Run detection across the codebase with the refined spec
python detect.py --spec spec.refined.json --codebase /path/to/repo \
  --model claude-opus-4-6 --output bugs.json
```
