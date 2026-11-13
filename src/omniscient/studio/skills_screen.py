"""The studio's skills library screen (`s` on the board).

Left: every Claude Code skill on this machine grouped by source (✓ = already in this
project). Right: the highlighted SKILL.md body. Keys install a skill into the project,
remove a project skill, or open a herdr atelier where Claude creates a new one.
The scan runs once per screen open (off the UI thread); install/remove re-read only
the project folder.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import ClassVar, Protocol

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from omniscient.studio.ideas import StudioError
from omniscient.studio.screens import ConfirmModal, PromptModal
from omniscient.studio.skills import (
    SOURCE_PROJECT,
    SkillInfo,
    SkillScan,
    install_dir_name,
    install_skill,
    read_body,
    refresh_installed,
    remove_skill,
    scan_report,
)
from omniscient.theme.apply import current_look

MARK_INSTALLED = "✓"
DESC_WIDTH = 60
LOADING = "*Chargement des skills…*"
EMPTY = "*Aucun skill ne correspond.*"
NO_HERDR = "herdr n'est pas lancé : lance `herdr`, puis `uv run omniscient studio` dans un pane."
BUSY = "Un atelier est déjà en cours d'ouverture…"
HINT = (
    "i installer dans le projet · d retirer du projet · c créer un skill · "
    "/ recherche · échap retour"
)


class SkillAtelier(Protocol):
    """What this screen needs from omniscient.studio.herdr.Atelier."""

    def available(self) -> bool: ...
    def open_skill_creator(self, text: str) -> str: ...


class _NeverRaised(Exception):
    """Stand-in while the herdr module is unavailable."""


def _herdr_error() -> type[Exception]:
    try:
        from omniscient.studio.herdr import HerdrError
    except ImportError:
        return _NeverRaised
    return HerdrError


def matches(skill: SkillInfo, query: str) -> bool:
    needle = query.strip().casefold()
    hay = f"{skill.name} {skill.description} {skill.source}".casefold()
    return not needle or needle in hay


def skill_prompt(skill: SkillInfo) -> Text:
    mark = Text(
        f"{MARK_INSTALLED} " if skill.installed_in_project else "  ", current_look().ok
    )
    desc = skill.description[:DESC_WIDTH]
    return mark + Text(skill.name, "bold") + Text(f"  {desc}", "dim")


def counter(skills: tuple[SkillInfo, ...]) -> str:
    in_project = sum(1 for s in skills if s.source == SOURCE_PROJECT)
    return f"{len(skills)} skills · {in_project} dans le projet"


class SkillsScreen(Screen[None]):
    """Browse, install, remove and create Claude Code skills (French UI)."""

    DEFAULT_CSS = """
    SkillsScreen #skills-count { padding: 0 1; height: 1; text-style: bold; }
    SkillsScreen #skills-body { height: 1fr; }
    SkillsScreen #skills-list { width: 3fr; }
    SkillsScreen #skills-preview-box { width: 2fr; border-left: tall $panel; padding: 0 1; }
    SkillsScreen #skills-hint { color: $text-muted; padding: 0 1; height: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("i", "install", "installer"),
        Binding("d", "remove", "retirer"),
        Binding("c", "create", "créer un skill"),
        Binding("slash", "search", "recherche"),
        Binding("escape,q", "back", "retour"),
    ]

    def __init__(
        self, repo: Path, atelier: SkillAtelier | None, home: Path | None = None
    ) -> None:
        super().__init__()
        self.repo = repo
        self.atelier = atelier
        self.home = home
        self.skills: tuple[SkillInfo, ...] = ()
        self.shown: tuple[SkillInfo, ...] = ()
        self.query_text = ""
        self.loaded = False
        self._bodies: dict[Path, str] = {}
        self._busy = threading.Lock()

    def compose(self) -> ComposeResult:
        yield Static("Skills", id="skills-count")
        with Horizontal(id="skills-body"):
            yield OptionList(id="skills-list")
            with VerticalScroll(id="skills-preview-box"):
                yield Markdown(LOADING, id="skills-preview")
        yield Static(HINT, id="skills-hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()
        self._scan()

    # ---------- loading ----------

    @work(thread=True, group="skills-scan", exclusive=True)
    def _scan(self) -> None:
        try:
            report = scan_report(self.repo, self.home)
        except OSError as exc:  # unreadable home or repo: say so, keep the screen up
            self.app.call_from_thread(
                self.notify, f"Lecture des skills impossible : {exc}", severity="error"
            )
            report = SkillScan((), ())
        self.app.call_from_thread(self._loaded, report)

    def _loaded(self, report: SkillScan) -> None:
        self.skills = report.skills
        self.loaded = True
        if report.problems:
            self.notify(
                f"{len(report.problems)} SKILL.md illisible(s) ignoré(s).",
                severity="warning",
                timeout=6,
            )
        self._redraw()

    def _redraw(self, keep: Path | None = None) -> None:
        self.shown = tuple(s for s in self.skills if matches(s, self.query_text))
        options: list[Option] = []
        source = None
        for index, skill in enumerate(self.shown):
            if skill.source != source:
                source = skill.source
                options.append(Option(Text(source, "bold underline"), disabled=True))
            options.append(Option(skill_prompt(skill), id=str(index)))
        listing = self.query_one(OptionList)
        listing.clear_options()
        listing.add_options(options)
        self._update_counter()
        self._restore_cursor(listing, keep)

    def _restore_cursor(self, listing: OptionList, keep: Path | None) -> None:
        target = next((i for i, s in enumerate(self.shown) if s.path == keep), 0)
        if not self.shown:
            self.query_one("#skills-preview", Markdown).update(EMPTY)
            return
        listing.highlighted = listing.get_option_index(str(target))
        self._show(self.shown[target])

    def _update_counter(self) -> None:
        text = counter(self.skills)
        if self.query_text:
            text += f" · recherche « {self.query_text} » : {len(self.shown)}"
        self.query_one("#skills-count", Static).update(text)

    # ---------- preview ----------

    def current(self) -> SkillInfo | None:
        listing = self.query_one(OptionList)
        if listing.highlighted is None:
            return None
        return self._skill_at(listing.get_option_at_index(listing.highlighted).id)

    def _skill_at(self, option_id: str | None) -> SkillInfo | None:
        """The skill an option id points at, or None for a header or a stale id
        (a highlight event can arrive after a search redrew the list)."""
        if option_id is None or not option_id.isdigit():
            return None
        index = int(option_id)
        return self.shown[index] if index < len(self.shown) else None

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        skill = self._skill_at(event.option.id)
        if skill is not None:
            self._show(skill)

    def _show(self, skill: SkillInfo) -> None:
        body = self._bodies.get(skill.path)
        if body is None:
            try:
                body = read_body(skill.path)
            except OSError as exc:
                body = f"*Lecture impossible : {exc}*"
            self._bodies[skill.path] = body
        self.query_one("#skills-preview", Markdown).update(body or "*(vide)*")

    # ---------- actions ----------

    def action_back(self) -> None:
        self.dismiss(None)

    def action_search(self) -> None:
        def apply(text: str | None) -> None:
            if text is not None:
                self.query_text = text.strip()
                self._redraw()

        self.app.push_screen(
            PromptModal(
                "Rechercher un skill", "nom, description ou source", self.query_text
            ),
            apply,
        )

    def _need_skill(self) -> SkillInfo | None:
        skill = self.current() if self.loaded else None
        if skill is None:
            self.notify("Aucun skill sous le curseur.", severity="warning")
        return skill

    def action_install(self) -> None:
        skill = self._need_skill()
        if skill is None:
            return
        if skill.installed_in_project:
            self.notify(
                f"« {skill.name} » est déjà dans le projet.", severity="warning"
            )
            return
        target = f".claude/skills/{install_dir_name(skill.name)}/"
        question = f"Installer « {skill.name} » dans le projet ({target}) ?"
        self.app.push_screen(
            ConfirmModal(question), lambda ok: self._install(skill) if ok else None
        )

    def _install(self, skill: SkillInfo) -> None:
        try:
            path = install_skill(skill, self.repo)
        except StudioError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self._after_change(
            path / "SKILL.md", f"« {skill.name} » installé dans le projet."
        )

    def action_remove(self) -> None:
        skill = self._need_skill()
        if skill is None:
            return
        if not skill.installed_in_project:
            self.notify(
                "Seuls les skills du projet peuvent être retirés.", severity="warning"
            )
            return
        dirname = (
            skill.path.parent.name
            if skill.source == SOURCE_PROJECT
            else install_dir_name(skill.name)
        )
        question = f"Retirer « {dirname} » du projet (.claude/skills/{dirname}/) ?"
        self.app.push_screen(
            ConfirmModal(question), lambda ok: self._remove(dirname) if ok else None
        )

    def _remove(self, dirname: str) -> None:
        try:
            remove_skill(dirname, self.repo)
        except StudioError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self._bodies.clear()
        self._after_change(None, f"« {dirname} » retiré du projet.")

    def _after_change(self, keep: Path | None, message: str) -> None:
        self.skills = refresh_installed(self.skills, self.repo)
        self._redraw(keep)
        self.notify(message)

    def action_create(self) -> None:
        def create(text: str | None) -> None:
            if text and text.strip():
                self._create(text.strip())

        self.app.push_screen(
            PromptModal("Quel skill créer ?", "ex. relire un PR selon nos règles"),
            create,
        )

    @work(thread=True, group="skills-atelier")
    def _create(self, text: str) -> None:
        """Open the skill-creator atelier off the UI thread."""
        if not self._busy.acquire(blocking=False):
            self.app.call_from_thread(self.notify, BUSY, severity="warning", timeout=4)
            return
        try:
            self._open_creator(text)
        finally:
            self._busy.release()

    def _open_creator(self, text: str) -> None:
        notify = self.app.call_from_thread
        atelier = self.atelier
        if atelier is None or not atelier.available():
            notify(self.notify, NO_HERDR, severity="warning", timeout=8)
            return
        notify(self.notify, "Ouverture de l'atelier skill…", timeout=4)
        try:
            atelier.open_skill_creator(text)
        except (_herdr_error(), OSError, StudioError) as exc:  # messages are French
            notify(self.notify, str(exc), severity="error", timeout=10)
            return
        notify(self.notify, f"Atelier skill ouvert dans herdr : {text[:40]}")
