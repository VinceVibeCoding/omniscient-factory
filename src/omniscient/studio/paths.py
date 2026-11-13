"""Where the studio finds the user's home, its vault, registry and config.

Kept apart from omniscient.cli so the studio never imports the big CLI module.
The attributes REPO (the home root), VAULT, REPOS, CONFIG, ... are resolved lazily,
on each access, from omniscient.home.find_home() — never at import — so
OMNISCIENT_HOME and the current directory are honoured, and tests can still
monkeypatch them as plain module attributes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from omniscient.home import Home, find_home, packaged

NO_HOME = "Aucun dossier Omniscient trouvé : lance `omniscient init`."

HOME_PATHS: dict[str, Callable[[Home], Path]] = {
    "REPO": lambda home: home.root,  # the home root (the owner's checkout is one)
    "VAULT": lambda home: home.vault,
    "REPOS": lambda home: home.repos,
    "CONFIG": lambda home: home.config,
    "ROUTING": lambda home: routing_file(home),
    "ENV_FILE": lambda home: home.env_file,
    "AGENTS_DIR": lambda home: home.agents_dir,
    "POCS": lambda home: home.pocs,
    "SITE_CLONE": lambda home: home.site_clone,  # clone of the PoC site, if any
}


def routing_file(home: Home) -> Path:
    """The home's config/routing.toml, else the packaged default routing."""
    local = home.root / "config" / "routing.toml"
    return local if local.is_file() else packaged("templates") / "routing.toml"


class NoHomeError(Exception):
    """No Omniscient home was found (no $OMNISCIENT_HOME, no config upwards, no ~/.omniscient)."""

    def __init__(self) -> None:
        super().__init__(NO_HOME)


def require_home() -> Home:
    """The current user's home, or NoHomeError when it has no config yet."""
    home = find_home()
    if not home.exists:
        raise NoHomeError()
    return home


def home_path(module: str, name: str) -> Path:
    """One of HOME_PATHS for the current home (module names the caller, for errors)."""
    if name in HOME_PATHS:
        return HOME_PATHS[name](require_home())
    raise AttributeError(f"module {module!r} has no attribute {name!r}")


def __getattr__(name: str) -> Path:
    return home_path(__name__, name)
