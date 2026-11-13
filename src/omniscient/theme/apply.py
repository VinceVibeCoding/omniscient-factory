"""Apply a palette: rich styles for the panes, Textual themes and the live theme picker.

The rich panes (war room, to-do, UI pane, board rows) ask `current_look()` for their
colours. It is the process-wide active look — like Textual's `App.theme` — loaded lazily
from the profile and swapped by the studio's picker (`t`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.console import Group, RenderableType
from rich.rule import Rule
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from omniscient.home import Home, find_home
from omniscient.theme.palettes import (
    DEFAULT_THEME,
    PALETTES,
    SEMANTIC_SLOTS,
    Palette,
    ThemeError,
)

# actor → palette slot (a semantic name) or an ANSI index (0-15)
ACTOR_SLOTS: dict[str, str | int] = {
    "visionnaire": "accent",
    "conseil": "warning",
    "stratège": "secondary",
    "bâtisseur": "success",
    "débatteur": 9,
    "questionneur": 4,
    "chercheur": 12,
    "éclaireur": 14,
    "traducteur": 5,
    "formateur": "muted",
    "atelier": "primary",
    "propriétaire": "foreground",
}
SWATCH = "██"


@dataclass(frozen=True)
class Look:
    """Rich styles derived from a palette's semantic slots."""

    palette: Palette

    def color(self, slot: str | int) -> str:
        if isinstance(slot, int):
            return self.palette.ansi[slot]
        return self.palette.colors()[slot]

    @property
    def text(self) -> str:
        return self.palette.foreground

    @property
    def muted(self) -> str:
        return self.palette.muted

    @property
    def signal(self) -> str:
        return self.palette.secondary

    @property
    def money(self) -> str:
        return self.palette.accent

    @property
    def warn(self) -> str:
        return self.palette.warning

    @property
    def alarm(self) -> str:
        return f"bold {self.palette.error}"

    @property
    def ok(self) -> str:
        return self.palette.success

    @property
    def heading(self) -> str:
        return f"bold {self.palette.warning}"

    def idea_status(self, status: str) -> str:
        p = self.palette
        return {
            "seed": p.secondary,
            "debated": p.warning,
            "planned": p.primary,
            "building": p.accent,
            "shipped": f"bold {p.success}",
            "dropped": f"dim {p.error}",
        }.get(status, "")

    def actor(self, name: str) -> str:
        return f"bold {self.color(ACTOR_SLOTS.get(name, 'foreground'))}"

    def agent_state(self, state: str) -> str:
        p = self.palette
        return {
            "working": f"bold {p.success}",
            "idle": p.warning,
            "blocked": f"bold {p.error}",
            "done": p.secondary,
        }.get(state, p.muted)

    def run_mark(self, mark: str) -> str | None:
        """Style of a war-room detail's leading status mark (✓ ✗ ⊘ ●)."""
        p = self.palette
        return {
            "✓": p.success,
            "✗": f"bold {p.error}",
            "⊘": p.muted,
            "●": f"bold {p.success}",
        }.get(mark)


_ACTIVE: dict[str, Look] = {}  # the process-wide look ("look" key)


def current_look() -> Look:
    """The active look; loaded once from the current home's profile."""
    if "look" not in _ACTIVE:
        from omniscient.theme import load_palette

        palette, _warning = load_palette(find_home())  # a bad profile → default
        _ACTIVE["look"] = Look(palette)
    return _ACTIVE["look"]


def use_palette(palette: Palette) -> Look:
    """Make `palette` the active look (the picker's live preview, theme_preview)."""
    _ACTIVE["look"] = Look(palette)
    return _ACTIVE["look"]


def swatch(palette: Palette, slots: tuple[str, ...] = SEMANTIC_SLOTS) -> Text:
    out = Text()
    for slot in slots:
        out.append(SWATCH, style=palette.colors()[slot])
    return out


def ansi_strip(palette: Palette) -> Text:
    """The 16 ANSI colours as two rows of blocks (normal, bright)."""
    out = Text()
    for row in (palette.ansi[:8], palette.ansi[8:]):
        for color in row:
            out.append("   ", style=f"on {color}")
        out.append("\n")
    out.rstrip()
    return out


def sample_panes(palette: Palette) -> RenderableType:
    """A sample war room and to-do checklist, rendered with the palette's look."""
    from omniscient.studio.todo import TodoItem, checklist
    from omniscient.studio.warroom import Snapshot, render
    from omniscient.studio.warroom_sources import LiveAgent, WarEvent

    use_palette(palette)
    at = datetime(2026, 1, 1, 9, 30).astimezone()
    events = (
        WarEvent(at, "bâtisseur", "PoC construit", "demo", "✓ ./run.sh démarre", 0.82),
        WarEvent(at, "conseil", "débat", "demo", "✓ score 0,78", 0.12),
        WarEvent(at, "visionnaire", "nouvelle idée", "demo", "graine", None),
    )
    live = (LiveAgent("riff-demo", "atelier", "working", "demo", "herdr"),)
    snap = Snapshot(events, live, {"demo": "Idée démo"}, (), 0.94)
    todos = (
        TodoItem(1, "maquette de l'écran", True),
        TodoItem(2, "brancher l'API", False),
    )
    return Group(
        Rule("salle de crise", style=palette.muted),
        render(snap, None, 12, 90, now=at),
        Rule("à faire", style=palette.muted),
        checklist(todos),
    )


