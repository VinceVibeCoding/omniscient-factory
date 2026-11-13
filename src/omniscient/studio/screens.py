"""Studio widgets: row rendering, filters and the small modal dialogs (French UI)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol

import frontmatter
import yaml
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, OptionList

from omniscient.build.poc import VERDICT_FR
from omniscient.studio.agents import AgentChoice
from omniscient.studio.ideas import IDEAS_DIR, IdeaCard
from omniscient.theme.apply import current_look

STATUS_FR = {
    "seed": "graine",
    "debated": "débattue",
    "planned": "planifiée",
    "building": "en construction",
    "shipped": "livrée",
    "dropped": "abandonnée",
}
MARK_SELECTED = "✓"
MARK_POC = "●"
MARK_PLAIN = "○"
INDENT = "  "
BRANCH = "└ "
COLUMNS = ("", "score", "idée", "statut", "PoC")
TITLE_WIDTH = 46  # keeps statut + PoC visible beside the preview


class IdeaTable(DataTable[object]):
    """Enter opens the atelier; a mouse click only moves the cursor."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "app.riff", "atelier", show=False)
    ]


class AtelierLike(Protocol):
    """What the board needs from omniscient.studio.herdr.Atelier."""

    def available(self) -> bool: ...
    def open_idea(
        self,
        card: IdeaCard,
        move: Literal["riff", "variant"],
        agent: AgentChoice | None = None,
    ) -> str: ...
    def open_remix(self, cards: Sequence[IdeaCard]) -> str: ...
    def open_seed(self, text: str) -> str: ...
    def open_warroom(self) -> str: ...
    def open_skill_creator(self, text: str) -> str: ...


class _NeverRaised(Exception):
    """Stand-in while the herdr module is unavailable."""


def herdr_error() -> type[Exception]:
    try:
        from omniscient.studio.herdr import HerdrError
    except ImportError:
        return _NeverRaised
    return HerdrError


@dataclass(frozen=True)
class StatusFilter:
    """One step of the `f` cycle: a French label and which statuses it keeps."""

    label: str
    keep: Callable[[str], bool]


FILTERS = (
    StatusFilter("actives", lambda s: s not in ("dropped", "shipped")),
    StatusFilter("toutes", lambda s: True),
    StatusFilter("graines", lambda s: s == "seed"),
    StatusFilter("débattues", lambda s: s == "debated"),
    StatusFilter("en construction", lambda s: s == "building"),
)


def status_label(status: str) -> str:
    return STATUS_FR.get(status, status)


def row_cells(card: IdeaCard, depth: int, selected: bool) -> tuple[Text, ...]:
    """The DataTable cells for one idea, variants indented under their parent."""
    look = current_look()  # status colours come from the owner's theme
    if selected:
        marker = Text(MARK_SELECTED, style=f"bold {look.ok}")
    elif card.poc_url:
        marker = Text(MARK_POC, style=f"bold {look.palette.accent}")
    else:
        marker = Text(MARK_PLAIN, style="dim")
    score = Text(
        f"{card.score:.2f}" if card.score is not None else "—", justify="right"
    )
    prefix = f"{INDENT * (depth - 1)}{BRANCH}" if depth else ""
    title = Text(prefix, style="dim") + Text(card.title[:TITLE_WIDTH])
    status = Text(status_label(card.status), style=look.idea_status(card.status))
    verdict = VERDICT_FR.get(card.poc_verdict or "", card.poc_verdict or "")
    return (marker, score, title, status, Text(verdict, style="italic"))


def vault_snapshot(vault: Path) -> tuple[tuple[str, int], ...]:
    """(name, mtime) of every idea note: the board reloads when this changes."""
    folder = vault / IDEAS_DIR
    if not folder.is_dir():
        return ()
    seen = []
    for path in folder.glob("*.md"):
        try:
            seen.append((path.name, path.stat().st_mtime_ns))
        except OSError:  # renamed/replaced between glob and stat (Obsidian, sync)
            continue
    return tuple(sorted(seen))


def by_score(cards: Iterable[IdeaCard]) -> list[IdeaCard]:
    """Best score first (seeds last), newest first among equals."""
    return sorted(
        cards,
        key=lambda c: (c.score if c.score is not None else -1.0, c.created),
        reverse=True,
    )


def note_body(card: IdeaCard) -> str:
    """The note's Markdown body for the preview, or a French error line."""
    try:
        return frontmatter.load(card.path).content
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return f"*Note illisible : {exc}*"


def matches(card: IdeaCard, query: str) -> bool:
    needle = query.strip().casefold()
    return (
        not needle or needle in card.title.casefold() or needle in card.stem.casefold()
    )


class PromptModal(ModalScreen[str | None]):
    """Ask for one line of text; dismiss with the text, or None on Échap."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "annuler")]

    def __init__(self, question: str, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self._question = question
        self._placeholder = placeholder
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._question, classes="dialog-title", markup=False)
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt")
            yield Label("entrée pour valider · échap pour annuler", classes="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Yes/no question; o/entrée confirms, n/échap cancels."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "answer(True)", "oui"),
        Binding("n,escape", "answer(False)", "non"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._question, classes="dialog-title", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Oui (o)", variant="error", id="yes")
                yield Button("Non (n)", variant="primary", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class AgentPickerModal(ModalScreen[AgentChoice | None]):
    """Pick who joins the atelier; entrée chooses, échap cancels."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "annuler")]

    def __init__(self, agents: Sequence[AgentChoice]) -> None:
        super().__init__()
        self._agents = tuple(agents)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Avec quel agent ouvrir l'atelier ?", classes="dialog-title")
            yield OptionList(*(a.label for a in self._agents), id="agents")
            yield Label(
                "entrée pour choisir · échap pour annuler · "
                "« crédit API » = coûte à chaque message",
                classes="hint",
            )

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = 0 if self._agents else None
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._agents[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


MODAL_CSS = """
PromptModal, ConfirmModal, AgentPickerModal { align: center middle; }
#agents { height: auto; max-height: 16; margin-bottom: 1; }
.dialog {
    width: 70; height: auto; padding: 1 2;
    border: thick $accent; background: $surface;
}
.dialog-title { text-style: bold; margin-bottom: 1; }
.buttons { height: auto; align-horizontal: right; }
.buttons Button { margin-left: 2; }
.hint { color: $text-muted; }
"""
