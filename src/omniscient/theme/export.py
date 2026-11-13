"""Export a palette to the user's terminal — opt-in, never silent.

- Windows Terminal: print a "schemes" entry; with --write, insert/replace ONLY the scheme
  named "Omniscient · <label>" in settings.json (JSONC: comments and trailing commas are
  tolerated, the rest of the text is kept byte for byte) after a timestamped backup.
- herdr (0.9.x `[theme]` + `[theme.custom]` tokens, checked with `herdr config check`):
  print the snippet; with --write, back up and replace only those keys.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniscient.theme import set_toml_keys, write_atomic
from omniscient.theme.palettes import Palette, ThemeError

TARGETS = ("windows-terminal", "herdr")
WT_GLOB = (
    "*/AppData/Local/Packages/Microsoft.WindowsTerminal_*/LocalState/settings.json"
)
WT_USERS = Path("/mnt/c/Users")
SCHEME_PREFIX = "Omniscient · "
BOM = "\ufeff"
AUTO_SWITCH_WARNING = (
    "[theme] auto_switch = true : herdr choisit dark_name/light_name, "
    "mets auto_switch = false pour voir ce thème."
)
# herdr built-in themes matching a palette exactly (others use the custom tokens)
HERDR_BUILTIN = {
    "tokyo-night": "tokyo-night",
    "catppuccin": "catppuccin",
    "catppuccin-latte": "catppuccin-latte",
    "gruvbox": "gruvbox",
    "nord": "nord",
    "kanagawa": "kanagawa",
    "rose-pine": "rose-pine",
}


# ---- Windows Terminal ----------------------------------------------------------


def scheme_name(palette: Palette) -> str:
    return f"{SCHEME_PREFIX}{palette.label}"


def windows_terminal_scheme(palette: Palette) -> dict[str, str]:
    colors = palette.colors()
    return {
        "name": scheme_name(palette),
        "background": palette.background,
        "foreground": palette.foreground,
        "cursorColor": colors["cursor"],
        "selectionBackground": palette.ansi[8],
        **palette.ansi_named(),
    }


class _Jsonc:
    """A tiny JSON-with-comments reader that also reports where values sit."""

    def __init__(self, text: str) -> None:
        self.text = text

    def skip(self, i: int) -> int:
        t = self.text
        while i < len(t):
            if t[i].isspace():
                i += 1
            elif t.startswith("//", i):
                nl = t.find("\n", i)
                i = len(t) if nl < 0 else nl + 1
            elif t.startswith("/*", i):
                close = t.find("*/", i + 2)
                if close < 0:
                    raise ThemeError("settings.json : commentaire /* non fermé")
                i = close + 2
            else:
                break
        return i

    def string(self, i: int) -> tuple[str, int]:
        j = i + 1
        while j < len(self.text) and self.text[j] != '"':
            j += 2 if self.text[j] == "\\" else 1
        if j >= len(self.text):
            raise ThemeError("settings.json : texte non fermé")
        return json.loads(self.text[i : j + 1]), j + 1

    def _expect(self, i: int, char: str) -> int:
        i = self.skip(i)
        if i >= len(self.text) or self.text[i] != char:
            raise ThemeError(f"settings.json : « {char} » attendu (position {i})")
        return i + 1

    def members(self, i: int) -> Iterator[tuple[str, int, int]]:
        """(key, value start, value end) of the object starting at i."""
        i = self._expect(i, "{")
        while True:
            i = self.skip(i)
            if i < len(self.text) and self.text[i] == "}":
                return
            if i >= len(self.text) or self.text[i] != '"':
                raise ThemeError(f"settings.json : clé attendue (position {i})")
            key, i = self.string(i)
            start = self.skip(self._expect(i, ":"))
            _, end = self.value(start)
            yield key, start, end
            i = self.skip(end)
            if i < len(self.text) and self.text[i] == ",":
                i += 1

    def items(self, i: int) -> Iterator[tuple[int, int]]:
        i = self._expect(i, "[")
        while True:
            i = self.skip(i)
            if i < len(self.text) and self.text[i] == "]":
                return
            _, end = self.value(i)
            yield i, end
            i = self.skip(end)
            if i < len(self.text) and self.text[i] == ",":
                i += 1

    def end_of(self, i: int, close: str) -> int:
        """Index after the closing bracket following the last member (trailing comma ok)."""
        i = self.skip(i)
        if self.text[i : i + 1] == ",":
            i += 1
        return self._expect(i, close)

    def value(self, i: int) -> tuple[Any, int]:
        i = self.skip(i)
        char = self.text[i : i + 1]
        if char == "{":
            out, last = {}, i + 1
            for key, start, end in self.members(i):
                out[key], last = self.value(start)[0], end
            return out, self.end_of(last, "}")
        if char == "[":
            vals, last = [], i + 1
            for start, end in self.items(i):
                vals.append(self.value(start)[0])
                last = end
            return vals, self.end_of(last, "]")
        if char == '"':
            return self.string(i)
        j = i
        while j < len(self.text) and self.text[j] not in ",}] \t\r\n/":
            j += 1
        try:
            return json.loads(self.text[i:j]), j
        except ValueError as exc:
            raise ThemeError(f"settings.json : valeur invalide (position {i})") from exc


def _indent_of(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    line = text[line_start:pos]
    return line[: len(line) - len(line.lstrip())]


def _render(obj: object, indent: str, nl: str) -> str:
    return json.dumps(obj, indent=4, ensure_ascii=False).replace("\n", nl + indent)


def _schemes_span(doc: _Jsonc) -> tuple[int, int] | None:
    for key, start, end in doc.members(doc.skip(0)):
        if key == "schemes":
            if doc.text[start] != "[":
                raise ThemeError("settings.json : « schemes » n'est pas une liste")
            return start, end
    return None


def update_wt_settings(text: str, scheme: dict[str, str]) -> str:
    """Insert or replace the scheme named scheme["name"]; the rest is kept verbatim."""
    bom = BOM if text.startswith(BOM) else ""
    body = text[len(bom) :]
    nl = "\r\n" if "\r\n" in body else "\n"
    doc = _Jsonc(body)
    before = doc.value(0)[0]
    if not isinstance(before, dict):
        raise ThemeError("settings.json : l'objet racine est attendu")
    span = _schemes_span(doc)
    if span is None:
        root = doc.skip(0) + 1
        ind = _indent_of(body, doc.skip(root)) or "    "
        entry = f'{nl}{ind}"schemes": [{nl}{ind}    {_render(scheme, ind + "    ", nl)}{nl}{ind}]'
        comma = "," if before else ""
        new = body[:root] + entry + comma + body[root:]
    else:
        new = _replace_or_insert(doc, span, scheme, nl)
    _check_only_scheme_changed(before, _Jsonc(new).value(0)[0], scheme)
    return bom + new


def _replace_or_insert(
    doc: _Jsonc, span: tuple[int, int], scheme: dict[str, str], nl: str
) -> str:
    body, (start, _end) = doc.text, span
    elements = list(doc.items(start))
    for el_start, el_end in elements:
        value = doc.value(el_start)[0]
        if isinstance(value, dict) and value.get("name") == scheme["name"]:
            rendered = _render(scheme, _indent_of(body, el_start), nl)
            return body[:el_start] + rendered + body[el_end:]
    if elements:
        ind = _indent_of(body, elements[0][0])
        first = elements[0][0]
        return body[:first] + _render(scheme, ind, nl) + "," + nl + ind + body[first:]
    ind = _indent_of(body, start) + "    "
    close = doc.end_of(start + 1, "]") - 1
    inner = f"{nl}{ind}{_render(scheme, ind, nl)}{nl}{_indent_of(body, start)}"
    return body[: start + 1] + inner + body[close:]


def _without(settings: dict[str, Any], name: str) -> dict[str, Any]:
    schemes = [
        s
        for s in settings.get("schemes", [])
        if not (isinstance(s, dict) and s.get("name") == name)
    ]
    rest = {k: v for k, v in settings.items() if k != "schemes"}
    return {**rest, "schemes": schemes}


def _check_only_scheme_changed(
    before: dict[str, Any], after: dict[str, Any], scheme: dict[str, str]
) -> None:
    ours = [
        s
        for s in after.get("schemes", [])
        if isinstance(s, dict) and s.get("name") == scheme["name"]
    ]
    if ours != [scheme] or _without(before, scheme["name"]) != _without(
        after, scheme["name"]
    ):
        raise ThemeError("settings.json : modification refusée (résultat inattendu)")


def find_wt_settings(users: Path = WT_USERS) -> tuple[Path, ...]:
    return tuple(sorted(users.glob(WT_GLOB))) if users.is_dir() else ()


def backup(path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.omniscient-{stamp}.bak")
    n = 1
    while target.exists():  # never overwrite an earlier backup
        n += 1
        target = path.with_name(f"{path.name}.omniscient-{stamp}-{n}.bak")
    shutil.copy2(path, target)
    return target


def write_windows_terminal(
    palette: Palette, settings: Path, now: datetime | None = None
) -> Path:
    """Back up then update settings.json; returns the backup path."""
    try:
        raw = settings.read_bytes().decode("utf-8")
        updated = update_wt_settings(raw, windows_terminal_scheme(palette))
        saved = backup(settings, now)
        tmp = settings.with_name(f".{settings.name}.omniscient.tmp")
        tmp.write_bytes(updated.encode("utf-8"))
        os.replace(tmp, settings)
    except (OSError, UnicodeDecodeError) as exc:
        raise _io_error(settings, exc) from exc
    return saved


def _io_error(path: Path, exc: Exception) -> ThemeError:
    if isinstance(exc, UnicodeDecodeError):
        return ThemeError(
            f"{path} n'est pas un fichier texte UTF-8 : rien n'a été modifié."
        )
    reason = getattr(exc, "strerror", None) or exc
    return ThemeError(f"Impossible de lire ou d'écrire {path} ({reason}).")


def pick_wt_settings(path: Path | None) -> Path:
    if path is not None:
        return path
    found = find_wt_settings()
    if len(found) == 1:
        return found[0]
    if not found:
        raise ThemeError(
            "settings.json de Windows Terminal introuvable sous /mnt/c/Users : "
            "colle le JSON ci-dessus dans « schemes », ou passe --path."
        )
    listed = ", ".join(str(p) for p in found)
    raise ThemeError(f"Plusieurs settings.json trouvés ({listed}) : passe --path.")


# ---- herdr ---------------------------------------------------------------------


def herdr_custom(palette: Palette) -> dict[str, str]:
    """herdr [theme.custom] tokens (empty when a built-in theme matches)."""
    if palette.name in HERDR_BUILTIN:
        return {}
    p, a = palette, palette.ansi
    return {
        "accent": p.accent,
        "panel_bg": p.background,
        "sidebar_bg": p.surface,
        "active_row_bg": p.surface,
        "selection_bg": a[8],
        "surface_dim": p.surface,
        "surface0": p.surface,
        "surface1": a[8],
        "overlay0": p.muted,
        "overlay1": p.muted,
        "subtext0": a[7],
        "text": p.foreground,
        "mauve": p.primary,
        "green": p.success,
        "yellow": a[3],
        "red": p.error,
        "peach": p.warning,
        "blue": a[4],
        "teal": p.secondary,
    }


def herdr_base(palette: Palette) -> str:
    fallback = "tokyo-night" if palette.dark else "catppuccin-latte"
    return HERDR_BUILTIN.get(palette.name, fallback)


def herdr_snippet(palette: Palette) -> str:
    lines = ["[theme]", f'name = "{herdr_base(palette)}"']
    custom = herdr_custom(palette)
    if custom:
        lines += ["", "[theme.custom]", *(f'{k} = "{v}"' for k, v in custom.items())]
    return "\n".join(lines) + "\n"


def update_herdr_config(text: str, palette: Palette) -> str:
    """Set [theme] name and replace [theme.custom]; everything else is kept."""
    out = set_toml_keys(text, "theme", {"name": herdr_base(palette)})
    custom = herdr_custom(palette)
    if custom or "[theme.custom]" in out:
        out = set_toml_keys(out, "theme.custom", custom, replace_body=True)
    return out


def herdr_config_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "herdr" / "config.toml"


def write_herdr(
    palette: Palette, config: Path, now: datetime | None = None
) -> Path | None:
    """Back up (when it exists) then update config.toml; returns the backup path."""
    try:
        try:
            text = config.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        updated = update_herdr_config(text, palette)
        saved = backup(config, now) if config.exists() else None
        write_atomic(config, updated)
    except (OSError, UnicodeDecodeError) as exc:
        raise _io_error(config, exc) from exc
    return saved


def herdr_warnings(config: Path) -> tuple[str, ...]:
    """French warnings about settings that would hide the exported theme."""
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return (f"config herdr illisible : {exc}",)
    theme = data.get("theme")
    if isinstance(theme, dict) and theme.get("auto_switch") is True:
        return (AUTO_SWITCH_WARNING,)
    return ()
