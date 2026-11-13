"""Pick a safe, bounded set of a local project's files to attach to a research run.

The researcher runs in AgentRun and cannot see local repos, so questions about the
owner's own projects came back "non vérifiable". This module only *selects* files;
uploading them is `omniscient.work.attachments`' job.

Order: the project's agent_docs, then README*, AGENTS.md, CLAUDE.md, docs/**/*.md,
then small source files (*.py, *.ts, *.tsx) under src/, app/ or the root, at most
two levels below that base (ranked by keyword hits in their path when keywords are
given). Selection stops once the total budget is reached. Hard exclusions
(secrets, data, vendored or built trees, binaries) apply to every candidate,
agent_docs included, and no path may leave the project root.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from omniscient.registry.core import Project

MAX_FILES = 12
# Kept small: attached files are re-read in context on every agent step (seen
# live: ~$0.45 per Opus step with 1 MB attached).
MAX_FILE_BYTES = 60_000
MAX_TOTAL_BYTES = 300_000
SOURCE_MAX_DEPTH = 2  # base/file.py or base/pkg/file.py
SOURCE_BASES = ("src", "app", ".")
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx"})
TOP_DOCS = ("AGENTS.md", "CLAUDE.md")
UPLOAD_ROOT = "projects"
EXCLUDED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "dist", "build", "data", "__pycache__"}
)
EXCLUDED_SUFFIXES = frozenset(
    {".token", ".key", ".pem", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3", ".fit"}
)
EXCLUDED_NAMES = frozenset({"api_keys.txt"})
EXCLUDED_NAME_PARTS = ("secret", "credential", "id_rsa", "id_ed25519")
_BINARY_PROBE_BYTES = 8192


def is_excluded(rel: Path) -> bool:
    """True for any path (relative to a project root) that must never be attached."""
    if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
        return True
    name = rel.name.lower()
    return (
        name.startswith(".env")
        or name in EXCLUDED_NAMES
        or rel.suffix.lower() in EXCLUDED_SUFFIXES
        or any(part in name for part in EXCLUDED_NAME_PARTS)
        or rel.name in EXCLUDED_DIRS
    )


def upload_path(project_name: str, root: Path, path: Path) -> str:
    """The run-workspace path for a project file: projects/<name>/<rel>.

    AgentRun refuses any segment that starts with '.', so a leading dot becomes '_'.
    """
    rel = path.relative_to(root)
    segments = (UPLOAD_ROOT, project_name, *rel.parts)
    return "/".join(f"_{s[1:]}" if s.startswith(".") else s for s in segments)


def _is_text(path: Path) -> bool:
    try:
        data = path.read_bytes()
        if b"\x00" in data[:_BINARY_PROBE_BYTES]:
            return False
        data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return True


def _safe(root: Path, path: Path) -> Path | None:
    """The resolved file if it is an allowed regular file inside root, else None."""
    try:
        rel = path.relative_to(root)
        resolved = path.resolve(strict=True)
        resolved_rel = resolved.relative_to(root)
    except (OSError, ValueError):  # missing, a loop, or outside the root
        return None
    if is_excluded(rel) or is_excluded(resolved_rel) or not resolved.is_file():
        return None
    return resolved


def _walk(base: Path, max_depth: int | None) -> Iterator[Path]:
    """Files under base, never descending into excluded or symlinked directories."""
    for current, dirs, files in os.walk(base, followlinks=False):
        here = Path(current)
        depth = len(here.relative_to(base).parts)
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in EXCLUDED_DIRS
            and not d.startswith(".")
            and (max_depth is None or depth + 1 < max_depth)
        )
        yield from (here / f for f in sorted(files))


def _docs(root: Path) -> Iterator[Path]:
    yield from sorted(root.glob("README*"))
    yield from (root / name for name in TOP_DOCS)
    docs = root / "docs"
    if docs.is_dir():
        yield from (p for p in _walk(docs, None) if p.suffix == ".md")


def _sources(root: Path, keywords: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for base_name in SOURCE_BASES:
        base = root if base_name == "." else root / base_name
        if not base.is_dir() or base.is_symlink():
            continue
        walked = _walk(base, SOURCE_MAX_DEPTH)
        found += [p for p in walked if p.suffix in SOURCE_SUFFIXES]
    words = tuple(k.lower() for k in keywords if k)

    def rank(path: Path) -> tuple[int, int]:
        rel = path.relative_to(root).as_posix().lower()
        return (-sum(word in rel for word in words), len(path.parts))

    return sorted(dict.fromkeys(found), key=rank)  # stable: walk order breaks ties


def _bounded(root: Path, candidates: Iterable[Path]) -> tuple[Path, ...]:
    picked: list[Path] = []
    total = 0
    for candidate in candidates:
        if len(picked) >= MAX_FILES:
            break
        path = _safe(root, candidate)
        if path is None or path in picked:
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            continue
        if total + size > MAX_TOTAL_BYTES:
            break  # budget reached: later (lower-priority) files are not added
        if not _is_text(path):
            continue
        picked.append(path)
        total += size
    return tuple(picked)


def select_project_files(
    project: Project, keywords: tuple[str, ...] = ()
) -> tuple[Path, ...]:
    """Resolved paths of the project's files to attach, bounded and secret-free."""
    try:
        root = project.path.resolve(strict=True)
    except OSError:
        return ()
    if not root.is_dir():
        return ()
    agent_docs = (root / doc for doc in project.agent_docs)
    candidates = (*agent_docs, *_docs(root), *_sources(root, keywords))
    return _bounded(root, candidates)
