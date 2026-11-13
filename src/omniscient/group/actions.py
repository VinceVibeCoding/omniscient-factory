"""What the widget's "Revenir" (jump) and "Envoyer un message" (prompt) buttons do.

Everything runs as argv lists through an injectable runner: no shell strings carry user
data. Windows apps are reached from WSL through interop (cmd.exe, explorer.exe, code.cmd);
outside WSL the command or URL comes back as text for the owner to open.

Claude Code sessions are never driven from here: a busy one, or one open in VS Code, is
refused; otherwise the owner gets the `claude -p --resume` command to run themselves.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from omniscient.group.collect_agentrun import (
    AgentRunHttpError,
    Api,
    default_api,
    send_message,
)
from omniscient.group.models import Session
from omniscient.studio.herdr import HerdrError
from omniscient.studio.herdr import default_runner as herdr_runner

RUN_TIMEOUT_S = 15
PROMPT_MAX = 8000
VSCODE_SESSION_URI = "vscode://anthropic.claude-code/open?session={id}"
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
DISTRO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_POSIX_PATH = re.compile(r"^/[A-Za-z0-9._/-]*$")
PANE_ID = re.compile(r"^[A-Za-z0-9:_.-]{1,64}$")
# cmd.exe would interpret these in a code.cmd path; refuse rather than escape.
CMD_META = re.compile(r'[&|<>^%"!\r\n]')
AGENTRUN_CONSOLE = "127.0.0.1:7340"  # the only plain-http page the widget may open

Runner = Callable[[Sequence[str]], tuple[int, str]]
HerdrRunner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str  # French, shown in the widget
    command: str | None = None  # what the owner can run or open themselves

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "command": self.command}


def default_runner(argv: Sequence[str]) -> tuple[int, str]:
    """Run argv (no shell); (exit code, stdout). A missing program is exit code 127."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout


def is_wsl(
    environ: Mapping[str, str] | None = None, proc_version: Path | None = None
) -> bool:
    env = os.environ if environ is None else environ
    if env.get("WSL_DISTRO_NAME"):
        return True
    path = proc_version or Path("/proc/version")
    try:
        return "microsoft" in path.read_text(encoding="utf-8").lower()
    except OSError:
        return False


@dataclass(frozen=True)
class Env:
    """Where actions run: the runners and whether Windows is reachable (WSL)."""

    runner: Runner = default_runner
    herdr: HerdrRunner = herdr_runner
    wsl: bool | None = None
    environ: Mapping[str, str] | None = None

    @property
    def on_wsl(self) -> bool:
        return is_wsl(self.environ) if self.wsl is None else self.wsl

    @property
    def distro(self) -> str:
        env = os.environ if self.environ is None else self.environ
        return env.get("WSL_DISTRO_NAME", "")


# ---- jump --------------------------------------------------------------------------


def jump(session: Session, env: Env | None = None) -> ActionResult:
    """Bring the owner back to the session (VS Code, herdr pane, console page, terminal)."""
    env = env or Env()
    target = session.jump
    if target is None:
        return ActionResult(False, "Aucun moyen connu de revenir à cette session.")
    handlers = {
        "vscode_session": _jump_vscode_session,
        "vscode_folder": _jump_vscode_folder,
        "herdr": _jump_herdr,
        "url": _jump_url,
        "terminal": _jump_terminal,
    }
    return handlers[target.kind](target.value, env)


def _windows(env: Env, argv: Sequence[str], shown: str, done: str) -> ActionResult:
    if not env.on_wsl:
        return ActionResult(True, f"Ouvre toi-même : {shown}", command=shown)
    code, _ = env.runner(argv)
    if code != 0:
        return ActionResult(
            False, f"Windows n'a pas pu ouvrir : {shown}", command=shown
        )
    return ActionResult(True, done, command=shown)


def _jump_vscode_session(value: str, env: Env) -> ActionResult:
    if not SESSION_ID.match(value):
        return ActionResult(False, "Identifiant de session Claude invalide.")
    uri = VSCODE_SESSION_URI.format(id=value)
    return _windows(
        env, ["cmd.exe", "/c", "start", "", uri], uri, "Session ouverte dans VS Code."
    )


def find_code_cmd(env: Env) -> str | None:
    """The Windows path of VS Code's code.cmd (`cmd.exe /c where code`), if safe to run."""
    code, out = env.runner(["cmd.exe", "/c", "where", "code"])
    if code != 0:
        return None
    for line in out.splitlines():
        path = line.strip()
        if path.lower().endswith("code.cmd") and not CMD_META.search(path):
            return path
    return None


def _jump_vscode_folder(value: str, env: Env) -> ActionResult:
    if not SAFE_POSIX_PATH.match(value) or ".." in value.split("/"):
        return ActionResult(False, "Dossier non pris en charge (caractères spéciaux).")
    distro = env.distro
    if not env.on_wsl or not DISTRO.match(distro):
        command = shlex.join(["code", value])
        return ActionResult(True, f"Ouvre toi-même : {command}", command=command)
    uri = f"vscode-remote://wsl+{distro}{value}"
    code_cmd = find_code_cmd(env)
    if code_cmd is None:
        return ActionResult(
            False, "VS Code (code.cmd) introuvable côté Windows.", command=uri
        )
    argv = ["cmd.exe", "/c", code_cmd, "--folder-uri", uri]
    return _windows(env, argv, uri, "Dossier ouvert dans VS Code.")


