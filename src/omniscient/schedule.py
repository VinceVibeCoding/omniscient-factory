"""`omniscient schedule`: render the systemd user units of the daily cycle (and Slack bot).

Never runs systemctl. `install` prints the rendered units; with `--write` it also writes
them to ~/.config/systemd/user/ and prints the one systemctl command the user runs.
ExecStart is the ABSOLUTE path of the running `omniscient` executable, so the units do
not depend on uv, a checkout or the PATH of the systemd user manager.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from omniscient.home import packaged

UNITS = (
    "omniscient-cycle.service",
    "omniscient-cycle.timer",
    "omniscient-slack.service",
    "omnigroup.service",
)
TIMER = "omniscient-cycle.timer"
SLACK = "omniscient-slack.service"
FORBIDDEN_IN_PATHS = ('"', "\\", "\n", "\r")
ENABLE_CMD = f"systemctl --user daemon-reload && systemctl --user enable --now {TIMER}"
GROUP = "omnigroup.service"
SLACK_CMD = f"systemctl --user enable --now {SLACK}"
GROUP_CMD = f"systemctl --user enable --now {GROUP}"
DISABLE_CMD = f"systemctl --user disable --now {TIMER} {SLACK} {GROUP}"
RELOAD_CMD = "systemctl --user daemon-reload"

Which = Callable[[str], str | None]


class ScheduleError(Exception):
    """The units cannot be rendered or written (French message for the user)."""


@dataclass(frozen=True)
class Unit:
    name: str
    text: str


def unit_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user"


def omniscient_executable(which: Which | None = None, argv0: str | None = None) -> Path:
    """Absolute path of the `omniscient` being run (PATH first, else argv[0])."""
    found = (which or shutil.which)("omniscient")
    if found:
        return Path(found).resolve()
    candidate = Path(argv0 if argv0 is not None else sys.argv[0]).expanduser()
    if candidate.name.startswith("omniscient") and candidate.is_file():
        return candidate.resolve()
    raise ScheduleError(
        "exécutable `omniscient` introuvable dans le PATH : installe-le avec "
        "`uv tool install …` puis relance `omniscient schedule install`."
    )


def _systemd_value(path: Path, what: str) -> str:
    text = str(path)
    if not path.is_absolute() or any(c in text for c in FORBIDDEN_IN_PATHS):
        raise ScheduleError(
            f"{what} inutilisable dans une unité systemd : {text!r} "
            "(chemin absolu sans guillemet, barre oblique inverse ni retour à la ligne)."
        )
    return text.replace("%", "%%")  # systemd specifiers start with %


def render_units(
    home_dir: Path, executable: Path, templates: Path | None = None
) -> tuple[Unit, ...]:
    folder = templates or packaged("templates") / "systemd"
    values = {
        "{HOME_DIR}": _systemd_value(home_dir, "dossier Omniscient"),
        "{OMNISCIENT}": f'"{_systemd_value(executable, "exécutable omniscient")}"',
    }
    units = []
    for name in UNITS:
        source = folder / name
        if not source.is_file():
            raise ScheduleError(f"modèle systemd introuvable : {source}")
        text = source.read_text(encoding="utf-8")
        for placeholder, value in values.items():
            text = text.replace(placeholder, value)
        units.append(Unit(name, text))
    return tuple(units)


def write_units(units: tuple[Unit, ...], target: Path) -> tuple[Path, ...]:
    try:
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for unit in units:
            path = target / unit.name
            path.write_text(unit.text, encoding="utf-8")
            written.append(path)
    except OSError as exc:
        raise ScheduleError(f"écriture impossible dans {target} ({exc})") from exc
    return tuple(written)


def remove_units(target: Path) -> tuple[Path, ...]:
    removed = []
    for name in UNITS:
        path = target / name
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ScheduleError(f"suppression impossible : {path} ({exc})") from exc
        removed.append(path)
    return tuple(removed)


def unit_states(units: tuple[Unit, ...], target: Path) -> tuple[tuple[str, str], ...]:
    """(name, état) per unit: absent, à jour, or différent (re-run install --write)."""
    states = []
    for unit in units:
        path = target / unit.name
        if not path.is_file():
            states.append((unit.name, "absent"))
            continue
        same = path.read_text(encoding="utf-8", errors="replace") == unit.text
        states.append((unit.name, "à jour" if same else "différent du modèle"))
    return tuple(states)
