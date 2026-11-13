"""Where the war room's events come from: AgentRun runs, idea notes, the cost log, herdr.

Every parser here is a pure function over already-fetched data (tested on fixtures);
the few functions that touch the outside world take an injectable fetcher/runner and
turn any failure into a French warning instead of crashing the screen.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from omniscient import i18n, ops
from omniscient.studio.ideas import H_ATELIER, IDEAS_DIR, _section

FACTORY = "usine"  # shown for events not tied to one idea
MAX_RUN_PAGES = 5  # 5 x 20 newest runs is plenty for a live timeline
TITLE_KEY_LEN = 30
TITLE_KEY_MIN = 10
COST_DEDUP = timedelta(minutes=15)  # a cost row this close to its run is the same work

ACTORS = {
    "visionary": "visionnaire",
    "council": "conseil",
    "strategist": "stratège",
    "builder": "bâtisseur",
    "debater": "débatteur",
    "griller": "questionneur",
    "news-scout": "éclaireur",
    "translator": "traducteur",
    "scout-formatter": "formateur",
}
VERBS = {
    "visionnaire": "imagine",
    "conseil": "délibère",
    "stratège": "planifie",
    "bâtisseur": "construit",
    "débatteur": "répond",
    "questionneur": "questionne",
    "chercheur": "cherche",
    "éclaireur": "veille",
    "traducteur": "traduit",
    "formateur": "formate",
}
TASK_VERBS = {
    "ingest": "veille",
    "think": "imagine",
    "plan": "planifie",
    "build": "construit",
    "grill": "questionne",
    "work": "cherche",
    "slack": "répond",
    "chat": "discute",
}
RUN_STATUS = {
    "completed": "✓ terminé",
    "failed": "✗ échec",
    "cancelled": "⊘ annulé",
    "working": "● en cours",
    "queued": "◌ en file",
    "input_required": "⏸ attend",
    "paused": "⏸ en pause",
}
LIVE_RUN_STATE = {
    "working": "working",
    "queued": "idle",
    "input_required": "blocked",
    "paused": "blocked",
}
ORIGIN_EVENT = {
    "council": ("visionnaire", "propose"),
    "owner": ("propriétaire", "sème"),
    "variant": ("atelier", "dérive"),
    "remix": ("atelier", "remixe"),
}
_VERDICT = re.compile(r"\*\*Verdict ?:\*\*\s*(\w+)")
_ATELIER_ENTRY = re.compile(
    r"^\*\*(\d{2}-\d{2}-\d{4} \d{2}:\d{2})\*\*[ \t]*\n(.*?)(?=^\*\*\d{2}-\d{2}-\d{4} |\Z)",
    re.MULTILINE | re.DOTALL,
)
_SPEAKER = re.compile(r"^\*\*([^*\n]{1,40}?) ?:\*\*\s*(.*)$", re.MULTILINE)
_SPACES = re.compile(r"\s+")

RunsFetcher = Callable[[str | None], Mapping[str, Any]]


class SourceError(Exception):
    """A live source (AgentRun, herdr) could not be read."""


@dataclass(frozen=True)
class WarEvent:
    """Who did what, on which idea (None = the factory), when, for how much."""

    at: datetime  # timezone-aware
    actor: str
    action: str
    subject: str | None
    detail: str
    cost_usd: float | None


@dataclass(frozen=True)
class LiveAgent:
    """An agent online right now (herdr pane or unfinished AgentRun run)."""

    name: str
    actor: str
    state: str  # working | idle | blocked | done | unknown
    subject: str | None
    where: str  # "herdr" | "agentrun"


@dataclass(frozen=True)
class Note:
    """The parts of an idea note the war room reads."""

    stem: str
    title: str
    origin: str
    created: datetime | None
    run_id: str | None
    content: str
    mtime: datetime


def actor_for(agent_id: str) -> str:
    """French actor name for an AgentRun agent id."""
    if agent_id.startswith("researcher"):
        return "chercheur"
    return ACTORS.get(agent_id, agent_id or "?")


def one_line(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def read_notes(vault: Path) -> tuple[tuple[Note, ...], tuple[str, ...]]:
    """(readable notes, French warnings for unreadable ones) — never raises."""
    folder = vault / IDEAS_DIR
    if not folder.is_dir():
        return (), ()
    notes: list[Note] = []
    bad: list[str] = []
    for path in sorted(folder.glob("*.md")):
        try:
            post = frontmatter.load(path)
            mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            bad.append(path.name)
            continue
        meta = post.metadata
        created = meta.get("created")
        if isinstance(created, datetime):
            created = created.isoformat()
        notes.append(
            Note(
                stem=path.stem,
                title=one_line(str(meta.get("title") or path.stem)),
                origin=str(meta.get("origin", "council")),
                created=parse_iso(created),
                run_id=str(meta["build_run_id"]) if meta.get("build_run_id") else None,
                content=post.content,
                mtime=mtime,
            )
        )
    warnings = (f"notes illisibles : {', '.join(bad)}",) if bad else ()
    return tuple(notes), warnings


def _body(content: str, heading: str) -> str:
    bounds = _section(content, heading)
    return content[bounds[0] : bounds[1]] if bounds else ""


def note_events(note: Note) -> tuple[WarEvent, ...]:
    """Creation, council verdict, atelier entries and discussion lines of one note."""
    events: list[WarEvent] = []
    if note.created is not None:
        actor, action = ORIGIN_EVENT.get(note.origin, ("visionnaire", "propose"))
        events.append(
            WarEvent(note.created, actor, action, note.stem, note.title, None)
        )
        verdict = _VERDICT.search(_body(note.content, i18n.H_COUNCIL))
        if verdict is not None:  # no stamp in the note: right after creation
            at = note.created + timedelta(seconds=1)
            detail = f"verdict : {verdict.group(1)}"
            events.append(WarEvent(at, "conseil", "tranche", note.stem, detail, None))
    events.extend(_atelier_events(note))
    events.extend(_discussion_events(note))
    return tuple(events)


def _atelier_events(note: Note) -> list[WarEvent]:
    events = []
    for match in _ATELIER_ENTRY.finditer(_body(note.content, H_ATELIER)):
        try:
            at = datetime.strptime(match.group(1), "%d-%m-%Y %H:%M").astimezone()
        except ValueError:
            continue
        lines = [ln for ln in match.group(2).splitlines() if ln.strip()]
        detail = one_line(lines[0].lstrip("#-* ")) if lines else ""
        events.append(WarEvent(at, "atelier", "note", note.stem, detail, None))
    return events


def _discussion_events(note: Note) -> list[WarEvent]:
    """Discussion lines carry no stamp: they are placed just before the note's mtime."""
    lines = list(_SPEAKER.finditer(_body(note.content, i18n.H_DISCUSSION)))
    events = []
    for i, match in enumerate(lines):
        speaker = match.group(1).strip().lower()
        actor = "propriétaire" if speaker == "propriétaire" else speaker
        action = "demande" if actor == "propriétaire" else "répond"
        at = note.mtime - timedelta(seconds=len(lines) - i)
        detail = one_line(match.group(2))
        events.append(WarEvent(at, actor, action, note.stem, detail, None))
    return events


