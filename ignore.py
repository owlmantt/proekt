"""Фильтрация путей по .gitignore и встроенным правилам мусора."""

from __future__ import annotations

from pathlib import Path

import pathspec

DEFAULT_IGNORE_PATTERNS = [
    ".git/",
    ".svn/",
    ".hg/",
    "venv/",
    ".venv/",
    "env/",
    "node_modules/",
    "dist/",
    "build/",
    "coverage/",
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    "chroma_db/",
    "*.pyc",
    "*.pyo",
    "*.min.js",
    ".DS_Store",
]


def load_ignore_spec(root: Path) -> pathspec.GitIgnoreSpec:
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    gitignore = root / ".gitignore"
    if gitignore.is_file():
        patterns.extend(gitignore.read_text(encoding="utf-8", errors="replace").splitlines())
    return pathspec.GitIgnoreSpec.from_lines(patterns)


def is_ignored(root: Path, path: Path, spec: pathspec.PathSpec | None = None) -> bool:
    spec = spec or load_ignore_spec(root)
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    if not relative.parts:
        return False
    rel = relative.as_posix()
    candidates = [rel]
    if path.is_dir() and not rel.endswith("/"):
        candidates.append(rel + "/")
    if spec.match_file(rel) or any(spec.match_file(item) for item in candidates):
        return True
    parts = relative.parts
    for i in range(len(parts)):
        prefix = "/".join(parts[: i + 1])
        if spec.match_file(prefix) or spec.match_file(prefix + "/"):
            return True
    return False
