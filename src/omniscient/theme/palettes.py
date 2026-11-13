"""Colour palettes for the studio and the user's terminal ("ricing").

Each Palette carries semantic slots (used by the Textual apps and the rich panes) and the
16 ANSI colours (exported to Windows Terminal / herdr). Popular Omarchy-style themes use
their canonical colours; "omniscient" is the house theme: an all-seeing oracle (deep ink
indigo, a warm gold eye, a cyan signal).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ANSI_NAMES = (
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "purple",
    "cyan",
    "white",
    "brightBlack",
    "brightRed",
    "brightGreen",
    "brightYellow",
    "brightBlue",
    "brightPurple",
    "brightCyan",
    "brightWhite",
)
SEMANTIC_SLOTS = ("primary", "secondary", "accent", "success", "warning", "error")
TEXT_CONTRAST = 4.5  # WCAG AA, normal text
ACCENT_CONTRAST = 3.0  # WCAG AA, large text / UI components


class ThemeError(ValueError):
    """A theme problem, with a French message for the user."""


@dataclass(frozen=True)
class Palette:
    name: str
    label: str
    dark: bool
    background: str
    surface: str
    foreground: str
    muted: str
    primary: str
    secondary: str
    accent: str
    success: str
    warning: str
    error: str
    ansi: tuple[str, ...]  # 16 colours, in ANSI_NAMES order
    cursor: str | None = None  # defaults to the foreground

    def __post_init__(self) -> None:
        if len(self.ansi) != len(ANSI_NAMES):
            raise ThemeError(f"{self.name} : il faut 16 couleurs ANSI")
        for value in (*self.colors().values(), *self.ansi):
            if not HEX.match(value):
                raise ThemeError(f"{self.name} : couleur invalide {value!r}")

    def colors(self) -> dict[str, str]:
        """The semantic slots by name (background … error, cursor)."""
        return {
            "background": self.background,
            "surface": self.surface,
            "foreground": self.foreground,
            "muted": self.muted,
            **{slot: getattr(self, slot) for slot in SEMANTIC_SLOTS},
            "cursor": self.cursor or self.foreground,
        }

    def ansi_named(self) -> dict[str, str]:
        return dict(zip(ANSI_NAMES, self.ansi, strict=True))


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    """WCAG relative luminance of '#rrggbb'."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours (1 … 21)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _ansi(*rows: str) -> tuple[str, ...]:
    """Space-separated hex rows (normal 0-7, bright 8-15) → the 16 ANSI colours."""
    return tuple(color for row in rows for color in row.split())


OMNISCIENT = Palette(
    name="omniscient",
    label="Omniscient (oracle)",
    dark=True,
    background="#0b0d1c",  # deep ink
    surface="#151831",  # indigo night
    foreground="#ebe6d6",  # parchment
    muted="#8a8fb3",  # indigo haze
    primary="#9a8cff",  # iris
    secondary="#3fd0e0",  # cyan signal
    accent="#f0b445",  # the gold eye
    success="#5fd39a",
    warning="#f59f4c",
    error="#ff6b81",
    cursor="#f0b445",
    ansi=_ansi(
        "#1a1d33 #ff6b81 #5fd39a #f0b445 #6f8cff #a98bff #3fd0e0 #d6d2c4",
        "#4a4f73 #ff8fa0 #8ae6b6 #ffd27a #9bb0ff #c7b3ff #8ae9f3 #f6f2e6",
    ),
)

TOKYO_NIGHT = Palette(
    name="tokyo-night",
    label="Tokyo Night",
    dark=True,
    background="#1a1b26",
    surface="#24283b",
    foreground="#c0caf5",
    muted="#565f89",
    primary="#7aa2f7",
    secondary="#7dcfff",
    accent="#bb9af7",
    success="#9ece6a",
    warning="#e0af68",
    error="#f7768e",
    ansi=_ansi(
        "#15161e #f7768e #9ece6a #e0af68 #7aa2f7 #bb9af7 #7dcfff #a9b1d6",
        "#414868 #f7768e #9ece6a #e0af68 #7aa2f7 #bb9af7 #7dcfff #c0caf5",
    ),
)

