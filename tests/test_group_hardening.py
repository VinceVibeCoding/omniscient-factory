"""omnigroup hardening units: « Toujours » rules (M2), previews and payload redaction
(M1, M3), wait tickets and presence, file modes and widget tokens (M4)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from omniscient.group import credentials as cred
from omniscient.group.gates import Presence, TicketBook
from omniscient.group.hooks import translate
from omniscient.group.models import ModelError, Pending
from omniscient.group.permission_rules import always_permissions, rule_labels
from omniscient.group.sanitize import PAYLOAD_TEXT_MAX
from omniscient.group.store import Store
from omniscient.home import Home
from omniscient.redact import SECRET_MARK


def perm(tool: str, args: Any, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": "s",
        "hook_event_name": "PermissionRequest",
        "tool_name": tool,
        "tool_input": args,
        **extra,
    }


# ---- M2 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "label"),
    [
        ("Bash", {"command": "git status"}, "Bash(git status)"),
        ("Write", {"file_path": "/w/a.py"}, "Write(//w/a.py)"),
        ("Edit", {"file_path": "/w/a.py"}, "Edit(//w/a.py)"),
        ("NotebookEdit", {"notebook_path": "/w/n.ipynb"}, "NotebookEdit(//w/n.ipynb)"),
        ("WebFetch", {"url": "https://x"}, "WebFetch"),
    ],
)
def test_always_rule_is_exact_for_risky_tools(tool: str, args: Any, label: str) -> None:
    updates = always_permissions(tool, args, perm(tool, args))
    assert updates is not None and rule_labels(updates) == [label]
    assert all(u["destination"] == "session" for u in updates)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("Bash", {}),
        ("Bash", {"command": "rm *.tmp"}),  # a wildcard would match more
        ("Bash", {"command": "a\nb"}),
        ("Edit", {"file_path": "relative.py"}),
        ("Write", {"file_path": "/w/[ab].py"}),
    ],
)
def test_no_always_without_an_exact_rule(tool: str, args: Any) -> None:
    assert always_permissions(tool, args, perm(tool, args)) is None
    t = translate("claude", perm(tool, args))
    assert t.pending is not None and t.pending.options == ("allow", "deny")


def test_session_suggestions_win_and_others_are_ignored() -> None:
    suggestions = [
        {"type": "setMode", "mode": "acceptEdits", "destination": "session"},
        {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
         "behavior": "allow", "destination": "localSettings"},
        {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm run"}],
         "behavior": "allow", "destination": "session"},
        {"type": "addRules", "rules": [{"toolName": "Bash"}],
         "behavior": "allow", "destination": "session"},
    ]  # fmt: skip
    event = perm("Bash", {"command": "npm run x"}, permission_suggestions=suggestions)
    updates = always_permissions("Bash", {"command": "npm run x"}, event)
    assert rule_labels(updates) == ["Bash(npm run)"]  # never the bare Bash rule


# ---- M1 / M3 -----------------------------------------------------------------------------


def test_preview_keeps_paths_masks_secrets_and_flags_truncation() -> None:
    cmd = "cat /home/alice/.env && curl -H 'Authorization: Bearer abcdefghijkl'"
    t = translate("claude", perm("Bash", {"command": cmd}))
    assert t.pending is not None
    preview = t.pending.payload["tool_input"]
    assert "/home/alice/.env" in preview and "abcdefghijkl" not in preview
    assert SECRET_MARK in preview and "truncated" not in t.pending.payload
    huge = translate("claude", perm("Write", {"file_path": "/w/a", "content": "x" * 70_000}))  # fmt: skip
    assert huge.pending is not None
    assert huge.pending.payload["truncated"] > PAYLOAD_TEXT_MAX
    assert len(huge.pending.payload["tool_input"]) <= PAYLOAD_TEXT_MAX


def test_every_payload_leaf_is_redacted_but_hashes_survive() -> None:
    payload = {
        "question": "use ghp_" + "a" * 36 + " from /home/bob/x?",
        "nested": {"k": ["API_KEY=xyz"]},
        "detection_sha256": "ab" * 32,
        "figure": 3,
    }
    p = Pending.new("agentrun:r", "question", payload=payload)
    assert "ghp_" not in p.payload["question"] and "/home/bob/" not in p.payload["question"]  # fmt: skip
    assert p.payload["nested"]["k"] == ["API_KEY=[masqué]"]
    assert p.payload["detection_sha256"] == "ab" * 32 and p.payload["figure"] == 3
    assert Pending.new("agentrun:r", "question", payload=p.payload).payload == p.payload


def test_oversized_payloads_are_refused() -> None:
    with pytest.raises(ModelError):
        Pending.new("s:x", "question", payload={f"k{i}": "y" * 60_000 for i in range(5)})  # fmt: skip


# ---- gates ------------------------------------------------------------------------------


def test_tickets_are_single_use_bound_and_expire() -> None:
    now = [0.0]
    book = TicketBook(clock=lambda: now[0])
    ticket = book.issue("p1")
    assert not book.redeem("p2", ticket) and not book.redeem("p1", "nope")
    assert book.redeem("p1", ticket) and not book.redeem("p1", ticket)
    late = book.issue("p3")
    now[0] += 301
    assert not book.redeem("p3", late)


def test_presence_counts_streams_and_remembers_recent_widgets() -> None:
    now = [0.0]
    presence = Presence(clock=lambda: now[0], max_streams=1)
    assert not presence.widget_present()
    assert presence.enter() and not presence.enter()
    now[0] += 100
    assert presence.widget_present()  # still connected
    presence.leave()
    now[0] += 29
    assert presence.widget_present()
    now[0] += 2
    assert not presence.widget_present()


# ---- M4 -----------------------------------------------------------------------------------


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_state_folder_database_and_token_are_private(tmp_path: Path) -> None:
    home = Home(tmp_path)
    home.state.mkdir(mode=0o755)
    db = cred.db_path(home)
    db.touch(mode=0o644)
    wal = db.with_name("group.db-wal")
    wal.touch(mode=0o644)
    cred.load_or_create_hook_token(home)
    Store(db).close()
    assert mode(home.state) == 0o700
    assert mode(db) == 0o600 and mode(wal) == 0o600
    assert mode(cred.hook_token_path(home)) == 0o600
    leftovers = [p.name for p in home.state.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_widget_tokens_keep_the_eight_newest_hashes(tmp_path: Path) -> None:
    tokens = cred.WidgetTokens(tmp_path / "group.db")
    minted = [tokens.mint(f"w{i}") for i in range(10)]
    assert not tokens.is_valid(minted[0]) and tokens.is_valid(minted[-1])
    assert len(tokens.hashes()) == cred.MAX_WIDGETS
    assert not tokens.is_valid("") and tokens.revoke_all() == cred.MAX_WIDGETS
    tokens.close()


def test_question_texts_keep_their_paths_they_are_answer_keys() -> None:
    qs = [{"question": "Lire /home/alice/a.txt ?", "options": [{"label": "Oui"}]}]
    p = Pending.new("claude:s", "question", payload={"questions": qs})
    assert p.payload["questions"][0]["question"] == "Lire /home/alice/a.txt ?"
