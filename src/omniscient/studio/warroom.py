"""The war room: who did what on which idea, live, in a small herdr pane.

`run_warroom` draws a rich timeline (Live, refreshed every 3 s with --follow): a header,
the « en ligne » strip (herdr agents + unfinished AgentRun runs), the « relais » chain
per idea (visionnaire → conseil → stratège → bâtisseur) and the latest events.
Reads only: AgentRun GET /v2/runs, the vault, `herdr agent list`.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from omniscient import ops
from omniscient.studio.herdr import date_less, slugify
from omniscient.studio.ideas import resolve
from omniscient.studio.warroom_sources import (
    FACTORY,
    LiveAgent,
    Note,
    RunsFetcher,
    SourceError,
    WarEvent,
    cost_events,
    default_fetcher,
    drop_logged_runs,
    fetch_runs,
    live_runs,
    note_events,
    one_line,
    read_notes,
    run_events,
)
from omniscient.theme.apply import current_look as look

REFRESH_SECONDS = 3.0
HERDR_TIMEOUT_S = 2.0  # polled every REFRESH_SECONDS: a hung herdr must not stall
SLUG_LEN = 16  # herdr agent names carry slugify(stem without date, 16)
MAX_EVENTS = 30
MAX_CHAIN = 6  # actors shown per relay line
CHAINS_ALL = 3  # relay lines when watching every idea
FIXED_ROWS = 4  # header + online strip + spacing
SUBJECT_WIDTH = 22
STATE_LOOK = {  # icon, label; colours come from the theme (omniscient.theme)
    "working": ("●", "au travail"),
    "idle": ("○", "en attente"),
    "blocked": ("■", "bloqué"),
    "done": ("✓", "terminé"),
}
Runner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class Snapshot:
    """Everything one frame shows."""

    events: tuple[WarEvent, ...]  # newest first
    live: tuple[LiveAgent, ...]
    titles: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    spent_today: float = 0.0


def collect(
    vault: Path,
    idea: str | None,
    fetch: RunsFetcher | None,
    runner: Runner | None,
    today: date | None = None,
) -> Snapshot:
    """Read every source once; a missing source becomes a warning, never a crash."""
    notes, warnings = read_notes(vault)
    runs, run_warning = fetch_runs(fetch) if fetch is not None else ((), None)
    agents, herdr_warning = herdr_agents(runner, notes)
    by_run = run_events(runs, notes)
    cost_log, cost_warning = _cost_log(vault)
    spent = _spent_today(vault, today or date.today())
    costs = cost_events(cost_log, notes)
    events = [*by_run, *drop_logged_runs(costs, by_run)]
    for note in notes:
        events.extend(note_events(note))
    live = (*agents, *live_runs(runs, notes))
    if idea is not None:
        events = [e for e in events if e.subject == idea]
        live = tuple(a for a in live if a.subject == idea)
    return Snapshot(
        events=tuple(sorted(events, key=lambda e: e.at, reverse=True)),
        live=live,
        titles={n.stem: n.title for n in notes},
        warnings=(
            *warnings,
            *(w for w in (cost_warning, run_warning, herdr_warning) if w),
        ),
        spent_today=spent,
    )


def _cost_log(vault: Path) -> tuple[str, str | None]:
    """(cost log text, French warning) — an unreadable log is a warning, not a crash."""
    try:
        return (vault / ops.COST_LOG).read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return "", None
    except (OSError, ValueError) as exc:
        return "", f"journal des coûts illisible : {one_line(str(exc))[:80]}"


def _spent_today(vault: Path, day: date) -> float:
    try:
        return ops.spent_on(vault, day)
    except (OSError, ValueError):
        return 0.0  # _cost_log already reported the unreadable log


def handoffs(
    events: Sequence[WarEvent],
) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
    """Per subject (most recently active first): distinct consecutive actors, oldest first."""
    chains: dict[str | None, list[str]] = {}
    last_seen: dict[str | None, datetime] = {}
    for event in sorted(events, key=lambda e: e.at):
        chain = chains.setdefault(event.subject, [])
        if not chain or chain[-1] != event.actor:
            chain.append(event.actor)
        last_seen[event.subject] = event.at
    order = sorted(chains, key=lambda s: last_seen[s], reverse=True)
    return tuple((subject, tuple(chains[subject])) for subject in order)


# ---- herdr -------------------------------------------------------------------


def herdr_runner(args: Sequence[str]) -> str:
    """`herdr <args>` stdout; any failure is a SourceError (read-only use only)."""
    try:
        proc = subprocess.run(
            ["herdr", *args],
            capture_output=True,
            text=True,
            timeout=HERDR_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"herdr indisponible : {exc}") from exc
    if proc.returncode != 0:
        raise SourceError(f"herdr : {one_line(proc.stderr)[:80]}")
    return proc.stdout


def _herdr_subject(name: str, notes: Sequence[Note]) -> str | None:
    for note in notes:
        if f"-{slugify(date_less(note.stem), SLUG_LEN)}-" in f"-{name}-":
            return note.stem
    return None


def parse_herdr_agents(stdout: str, notes: Sequence[Note]) -> tuple[LiveAgent, ...]:
    """`herdr agent list` JSON: {"result": {"agents": [{name, agent, agent_status}]}}."""
    try:
        agents = json.loads(stdout)["result"]["agents"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError("réponse herdr inattendue") from exc
    if not isinstance(agents, list):
        raise SourceError("réponse herdr inattendue")
    return tuple(
        LiveAgent(
            name=str(a.get("name") or a.get("agent") or "?"),
            actor=str(a.get("agent") or "agent"),
            state=str(a.get("agent_status") or "unknown"),
            subject=_herdr_subject(str(a.get("name") or ""), notes),
            where="herdr",
        )
        for a in agents
        if isinstance(a, Mapping)
    )


def herdr_agents(
    runner: Runner | None, notes: Sequence[Note]
) -> tuple[tuple[LiveAgent, ...], str | None]:
    """(live herdr agents, warning) — skipped silently when no runner is given."""
    if runner is None:
        return (), None
    try:
        return parse_herdr_agents(runner(["agent", "list"]), notes), None
    except SourceError as exc:
        return (), str(exc)


# ---- rendering ---------------------------------------------------------------


def _actor(name: str) -> Text:
    return Text(name, style=look().actor(name))


def _subject_name(subject: str | None, titles: dict[str, str]) -> str:
    if subject is None:
        return FACTORY
    return titles.get(subject, subject)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else f"{text[: width - 1]}…"


def _money(usd: float) -> str:
    return f"{usd:.2f} $".replace(".", ",")


def header(snap: Snapshot, idea: str | None, now: datetime, width: int) -> Text:
    """« ◆ Salle de crise · <idée> » on the left, today's spend and the clock right."""
    right = f"aujourd'hui {_money(snap.spent_today)} · {now:%H:%M:%S}"
    title = _subject_name(idea, snap.titles) if idea else "tout"
    room = max(width - len(right) - len("◆ Salle de crise · ") - 2, 8)
    line = Text.assemble(
        ("◆ Salle de crise", look().alarm),
        (" · ", look().muted),
        (_clip(title, room), "bold"),
    )
    line.append(" " * max(width - len(line) - len(right), 2))
    line.append(right[: right.index(" · ")], style=look().money)
    line.append(right[right.index(" · ") :], style=look().muted)
    return line


