"""`omniscient idea ui STEM`: the atelier's UI pane (live prototype + builder progress).

Shows whether the idea is being built (AgentRun run status, cost, last builder events),
serves the prototype's web/ folder with live reload (devserver.py, 127.0.0.1 only), and
can launch a build — only after a confirm, since a build spends API credit. The build
runs as `uv run omniscient build STEM` in its own session, logging to
.omniscient/builds/<slug>.log, so quitting the pane never kills it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import frontmatter
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Log, Static

from omniscient.build.poc import RUN_ID_KEY, VERDICT_FR, poc_slug
from omniscient.studio.buildwatch import (
    EVENT_WIDTH,
    KEEP_EVENTS,
    RunSource,
    RunStatus,
    build_log_path,
    default_source,
    launch_build,
    parse_run,
    read_timeline,
    source_errors,
    tail_lines,
)
from omniscient.studio.devserver import (
    DevServer,
    DevServerError,
    default_port,
    open_in_browser,
)
from omniscient.studio.ideas import StudioError, load_card, resolve
from omniscient.studio.screens import MODAL_CSS, ConfirmModal
from omniscient.theme.apply import ThemedApp, current_look

README_LINES = 4
POLL_SECONDS = 2.0
TAIL_SECONDS = 0.5
HINT = "o ouvrir · b construire · r rafraîchir · q quitter"
CONFIRM_BUILD = (
    "Lancer le bâtisseur sur cette idée ?\n"
    "Ça coûte jusqu'à 1,50 $ de crédit API."  # BUILD_CAP_USD, in French notation
)


@dataclass(frozen=True)
class PaneInfo:
    stem: str
    title: str
    status: str
    run_id: str | None
    poc_dir: Path
    has_web: bool
    poc_url: str | None
    verdict: str | None
    readme: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    info: PaneInfo
    run: RunStatus | None
    events: tuple[str, ...]  # new events since `cursor` (or all, when reset)
    reset: bool
    cursor: int | None
    error: str | None


def readme_head(poc_dir: Path, lines: int = README_LINES) -> tuple[str, ...]:
    path = poc_dir / "README.md"
    if not path.is_file():
        return ()
    text = path.read_text(encoding="utf-8", errors="replace")
    kept = [ln.strip().lstrip("# ").strip() for ln in text.splitlines() if ln.strip()]
    return tuple(ln[:EVENT_WIDTH] for ln in kept[:lines])


def load_info(vault: Path, repo: Path, stem: str) -> PaneInfo:
    """What the pane shows about the idea; StudioError for an unknown/unreadable note."""
    path = resolve(vault, stem)
    card = load_card(path)
    run_id = frontmatter.load(path).get(RUN_ID_KEY)
    poc_dir = repo / "pocs" / poc_slug(path)
    return PaneInfo(
        stem=stem,
        title=card.title,
        status=card.status,
        run_id=str(run_id) if run_id else None,
        poc_dir=poc_dir,
        has_web=(poc_dir / "web" / "index.html").is_file(),
        poc_url=card.poc_url,
        verdict=card.poc_verdict,
        readme=readme_head(poc_dir),
    )


def format_elapsed(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def is_building(info: PaneInfo, run: RunStatus | None) -> bool:
    return (
        info.status == "building"
        and info.run_id is not None
        and not (run is not None and run.finished)
    )


def state_line(info: PaneInfo, run: RunStatus | None, now: datetime) -> str:
    if is_building(info, run):
        if run is None:
            return "● en construction · suivi AgentRun indisponible"
        start = run.created_at or now
        cap = f"{run.cap_usd:.2f}" if run.cap_usd is not None else "?"
        elapsed = format_elapsed((now - start).total_seconds())
        return f"● en construction {elapsed} · ${run.cost_usd:.2f}/{cap}"
    if info.poc_dir.is_dir() or info.verdict:
        verdict = VERDICT_FR.get(info.verdict or "", "inconnu")
        return f"✓ prototype prêt · verdict {verdict}"
    return "○ pas encore de prototype"


def take_snapshot(
    vault: Path,
    repo: Path,
    stem: str,
    source: RunSource | None,
    seen: tuple[str | None, int | None] = (None, None),
) -> Snapshot:
    """Read the note, then (only while a build may be running) the run and its events.

    `seen` is (run id, timeline cursor) from the previous poll; a new run starts over.
    """
    info = load_info(vault, repo, stem)
    cursor = seen[1] if seen[0] == info.run_id else None
    snap = Snapshot(info, None, (), False, cursor, None)
    if source is None or info.status != "building" or info.run_id is None:
        return snap
    try:
        run = parse_run(source.get(info.run_id))
        events, reset, nxt = read_timeline(source, info.run_id, cursor)
    except source_errors() as exc:  # shown in the pane; the next poll retries
        return replace(snap, error=f"AgentRun injoignable : {exc}")
    return replace(snap, run=run, events=events, reset=reset, cursor=nxt)


Launcher = Callable[[Path, str, Path], "subprocess.Popen[bytes]"]


class UiPaneApp(ThemedApp):
    TITLE = "Omniscient · UI"
    CSS = (
        """
    #state { height: auto; padding: 0 1; text-style: bold; }
    #links { height: auto; padding: 0 1; }
    #events { height: 1fr; border-top: tall $panel; }
    #hint { height: 1; color: $text-muted; padding: 0 1; }
    """
        + MODAL_CSS
    )
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "open", "ouvrir"),
        Binding("b", "build", "construire"),
        Binding("r", "refresh", "rafraîchir"),
        Binding("q", "quit", "quitter"),
    ]

    def __init__(
        self,
        vault: Path,
        repo: Path,
        stem: str,
        *,
        source_factory: Callable[[], RunSource | None] = default_source,
        launcher: Launcher = launch_build,
        opener: Callable[[str], bool] = open_in_browser,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_seconds: float = POLL_SECONDS,
        port: int | None = None,
    ) -> None:
        super().__init__()
        self.vault, self.repo, self.stem = vault, repo, stem
        self._source_factory, self._launcher = source_factory, launcher
        self._opener, self._clock = opener, clock
        self.poll_seconds = poll_seconds
        self.port = port if port is not None else default_port(stem)
        self.source: RunSource | None = None
        self.server: DevServer | None = None
        self.snapshot: Snapshot | None = None
        self._seen: tuple[str | None, int | None] = (None, None)
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_path: Path | None = None
        self._log_offset = 0

    def compose(self) -> ComposeResult:
        yield Static("…", id="state")
        yield Static("", id="links")
        yield Log(id="events", max_lines=500)
        yield Static(HINT, id="hint")

    def on_mount(self) -> None:
        self.source = self._source_factory()
        self.action_refresh()
        self.set_interval(self.poll_seconds, self.action_refresh)
        self.set_interval(TAIL_SECONDS, self.tail_build_log)

    def on_unmount(self) -> None:
        if self.server is not None:
            self.server.stop()

    # ---------- polling ----------

    def action_refresh(self) -> None:
        self._poll()

    @work(thread=True, exclusive=True, group="poll")
    def _poll(self) -> None:
        try:
            snap = take_snapshot(
                self.vault, self.repo, self.stem, self.source, self._seen
            )
        except StudioError as exc:
            self.call_from_thread(self._set_state, f"⚠ {exc}")
            return
        self.call_from_thread(self.apply, snap)

    def apply(self, snap: Snapshot) -> None:
        self.snapshot = snap
        self._seen = (snap.info.run_id, snap.cursor)
        log = self.query_one("#events", Log)
        if snap.reset:
            log.clear()
        for line in snap.events[-KEEP_EVENTS:] if snap.reset else snap.events:
            log.write_line(line)
        state = state_line(snap.info, snap.run, self._clock())
        self._set_state(f"{state}\n⚠ {snap.error}" if snap.error else state)
        self._ensure_server(snap.info)
        self.query_one("#links", Static).update(self._links(snap.info))

    def _set_state(self, text: str) -> None:
        self.query_one("#state", Static).update(Text(text))

    def _ensure_server(self, info: PaneInfo) -> None:
        if not info.has_web or self.server is not None:
            return
        try:
            server = DevServer(info.poc_dir / "web", self.port)
            server.start()
        except DevServerError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.server = server

    def _links(self, info: PaneInfo) -> Text:
        text = Text()
        if self.server is not None:
            style = f"bold {current_look().signal}"
            text.append(f"▸ {self.server.url} (rechargement auto)\n", style=style)
        if info.poc_url:
            text.append(f"🌐 en ligne : {info.poc_url}\n")
        if info.verdict:
            text.append(f"verdict : {VERDICT_FR.get(info.verdict, info.verdict)}\n")
        for line in info.readme:
            text.append(f"{line}\n", style="dim")
        return text

    # ---------- keys ----------

    def action_open(self) -> None:
        info = self.snapshot.info if self.snapshot else None
        url = self.server.url if self.server else (info.poc_url if info else None)
        if url is None:
            self.notify("Pas encore de page web à ouvrir.", severity="warning")
            return
        self._open_url(url)

    @work(thread=True, group="browser")
    def _open_url(self, url: str) -> None:
        if not self._opener(url):
            self.call_from_thread(self.notify, f"Ouvre : {url}", timeout=15)

    def action_build(self) -> None:
        refusal = self._build_refusal()
        if refusal is not None:
            self.notify(refusal, severity="warning", timeout=6)
            return
        self.push_screen(ConfirmModal(CONFIRM_BUILD), self._confirmed_build)

    def _build_refusal(self) -> str | None:
        if self.snapshot is None:
            return "Lecture de l'idée en cours…"
        info = self.snapshot.info
        if self._proc is not None and self._proc.poll() is None:
            return "Une construction est déjà lancée depuis ce pane."
        if is_building(info, self.snapshot.run):
            return "Le bâtisseur travaille déjà sur cette idée."
        if info.poc_dir.exists():
            return f"Un prototype existe déjà ({info.poc_dir}) : supprime-le pour reconstruire."
        return None

    def _confirmed_build(self, yes: bool | None) -> None:
        if not yes or self.snapshot is None:
            self.notify("Construction annulée.")
            return
        path = build_log_path(self.repo, self.snapshot.info.poc_dir.name)
        try:
            self._proc = self._launcher(self.repo, self.stem, path)
        except OSError as exc:
            self.notify(
                f"Impossible de lancer la construction : {exc}", severity="error"
            )
            return
        self._log_path, self._log_offset = path, 0
        self.query_one("#events", Log).write_line(
            f"▶ omniscient build · journal {path}"
        )
        self.notify("Bâtisseur lancé.")

    def tail_build_log(self) -> None:
        """Stream the build's output into the event log; close it when the build ends."""
        if self._log_path is None or self._proc is None:
            return
        ended = self._proc.poll()
        lines, self._log_offset = tail_lines(self._log_path, self._log_offset)
        log = self.query_one("#events", Log)
        for line in lines:
            log.write_line(f"│ {line}")
        if ended is not None:
            log.write_line(f"■ construction terminée (code {ended})")
            self._log_path = None
            self.action_refresh()


def run_ui(vault: Path, repo: Path, stem: str) -> None:
    """The UI pane for one idea; StudioError (French-safe) for an unknown stem."""
    load_info(vault, repo, stem)  # fail fast, before taking over the terminal
    UiPaneApp(vault, repo, stem).run()