def _jump_herdr(value: str, env: Env) -> ActionResult:
    if not PANE_ID.match(value):
        return ActionResult(False, "Panneau herdr invalide.")
    try:
        env.herdr(["agent", "focus", value])
    except HerdrError as exc:
        return ActionResult(
            False, f"herdr : {exc}", command=f"herdr agent focus {value}"
        )
    return ActionResult(True, "Panneau herdr mis au premier plan.")


def safe_url(value: str) -> bool:
    """https anywhere (no credentials in it), or exactly the local AgentRun console
    (http://127.0.0.1:7340/…)."""
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    if any(ch.isspace() or ch in '"<>^`|' for ch in value) or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.scheme == "https":
        return True
    console = parts.scheme == "http" and parts.netloc == AGENTRUN_CONSOLE
    return console and value.startswith(f"http://{AGENTRUN_CONSOLE}/")


def ps_quote(arg: str) -> str:
    """One PowerShell argument, single-quoted (a quote is doubled; nothing expands)."""
    return "'" + arg.replace("'", "''") + "'"


def windows_command(argv: Sequence[str], env: Env) -> str:
    """What the owner can paste in PowerShell on Windows: argv run inside WSL."""
    distro = env.distro
    prefix = ["wsl.exe", *(["-d", distro] if DISTRO.match(distro) else []), "-e"]
    return "& " + " ".join(ps_quote(a) for a in [*prefix, *argv])


def _yourself(lead: str, argv: Sequence[str], env: Env) -> ActionResult:
    """« lance toi-même »: the POSIX command, plus its PowerShell form on WSL."""
    command = shlex.join(argv)
    message = f"{lead} : {command}"
    if env.on_wsl:
        message += f" — depuis PowerShell : {windows_command(argv, env)}"
    return ActionResult(True, message, command=command)


def _jump_url(value: str, env: Env) -> ActionResult:
    if not safe_url(value):
        return ActionResult(
            False, "Lien refusé : seulement https (ou la console locale)."
        )
    return _windows(
        env, ["explorer.exe", value], value, "Page ouverte dans le navigateur."
    )


def _jump_terminal(value: str, env: Env) -> ActionResult:
    if not SESSION_ID.match(value):
        return ActionResult(False, "Identifiant de session Claude invalide.")
    return _yourself("Dans un terminal", ["claude", "--resume", value], env)


# ---- prompt ------------------------------------------------------------------------


def prompt(
    session: Session, text: str, env: Env | None = None, agentrun: Api | None = None
) -> ActionResult:
    """Send the owner's message to an idle agent, or say how to do it by hand."""
    env = env or Env()
    clean = text.strip() if isinstance(text, str) else ""
    if not clean:
        return ActionResult(False, "Message vide.")
    if len(clean) > PROMPT_MAX:
        return ActionResult(False, f"Message trop long (max {PROMPT_MAX} caractères).")
    pane = _herdr_pane(session)
    if pane is not None:
        return _prompt_herdr(session, pane, clean, env)
    if session.source == "agentrun":
        return _prompt_agentrun(session, clean, agentrun)
    if session.source == "claude":
        return _prompt_claude(session, clean, env)
    return ActionResult(
        False, "Impossible d'écrire à cet agent d'ici : reviens à sa session."
    )


def _herdr_pane(session: Session) -> str | None:
    if session.source == "herdr":
        return session.native_id
    if session.jump is not None and session.jump.kind == "herdr":
        return session.jump.value
    return None


def _prompt_herdr(session: Session, pane: str, text: str, env: Env) -> ActionResult:
    if session.state not in ("idle", "done"):
        return ActionResult(
            False, "L'agent n'est pas au repos : attends qu'il ait fini."
        )
    if not PANE_ID.match(pane):
        return ActionResult(False, "Panneau herdr invalide.")
    try:
        env.herdr(["agent", "prompt", pane, text])
    except HerdrError as exc:
        return ActionResult(False, f"herdr a refusé : {exc}")
    return ActionResult(True, "Message envoyé dans herdr.")


def _prompt_agentrun(session: Session, text: str, api: Api | None) -> ActionResult:
    if session.state == "working":
        return ActionResult(
            False, "Le run travaille : attends qu'il s'arrête ou te pose une question."
        )
    if api is None:
        from omniscient.home import find_home

        api = default_api(find_home().env_file)
    try:
        ok, message = send_message(api, session.native_id, text)
    except AgentRunHttpError as exc:
        return ActionResult(False, f"AgentRun : {exc}")
    return ActionResult(ok, message)


def _prompt_claude(session: Session, text: str, env: Env) -> ActionResult:
    if session.state != "idle":
        return ActionResult(
            False, "Claude n'est pas au repos : attends la fin de son tour."
        )
    in_vscode = (
        session.jump is not None and session.jump.kind == "vscode_session"
    ) or (session.meta.get("ide") == "vscode")
    if in_vscode:
        return ActionResult(
            False, "Cette session est ouverte dans VS Code : réponds-y directement."
        )
    if not SESSION_ID.match(session.native_id):
        return ActionResult(False, "Identifiant de session Claude invalide.")
    argv = ["claude", "-p", "--resume", session.native_id, text]
    return _yourself("Lance toi-même", argv, env)
