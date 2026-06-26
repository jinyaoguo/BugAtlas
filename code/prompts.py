"""Prompt loading. All prompt templates live in ../prompt/*.md.

Templates use Python str.format placeholders ({name}); literal braces in JSON
schema examples are doubled ({{ }}). `render` fills a template; `load_raw` reads
a placeholder-free prompt (e.g. validator.md, whose body contains literal { }).
"""

from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompt"


def load_raw(name: str) -> str:
    """Read a prompt file verbatim (no placeholder substitution)."""
    return (PROMPT_DIR / name).read_text()


def render(name: str, **kwargs) -> str:
    """Read a prompt template and fill its {placeholders} with kwargs.

    Substituted values are inserted verbatim and are NOT re-scanned for braces,
    so source code / trajectories containing { } are safe.
    """
    return load_raw(name).format(**kwargs)