def online_strip(snap: Snapshot, show_subject: bool) -> Text:
    line = Text("en ligne ", style=f"bold {look().muted}")
    if not snap.live:
        return line.append("personne", style=f"{look().muted} italic")
    for i, agent in enumerate(snap.live):
        icon, label = STATE_LOOK.get(agent.state, ("?", agent.state or "inconnu"))
        style = look().agent_state(agent.state)
        if i:
            line.append("  ")
        line.append(f"{icon} ", style=style)
        line.append_text(_actor(agent.actor))
        if show_subject and agent.subject:
            subject = _clip(_subject_name(agent.subject, snap.titles), 18)
            line.append(f" {subject}", style=f"italic {look().muted}")
        line.append(f" {label}", style=style)
    return line


def relay_lines(snap: Snapshot, limit: int) -> list[Text]:
    lines = []
    for subject, chain in handoffs(snap.events)[:limit]:
        line = Text("relais ", style=f"bold {look().muted}")
        line.append(_clip(_subject_name(subject, snap.titles), 20), style="italic")
        line.append("  ")
        shown = chain[-MAX_CHAIN:]
        if len(chain) > MAX_CHAIN:
            line.append("… → ", style=look().muted)
        for i, actor in enumerate(shown):
            if i:
                line.append(" → ", style=look().muted)
            line.append_text(_actor(actor))
        lines.append(line)
    return lines