# ---- Textual -----------------------------------------------------------------


def textual_name(palette: Palette) -> str:
    """Registered Textual theme name (prefixed so built-ins like "nord" stay intact)."""
    return (
        palette.name if palette.name == DEFAULT_THEME else f"omniscient-{palette.name}"
    )


def to_textual(palette: Palette) -> Theme:
    p = palette
    return Theme(
        name=textual_name(p),
        primary=p.primary,
        secondary=p.secondary,
        accent=p.accent,
        warning=p.warning,
        error=p.error,
        success=p.success,
        foreground=p.foreground,
        background=p.background,
        surface=p.surface,
        dark=p.dark,
        variables={
            "footer-key-foreground": p.accent,
            "input-selection-background": f"{p.primary} 35%",
            "block-cursor-text-style": "bold",
        },
    )


def register_themes(app: App[object]) -> None:
    for palette in PALETTES.values():
        app.register_theme(to_textual(palette))


def palette_for(theme_name: str) -> Palette | None:
    """The palette behind a registered Textual theme name, None for others."""
    for palette in PALETTES.values():
        if textual_name(palette) == theme_name:
            return palette
    return None


def _option(palette: Palette) -> Option:
    label = Text.assemble(swatch(palette), "  ", (palette.label, "bold"))
    label.append(f"  {palette.name}", style="dim")
    return Option(label, id=palette.name)


class ThemePickerModal(ModalScreen[str | None]):
    """Pick a palette; highlighting one previews it live, Escape restores the old one."""

    DEFAULT_CSS = """
    ThemePickerModal { align: center middle; }
    ThemePickerModal > Vertical {
        width: 64; height: auto; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    ThemePickerModal OptionList { height: auto; max-height: 16; }
    ThemePickerModal #theme-sample { height: auto; margin-top: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "annuler")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self.original = current  # Textual theme name to restore on Escape

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Thème du studio  (entrée choisir · échap annuler)")
            yield OptionList(*(_option(p) for p in PALETTES.values()))
            yield Static("", id="theme-sample")

    def on_mount(self) -> None:
        names = list(PALETTES)
        current = palette_for(self.original)
        options = self.query_one(OptionList)
        options.highlighted = names.index(current.name) if current else 0
        options.focus()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        palette = PALETTES[str(event.option.id)]
        self.app.theme = textual_name(palette)
        self.query_one("#theme-sample", Static).update(sample_line(palette))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.app.theme = self.original
        self.dismiss(None)


def sample_line(palette: Palette) -> Text:
    look = Look(palette)
    out = Text()
    for status in ("seed", "debated", "planned", "building", "shipped"):
        out.append(f"{status} ", style=look.idea_status(status))
    out.append("\n")
    for actor in ("visionnaire", "conseil", "stratège", "bâtisseur"):
        out.append(f"{actor} ", style=look.actor(actor))
    return out


class ThemedApp(App[None]):
    """An App using the owner's palette; `t` opens the picker and saves the choice."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("t", "pick_theme", "thème", show=False)
    ]
    _theme_warning: str | None = None
    _theme_ready = False

    def theme_home(self) -> Home:
        """Where the profile lives: the app's `repo` (the home root) if it has one."""
        repo = getattr(self, "repo", None)
        return Home(Path(repo)) if repo is not None else find_home()

    def on_load(self) -> None:
        from omniscient.theme import load_palette

        register_themes(self)
        palette, self._theme_warning = load_palette(self.theme_home())
        use_palette(palette)
        self.theme = textual_name(palette)

    def on_mount(self) -> None:
        if self._theme_warning:
            self.notify(self._theme_warning, severity="warning", timeout=8)
        self._theme_ready = True

    def watch_theme(self, theme_name: str) -> None:
        palette = palette_for(theme_name)
        if palette is None:
            return
        use_palette(palette)
        if self._theme_ready:
            self.restyle()

    def restyle(self) -> None:
        """Redraw rich content baked with the old colours (apps with `reload`)."""
        reload = getattr(self, "reload", None)
        if callable(reload):
            reload()

    def action_pick_theme(self) -> None:
        self.push_screen(ThemePickerModal(self.theme), self._theme_picked)

    def _theme_picked(self, name: str | None) -> None:
        if name is None:
            return
        from omniscient.theme import save_theme_name

        try:
            palette = save_theme_name(self.theme_home().profile, name)
        except (OSError, ThemeError) as exc:
            self.notify(f"Thème non enregistré : {exc}", severity="error")
            return
        self.notify(f"Thème : {palette.label}", timeout=3)
