"""Attach the owner's local project files to a research run.

AgentRun runs cannot see local repos. An idea note's frontmatter lists the
`projects` it touches; each project's files are picked by `context_files`, uploaded
through POST /files (bytes in one call, `path` in the query), and their ids passed
as RunCreate.file_ids. AgentRun copies each file into the run's workspace at its
`path`, where the agent reads it with its `read` tool.

Attaching is best effort: any failure is logged and the run goes on without files.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import chain, zip_longest
from pathlib import Path
from typing import Any, Protocol

import frontmatter

from omniscient.registry.core import Project
from omniscient.work.context_files import select_project_files, upload_path

# Under the live server's limits.max_files_per_run (20); hosted documents 10.
MAX_FILES_PER_RUN = 12
KEYWORD_MIN_CHARS = 4
KEYWORD_STEM_CHARS = 5  # "signaux" and "signals" both match signal.py
_WORD = re.compile(r"[a-z0-9]+")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")

log = logging.getLogger("omniscient.attachments")


class UploadFailed(Exception):
    """AgentRun did not answer an upload with a ready file."""


class FileUploader(Protocol):
    def upload(self, path: Path, upload_path: str) -> str:
        """Upload one local file to the run-workspace path; return its file_id."""
        ...


@dataclass(frozen=True)
class Attachment:
    project: str
    path: str  # where the run sees the file, relative to its workspace
    file_id: str


@dataclass(frozen=True)
class ProjectContext:
    """The registry's projects and how to upload their files."""

    projects: tuple[Project, ...]
    uploader: FileUploader


@dataclass(frozen=True)
class AgentRunUploader:
    """FileUploader over the agentrun client's `files` resource."""

    files: Any

    def upload(self, path: Path, upload_path: str) -> str:
        data = path.read_bytes()
        made = self.files.create(
            data, path=upload_path, sha256=hashlib.sha256(data).hexdigest()
        )
        file = (made or {}).get("file") or {}
        if file.get("status") != "ready" or not file.get("file_id"):
            raise UploadFailed(f"{upload_path}: upload not ready: {file!r}")
        return str(file["file_id"])


def idea_note(vault: Path, map_path: Path) -> tuple[str, Path | None]:
    """(the map's idea link, its note in vault/ideas if it exists there)."""
    link = str(frontmatter.load(map_path).metadata.get("idea", ""))
    match = _WIKILINK.search(link)
    if match is None:
        return "", None
    name = match.group(1).strip()
    ideas_dir = (vault / "ideas").resolve()
    note = (ideas_dir / f"{name}.md").resolve()
    if note.parent != ideas_dir or not note.is_file():  # no escape from vault/ideas
        return name, None
    return name, note


def idea_projects(note: Path) -> tuple[str, ...]:
    """The idea's `projects` frontmatter list; () (logged) when it is malformed."""
    value = frontmatter.load(note).metadata.get("projects", [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        log.warning("%s: ignoring malformed projects: %r", note.name, value)
        return ()
    return tuple(v.strip() for v in value if v.strip())


def keywords(text: str) -> tuple[str, ...]:
    """Cheap relevance hints: stems of the text's longer words, in order."""
    words = (w for w in _WORD.findall(text.lower()) if len(w) >= KEYWORD_MIN_CHARS)
    return tuple(dict.fromkeys(w[:KEYWORD_STEM_CHARS] for w in words))


def _interleaved(groups: Iterable[tuple[Any, ...]]) -> tuple[Any, ...]:
    """Round robin over groups, so every project gets a share of the run cap."""
    rows = zip_longest(*groups)
    return tuple(item for item in chain.from_iterable(rows) if item is not None)


def _selected(
    context: ProjectContext, names: tuple[str, ...], hints: tuple[str, ...]
) -> tuple[tuple[Project, Path], ...]:
    by_name = {p.name: p for p in context.projects}
    groups = []
    for name in dict.fromkeys(names):
        project = by_name.get(name)
        if project is None:
            log.warning("project %r is not in the registry; not attached", name)
            continue
        files = select_project_files(project, hints)
        groups.append(tuple((project, f) for f in files))
    return _interleaved(groups)[:MAX_FILES_PER_RUN]


def attach_projects(
    context: ProjectContext, names: tuple[str, ...], hints: tuple[str, ...] = ()
) -> tuple[Attachment, ...]:
    """Upload the named projects' files; () (logged) if anything fails."""
    try:
        attached = []
        for project, path in _selected(context, names, hints):
            target = upload_path(project.name, project.path.resolve(), path)
            file_id = context.uploader.upload(path, target)
            attached.append(Attachment(project.name, target, file_id))
    except Exception as exc:  # noqa: BLE001 -- logged; research goes on without files
        log.warning("attaching %s failed, running without files: %s", names, exc)
        return ()
    return tuple(attached)


def attach_for_map(
    context: ProjectContext, vault: Path, map_path: Path, question: str
) -> tuple[Attachment, ...]:
    """Attach the files of the projects the map's idea touches (best effort)."""
    _, note = idea_note(vault, map_path)
    names = idea_projects(note) if note is not None else ()
    return attach_projects(context, names, keywords(question)) if names else ()


def attachments_section(attachments: tuple[Attachment, ...]) -> str:
    """The prompt section naming the attached projects; '' when nothing is attached."""
    if not attachments:
        return ""
    names = ", ".join(dict.fromkeys(a.project for a in attachments))
    listing = "\n".join(f"- {a.path}" for a in attachments)
    return (
        "## Attached project files\n"
        f"Files of the owner's local projects ({names}) are attached to this run, "
        "in your workspace at the paths below. Open only the files relevant to the "
        "question with the `read` tool (each file read costs context on every "
        "later step), not all of them: they show how these projects work today. "
        "Cite them by path; "
        "they are a bounded selection, so say when something is not in them.\n"
        f"{listing}"
    )
