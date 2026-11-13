"""Mirror each agent's kept AgentRun learnings into the shared brain (vault/tech/learned/).

AgentRun learning is per agent; Omniscient's brain is shared. This sync makes every kept
note visible to every agent (and to the owner in Obsidian). The vault copy is a mirror:
notes retired or superseded in AgentRun are removed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import frontmatter

LEARNED_DIR = Path("tech") / "learned"
SAFE_FILENAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}\.md$")
INDEX_FILE = "MEMORY.md"


class LearningApi(Protocol):
    def notes(
        self, agent_id: str, *, status: str, cursor: str | None = None
    ) -> Any: ...


class LearningClient(Protocol):
    @property
    def learning(self) -> LearningApi: ...


@dataclass(frozen=True)
class SyncResult:
    written: tuple[Path, ...]
    unchanged: tuple[Path, ...]
    removed: tuple[Path, ...]
    rejected: tuple[str, ...]


def _active_notes(client: LearningClient, agent_id: str) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = client.learning.notes(agent_id, status="active", cursor=cursor)
        notes.extend(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            return notes


def _render(agent_id: str, note: dict[str, Any]) -> str:
    post = frontmatter.loads(note.get("text", ""))
    metadata = {"type": "tech", "kind": "learned", "agent": agent_id, **post.metadata}
    metadata["type"], metadata["kind"] = "tech", "learned"
    if "type" in post.metadata:
        metadata["agentrun_type"] = post.metadata["type"]
    return frontmatter.dumps(frontmatter.Post(post.content, **metadata)) + "\n"


def _sync_agent(
    client: LearningClient, agent_id: str, vault: Path
) -> tuple[list[Path], list[Path], list[Path], list[str]]:
    target = vault / LEARNED_DIR / agent_id
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    unchanged: list[Path] = []
    rejected: list[str] = []
    keep: set[str] = set()
    for note in _active_notes(client, agent_id):
        name = str(note.get("file", ""))
        if name == INDEX_FILE:
            continue
        if not SAFE_FILENAME.match(name):
            rejected.append(name)
            continue
        keep.add(name)
        path = target / name
        content = _render(agent_id, note)
        if path.exists() and path.read_text(encoding="utf-8") == content:
            unchanged.append(path)
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
    removed = [p for p in sorted(target.glob("*.md")) if p.name not in keep]
    for path in removed:
        path.unlink()
    return written, unchanged, removed, rejected


def sync_learnings(
    client: LearningClient, agent_ids: tuple[str, ...], vault: Path
) -> SyncResult:
    written: list[Path] = []
    unchanged: list[Path] = []
    removed: list[Path] = []
    rejected: list[str] = []
    for agent_id in agent_ids:
        w, u, r, x = _sync_agent(client, agent_id, vault)
        written += w
        unchanged += u
        removed += r
        rejected += x
    return SyncResult(tuple(written), tuple(unchanged), tuple(removed), tuple(rejected))
