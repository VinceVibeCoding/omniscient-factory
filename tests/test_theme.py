"""Themes: palettes, the stored choice, terminal exports and the studio picker."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

from omniscient.home import Home
from omniscient.studio.app import StudioApp
from omniscient.theme import (
    DEFAULT_THEME,
    PALETTES,
    ThemeError,
    get_palette,
    load_palette,
    read_theme_name,
    save_theme_name,
    set_toml_keys,
    theme_list,
    theme_preview,
    theme_set,
)
from omniscient.theme import apply as theme_apply
from omniscient.theme.apply import (
    Look,
    current_look,
    palette_for,
    textual_name,
    use_palette,
)
from omniscient.theme.palettes import (
    ACCENT_CONTRAST,
    HEX,
    SEMANTIC_SLOTS,
    TEXT_CONTRAST,
    Palette,
    contrast,
)

NOW = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)
REQUIRED = [
    "omniscient",
    "tokyo-night",
    "catppuccin",
    "catppuccin-latte",
    "gruvbox",
    "nord",
    "everforest",
    "kanagawa",
    "rose-pine",
    "matte-black",
    "osaka-jade",
    "ristretto",
]


@pytest.fixture(autouse=True)
def _reset_look() -> Iterator[None]:
    theme_apply._ACTIVE.clear()
    yield
    theme_apply._ACTIVE.clear()


def _console() -> Console:
    return Console(record=True, width=120, color_system=None)


# ---- palettes ------------------------------------------------------------------


def test_every_required_theme_exists_and_default_is_omniscient() -> None:
    assert set(REQUIRED) <= set(PALETTES)
    assert DEFAULT_THEME == "omniscient"
    assert PALETTES["catppuccin-latte"].dark is False


@pytest.mark.parametrize("palette", PALETTES.values(), ids=lambda p: p.name)
def test_palette_colours_are_hex_and_readable(palette: Palette) -> None:
    assert all(HEX.match(c) for c in (*palette.colors().values(), *palette.ansi))
    assert len(palette.ansi) == 16
    assert contrast(palette.foreground, palette.background) >= TEXT_CONTRAST
    for slot in SEMANTIC_SLOTS:
        ratio = contrast(getattr(palette, slot), palette.background)
        assert ratio >= ACCENT_CONTRAST, (slot, ratio)


def test_contrast_matches_wcag_extremes() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#777777") == pytest.approx(1.0)


def test_invalid_palette_is_rejected() -> None:
    base = PALETTES["nord"]
    with pytest.raises(ThemeError, match="couleur invalide"):
        Palette(**{**base.__dict__, "accent": "orange"})
    with pytest.raises(ThemeError, match="16 couleurs"):
        Palette(**{**base.__dict__, "ansi": base.ansi[:8]})


def test_get_palette_is_case_insensitive_and_lists_choices() -> None:
    assert get_palette(" Nord ").name == "nord"
    with pytest.raises(ThemeError, match="Choix : omniscient"):
        get_palette("solarized")


# ---- stored choice ---------------------------------------------------------------


PROFILE = """# my profile
[profile]
name = "Alex"
language = "fr"
interests = [
    "LLM locaux",
    "fintech",
]

