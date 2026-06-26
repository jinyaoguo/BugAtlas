"""Spec loading and trigger-pattern matching.

A spec's `trigger_pattern` is a deterministic pre-screen: it cheaply narrows the
codebase down to the files worth handing to Claude Code for the (expensive)
`detection_logic` judgement. We support both spec shapes seen in the artifacts:

  * legacy list form:   "trigger_pattern": ["regex1", "regex2"]
  * structured form:    "trigger_pattern": {"semantic": "...", "syntactic": [...]}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# Default file extensions to scan. The specs in this artifact target Go, but the
# matcher is language-agnostic; override via --extensions on the CLI.
DEFAULT_EXTENSIONS = (".go",)

# Directories that never contain reviewable product code.
SKIP_DIRS = {".git", "node_modules", "vendor", "bazel-out", "bazel-bin", ".idea"}


@dataclass
class Spec:
    raw: dict
    id: str
    name: str
    description: str
    detection_logic: list[str]
    syntactic_patterns: list[str]
    semantic: str = ""

    @property
    def compiled(self) -> list[re.Pattern]:
        # Patterns are authored as ag/rg (RE2) regexes; some constructs (e.g.
        # variable-width look-behind) are valid there but rejected by Python's
        # `re`. Skip any that don't compile rather than crash the pipeline.
        out: list[re.Pattern] = []
        for p in self.syntactic_patterns:
            try:
                out.append(re.compile(p))
            except re.error:
                continue
        return out


@dataclass
class FileMatch:
    spec: Spec
    path: Path             # absolute path on disk
    rel_path: str          # path relative to the codebase root
    matched_lines: list[int] = field(default_factory=list)


def _syntactic_patterns(obj: dict) -> list[str]:
    """Regex pre-screen patterns. New format: `syntactic_specification` (list).
    Backward-compat: old `trigger_pattern` (list, or {"syntactic": [...]})."""
    if isinstance(obj.get("syntactic_specification"), list):
        return [p for p in obj["syntactic_specification"] if isinstance(p, str)]
    trigger = obj.get("trigger_pattern", [])
    if isinstance(trigger, list):
        return [p for p in trigger if isinstance(p, str)]
    if isinstance(trigger, dict):
        return [p for p in trigger.get("syntactic", []) if isinstance(p, str)]
    return []


def _semantic_spec(obj: dict) -> list[str]:
    """Semantic detection patterns. New format: `semantic_specification` (list).
    Backward-compat: old `detection_logic` (list or str)."""
    logic = obj.get("semantic_specification")
    if logic is None:
        logic = obj.get("detection_logic", [])
    if isinstance(logic, str):
        logic = [logic]
    return [s for s in logic if isinstance(s, str)]


def _to_spec(obj: dict) -> Spec:
    return Spec(
        raw=obj,
        # `id` is no longer part of the spec format; derive a stable identifier
        # from the name (falling back to an explicit id if an old spec has one).
        id=obj.get("id") or obj.get("name") or "unknown",
        name=obj.get("name", "unknown"),
        description=obj.get("description", ""),
        detection_logic=_semantic_spec(obj),
        syntactic_patterns=_syntactic_patterns(obj),
        # old-format only: trigger_pattern.semantic (a single NL string)
        semantic=(obj.get("trigger_pattern", {}).get("semantic", "")
                  if isinstance(obj.get("trigger_pattern"), dict) else ""),
    )


def load_specs(spec_path: str | Path) -> list[Spec]:
    """Load one spec object or an array of specs from a JSON file."""
    data = json.loads(Path(spec_path).read_text())
    if isinstance(data, dict):
        # Either a single spec, or a {"specs": [...]} wrapper.
        if "specs" in data and isinstance(data["specs"], list):
            return [_to_spec(s) for s in data["specs"]]
        return [_to_spec(data)]
    if isinstance(data, list):
        return [_to_spec(s) for s in data]
    raise ValueError(f"unrecognized spec file shape: {type(data)}")


def _iter_source_files(
    codebase: Path, extensions: Iterable[str]
) -> Iterable[Path]:
    exts = {e if e.startswith(".") else f".{e}" for e in extensions}
    for path in codebase.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if exts and path.suffix not in exts:
            continue
        yield path


def match_files(
    spec: Spec,
    codebase: str | Path,
    *,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    include_tests: bool = True,
) -> list[FileMatch]:
    """Return files whose content matches ANY of the spec's syntactic patterns.

    Patterns are OR'd (matching any one is sufficient), mirroring the ag/rg
    semantics described in the generator instructions.
    """
    codebase = Path(codebase).resolve()
    patterns = spec.compiled
    if not patterns:
        return []

    matches: list[FileMatch] = []
    for path in _iter_source_files(codebase, extensions):
        if not include_tests and path.name.endswith("_test.go"):
            continue
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue

        matched_lines: list[int] = []
        for pat in patterns:
            for m in pat.finditer(content):
                line_no = content.count("\n", 0, m.start()) + 1
                matched_lines.append(line_no)
        if matched_lines:
            matches.append(
                FileMatch(
                    spec=spec,
                    path=path,
                    rel_path=str(path.relative_to(codebase)),
                    matched_lines=sorted(set(matched_lines)),
                )
            )
    return matches
