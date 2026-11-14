"""Widget display settings: `~/.config/omnigroup/config.json` (+ Claude Code settings).

The file holds the owner's choices (theme, OSD view, ticker); the widget's right-click
menu, `omniscient group config set` and the /omnigroup Claude Code skills all write it.
A key can also be set in `~/.claude/settings.json` under `env` (OMNIGROUP_THEME,
OMNIGROUP_OSD_VIEW, OMNIGROUP_SHOW_TICKER): it then wins and is "locked" — changing it
elsewhere is refused (409) so the two never disagree silently. Unknown keys in the file
are kept on write; invalid values are ignored with a warning.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniscient.group.credentials import secure_dir, write_atomic

log = logging.getLogger(__name__)

THEMES = ("default", "terminal", "dashboard")
VIEWS = ("bars", "gauge")
DEFAULTS: dict[str, Any] = {"theme": "default", "osd_view": "bars", "show_ticker": True}
ENV_KEYS = {
    "theme": "OMNIGROUP_THEME",
    "osd_view": "OMNIGROUP_OSD_VIEW",
    "show_ticker": "OMNIGROUP_SHOW_TICKER",
}
TRUE_WORDS = frozenset({"1", "true", "yes", "on", "oui"})
FALSE_WORDS = frozenset({"0", "false", "no", "off", "non"})
MAX_FILE_BYTES = 256 * 1024


class ConfigError(Exception):
    """A refused change: `status` is the HTTP code (400 invalid, 409 locked)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ConfigPaths:
    config: Path
    settings: Path

    @classmethod
    def default(cls, environ: Mapping[str, str] | None = None) -> ConfigPaths:
        env = os.environ if environ is None else environ
        xdg = env.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        claude = env.get("CLAUDE_CONFIG_DIR", "").strip()
        claude_dir = Path(claude).expanduser() if claude else Path.home() / ".claude"
        return cls(base / "omnigroup" / "config.json", claude_dir / "settings.json")


def coerce(key: str, value: Any) -> Any:
    """The valid value for `key`, or raise ConfigError(400)."""
    if key == "theme" and isinstance(value, str) and value.strip().lower() in THEMES:
        return value.strip().lower()
    if key == "osd_view" and isinstance(value, str) and value.strip().lower() in VIEWS:
        return value.strip().lower()
    if key == "show_ticker":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in TRUE_WORDS | FALSE_WORDS:
            return value.strip().lower() in TRUE_WORDS
    if key not in DEFAULTS:
        raise ConfigError(
            400, f"réglage inconnu : {key} (connus : {', '.join(DEFAULTS)})"
        )
    allowed = {"theme": THEMES, "osd_view": VIEWS, "show_ticker": ("true", "false")}[
        key
    ]
    raise ConfigError(
        400, f"valeur invalide pour {key} : {value!r} (attendu : {', '.join(allowed)})"
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.warning("omnigroup: %s ignored (too large)", path.name)
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("omnigroup: %s ignored: %s", path.name, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _valid_or_none(key: str, value: Any, origin: str) -> Any:
    try:
        return coerce(key, value)
    except ConfigError:
        log.warning("omnigroup: %s: invalid %s=%r ignored", origin, key, value)
        return None


def settings_overrides(paths: ConfigPaths) -> dict[str, Any]:
    """Valid OMNIGROUP_* values from the Claude Code settings `env` block."""
    env = _read_json(paths.settings).get("env")
    if not isinstance(env, dict):
        return {}
    found: dict[str, Any] = {}
    for key, var in ENV_KEYS.items():
        if var in env:
            value = _valid_or_none(key, env[var], "settings.json")
            if value is not None:
                found[key] = value
    return found


def effective(paths: ConfigPaths) -> dict[str, Any]:
    """{"config": {...}, "sources": {key: "default"|"config"|"settings"}}."""
    stored = _read_json(paths.config)
    locked = settings_overrides(paths)
    config: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, default in DEFAULTS.items():
        value = (
            _valid_or_none(key, stored[key], "config.json") if key in stored else None
        )
        config[key], sources[key] = (
            (default, "default") if value is None else (value, "config")
        )
        if key in locked:
            config[key], sources[key] = locked[key], "settings"
    return {"config": config, "sources": sources}


def update(paths: ConfigPaths, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and write `patch` into config.json; returns the effective view."""
    if not isinstance(patch, Mapping) or not patch:
        raise ConfigError(400, "aucun réglage à changer")
    clean = {key: coerce(key, value) for key, value in patch.items()}
    locked = settings_overrides(paths)
    for key in clean:
        if key in locked:
            raise ConfigError(
                409, f"{key} est fixé dans settings.json ({ENV_KEYS[key]})"
            )
    stored = {**_read_json(paths.config), **clean}
    secure_dir(paths.config.parent)
    write_atomic(paths.config, json.dumps(stored, indent=2, ensure_ascii=False) + "\n")
    return effective(paths)


def fingerprint(paths: ConfigPaths) -> tuple[float, float]:
    """Modification times of both files (0 when missing): a cheap change detector."""

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return mtime(paths.config), mtime(paths.settings)