CATPPUCCIN = Palette(
    name="catppuccin",
    label="Catppuccin Mocha",
    dark=True,
    background="#1e1e2e",
    surface="#313244",
    foreground="#cdd6f4",
    muted="#6c7086",
    primary="#89b4fa",
    secondary="#94e2d5",
    accent="#cba6f7",
    success="#a6e3a1",
    warning="#f9e2af",
    error="#f38ba8",
    cursor="#f5e0dc",
    ansi=_ansi(
        "#45475a #f38ba8 #a6e3a1 #f9e2af #89b4fa #f5c2e7 #94e2d5 #bac2de",
        "#585b70 #f38ba8 #a6e3a1 #f9e2af #89b4fa #f5c2e7 #94e2d5 #a6adc8",
    ),
)

CATPPUCCIN_LATTE = Palette(
    name="catppuccin-latte",
    label="Catppuccin Latte (clair)",
    dark=False,
    background="#eff1f5",
    surface="#e6e9ef",
    foreground="#4c4f69",
    muted="#8c8fa1",
    primary="#1e66f5",
    secondary="#8839ef",
    accent="#e64553",
    success="#179299",  # canonical green/peach/yellow are < 3:1 on latte base
    warning="#e64553",
    error="#d20f39",
    cursor="#dc8a78",
    ansi=_ansi(
        "#5c5f77 #d20f39 #40a02b #df8e1d #1e66f5 #ea76cb #179299 #acb0be",
        "#6c6f85 #d20f39 #40a02b #df8e1d #1e66f5 #ea76cb #179299 #bcc0cc",
    ),
)

GRUVBOX = Palette(
    name="gruvbox",
    label="Gruvbox",
    dark=True,
    background="#282828",
    surface="#3c3836",
    foreground="#ebdbb2",
    muted="#928374",
    primary="#83a598",
    secondary="#8ec07c",
    accent="#fe8019",
    success="#b8bb26",
    warning="#fabd2f",
    error="#fb4934",
    ansi=_ansi(
        "#282828 #cc241d #98971a #d79921 #458588 #b16286 #689d6a #a89984",
        "#928374 #fb4934 #b8bb26 #fabd2f #83a598 #d3869b #8ec07c #ebdbb2",
    ),
)

NORD = Palette(
    name="nord",
    label="Nord",
    dark=True,
    background="#2e3440",
    surface="#3b4252",
    foreground="#d8dee9",
    muted="#4c566a",
    primary="#88c0d0",
    secondary="#8fbcbb",
    accent="#b48ead",
    success="#a3be8c",
    warning="#ebcb8b",
    error="#bf616a",
    ansi=_ansi(
        "#3b4252 #bf616a #a3be8c #ebcb8b #81a1c1 #b48ead #88c0d0 #e5e9f0",
        "#4c566a #bf616a #a3be8c #ebcb8b #81a1c1 #b48ead #8fbcbb #eceff4",
    ),
)

EVERFOREST = Palette(
    name="everforest",
    label="Everforest",
    dark=True,
    background="#2d353b",
    surface="#343f44",
    foreground="#d3c6aa",
    muted="#859289",
    primary="#a7c080",
    secondary="#83c092",
    accent="#d699b6",
    success="#a7c080",
    warning="#dbbc7f",
    error="#e67e80",
    ansi=_ansi(
        "#475258 #e67e80 #a7c080 #dbbc7f #7fbbb3 #d699b6 #83c092 #d3c6aa",
        "#7a8478 #e67e80 #a7c080 #dbbc7f #7fbbb3 #d699b6 #83c092 #d3c6aa",
    ),
)

KANAGAWA = Palette(
    name="kanagawa",
    label="Kanagawa",
    dark=True,
    background="#1f1f28",
    surface="#2a2a37",
    foreground="#dcd7ba",
    muted="#727169",
    primary="#7e9cd8",
    secondary="#7aa89f",
    accent="#ffa066",
    success="#98bb6c",
    warning="#e6c384",
    error="#e46876",
    cursor="#c8c093",
    ansi=_ansi(
        "#090618 #c34043 #76946a #c0a36e #7e9cd8 #957fb8 #6a9589 #c8c093",
        "#727169 #e82424 #98bb6c #e6c384 #7fb4ca #938aa9 #7aa89f #dcd7ba",
    ),
)

