from pathlib import Path
from typing import Any

import frontmatter

from omniscient.learn.sync import sync_learnings

NOTE = (
    "---\nname: DeepMind blog listing dates\n"
    "description: How to date items from the deepmind.google/blog listing\n"
    "type: reference\n---\nThe listing shows only month and year.\n"
)


class _Learning:
    def __init__(self, pages: dict[str, list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str | None]] = []

    def notes(
        self, agent_id: str, *, status: str, cursor: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((agent_id, cursor))
        items = self.pages.get(agent_id, [])
        if cursor is None and len(items) > 1:
            return {"items": items[:1], "next_cursor": "page-2"}
        return {"items": items[1:] if cursor else items, "next_cursor": None}


class _Client:
    def __init__(self, pages: dict[str, list[dict[str, Any]]]) -> None:
        self.learning = _Learning(pages)


def _note(file: str, text: str = NOTE) -> dict[str, Any]:
    return {"file": file, "status": "active", "text": text, "etag": '"e1"'}


def test_sync_mirrors_active_notes_with_tech_frontmatter(tmp_path: Path) -> None:
    client = _Client({"news-scout": [_note("deepmind-blog-dates.md")]})

    result = sync_learnings(client, ("news-scout",), tmp_path)

    path = tmp_path / "tech" / "learned" / "news-scout" / "deepmind-blog-dates.md"
    post = frontmatter.load(path)
    assert result.written == (path,)
    assert post["type"] == "tech"
    assert post["kind"] == "learned"
    assert post["agent"] == "news-scout"
    assert post["name"] == "DeepMind blog listing dates"
    assert "month and year" in post.content


def test_sync_follows_pagination(tmp_path: Path) -> None:
    client = _Client({"news-scout": [_note("a.md"), _note("b.md")]})

    result = sync_learnings(client, ("news-scout",), tmp_path)

    assert len(result.written) == 2
    assert client.learning.calls == [("news-scout", None), ("news-scout", "page-2")]


def test_sync_is_idempotent(tmp_path: Path) -> None:
    client = _Client({"news-scout": [_note("a.md")]})
    sync_learnings(client, ("news-scout",), tmp_path)

    second = sync_learnings(client, ("news-scout",), tmp_path)

    assert second.written == ()
    assert len(second.unchanged) == 1


def test_sync_removes_notes_no_longer_active(tmp_path: Path) -> None:
    sync_learnings(
        _Client({"news-scout": [_note("old.md")]}), ("news-scout",), tmp_path
    )

    result = sync_learnings(_Client({"news-scout": []}), ("news-scout",), tmp_path)

    assert [p.name for p in result.removed] == ["old.md"]
    assert not (tmp_path / "tech" / "learned" / "news-scout" / "old.md").exists()


def test_sync_rejects_unsafe_filenames(tmp_path: Path) -> None:
    client = _Client({"news-scout": [_note("../../AGENTS.md"), _note("ok.md")]})

    result = sync_learnings(client, ("news-scout",), tmp_path)

    assert [p.name for p in result.written] == ["ok.md"]
    assert result.rejected == ("../../AGENTS.md",)
    assert not (tmp_path.parent / "AGENTS.md").exists()


def test_sync_keeps_note_without_frontmatter(tmp_path: Path) -> None:
    client = _Client({"news-scout": [_note("plain.md", "Just a fact.\n")]})

    sync_learnings(client, ("news-scout",), tmp_path)

    post = frontmatter.load(tmp_path / "tech" / "learned" / "news-scout" / "plain.md")
    assert post["agent"] == "news-scout"
    assert post.content.strip() == "Just a fact."