def _stamp(at: datetime, today: date) -> Text:
    local = at.astimezone()
    if local.date() == today:
        return Text(f"{local:%H:%M}", style=look().text)
    return Text(f"{local:%d-%m %H:%M}", style=look().muted)


def _detail(text: str) -> Text:
    style = look().run_mark(text[:1])
    if style is None:
        return Text(text, style=look().text)
    out = Text(text[:1], style=style)
    out.append(text[1:], style=look().text)
    return out


def event_table(snap: Snapshot, rows: int, show_subject: bool, today: date) -> Table:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, style=f"italic {look().muted}")
    if show_subject:
        table.add_column(no_wrap=True, max_width=SUBJECT_WIDTH, style=look().signal)
    table.add_column(no_wrap=True, ratio=1, overflow="ellipsis")
    table.add_column(no_wrap=True, justify="right", style=look().money)
    for event in snap.events[:rows]:
        cells: list[RenderableType] = [
            _stamp(event.at, today),
            _actor(event.actor),
            Text(event.action),  # Text: note titles/actions never parse as markup
        ]
        if show_subject:
            subject = _subject_name(event.subject, snap.titles)
            cells.append(Text(_clip(subject, SUBJECT_WIDTH)))
        cost = _money(event.cost_usd) if event.cost_usd else ""
        cells += [_detail(event.detail), Text(cost)]
        table.add_row(*cells)
    return table


def render(
    snap: Snapshot,
    idea: str | None,
    height: int,
    width: int = 90,
    now: datetime | None = None,
) -> Group:
    """One frame sized for `height` terminal rows (the pane is short)."""
    moment = (now or datetime.now()).astimezone()
    show_subject = idea is None
    parts: list[RenderableType] = [
        header(snap, idea, moment, width),
        online_strip(snap, show_subject),
    ]
    parts += relay_lines(snap, 1 if idea else CHAINS_ALL)
    parts += [Text(f"⚠ {w}", style=look().warn) for w in snap.warnings]
    parts.append(Rule(style=look().muted))
    rows = max(height - len(parts) - 1, 3)
    if not snap.events:
        parts.append(
            Text("Aucun événement pour l'instant.", style=f"{look().muted} italic")
        )
    else:
        parts.append(
            event_table(snap, min(rows, MAX_EVENTS), show_subject, moment.date())
        )
    return Group(*parts)


# ---- entry point -------------------------------------------------------------


def run_warroom(
    vault: Path,
    repo: Path,
    idea: str | None,
    follow: bool,
    *,
    fetcher: Callable[[], RunsFetcher] = default_fetcher,
    runner: Runner | None = herdr_runner,
    console: Console | None = None,
) -> None:
    """Draw the war room once, or live until Ctrl-C. StudioError on an unknown idea."""
    del repo  # kept for the CLI's uniform signature; the war room only reads the vault
    if idea is not None:
        idea = resolve(vault, idea).stem
    out = console or Console()
    try:
        fetch: RunsFetcher | None = fetcher()
    except SourceError as exc:
        fetch = None
        out.print(Text(f"⚠ AgentRun : {exc}", style=look().warn))

    def frame() -> Group:
        snap = collect(vault, idea, fetch, runner)
        height = out.size.height if follow else MAX_EVENTS + 8
        return render(snap, idea, height, out.size.width)

    if not follow:
        out.print(frame())
        return
    try:
        with Live(frame(), console=out, screen=True, auto_refresh=False) as live:
            while True:
                time.sleep(REFRESH_SECONDS)
                live.update(frame(), refresh=True)
    except KeyboardInterrupt:
        return