def _norm(text: str) -> str:
    return one_line(text.replace("…", " ")).casefold()


def clean_title(title: str) -> str:
    """'# Idée : Radar pluie…' -> 'Radar pluie…'."""
    text = one_line(title.lstrip("#"))
    for prefix in ("Idée :", "Idée:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text


def link_run(run: Mapping[str, Any], notes: Sequence[Note]) -> str | None:
    """The idea a run worked on: frontmatter build_run_id, else its title."""
    run_id = run.get("run_id")
    for note in notes:
        if note.run_id and note.run_id == run_id:
            return note.stem
    title = _norm(str(run.get("title") or ""))
    for note in notes:
        key = _norm(note.title)[:TITLE_KEY_LEN].strip()
        if len(key) >= TITLE_KEY_MIN and key in title:
            return note.stem
    return None


def _cost(run: Mapping[str, Any]) -> float | None:
    cost = run.get("cost")
    usd = cost.get("usd") if isinstance(cost, Mapping) else None
    return float(usd) if isinstance(usd, int | float) else None


def _error_text(error: Any) -> str:
    """AgentRun errors are {"code", "message", ...}; keep the message."""
    if not error:
        return ""
    if isinstance(error, Mapping):
        error = error.get("message") or error.get("code") or ""
    return one_line(str(error))


def run_event(run: Mapping[str, Any], notes: Sequence[Note]) -> WarEvent | None:
    """One event per run (None when the run has no usable timestamp)."""
    at = parse_iso(run.get("ended_at")) or parse_iso(run.get("created_at"))
    if at is None:
        return None
    actor = actor_for(str(run.get("agent_id") or ""))
    status = str(run.get("status") or "")
    subject = link_run(run, notes)
    what = _error_text(run.get("error"))
    if not what and subject is None:  # a linked run's title is the idea: no repeat
        what = clean_title(str(run.get("title") or ""))
    detail = f"{RUN_STATUS.get(status, status)} · {what}".strip(" ·")
    return WarEvent(at, actor, VERBS.get(actor, "agit"), subject, detail, _cost(run))


def run_events(
    runs: Sequence[Mapping[str, Any]], notes: Sequence[Note]
) -> tuple[WarEvent, ...]:
    events = (run_event(run, notes) for run in runs if isinstance(run, Mapping))
    return tuple(e for e in events if e is not None)


def live_runs(
    runs: Sequence[Mapping[str, Any]], notes: Sequence[Note]
) -> tuple[LiveAgent, ...]:
    """Unfinished runs, shown in the online strip."""
    return tuple(
        LiveAgent(
            name=str(run.get("agent_id") or "?"),
            actor=actor_for(str(run.get("agent_id") or "")),
            state=LIVE_RUN_STATE[str(run.get("status"))],
            subject=link_run(run, notes),
            where="agentrun",
        )
        for run in runs
        if isinstance(run, Mapping) and run.get("status") in LIVE_RUN_STATE
    )


def default_fetcher() -> RunsFetcher:
    """GET /v2/runs through the AgentRun client (read-only)."""
    from agentrun import AgentRun, AgentRunError

    key = os.environ.get("AGENTRUN_API_KEY")
    token = Path.home() / ".agentrun" / ".token"
    if not key:
        try:
            key = token.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SourceError("pas de jeton AgentRun") from exc
    base = os.environ.get("AGENTRUN_BASE_URL", "http://127.0.0.1:7340/v2")
    client = AgentRun(base_url=base, api_key=key)

    def fetch(cursor: str | None) -> Mapping[str, Any]:
        try:
            return client.runs.list(cursor=cursor)  # type: ignore[return-value]
        except AgentRunError as exc:
            raise SourceError(str(exc)) from exc

    return fetch


def fetch_runs(
    fetch: RunsFetcher, max_pages: int = MAX_RUN_PAGES
) -> tuple[tuple[Mapping[str, Any], ...], str | None]:
    """(runs, French warning or None), following next_cursor for a few pages."""
    runs: list[Mapping[str, Any]] = []
    cursor: str | None = None
    try:
        for _ in range(max_pages):
            page = fetch(cursor)
            items = page.get("runs") if isinstance(page, Mapping) else None
            if not isinstance(items, list):
                raise SourceError("réponse sans liste de runs")
            runs.extend(r for r in items if isinstance(r, Mapping))
            cursor = page.get("next_cursor")
            if not cursor:
                break
    except (SourceError, OSError, ValueError) as exc:
        return tuple(runs), f"AgentRun injoignable : {one_line(str(exc))[:60]}"
    return tuple(runs), None


def cost_events(text: str, notes: Sequence[Note]) -> tuple[WarEvent, ...]:
    """Rows of vault/ops/costs.md (| date | task | role | model | effort | usd |)."""
    stems = sorted((n.stem for n in notes), key=len, reverse=True)
    events = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        try:
            at = datetime.strptime(cells[0], ops.COST_STAMP_FORMAT).astimezone()
            usd = float(cells[-1])
        except ValueError:
            continue  # header, separator or a malformed row
        task, role = cells[1], cells[2]
        actor = "+".join(actor_for(r.strip()) for r in role.split("+") if r.strip())
        subject = next((s for s in stems if s in task), None)
        verb = TASK_VERBS.get(task.split(" ", 1)[0], "dépense")
        detail = task.replace(subject, "").strip() if subject else task
        events.append(WarEvent(at, actor or "?", verb, subject, detail, usd))
    return tuple(events)


def drop_logged_runs(
    costs: Sequence[WarEvent], runs: Sequence[WarEvent]
) -> tuple[WarEvent, ...]:
    """Cost rows that are not already shown as an AgentRun run event."""

    def seen(row: WarEvent) -> bool:
        actors = set(row.actor.split("+"))
        return any(
            run.actor in actors and abs(run.at - row.at) <= COST_DEDUP for run in runs
        )

    return tuple(row for row in costs if not seen(row))