[github]
user = "octocat"  # kept
"""


def test_missing_profile_reads_the_default(tmp_path: Path) -> None:
    assert read_theme_name(tmp_path / "profile.toml") == DEFAULT_THEME


def test_save_theme_keeps_every_other_line(tmp_path: Path) -> None:
    profile = tmp_path / "config" / "profile.toml"
    profile.parent.mkdir()
    profile.write_text(PROFILE, encoding="utf-8")

    save_theme_name(profile, "kanagawa")
    save_theme_name(profile, "gruvbox")  # replaces, does not duplicate

    text = profile.read_text(encoding="utf-8")
    assert read_theme_name(profile) == "gruvbox"
    assert text.count("theme =") == 1
    assert text.replace('theme = "gruvbox"\n', "") == PROFILE
    data = tomllib.loads(text)
    assert data["github"]["user"] == "octocat"
    assert data["profile"]["interests"] == ["LLM locaux", "fintech"]


def test_save_theme_creates_the_profile(tmp_path: Path) -> None:
    profile = tmp_path / "config" / "profile.toml"
    save_theme_name(profile, "nord")
    assert tomllib.loads(profile.read_text())["profile"]["theme"] == "nord"


def test_save_unknown_theme_writes_nothing(tmp_path: Path) -> None:
    profile = tmp_path / "profile.toml"
    with pytest.raises(ThemeError):
        save_theme_name(profile, "neon")
    assert not profile.exists()


def test_broken_profile_falls_back_with_a_warning(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.toml").write_text("[profile]\ntheme = 'neon'\n")
    palette, warning = load_palette(Home(tmp_path))
    assert palette.name == DEFAULT_THEME
    assert warning is not None and "neon" in warning


def test_set_toml_keys_refuses_an_unsafe_edit() -> None:
    with pytest.raises(ThemeError):
        set_toml_keys('profile.theme = "x"\n', "profile", {"theme": "nord"})


# ---- rich look -------------------------------------------------------------------


def test_look_maps_semantic_slots() -> None:
    nord = PALETTES["nord"]
    look = Look(nord)
    assert look.idea_status("shipped") == f"bold {nord.success}"
    assert look.actor("visionnaire") == f"bold {nord.accent}"
    assert look.actor("débatteur") == f"bold {nord.ansi[9]}"
    assert look.agent_state("blocked") == f"bold {nord.error}"
    assert look.run_mark("?") is None


def test_panes_render_with_the_active_palette() -> None:
    from omniscient.studio.todo import TodoItem, checklist

    use_palette(PALETTES["ristretto"])
    assert current_look().palette.name == "ristretto"
    text = checklist((TodoItem(1, "a", False),))
    assert PALETTES["ristretto"].warning in str(text.style)


def test_preview_list_and_set(tmp_path: Path) -> None:
    out = _console()
    home = Home(tmp_path)
    theme_set("everforest", home=home, console=out)
    theme_list(home=home, console=out)
    theme_preview(None, home=home, console=out)
    shown = out.export_text()
    assert re.search(r"●\s+█+\s+everforest", shown)
    assert "Salle de crise" in shown and "À faire" in shown


# ---- Textual -------------------------------------------------------------------------


def test_textual_names_round_trip() -> None:
    for palette in PALETTES.values():
        assert palette_for(textual_name(palette)) == palette
    assert textual_name(PALETTES["nord"]) == "omniscient-nord"  # built-in kept
    assert palette_for("textual-dark") is None


@pytest.mark.asyncio
async def test_studio_registers_themes_and_uses_the_profile(tmp_path: Path) -> None:
    (tmp_path / "vault" / "ideas").mkdir(parents=True)
    save_theme_name(Home(tmp_path).profile, "nord")
    app = StudioApp(tmp_path / "vault", tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert app.theme == "omniscient-nord"
        assert {textual_name(p) for p in PALETTES.values()} <= set(app.available_themes)
        assert current_look().palette.name == "nord"


@pytest.mark.asyncio
async def test_picker_previews_live_and_escape_restores(tmp_path: Path) -> None:
    (tmp_path / "vault" / "ideas").mkdir(parents=True)
    app = StudioApp(tmp_path / "vault", tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert app.theme == "omniscient"
        await pilot.press("t")
        await pilot.press("down")
        await pilot.pause()
        assert app.theme == textual_name(list(PALETTES.values())[1])
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == "omniscient"
        assert not (tmp_path / "config" / "profile.toml").exists()

        await pilot.press("t", "down", "down", "enter")
        await pilot.pause()
        chosen = list(PALETTES.values())[2]
        assert app.theme == textual_name(chosen)
        assert read_theme_name(Home(tmp_path).profile) == chosen.name
