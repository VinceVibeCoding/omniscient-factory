"""Deterministic idea operations for the studio (the shared contract).

The studio TUI, the `omniscient idea ...` commands and the Claude Code atelier skills
all change the vault ONLY through these functions, so notes keep one valid shape.
Machine strings (keys, statuses) stay English; headings shown to the owner are French.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from omniscient import i18n
from omniscient.brain import Idea, IdeaScores
from omniscient.brain.models import BrainError
from omniscient.brain.notes import write_idea

IDEAS_DIR = "ideas"
H_ORIGIN = "## Origine"
H_ATELIER = "## Atelier"
ORIGINS = ("council", "owner", "remix", "variant")
NEUTRAL = 0.5
_HEADING = re.compile(r"^## ", re.MULTILINE)
_TOP_HEADING = re.compile(r"^(#{1,2}) ", re.MULTILINE)
_VERDICT = re.compile(r"\*\*Verdict ?:\*\*\s*(\w+)")
VERDICT_STATUS = {
    "poursuivre": "debated",
    "remodeler": "debated",
    "abandonner": "dropped",
}


class StudioError(Exception):
    """Unknown idea, invalid input, or a note that cannot be changed safely."""


@dataclass(frozen=True)
class IdeaCard:
    """What the studio shows for one idea note (read-only snapshot)."""

    stem: str
    path: Path
    title: str
    status: str
    kind: str
    score: float | None  # None until debated (seeds)
    created: str
    projects: tuple[str, ...]
    sources: tuple[str, ...]
    origin: str  # one of ORIGINS
    parent: str | None  # stem of the idea this is a variant of
    parents: tuple[str, ...]  # stems a remix combines
    variant: int  # 1 for an original, 2.. for variants
    poc_url: str | None
    poc_verdict: str | None
    owner_verdict: str | None


def resolve(vault: Path, stem: str) -> Path:
    """The note for a stem, refusing anything outside vault/ideas."""
    ideas = (vault / IDEAS_DIR).resolve()
    try:
        path = (ideas / f"{stem}.md").resolve()
    except (OSError, ValueError) as exc:  # NUL bytes, absurd lengths
        raise StudioError(f"no idea note named {stem!r}") from exc
    if path.parent != ideas or not path.is_file():
        raise StudioError(f"no idea note named {stem!r}")
    return path


def _tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(v) for v in (value if isinstance(value, list) else [value]))


def load_card(path: Path) -> IdeaCard:
    """Read one note; a malformed or vanished note raises StudioError."""
    try:
        return _card(path, frontmatter.load(path).metadata)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise StudioError(f"unreadable idea note {path.name}: {exc}") from exc


def _card(path: Path, meta: dict[str, Any]) -> IdeaCard:
    score = meta.get("score")
    return IdeaCard(
        stem=path.stem,
        path=path,
        title=str(meta.get("title") or path.stem),
        status=str(meta.get("status", "seed")),
        kind=str(meta.get("kind", "new-build")),
        score=float(score) if isinstance(score, int | float) else None,
        created=str(meta.get("created", "")),
        projects=_tuple(meta.get("projects")),
        sources=_tuple(meta.get("sources")),
        origin=str(meta.get("origin", "council")),
        parent=str(meta["parent"]) if meta.get("parent") else None,
        parents=_tuple(meta.get("parents")),
        variant=int(meta.get("variant", 1)),
        poc_url=meta.get("poc_url"),
        poc_verdict=meta.get("poc_verdict"),
        owner_verdict=meta.get("owner_verdict"),
    )


def list_ideas(
    vault: Path, statuses: frozenset[str] | None = None
) -> tuple[IdeaCard, ...]:
    """Every readable idea (optionally only some statuses), best score first."""
    cards, _ = scan_ideas(vault)
    if statuses is not None:
        cards = [c for c in cards if c.status in statuses]
    return tuple(
        sorted(
            cards,
            key=lambda c: (c.score if c.score is not None else -1.0, c.created),
            reverse=True,
        )
    )


def scan_ideas(vault: Path) -> tuple[list[IdeaCard], tuple[str, ...]]:
    """(readable cards, names of notes that could not be read) — never raises."""
    folder = vault / IDEAS_DIR
    if not folder.is_dir():
        return [], ()
    cards: list[IdeaCard] = []
    bad: list[str] = []
    for path in sorted(folder.glob("*.md")):
        try:
            cards.append(load_card(path))
        except StudioError:
            bad.append(path.name)
    return cards, tuple(bad)


def tree_order(cards: tuple[IdeaCard, ...]) -> tuple[tuple[IdeaCard, int], ...]:
    """Cards in display order with their depth: each variant right under its parent."""
    stems = {c.stem for c in cards}
    children: dict[str, list[IdeaCard]] = {}
    for card in cards:
        if card.parent in stems:
            children.setdefault(card.parent, []).append(card)
    out: list[tuple[IdeaCard, int]] = []

    def visit(card: IdeaCard, depth: int) -> None:
        out.append((card, depth))
        for child in sorted(children.get(card.stem, []), key=lambda c: c.variant):
            visit(child, depth + 1)

    for card in cards:
        if card.parent not in stems:
            visit(card, 0)
    shown = {c.stem for c, _ in out}
    out.extend((c, 0) for c in cards if c.stem not in shown)  # parent cycles
    return tuple(out)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _update(path: Path, content: str | None = None, **meta: Any) -> None:
    post = frontmatter.load(path)
    for key, value in meta.items():
        if value is None:
            post.metadata.pop(key, None)
        else:
            post[key] = value
    if content is not None:
        post.content = content
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def safe_text(text: str) -> str:
    """Demote '# ' / '## ' lines to '### ' so agent text never opens a top section."""
    return _TOP_HEADING.sub("### ", text.strip())


def _section(content: str, heading: str) -> tuple[int, int] | None:
    """(body start, body end) of an exact '## X' line's section, or None."""
    match = re.search(rf"^{re.escape(heading)}[ \t]*$\n?", content, re.MULTILINE)
    if match is None:
        return None
    nxt = _HEADING.search(content, match.end())
    return match.end(), nxt.start() if nxt else len(content)


