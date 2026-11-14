"""Chat data and storage for the widget chat: the frozen Chat/Message records, the
projects a chat may run in (registry + home, `~`-relative labels only) and the 0600 JSON
file kept per chat under `<home>/.omniscient/chats/`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from omniscient.group.models import iso
from omniscient.home import Home
from omniscient.registry.core import RegistryError, load_registry

log = logging.getLogger(__name__)

MAX_MESSAGES = 500


@dataclass(frozen=True)
class ProjectRef:
    name: str
    path: Path
    path_label: str


@dataclass(frozen=True)
class Message:
    role: str  # user | assistant | tool | error
    text: str
    at: datetime
    tool: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "role": self.role,
            "text": self.text,
            "at": iso(self.at),
        }
        if self.tool is not None:
            data["tool"] = self.tool
        return data


@dataclass(frozen=True)
class Chat:
    chat_id: str
    project: str
    title: str
    state: str  # running | idle | error
    created_at: datetime
    updated_at: datetime
    claude_session_id: str | None = None
    cost_usd: float | None = None
    messages: tuple[Message, ...] = ()

    def summary(self) -> dict[str, Any]:
        """The API view: no Claude session id, no messages."""
        return {
            "chat_id": self.chat_id,
            "project": self.project,
            "title": self.title,
            "state": self.state,
            "updated_at": iso(self.updated_at),
            "cost_usd": self.cost_usd,
        }

    def with_message(self, message: Message) -> Chat:
        kept = (*self.messages, message)[-MAX_MESSAGES:]
        return replace(self, messages=kept, updated_at=message.at)


# ---- projects ------------------------------------------------------------------------


def path_label(path: Path) -> str:
    """`~/…` under the user's home, else the folder name: never an absolute path."""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return path.name


def list_projects(home: Home) -> tuple[ProjectRef, ...]:
    """Registered projects whose folder exists, plus the home itself (registry wins)."""
    try:
        projects = load_registry(home.repos)
    except RegistryError as exc:
        log.warning("omnigroup chat: registry unreadable: %s", exc)
        projects = ()
    refs = [
        ProjectRef(p.name, p.path, path_label(p.path))
        for p in projects
        if p.path.is_dir()
    ]
    if home.root.name not in {r.name for r in refs}:
        refs.append(ProjectRef(home.root.name, home.root, path_label(home.root)))
    return tuple(sorted(refs, key=lambda r: r.name))


# ---- persistence ---------------------------------------------------------------------


def dump(chat: Chat) -> str:
    data = {
        "chat": {
            **chat.summary(),
            "created_at": iso(chat.created_at),
            "claude_session_id": chat.claude_session_id,
        },
        "messages": [m.to_json() for m in chat.messages],
    }
    return json.dumps(data, ensure_ascii=False)


def load(text: str) -> Chat:
    data = json.loads(text)
    meta = data["chat"]
    messages = tuple(
        Message(m["role"], m["text"], datetime.fromisoformat(m["at"]), m.get("tool"))
        for m in data.get("messages", [])
    )
    state = meta["state"] if meta["state"] != "running" else "idle"  # daemon restarted
    return Chat(
        chat_id=meta["chat_id"],
        project=meta["project"],
        title=meta["title"],
        state=state,
        created_at=datetime.fromisoformat(meta["created_at"]),
        updated_at=datetime.fromisoformat(meta["updated_at"]),
        claude_session_id=meta.get("claude_session_id"),
        cost_usd=meta.get("cost_usd"),
        messages=messages,
    )


def write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
