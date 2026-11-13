"""herdr answers are claimed against the dialog they were shown for (H2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniscient.group.collect_herdr import dialog_hash
from omniscient.group.store import Store
from tests.test_group_collect import FakeHerdr, agent, collector


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "group.db")


def test_pending_carries_the_dialog_hash(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")], dialog="Bash: ls\n❯ 1. Yes")
    collector(fake).poll(store)
    pending = store.pending_for("herdr:w1:p1")[0]
    assert pending.payload["detection_sha256"] == dialog_hash("Bash: ls\n❯ 1. Yes")
    assert pending.payload["answerable"] is True


def test_changed_dialog_is_never_answered(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")], dialog="Bash: ls")
    col = collector(fake)
    col.poll(store)
    pending = store.pending_for("herdr:w1:p1")[0]
    fake.dialog = "Bash: rm -rf ~"  # another request took its place
    ok, message = col.answer(pending, {"decision": "allow"})
    assert not ok and "changé" in message
    assert fake.sent_keys() == []
    fake.dialog = "Bash: ls"
    assert col.answer(pending, {"decision": "allow"})[0] is True
    assert fake.sent_keys() == [["agent", "send-keys", "w1:p1", "enter"]]


def test_pending_without_hash_is_refused(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked")])
    col = collector(fake)
    col.poll(store)
    pending = store.pending_for("herdr:w1:p1")[0]
    legacy = pending.__class__(
        **{**pending.__dict__, "payload": {**pending.payload, "detection_sha256": ""}}
    )
    assert col.answer(legacy, {"decision": "deny"})[0] is False
    assert fake.sent_keys() == []


def test_unverified_agents_answer_in_the_terminal(store: Store) -> None:
    fake = FakeHerdr([agent("w1:p1", "blocked", agent="codex")])
    col = collector(fake)
    col.poll(store)
    pending = store.pending_for("herdr:w1:p1")[0]
    assert pending.payload["answerable"] is False and pending.options == ("deny",)
    ok, message = col.answer(pending, {"decision": "deny"})
    assert not ok and "réponds dans le terminal" in message
    assert fake.sent_keys() == []
