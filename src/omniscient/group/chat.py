"""Widget chat (mode A): headless Claude Code conversations run by the daemon.

A chat is bound to one project of the Omniscient registry (or the home itself): the
widget names the project, never a path, so Claude only ever runs in a folder the owner
registered. Each turn is `chat_runner.run_turn` on a worker thread; the first turn starts
a new Claude session, the next ones `--resume` it. Events go to the SSE stream as
`chat` events; chats persist as one 0600 JSON file each under `<home>/.omniscient/chats/`.

Security: a widget token can run Claude Code as the owner, in any registered project.
Claude always runs with `--permission-mode default` (chat_runner), so tool calls that
need approval go through Claude Code's PermissionRequest hook to the widget; with the
hooks not installed or no widget answering, headless Claude denies them. Each turn is
capped (`--max-budget-usd`, DEFAULT_MAX_BUDGET_USD) and so is the number of turns per
hour; messages kept or sent to the widget are clipped to MAX_MESSAGE_CHARS.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from omniscient.group import chat_runner
from omniscient.group.chat_runner import RunnerEvent, TurnHandle, TurnOutcome, TurnSpec
from omniscient.group.chat_store import (
    Chat,
    Message,
    ProjectRef,
    dump,
    list_projects,
    load,
    path_label,
    write_private,
)
from omniscient.group.credentials import secure_dir
from omniscient.group.models import iso, utcnow
from omniscient.home import Home
from omniscient.redact import redact_secrets

log = logging.getLogger(__name__)

MAX_TEXT = 20_000
MAX_RUNNING = 2
MAX_CHATS = 200
TITLE_MAX = 60
MAX_MESSAGE_CHARS = 8000  # one assistant / tool / error message (~8 KB)
DEFAULT_MAX_BUDGET_USD = 2.0  # per turn, passed to claude --max-budget-usd
MAX_TURNS_PER_HOUR = 30
HOUR_S = 3600.0
CHAT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
Publish = Callable[[str, dict[str, Any]], None]
Runner = Callable[[TurnSpec, Callable[[RunnerEvent], None], TurnHandle], TurnOutcome]


class ChatError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _text(raw: Any) -> str:
    if not isinstance(raw, str) or len(raw) > MAX_TEXT:
        raise ChatError(400, f"text : texte de {MAX_TEXT} caractères au plus attendu")
    if not raw.strip():
        raise ChatError(400, "text ne doit pas être vide")
    return raw


def _clip(text: str) -> str:
    return (
        text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "…"
    )


def _resolve(claude: Sequence[str]) -> tuple[str, ...]:
    """Resolve a bare `claude` command once through the daemon's PATH (logged)."""
    argv = tuple(claude)
    if not argv or "/" in argv[0]:
        return argv
    found = shutil.which(argv[0])
    if found is None:
        log.warning("omnigroup chat: %s not found in the daemon PATH", argv[0])
        return argv
    log.info("omnigroup chat: using %s", found)
    return (found, *argv[1:])


def _title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 1] + "…"


# ---- manager -------------------------------------------------------------------------


