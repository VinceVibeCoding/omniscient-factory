"""Widget pairing: a 6-digit code shown only on the Windows desktop, typed in the widget.

POST /pair/start (no credential) opens one pairing at a time, for two minutes, and shows
the code through a native Windows message (`powershell.exe` with a fixed script, the
code as its argument; fallback `msg.exe *`). If Windows cannot show it, pairing is
refused: the owner can then run `omniscient group pair --show-code` in a terminal they
opened themselves (only when the daemon runs with --allow-terminal-pairing: weaker,
see docs/omnigroup.md). POST /pair/finish {code, client_name} allows five wrong codes,
then locks pairing for ten minutes. Success mints a widget token (credentials.py).
"""

from __future__ import annotations

import hmac
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

CODE_TTL_S = 120.0
MAX_ATTEMPTS = 5
LOCK_S = 600.0
START_WINDOW_S = 600.0
MAX_STARTS = 5
LAUNCH_GRACE_S = 2.0
POPUP_SECONDS = 120
CODE_DIGITS = 6
# Single quotes only (no quoting games through WSL interop); the code is an argument,
# checked again by the script itself.
POWERSHELL_SCRIPT = (
    "& { param([string]$c); if ($c -notmatch '^[0-9]{6}$') { exit 2 }; "
    "$n = [Environment]::NewLine; "
    "$t = 'Code de couplage Omnigroup : ' + $c + $n + $n + "
    "'Tape-le dans le widget (valable 2 minutes).' + $n + "
    "'Si tu n''as rien demandé, ignore ce message.'; "
    "[void](New-Object -ComObject WScript.Shell).Popup($t, 120, 'Omnigroup', 64) }"
)
TERMINAL_HINT = (
    "Impossible d'afficher le code sur le bureau Windows. Dans un terminal que tu "
    "ouvres toi-même, lance `omniscient group pair --show-code` (le démon doit tourner "
    "avec --allow-terminal-pairing)."
)

Clock = Callable[[], float]
Launcher = Callable[[Sequence[str]], bool]


class PairError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def valid_code(code: object) -> bool:
    return isinstance(code, str) and len(code) == CODE_DIGITS and code.isascii() and code.isdigit()  # fmt: skip


@dataclass
class PairingDesk:
    """One pairing at a time; attempts and starts are limited. Thread-safe."""

    clock: Clock = time.monotonic
    _code: str | None = None
    _expires: float = 0.0
    _attempts: int = 0
    _locked_until: float = 0.0
    _starts: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> str:
        """A new code; 429 when locked or started too often, 409 when one is open."""
        with self._lock:
            now = self.clock()
            self._check_lock(now)
            if self._code is not None and now < self._expires:
                raise PairError(409, "Un couplage est déjà en cours (2 minutes max).")
            self._starts = [t for t in self._starts if now - t < START_WINDOW_S]
            if len(self._starts) >= MAX_STARTS:
                raise PairError(429, "Trop de couplages demandés : réessaie plus tard.")
            self._starts.append(now)
            self._code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
            self._expires = now + CODE_TTL_S
            self._attempts = 0
            return self._code

    def cancel(self) -> None:
        with self._lock:
            self._code = None

    def finish(self, code: str) -> None:
        """Accept the code once; wrong codes count, the fifth locks pairing."""
        with self._lock:
            now = self.clock()
            self._check_lock(now)
            if self._code is None or now >= self._expires:
                self._code = None
                raise PairError(409, "Aucun couplage en cours : redemande un code.")
            if hmac.compare_digest(code.encode(), self._code.encode()):
                self._code = None
                return
            self._attempts += 1
            if self._attempts >= MAX_ATTEMPTS:
                self._code = None
                self._locked_until = now + LOCK_S
                raise PairError(429, "Trop d'essais : couplage bloqué 10 minutes.")
            left = MAX_ATTEMPTS - self._attempts
            raise PairError(403, f"Code incorrect ({left} essai(s) restant(s)).")

    def _check_lock(self, now: float) -> None:
        if now < self._locked_until:
            raise PairError(429, "Couplage bloqué après trop d'essais : réessaie plus tard.")  # fmt: skip


# ---- showing the code on Windows ----------------------------------------------------


def launch(argv: Sequence[str]) -> bool:
    """Start argv (no shell); True when it is still running after a short grace (a
    message box is up) or exited with 0. A missing program is False."""
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    try:
        return proc.wait(timeout=LAUNCH_GRACE_S) == 0
    except subprocess.TimeoutExpired:
        return True


def code_commands(code: str) -> list[list[str]]:
    """The Windows commands that can show `code`, in order of preference."""
    if not valid_code(code):
        raise ValueError("pairing code must be six digits")
    message = (
        f"Code de couplage Omnigroup : {code} — tape-le dans le widget "
        "(valable 2 minutes). Si tu n'as rien demandé, ignore ce message."
    )
    return [
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            POWERSHELL_SCRIPT,
            code,
        ],
        ["msg.exe", "*", f"/TIME:{POPUP_SECONDS}", message],
    ]


def show_code_on_windows(code: str, launcher: Launcher = launch) -> bool:
    """True when one of the Windows commands showed the code."""
    return any(launcher(argv) for argv in code_commands(code))
