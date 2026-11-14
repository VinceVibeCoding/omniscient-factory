"""ChatManager: projects from the registry, limits, events, persistence (fake runner)."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import chat as ch
from omniscient.group.chat_runner import RunnerEvent, TurnHandle, TurnOutcome, TurnSpec
from omniscient.home import Home

REPO_MD = """---
name: {name}
path: {path}
purpose: demo project
git: true
---
"""


@pytest.fixture
def home(tmp_path: Path, isolated_home: Path) -> Home:
    root = isolated_home
    proj = tmp_path / "work" / "alpha"
    proj.mkdir(parents=True)
    (root / "repos" / "alpha.md").write_text(
        REPO_MD.format(name="alpha", path=proj), encoding="utf-8"
    )
    ghost = tmp_path / "work" / "ghost-missing"
    (root / "repos" / "ghost.md").write_text(
        REPO_MD.format(name="ghost", path=ghost), encoding="utf-8"
    )
    return Home(root)


class FakeRunner:
    def __init__(self, ok: bool = True, block: threading.Event | None = None) -> None:
        self.specs: list[TurnSpec] = []
        self.ok = ok
        self.block = block

    def __call__(
        self, spec: TurnSpec, on_event: Any, handle: TurnHandle
    ) -> TurnOutcome:
        self.specs.append(spec)
        on_event(RunnerEvent("init", session_id="sess-1"))
        if self.block is not None:
            self.block.wait(5)
            if handle.stop_requested.is_set():
                return TurnOutcome(False, "sess-1", None, "Tour arrêté.", stopped=True)
        on_event(
            RunnerEvent(
                "text", text="Réponse token=sk-ant-abcdefghijklmnopqrstuvwxyz0123456"
            )
        )
        on_event(RunnerEvent("tool", text="ls", tool="Bash"))
        if not self.ok:
            return TurnOutcome(
                False, "sess-1", None, "Claude Code a échoué (code 1) : raté"
            )
        on_event(RunnerEvent("result", text="fin", cost_usd=0.02, session_id="sess-1"))
        return TurnOutcome(True, "sess-1", 0.02)


def manager(
    home: Home, runner: Any, events: list | None = None, **kw: Any
) -> ch.ChatManager:
    sink = events if events is not None else []
    return ch.ChatManager(
        home, lambda kind, data: sink.append((kind, data)), runner=runner, **kw
    )


def test_projects_come_from_registry_plus_home(home: Home) -> None:
    refs = ch.list_projects(home)
    names = [r.name for r in refs]
    assert "alpha" in names and "ghost" not in names  # missing folder skipped
    assert home.root.name in names
    assert all(not r.path_label.startswith("/") for r in refs)


def test_path_label_never_absolute(tmp_path: Path) -> None:
    assert ch.path_label(Path.home() / "projects" / "x") == "~/projects/x"
    assert ch.path_label(Path("/srv/elsewhere/proj")) == "proj"


def test_create_runs_a_turn_and_publishes(home: Home) -> None:
    events: list = []
    runner = FakeRunner()
    mgr = manager(home, runner, events)
    chat_id = mgr.create("alpha", "Bonjour Claude")
    mgr.join(chat_id, 5)
    spec = runner.specs[0]
    assert spec.text == "Bonjour Claude" and spec.cwd.name == "alpha"
    assert "--resume" not in spec.argv
    _, detail = mgr.get(chat_id)
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert "sk-ant-" not in detail["messages"][1]["text"]
    assert (
        detail["chat"]["state"] == "idle"
        and detail["chat"]["title"] == "Bonjour Claude"
    )
    kinds = [d["kind"] for k, d in events if k == "chat"]
    assert (
        kinds[0] == "state"
        and "text" in kinds
        and "result" in kinds
        and kinds[-1] == "state"
    )
    assert all(d["chat_id"] == chat_id for _, d in events)


def test_send_resumes_the_claude_session(home: Home) -> None:
    runner = FakeRunner()
    mgr = manager(home, runner)
    chat_id = mgr.create("alpha", "un")
    mgr.join(chat_id, 5)
    mgr.send(chat_id, "deux")
    mgr.join(chat_id, 5)
    argv = list(runner.specs[1].argv)
    assert argv[argv.index("--resume") + 1] == "sess-1"


def test_validation_errors(home: Home) -> None:
    mgr = manager(home, FakeRunner())
    with pytest.raises(ch.ChatError) as unknown:
        mgr.create("nope", "x")
    assert unknown.value.status == 404
    with pytest.raises(ch.ChatError):
        mgr.create("alpha", "   ")
    with pytest.raises(ch.ChatError) as long:
        mgr.create("alpha", "x" * (ch.MAX_TEXT + 1))
    assert long.value.status == 400
    with pytest.raises(ch.ChatError) as missing:
        mgr.send("00000000-0000-4000-8000-000000000000", "x")
    assert missing.value.status == 404
    with pytest.raises(ch.ChatError):
        mgr.get("../etc/passwd")


def test_concurrency_limit_busy_chat_and_stop(home: Home) -> None:
    gate = threading.Event()
    mgr = manager(home, FakeRunner(block=gate), max_running=1)
    first = mgr.create("alpha", "long")
    with pytest.raises(ch.ChatError) as busy:
        mgr.create("alpha", "autre")
    assert busy.value.status == 429
    with pytest.raises(ch.ChatError) as running:
        mgr.send(first, "encore")
    assert running.value.status == 409
    assert mgr.stop(first) is True
    gate.set()
    mgr.join(first, 5)
    _, detail = mgr.get(first)
    assert detail["chat"]["state"] == "idle"
    assert mgr.stop(first) is False


def test_failure_marks_error_and_keeps_message(home: Home) -> None:
    mgr = manager(home, FakeRunner(ok=False))
    chat_id = mgr.create("alpha", "x")
    mgr.join(chat_id, 5)
    _, detail = mgr.get(chat_id)
    assert detail["chat"]["state"] == "error"
    assert detail["messages"][-1]["role"] == "error"


def test_persistence_and_permissions(home: Home) -> None:
    mgr = manager(home, FakeRunner())
    chat_id = mgr.create("alpha", "garde-moi")
    mgr.join(chat_id, 5)
    folder = home.state / "chats"
    file = folder / f"{chat_id}.json"
    assert stat.S_IMODE(os.stat(file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    again = manager(home, FakeRunner())
    chats = again.list()
    assert [c["chat_id"] for c in chats] == [chat_id]
    assert "claude_session_id" not in chats[0]
    again.send(chat_id, "suite")
    again.join(chat_id, 5)


def test_old_chats_are_pruned(home: Home) -> None:
    mgr = manager(home, FakeRunner(), max_chats=2)
    ids = []
    for n in range(3):
        ids.append(mgr.create("alpha", f"n{n}"))
        mgr.join(ids[-1], 5)
    listed = [c["chat_id"] for c in mgr.list()]
    assert ids[0] not in listed and len(listed) == 2
    assert not (home.state / "chats" / f"{ids[0]}.json").exists()


def test_stop_all_without_running_turns_returns_zero(tmp_path) -> None:
    from omniscient.group.chat import ChatManager
    from omniscient.home import find_home

    manager = ChatManager(find_home(), lambda kind, data: None)
    assert manager.stop_all() == 0