class ChatManager:
    """Chats in memory (under a lock) mirrored to disk; one worker thread per turn."""

    def __init__(
        self,
        home: Home,
        publish: Publish,
        claude: Sequence[str] = ("claude",),
        runner: Runner = chat_runner.run_turn,
        max_running: int = MAX_RUNNING,
        max_chats: int = MAX_CHATS,
        timeout_s: float = chat_runner.DEFAULT_TIMEOUT_S,
        max_budget_usd: float | None = DEFAULT_MAX_BUDGET_USD,
        max_turns_per_hour: int = MAX_TURNS_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.home = home
        self._publish = publish
        self._claude = _resolve(claude)
        self._runner = runner
        self._max_running = max_running
        self._max_chats = max_chats
        self._timeout_s = timeout_s
        self._budget = max_budget_usd
        self._max_per_hour = max_turns_per_hour
        self._clock = clock
        self._turns: deque[float] = deque()  # start times of the last hour's turns
        self._lock = threading.Lock()
        self._running: dict[str, TurnHandle] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._dir = home.state / "chats"
        self._chats: dict[str, Chat] = self._load_all()

    # -- public API (raises ChatError) --

    def create(self, project: str, text: Any) -> str:
        ref = self._project(project)
        prompt = _text(text)
        now = utcnow()
        chat = Chat(str(uuid.uuid4()), ref.name, _title(prompt), "running", now, now)
        with self._lock:
            self._reserve_slot()
            self._chats[chat.chat_id] = chat.with_message(Message("user", prompt, now))
            self._running[chat.chat_id] = TurnHandle()
        self._prune()
        self._start(chat.chat_id, ref, prompt, None)
        return chat.chat_id

    def send(self, chat_id: str, text: Any) -> None:
        prompt = _text(text)
        with self._lock:
            chat = self._get(chat_id)
            if chat_id in self._running:
                raise ChatError(409, "Claude répond encore : attends la fin du tour.")
            ref = self._project(chat.project)
            self._reserve_slot()
            now = utcnow()
            chat = replace(
                chat.with_message(Message("user", prompt, now)), state="running"
            )
            self._chats[chat_id] = chat
            self._running[chat_id] = TurnHandle()
        self._start(chat_id, ref, prompt, chat.claude_session_id)

    def stop(self, chat_id: str) -> bool:
        with self._lock:
            self._get(chat_id)
            handle = self._running.get(chat_id)
        if handle is None:
            return False
        handle.stop()
        return True

    def stop_all(self) -> int:
        """Stop every running turn (daemon shutdown); returns how many were stopped."""
        with self._lock:
            handles = list(self._running.values())
        for handle in handles:
            handle.stop()
        return len(handles)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            chats = sorted(
                self._chats.values(), key=lambda c: c.updated_at, reverse=True
            )
        return [c.summary() for c in chats]

    def get(self, chat_id: str) -> tuple[Chat, dict[str, Any]]:
        with self._lock:
            chat = self._get(chat_id)
        return chat, {
            "chat": chat.summary(),
            "messages": [m.to_json() for m in chat.messages],
        }

    def join(self, chat_id: str, timeout: float | None = None) -> None:
        """Wait for the chat's running turn (tests, shutdown)."""
        thread = self._threads.get(chat_id)
        if thread is not None:
            thread.join(timeout)

    def projects(self) -> tuple[ProjectRef, ...]:
        return list_projects(self.home)

    # -- internals --

    def _project(self, name: Any) -> ProjectRef:
        if not isinstance(name, str):
            raise ChatError(400, "project : nom de projet attendu")
        found = next((r for r in list_projects(self.home) if r.name == name), None)
        if found is None:
            raise ChatError(404, f"projet inconnu : {name[:80]}")
        return found

    def _get(self, chat_id: str) -> Chat:
        chat = self._chats.get(chat_id) if CHAT_ID.match(chat_id or "") else None
        if chat is None:
            raise ChatError(404, "conversation inconnue")
        return chat

    def _reserve_slot(self) -> None:
        """Under the lock: refuse a turn over the concurrency or hourly cap."""
        if len(self._running) >= self._max_running:
            raise ChatError(
                429, f"Déjà {self._max_running} conversation(s) en cours : attends."
            )
        now = self._clock()
        while self._turns and now - self._turns[0] >= HOUR_S:
            self._turns.popleft()
        if len(self._turns) >= self._max_per_hour:
            raise ChatError(
                429,
                f"Limite de {self._max_per_hour} tours par heure atteinte : réessaie plus tard.",
            )
        self._turns.append(now)

    def _start(
        self, chat_id: str, ref: ProjectRef, prompt: str, resume: str | None
    ) -> None:
        self._save(chat_id)
        self._emit(chat_id, "state", state="running")
        thread = threading.Thread(
            target=self._run, args=(chat_id, ref, prompt, resume), daemon=True,
            name=f"omnigroup-chat-{chat_id[:8]}",
        )  # fmt: skip
        self._threads[chat_id] = thread
        thread.start()

    def _run(
        self, chat_id: str, ref: ProjectRef, prompt: str, resume: str | None
    ) -> None:
        handle = self._running[chat_id]
        try:
            argv = chat_runner.build_argv(self._claude, resume, self._budget)
            spec = TurnSpec(argv, ref.path, prompt, self._timeout_s)
            outcome = self._runner(spec, lambda e: self._on_event(chat_id, e), handle)
        except Exception as exc:  # never leave a chat stuck in "running"
            log.exception("omnigroup chat: turn crashed")
            outcome = TurnOutcome(
                False, error=f"Erreur interne du démon : {type(exc).__name__}"
            )
        self._finish(chat_id, outcome)

    def _on_event(self, chat_id: str, event: RunnerEvent) -> None:
        now = utcnow()
        message: Message | None = None
        if event.kind == "text" and event.text:
            text = _clip(redact_secrets(event.text, keep_paths=True))
            message = Message("assistant", text, now)
        elif event.kind == "tool":
            message = Message("tool", _clip(event.text or ""), now, event.tool)
        with self._lock:
            chat = self._chats[chat_id]
            if event.session_id:
                chat = replace(chat, claude_session_id=event.session_id)
            if event.kind == "result" and event.cost_usd is not None:
                chat = replace(chat, cost_usd=event.cost_usd)
            if message is not None:
                chat = chat.with_message(message)
            self._chats[chat_id] = chat
        if message is not None:
            self._emit(
                chat_id,
                "text" if message.role == "assistant" else "tool",
                text=message.text,
                tool=message.tool,
            )
        elif event.kind == "result":
            self._emit(
                chat_id,
                "error" if event.is_error else "result",
                cost_usd=event.cost_usd,
            )

    def _finish(self, chat_id: str, outcome: TurnOutcome) -> None:
        state = "idle" if outcome.ok or outcome.stopped else "error"
        with self._lock:
            chat = replace(self._chats[chat_id], state=state)
            if outcome.session_id:
                chat = replace(chat, claude_session_id=outcome.session_id)
            if not outcome.ok and not outcome.stopped:
                error = _clip(outcome.error)
                chat = chat.with_message(Message("error", error, utcnow()))
            self._chats[chat_id] = chat
            self._running.pop(chat_id, None)
        if not outcome.ok and not outcome.stopped:
            self._emit(chat_id, "error", text=_clip(outcome.error))
        self._save(chat_id)
        self._emit(chat_id, "state", state=state)

    def _emit(self, chat_id: str, kind: str, **fields: Any) -> None:
        data = {"chat_id": chat_id, "kind": kind, "at": iso(utcnow())}
        data.update({k: v for k, v in fields.items() if v is not None})
        try:
            self._publish("chat", data)
        except Exception:
            log.exception("omnigroup chat: event not published")

    def _save(self, chat_id: str) -> None:
        with self._lock:
            chat = self._chats.get(chat_id)
        if chat is None:
            return
        try:
            write_private(secure_dir(self._dir) / f"{chat_id}.json", dump(chat))
        except OSError:
            log.exception("omnigroup chat: %s not saved", chat_id)

    def _prune(self) -> None:
        with self._lock:
            idle = sorted(
                (c for c in self._chats.values() if c.chat_id not in self._running),
                key=lambda c: c.updated_at,
            )
            extra = max(0, len(self._chats) - self._max_chats)
            dropped = [c.chat_id for c in idle[:extra]]
            for chat_id in dropped:
                self._chats.pop(chat_id, None)
        for chat_id in dropped:
            try:
                (self._dir / f"{chat_id}.json").unlink(missing_ok=True)
            except OSError:
                log.exception("omnigroup chat: %s not deleted", chat_id)

    def _load_all(self) -> dict[str, Chat]:
        chats: dict[str, Chat] = {}
        if not self._dir.is_dir():
            return chats
        for file in self._dir.glob("*.json"):
            if not CHAT_ID.match(file.stem):
                continue
            try:
                chat = load(file.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("omnigroup chat: %s ignored (%s)", file.name, exc)
                continue
            chats[chat.chat_id] = chat
        return chats


__all__ = [
    "Chat",
    "ChatError",
    "ChatManager",
    "Message",
    "ProjectRef",
    "list_projects",
    "path_label",
]
