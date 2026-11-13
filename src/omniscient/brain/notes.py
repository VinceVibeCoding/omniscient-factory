"""Read and write brain notes as markdown with YAML frontmatter."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import get_args

import frontmatter
import yaml

from omniscient import i18n
from omniscient.brain.models import BrainError, Idea, IdeaStatus, NewsItem

NEWS_DIR = "news"
IDEAS_DIR = "ideas"
SLUG_MAX_LENGTH = 60
FILENAME_ID_LENGTH = 8


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-") or "untitled"


def slugify(title: str) -> str:
    """Filesystem- and URL-safe slug, as used for note and map names."""
    return _slug(title)


def set_idea_status(path: Path, status: str) -> None:
    """Change an idea note's status, keeping everything else as written."""
    if status not in get_args(IdeaStatus):
        raise BrainError(f"status must be one of {get_args(IdeaStatus)}")
    post = frontmatter.load(path)
    post["status"] = status
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def _dump(path: Path, metadata: dict[str, object], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        frontmatter.dumps(frontmatter.Post(body, **metadata)) + "\n", "utf-8"
    )


def find_news(vault: Path, item_id: str) -> Path | None:
    """Return the note for a news id, or None. Filenames carry the first id chars."""
    for path in (vault / NEWS_DIR).glob(f"* {item_id[:FILENAME_ID_LENGTH]}.md"):
        if frontmatter.load(path).get("id") == item_id:
            return path
    return None


def write_news(vault: Path, item: NewsItem) -> Path:
    """Write a news note; if one with the same URL exists, keep it and return its path."""
    existing = find_news(vault, item.id)
    if existing is not None:
        return existing
    name = f"{item.published.isoformat()} {_slug(item.title)} {item.id[:FILENAME_ID_LENGTH]}.md"
    path = vault / NEWS_DIR / name
    metadata = {
        "type": "news",
        "id": item.id,
        "source": item.source,
        "url": item.url,
        "title": item.title,
        "published": item.published.isoformat(),
        "ingested": item.ingested.isoformat(),
        "tags": list(item.tags),
        "summary": item.summary,
    }
    _dump(path, metadata, f"# {item.title}\n\n{item.summary}\n\n[Source]({item.url})")
    return path


def read_news(path: Path) -> NewsItem:
    try:
        meta = frontmatter.load(path).metadata
        return NewsItem(
            source=meta["source"],
            url=meta["url"],
            title=meta["title"],
            published=date.fromisoformat(meta["published"]),
            ingested=datetime.fromisoformat(meta["ingested"]),
            summary=meta.get("summary", ""),
            tags=tuple(meta.get("tags", [])),
        )
    except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError) as exc:
        raise BrainError(f"{path.name}: not a valid news note: {exc!r}") from exc


def _news_link(vault: Path, item_id: str) -> str:
    """An Obsidian wikilink to the news note for an id, or the bare id if unknown."""
    path = find_news(vault, item_id)
    return f"[[{path.stem}]]" if path is not None else item_id


def write_idea(vault: Path, idea: Idea, score: float, council: str = "") -> Path:
    """Write a new idea note. Refuses to overwrite: ideas evolve through edits, not rewrites."""
    path = (
        vault / IDEAS_DIR / f"{idea.created.date().isoformat()} {_slug(idea.title)}.md"
    )
    if path.exists():
        raise BrainError(f"idea note already exists: {path.name}")
    metadata = {
        "type": "idea",
        "title": idea.title,
        "status": idea.status,
        "kind": idea.kind,
        "scores": asdict(idea.scores),
        "score": round(score, 4),
        "sources": list(idea.sources),
        "projects": list(idea.projects),
        "created": idea.created.isoformat(),
    }
    projects = ", ".join(f"[[{p}]]" for p in idea.projects) or i18n.NEW_BUILD
    sources = (
        "\n".join(f"- {_news_link(vault, s)}" for s in idea.sources) or f"- {i18n.NONE}"
    )
    body = (
        f"# {idea.title}\n\n{idea.pitch}\n\n{i18n.H_PROJECTS}\n{projects}\n\n"
        f"{i18n.H_SOURCES}\n{sources}\n\n{i18n.H_COUNCIL}\n{council.strip() or i18n.COUNCIL_PENDING}"
    )
    _dump(path, metadata, body)
    return path
