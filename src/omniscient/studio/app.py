"""`omniscient studio`: the Textual idea board that opens herdr ateliers.

Left: every idea in tree order (variants under their parent). Right: the highlighted note.
Keys open an atelier (riff, variant, remix, seed) through herdr, or record the owner's
verdict. The vault is polled so notes written by the atelier agents show up live.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Markdown, Static

from omniscient.studio.agents import AgentChoice, available_agents
from omniscient.studio.ideas import (
    IdeaCard,
    StudioError,
    scan_ideas,
    tree_order,
)
from omniscient.studio.screens import (
    COLUMNS,
    FILTERS,
    MODAL_CSS,
    AgentPickerModal,
    AtelierLike,
    ConfirmModal,
    IdeaTable,
    PromptModal,
    by_score,
    herdr_error,
    matches,
    note_body,
    row_cells,
    vault_snapshot,
)
from omniscient.theme.apply import ThemedApp

POLL_SECONDS = 3.0
REMIX_MIN, REMIX_MAX = 2, 3
NO_HERDR = "herdr n'est pas lancé : lance `herdr`, puis `uv run omniscient studio` dans un pane."
OPENING = "Ouverture de l'atelier…"
EMPTY_PREVIEW = "*Aucune idée ici. Appuie sur `n` pour semer une graine, ou `f` pour changer de filtre.*"
HINT = (
    "entrée atelier Claude · a choisir l'agent · v variante · espace sélection · "
    "m remixer · n nouvelle idée · s skills · w salle de crise · p poursuivre · "
    "x abandonner · f filtre · / recherche · t thème"
)
NO_SKILLS = "Bibliothèque de skills indisponible dans cette version."
BUSY = "Un atelier est déjà en cours d'ouverture…"
Move = Literal["riff", "variant"]


class StudioApp(ThemedApp):  # `t`: theme picker (omniscient.theme)
    TITLE = "Omniscient · studio"
    CSS = (
        """
    #board { height: 1fr; }
    #ideas { width: 3fr; }
    #preview-box { width: 2fr; border-left: tall $panel; padding: 0 1; }
    #hint { color: $text-muted; padding: 0 1; height: auto; }
    """
        + MODAL_CSS
    )
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "riff", "atelier"),
        Binding("a", "pick_agent", "agent"),
        Binding("v", "variant", "variante"),
        Binding("space", "toggle_select", "sélection remix"),
        Binding("m", "remix", "remixer"),
        Binding("n", "seed", "nouvelle idée"),
        Binding("s", "skills", "skills"),
        Binding("w", "warroom", "salle de crise"),
        Binding("p", "pursue", "poursuivre"),
        Binding("x", "drop", "abandonner"),
        Binding("f", "cycle_filter", "filtre"),
        Binding("slash", "search", "recherche"),
        Binding("r", "reload", "rafraîchir"),
        Binding("q", "quit", "quitter"),
    ]

    def __init__(
        self,
        vault: Path,
        repo: Path,
        atelier: AtelierLike | None = None,
        *,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        super().__init__()
        self.vault = vault
        self.repo = repo
        self.atelier = atelier
        self.poll_seconds = poll_seconds
        self.filter_index = 0
        self.query_text = ""
        self.selected: tuple[str, ...] = ()  # remix picks, in selection order
        self.cards: dict[str, IdeaCard] = {}
        self.rows: tuple[tuple[IdeaCard, int], ...] = ()
        self._seen: tuple[tuple[str, int], ...] = ()
        self._bad: tuple[str, ...] = ()
        self._busy = threading.Lock()  # one herdr build at a time

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="board"):
            yield IdeaTable(id="ideas", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="preview-box"):
                yield Markdown(EMPTY_PREVIEW, id="preview")
        yield Static(HINT, id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(*COLUMNS)
        self.reload()
        self.set_interval(self.poll_seconds, self.poll_vault)

    # ---------- data ----------

    def poll_vault(self) -> None:
        if vault_snapshot(self.vault) != self._seen:
            self.reload()

    def reload(self) -> None:
        """Re-read the vault and redraw, keeping the cursor on the same idea."""
        self._seen = vault_snapshot(self.vault)
        cards, bad = scan_ideas(self.vault)
        if bad and bad != self._bad:
            self.notify(
                f"Notes illisibles ignorées : {', '.join(bad)}", severity="warning"
            )
        self._bad = bad
        cards = by_score(cards)
        stem = self.current_stem()  # before self.rows changes under the cursor
        self.cards = {c.stem: c for c in cards}
        self.selected = tuple(s for s in self.selected if s in self.cards)
        keep = FILTERS[self.filter_index].keep
        shown = tuple(
            c for c in cards if keep(c.status) and matches(c, self.query_text)
        )
        self.rows = tree_order(shown)
        self._redraw(stem)

    def _redraw(self, stem: str | None) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for card, depth in self.rows:
            table.add_row(
                *row_cells(card, depth, card.stem in self.selected), key=card.stem
            )
        stems = [card.stem for card, _ in self.rows]
        if stem in stems:
            table.move_cursor(row=stems.index(stem))
        self._update_subtitle()
        self.show_preview(self.current_stem())

    def _update_subtitle(self) -> None:
        parts = [
            f"filtre : {FILTERS[self.filter_index].label}",
            f"{len(self.rows)} idées",
        ]
        if self.query_text:
            parts.append(f"recherche : « {self.query_text} »")
        if self.selected:
            parts.append(f"{len(self.selected)} sélectionnée(s) pour remix")
        self.sub_title = " · ".join(parts)

    def current_stem(self) -> str | None:
        table = self.query_one(DataTable)
        if not self.rows or table.cursor_row < 0 or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row][0].stem

    def current_card(self) -> IdeaCard | None:
        stem = self.current_stem()
        return self.cards.get(stem) if stem else None

    def show_preview(self, stem: str | None) -> None:
        preview = self.query_one("#preview", Markdown)
        card = self.cards.get(stem) if stem else None
        if card is None:
            preview.update(EMPTY_PREVIEW)
            return
        preview.update(note_body(card))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_preview(event.row_key.value)

    # ---------- atelier actions ----------

    def _need_card(self) -> IdeaCard | None:
        card = self.current_card()
        if card is None:
            self.notify("Aucune idée sous le curseur.", severity="warning")
        return card

    @work(thread=True, group="atelier")
    def _run_atelier(
        self,
        call: Callable[[AtelierLike], str],
        done: str,
        on_success: Callable[[], None] | None = None,
    ) -> None:
        """Talk to herdr off the UI thread; report the outcome as a notification."""
        if not self._busy.acquire(blocking=False):  # double Enter: don't duplicate
            self.call_from_thread(self.notify, BUSY, severity="warning", timeout=4)
            return
        try:
            self._atelier_call(call, done, on_success)
        finally:
            self._busy.release()

    def _atelier_call(
        self,
        call: Callable[[AtelierLike], str],
        done: str,
        on_success: Callable[[], None] | None,
    ) -> None:
        atelier = self.atelier
        if atelier is None or not atelier.available():
            self.call_from_thread(self.notify, NO_HERDR, severity="warning", timeout=8)
            return
        self.call_from_thread(self.notify, OPENING, timeout=4)
        try:
            call(atelier)  # blocks up to ~45 s while Claude Code starts in the pane
        except (herdr_error(), OSError, StudioError) as exc:  # messages are French
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=10)
            return
        self.call_from_thread(self.notify, done)
        if on_success is not None:
            self.call_from_thread(on_success)

    def _open(self, move: Move, agent: AgentChoice | None = None) -> None:
        card = self._need_card()
        if card is None:
            return
        verb = "Atelier" if move == "riff" else "Atelier variante"
        with_agent = f" ({agent.label})" if agent is not None else ""
        self._run_atelier(
            lambda a: a.open_idea(card, move, agent),
            f"{verb} ouvert dans herdr : {card.title}{with_agent}",
        )

    def action_pick_agent(self) -> None:
        if self._need_card() is None:
            return

        def picked(agent: AgentChoice | None) -> None:
            if agent is not None:
                self._open("riff", agent)

        self.push_screen(AgentPickerModal(available_agents(self.repo)), picked)

    def action_skills(self) -> None:
        try:
            from omniscient.studio.skills_screen import SkillsScreen
        except ImportError:
            self.notify(NO_SKILLS, severity="warning")
            return
        self.push_screen(SkillsScreen(self.repo, self.atelier))

    def action_warroom(self) -> None:
        self._run_atelier(
            lambda a: a.open_warroom(), "Salle de crise ouverte dans herdr"
        )

    def action_riff(self) -> None:
        self._open("riff")

    def action_variant(self) -> None:
        self._open("variant")

    def action_toggle_select(self) -> None:
        stem = self.current_stem()
        if stem is None:
            return
        if stem in self.selected:
            self.selected = tuple(s for s in self.selected if s != stem)
        elif len(self.selected) >= REMIX_MAX:
            self.notify(
                f"Un remix combine au plus {REMIX_MAX} idées.", severity="warning"
            )
            return
        else:
            self.selected = (*self.selected, stem)
        self._redraw(stem)

    def action_remix(self) -> None:
        cards = tuple(self.cards[s] for s in self.selected)
        if not REMIX_MIN <= len(cards) <= REMIX_MAX:
            self.notify(
                f"Sélectionne {REMIX_MIN} ou {REMIX_MAX} idées avec espace avant de remixer.",
                severity="warning",
            )
            return
        names = " + ".join(c.title[:20] for c in cards)

        def clear_selection() -> None:
            self.selected = ()
            self._redraw(self.current_stem())

        self._run_atelier(
            lambda a: a.open_remix(cards),
            f"Remix ouvert dans herdr : {names}",
            clear_selection,
        )

    def action_seed(self) -> None:
        def submitted(text: str | None) -> None:
            if not text or not text.strip():
                return
            seed = text.strip()
            self._run_atelier(
                lambda a: a.open_seed(seed), "Atelier graine ouvert dans herdr"
            )

        self.push_screen(
            PromptModal("Nouvelle idée : décris-la en une phrase", "ex. un bot qui…"),
            submitted,
        )

    # ---------- owner verdicts ----------

    def _verdict(self, card: IdeaCard, verdict: str, done: str) -> None:
        from omniscient.slack.core import apply_owner_verdict

        try:
            apply_owner_verdict(self.vault, card.stem, verdict)
        except (ValueError, OSError, StudioError) as exc:
            self.notify(f"Verdict non enregistré : {exc}", severity="error")
            return
        self.notify(done)
        self.reload()

    def action_pursue(self) -> None:
        card = self._need_card()
        if card is None:
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._verdict(card, "pursue", f"Idée poursuivie : {card.title}")

        question = (
            f"Poursuivre « {card.title} » ? Elle passera en tête du plan "
            "au prochain cycle (07:00), qui dépense du crédit API."
        )
        self.push_screen(ConfirmModal(question), confirmed)

    def action_drop(self) -> None:
        card = self._need_card()
        if card is None:
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._verdict(card, "drop", f"Idée abandonnée : {card.title}")

        self.push_screen(ConfirmModal(f"Abandonner « {card.title} » ?"), confirmed)

    # ---------- view ----------

    def action_cycle_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(FILTERS)
        self.reload()
        self.notify(f"Filtre : {FILTERS[self.filter_index].label}", timeout=2)

    def action_search(self) -> None:
        def submitted(text: str | None) -> None:
            if text is not None:
                self.query_text = text.strip()
                self.reload()

        self.push_screen(
            PromptModal(
                "Rechercher (vide = tout afficher)", "mot du titre…", self.query_text
            ),
            submitted,
        )

    def action_reload(self) -> None:
        self.reload()
        self.notify("Idées rechargées.", timeout=2)


def run_studio(vault: Path, repo: Path) -> None:
    """Launch the board with a real herdr atelier (inside a herdr pane)."""
    from omniscient.studio.herdr import Atelier

    StudioApp(vault, repo, Atelier(repo)).run()