ROSE_PINE = Palette(
    name="rose-pine",
    label="Rosé Pine",
    dark=True,
    background="#191724",
    surface="#1f1d2e",
    foreground="#e0def4",
    muted="#6e6a86",
    primary="#c4a7e7",
    secondary="#9ccfd8",
    accent="#ebbcba",
    success="#31748f",
    warning="#f6c177",
    error="#eb6f92",
    cursor="#524f67",
    ansi=_ansi(
        "#26233a #eb6f92 #31748f #f6c177 #9ccfd8 #c4a7e7 #ebbcba #e0def4",
        "#6e6a86 #eb6f92 #31748f #f6c177 #9ccfd8 #c4a7e7 #ebbcba #e0def4",
    ),
)

MATTE_BLACK = Palette(
    name="matte-black",
    label="Matte Black",
    dark=True,
    background="#121212",
    surface="#1e1e1e",
    foreground="#bebebe",
    muted="#8a8a8d",
    primary="#e68e0d",
    secondary="#eaeaea",
    accent="#f59e0b",
    success="#ffc107",
    warning="#e68e0d",
    error="#d35f5f",
    cursor="#eaeaea",
    ansi=_ansi(
        "#333333 #d35f5f #ffc107 #b91c1c #e68e0d #d35f5f #bebebe #bebebe",
        "#8a8a8d #b91c1c #ffc107 #b90a0a #f59e0b #b91c1c #eaeaea #ffffff",
    ),
)

OSAKA_JADE = Palette(
    name="osaka-jade",
    label="Osaka Jade",
    dark=True,
    background="#111c18",
    surface="#23372b",
    foreground="#c1c497",
    muted="#53685b",
    primary="#2dd5b7",
    secondary="#acd4cf",
    accent="#e5c736",
    success="#549e6a",
    warning="#e5c736",
    error="#ff5345",
    cursor="#d7c995",
    ansi=_ansi(
        "#23372b #ff5345 #549e6a #459451 #509475 #d2689c #2dd5b7 #f6f5dd",
        "#53685b #db9f9c #143614 #e5c736 #acd4cf #75bbb3 #8cd3cb #9eebb3",
    ),
)

RISTRETTO = Palette(
    name="ristretto",
    label="Ristretto",
    dark=True,
    background="#2c2525",
    surface="#403838",
    foreground="#e6d9db",
    muted="#948a8b",
    primary="#f9cc6c",
    secondary="#85dacc",
    accent="#f38d70",
    success="#adda78",
    warning="#f9cc6c",
    error="#fd6883",
    cursor="#c3b7b8",
    ansi=_ansi(
        "#72696a #fd6883 #adda78 #f9cc6c #f38d70 #a8a9eb #85dacc #e6d9db",
        "#948a8b #ff8297 #c8e292 #fcd675 #f8a788 #bebffd #9bf1e1 #f1e5e7",
    ),
)

PALETTES: dict[str, Palette] = {
    p.name: p
    for p in (
        OMNISCIENT,
        TOKYO_NIGHT,
        CATPPUCCIN,
        CATPPUCCIN_LATTE,
        GRUVBOX,
        NORD,
        EVERFOREST,
        KANAGAWA,
        ROSE_PINE,
        MATTE_BLACK,
        OSAKA_JADE,
        RISTRETTO,
    )
}
DEFAULT_THEME = OMNISCIENT.name


def get_palette(name: str) -> Palette:
    """The palette called `name`; ThemeError (French, lists choices) otherwise."""
    key = name.strip().lower()
    if key not in PALETTES:
        choices = ", ".join(PALETTES)
        raise ThemeError(f"Thème inconnu : « {name} ». Choix : {choices}.")
    return PALETTES[key]
