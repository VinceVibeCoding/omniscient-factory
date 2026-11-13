"""Terminal themes ("ricing"): palettes, the stored choice and the CLI functions.

The choice lives in <home>/config/profile.toml under `[profile] theme = "<name>"`. It is
written by a minimal line editor that touches only that key, so every other key and
comment of the profile is kept. The integrator registers the CLI functions:
`theme list | set NAME | preview [NAME] | export TARGET NAME [--write]`.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from omniscient.home import Home, find_home
from omniscient.theme.palettes import (
    DEFAULT_THEME,
    PALETTES,
    Palette,
    ThemeError,
    get_palette,
)

__all__ = [
    "DEFAULT_THEME",
    "PALETTES",
    "Palette",
    "ThemeError",
    "get_palette",
    "list_themes",
    "load_palette",
    "read_theme_name",
    "save_theme_name",
    "set_theme",
    "set_toml_keys",
    "theme_export",
    "theme_list",
    "theme_preview",
    "theme_set",
]

PROFILE_TABLE = "profile"
THEME_KEY = "theme"
_HEADER = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-]+)\s*\]\]?\s*(#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=")


# ---- minimal TOML line editor ------------------------------------------------


def _table_span(lines: list[str], table: str) -> tuple[int, int] | None:
    """(header index, end index) of `[table]` — None when the table has no header."""
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if (m := _HEADER.match(line))
            and m.group(1) == table
            and not line.lstrip().startswith("[[")
        ),
        None,
    )
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if _HEADER.match(lines[i])),
        len(lines),
    )
    return start, end


def _merge_keys(body: list[str], rendered: Mapping[str, str]) -> list[str]:
    out = list(body)
    for key, line in rendered.items():
        idx = next(
            (
                j
                for j, ln in enumerate(out)
                if (m := _KEY.match(ln)) and m.group(1) == key
            ),
            None,
        )
        if idx is not None:
            out[idx] = line
            continue
        last = max((j for j, ln in enumerate(out) if ln.strip()), default=-1)
        out.insert(last + 1, line)
    return out


def _lookup(data: Mapping[str, object], table: str) -> object:
    node: object = data
    for part in table.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return node


def set_toml_keys(
    text: str, table: str, values: Mapping[str, str], *, replace_body: bool = False
) -> str:
    """Set string keys of `[table]`, leaving every other line untouched.

    replace_body=True replaces the table's whole body (e.g. `[theme.custom]`). The
    result is re-parsed; ThemeError when the file can't be edited safely this way.
    """
    lines = text.splitlines()
    rendered = {
        k: f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in values.items()
    }
    span = _table_span(lines, table)
    if span is None:
        gap = [""] if lines and lines[-1].strip() else []
        lines = [*lines, *gap, f"[{table}]", *rendered.values()]
    else:
        start, end = span
        body = lines[start + 1 : end]
        if replace_body:
            tail = [""] if end < len(lines) else []
            new_body = [*rendered.values(), *tail]
        else:
            new_body = _merge_keys(body, rendered)
        lines = [*lines[: start + 1], *new_body, *lines[end:]]
    result = "\n".join(lines) + "\n"
    try:
        parsed = _lookup(tomllib.loads(result), table)
    except tomllib.TOMLDecodeError as exc:
        raise ThemeError(f"modification TOML impossible sans risque ({exc})") from exc
    if not isinstance(parsed, Mapping) or any(
        parsed.get(k) != v for k, v in values.items()
    ):
        raise ThemeError(f"modification TOML impossible sans risque ([{table}])")
    return result


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---- the stored choice -------------------------------------------------------


def read_theme_name(profile: Path) -> str:
    """The theme stored in profile.toml; the default when absent. ThemeError if bad."""
    try:
        data = tomllib.loads(profile.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_THEME
    except tomllib.TOMLDecodeError as exc:
        raise ThemeError(f"{profile.name} illisible : {exc}") from exc
    value = _lookup(data, PROFILE_TABLE)
    value = value.get(THEME_KEY) if isinstance(value, Mapping) else None
    if value is None:
        return DEFAULT_THEME
    if not isinstance(value, str):
        raise ThemeError(f"{profile.name} : `theme` doit être un texte")
    return get_palette(value).name


def load_palette(home: Home) -> tuple[Palette, str | None]:
    """(palette, French warning) — a broken profile falls back to the default theme."""
    try:
        return get_palette(read_theme_name(home.profile)), None
    except (OSError, ThemeError) as exc:
        default = PALETTES[DEFAULT_THEME]
        return default, f"Thème : {exc} — thème « {default.label} » utilisé."


def save_theme_name(profile: Path, name: str) -> Palette:
    """Store `name` under [profile] theme, keeping the rest of the file as it is."""
    palette = get_palette(name)
    try:
        text = profile.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    write_atomic(profile, set_toml_keys(text, PROFILE_TABLE, {THEME_KEY: palette.name}))
    return palette


def list_themes() -> tuple[Palette, ...]:
    """Every palette (used by `omniscient init`'s theme step)."""
    return tuple(PALETTES.values())


def set_theme(home: Home, name: str) -> Palette:
    """Store `name` as the home's theme (onboarding API)."""
    return save_theme_name(home.profile, name)


# ---- CLI functions (the integrator registers them) ---------------------------


def theme_list(home: Home | None = None, console: Console | None = None) -> None:
    """Every theme with its swatch; the current one is marked ●."""
    from omniscient.theme.apply import swatch

    out = console or Console()
    current, warning = load_palette(home or find_home())
    if warning:
        out.print(Text(warning, style="yellow"))
    table = Table(box=None, padding=(0, 1), show_header=False)
    for palette in PALETTES.values():
        mark = "●" if palette.name == current.name else " "
        kind = "sombre" if palette.dark else "clair"
        table.add_row(mark, swatch(palette), palette.name, palette.label, kind)
    out.print(table)


def theme_set(
    name: str, home: Home | None = None, console: Console | None = None
) -> Palette:
    """Store the theme; the studio uses it at next launch (or live with `t`)."""
    out = console or Console()
    target = home or find_home()
    palette = save_theme_name(target.profile, name)
    out.print(f"Thème enregistré : {palette.label} ({target.profile})")
    out.print(
        "Le studio l'utilise au prochain lancement (ou `t` dans le studio). "
        f"Pour le terminal : omniscient theme export windows-terminal {palette.name}"
    )
    return palette


def theme_preview(
    name: str | None = None, home: Home | None = None, console: Console | None = None
) -> None:
    """Swatches, the ANSI strip and a sample war room / to-do rendered in the theme."""
    from omniscient.theme.apply import ansi_strip, sample_panes, swatch, use_palette

    out = console or Console()
    palette = get_palette(name) if name else load_palette(home or find_home())[0]
    use_palette(palette)
    slots = Text()
    for slot, color in palette.colors().items():
        slots.append("██", style=color).append(f" {slot} {color}  ")
    out.print(Text(f"{palette.label} ({palette.name})", style=f"bold {palette.accent}"))
    out.print(Group(slots, swatch(palette), ansi_strip(palette)))
    out.print(sample_panes(palette))


def theme_export(
    target: str,
    name: str,
    write: bool = False,
    *,
    path: Path | None = None,
    console: Console | None = None,
) -> None:
    """Print the theme for `windows-terminal` or `herdr` (+ an ANSI strip); with
    write=True, update that config after a timestamped backup (theme keys only)."""
    from omniscient.theme import export as ex
    from omniscient.theme.apply import ansi_strip

    if target not in ex.TARGETS:
        raise ThemeError(
            f"Cible inconnue : « {target} ». Choix : {', '.join(ex.TARGETS)}."
        )
    out = console or Console()
    palette = get_palette(name)
    wt = target == "windows-terminal"
    scheme = ex.windows_terminal_scheme(palette)
    text = (
        json.dumps(scheme, indent=4, ensure_ascii=False)
        if wt
        else ex.herdr_snippet(palette)
    )
    out.print(text, markup=False, highlight=False)
    out.print(ansi_strip(palette))
    if not write:
        where = "« schemes » de settings.json" if wt else "~/.config/herdr/config.toml"
        hint = f"(aperçu seulement : colle-le dans {where}, ou relance avec --write)"
        out.print(Text(hint, style="dim"))
        return
    if wt:
        settings = ex.pick_wt_settings(path)
        saved = ex.write_windows_terminal(palette, settings)
        out.print(
            f"Jeu « {scheme['name']} » écrit dans {settings} (sauvegarde : {saved})."
        )
        out.print(
            "Windows Terminal → Paramètres → ton profil → Apparence → Jeu de couleurs."
        )
        return
    config = path or ex.herdr_config_path()
    saved_herdr = ex.write_herdr(palette, config)
    kept = f" (sauvegarde : {saved_herdr})" if saved_herdr else ""
    out.print(f"Thème herdr écrit dans {config}{kept}.")
    for warning in ex.herdr_warnings(config):
        out.print(Text(warning, style="yellow"))
    out.print("Applique-le : herdr server reload-config")