def _set_section(content: str, heading: str, body: str) -> str:
    """Replace a '## X' section's body (up to the next '## '), or append the section."""
    bounds = _section(content, heading)
    if bounds is None:
        return f"{content.rstrip()}\n\n{heading}\n{body.strip()}\n"
    after, end = bounds
    tail = f"\n{content[end:]}" if end < len(content) else "\n"
    return f"{content[:after]}{body.strip()}\n{tail}"


def new_seed(
    vault: Path,
    *,
    title: str,
    pitch: str,
    projects: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
    kind: str = "new-build",
    origin: str = "owner",
    parents: tuple[str, ...] = (),
    now: datetime | None = None,
) -> Path:
    """Write a new undebated idea (status seed, no score yet)."""
    if origin not in ORIGINS:
        raise StudioError(f"origin must be one of {ORIGINS}")
    for stem in parents:
        resolve(vault, stem)
    try:
        idea = Idea(
            title=title.strip(),
            pitch=safe_text(pitch),
            status="seed",
            scores=IdeaScores(*(NEUTRAL,) * 5),
            sources=sources,
            projects=projects,
            created=_now(now),
            kind=kind,  # type: ignore[arg-type]
        )
        path = write_idea(vault, idea, score=0.0)
    except BrainError as exc:
        raise StudioError(str(exc)) from exc
    content = frontmatter.load(path).content
    if parents:
        links = ", ".join(f"[[{s}]]" for s in parents)
        content = _set_section(content, H_ORIGIN, f"Remix de {links}")
    _update(
        path,
        content,
        score=None,
        scores=None,
        origin=origin,
        parents=list(parents) or None,
    )
    return path


def fork_variant(
    vault: Path,
    stem: str,
    *,
    pitch: str,
    title: str | None = None,
    now: datetime | None = None,
) -> Path:
    """A variant of an idea: same projects, sources and kind; new pitch; status seed."""
    parent = load_card(resolve(vault, stem))
    siblings = [c for c in list_ideas(vault) if c.parent == stem]
    number = max([parent.variant, *(c.variant for c in siblings)]) + 1
    path = new_seed(
        vault,
        title=title or f"v{number} · {parent.title}",  # prefix: slugs are truncated
        pitch=pitch,
        projects=parent.projects,
        sources=parent.sources,
        kind=parent.kind,
        origin="variant",
        now=now,
    )
    content = _set_section(
        frontmatter.load(path).content, H_ORIGIN, f"Variante de [[{stem}]]"
    )
    _update(path, content, parent=stem, variant=number)
    return path


def append_atelier(
    vault: Path, stem: str, text: str, *, now: datetime | None = None
) -> Path:
    """Add a dated entry to the idea's '## Atelier' section (the riff log)."""
    if not text.strip():
        raise StudioError("nothing to append")
    path = resolve(vault, stem)
    content = frontmatter.load(path).content
    stamp = _now(now).astimezone().strftime("%d-%m-%Y %H:%M")
    entry = f"**{stamp}**\n{safe_text(text)}"
    bounds = _section(content, H_ATELIER)
    current = content[bounds[0] : bounds[1]].strip() if bounds else ""
    content = _set_section(content, H_ATELIER, f"{current}\n\n{entry}".strip())
    _update(path, content)
    return path


def record_debate(
    vault: Path,
    stem: str,
    *,
    council: str,
    scores: IdeaScores,
    weights: dict[str, float],
) -> Path:
    """Write the council's verdict and scores.

    Like the factory: 'abandonner' drops a seed/debated idea; otherwise a seed becomes
    'debated' (and so eligible for the next cycle's planning if it scores high enough).
    """
    if not council.strip():
        raise StudioError("council text is empty")
    match = _VERDICT.search(council)
    if match is None or match.group(1) not in VERDICT_STATUS:
        raise StudioError(
            f"council must start with {i18n.L_VERDICT} poursuivre|remodeler|abandonner"
        )
    path = resolve(vault, stem)
    card = load_card(path)
    try:
        score = scores.weighted(weights)
    except BrainError as exc:
        raise StudioError(str(exc)) from exc
    content = _set_section(
        frontmatter.load(path).content, i18n.H_COUNCIL, safe_text(council)
    )
    status = card.status
    if card.status in ("seed", "debated"):
        status = VERDICT_STATUS[match.group(1)]
    _update(
        path,
        content,
        scores={k: getattr(scores, k) for k in weights},
        score=round(score, 4),
        status=status,
    )
    return path
