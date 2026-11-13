"""`omniscient group …`: the omnigroup daemon, its hooks and the Windows widget.

The agents' hooks do NOT go through this module: the installer writes the lightweight
`omniscient-hook` script (omniscient.group.hook_client), which starts without importing
the whole CLI. `omniscient group hook` is the same client, for manual use and old
installs. Only serve and status talk to a running daemon; hook and statusline stay
silent (exit 0) without one, and without an Omniscient home.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer

from omniscient.home import Home, find_home

DEFAULT_PORT = 7341
STATUS_TIMEOUT_S = 3.0
REPO_URL = "https://github.com/VinceVibeCoding/omniscient-factory"
STATE_LABELS = {
    "working": "en cours",
    "waiting": "en attente",
    "idle": "inactives",
    "done": "terminées",
    "error": "en erreur",
}

group_app = typer.Typer(
    help="omnigroup : tous tes agents (Claude, Copilot, herdr, AgentRun) dans un widget.",
    no_args_is_help=True,
)
hooks_app = typer.Typer(
    help="Hooks omnigroup de Claude Code et de Copilot (aperçu, puis --write).",
    no_args_is_help=True,
)
group_app.add_typer(hooks_app, name="hooks")

HomeOpt = Annotated[
    Path | None,
    typer.Option("--home", help="Dossier Omniscient (défaut : celui détecté)."),
]
PortOpt = Annotated[int, typer.Option("--port", help="Port local du démon.")]


def _home(home_dir: Path | None) -> Home:
    return Home(home_dir.expanduser().resolve()) if home_dir else find_home()


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Erreur : {message}", err=True)
    return typer.Exit(code=1)


# ---- daemon ----------------------------------------------------------------------------


@group_app.command("serve")
def serve(
    port: PortOpt = DEFAULT_PORT,
    no_collectors: Annotated[
        bool,
        typer.Option("--no-collectors", help="Sans herdr ni AgentRun : hooks seuls."),
    ] = False,
    home_dir: HomeOpt = None,
) -> None:
    """Lancer le démon omnigroup (127.0.0.1 seulement ; Ctrl-C pour arrêter)."""
    from omniscient.group.server import serve as run_server

    try:
        run_server(_home(home_dir), port, () if no_collectors else None)
    except (OSError, RuntimeError) as exc:
        raise _fail(f"le démon n'a pas pu démarrer ({exc})") from exc


def _get(url: str, token: str | None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=STATUS_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summary(sessions: list[dict[str, Any]]) -> list[str]:
    """French lines: sessions by state, then the open requests."""
    if not sessions:
        return ["Aucune session suivie pour le moment."]
    by_state = Counter(str(s.get("state")) for s in sessions)
    parts = [f"{n} {STATE_LABELS.get(k, k)}" for k, n in sorted(by_state.items())]
    pending = sum(len(s.get("pending") or []) for s in sessions)
    lines = [f"{len(sessions)} session(s) : " + ", ".join(parts)]
    lines.append(f"{pending} demande(s) en attente de ta réponse.")
    return lines


@group_app.command("status")
def status(port: PortOpt = DEFAULT_PORT, home_dir: HomeOpt = None) -> None:
    """État du démon et nombre de sessions suivies."""
    from omniscient.group.server import read_token

    base = f"http://127.0.0.1:{port}"
    try:
        health = _get(base + "/health", None)
    except (OSError, ValueError) as exc:
        typer.echo(
            f"omnigroup ne répond pas sur {base} : lance `omniscient group serve`."
        )
        raise typer.Exit(code=1) from exc
    typer.echo(f"omnigroup {health.get('version', '?')} répond sur {base}.")
    token = read_token(_home(home_dir))
    if token is None:
        raise _fail("jeton introuvable : lance `omniscient group token`.")
    try:
        sessions = _get(base + "/sessions", token).get("sessions") or []
    except urllib.error.HTTPError as exc:
        raise _fail(f"le démon refuse le jeton ({exc.code}).") from exc
    except (OSError, ValueError) as exc:
        raise _fail(f"sessions illisibles ({exc}).") from exc
    for line in summary(sessions):
        typer.echo(line)


@group_app.command("token")
def token(home_dir: HomeOpt = None) -> None:
    """Afficher le jeton du démon (créé s'il manque) : c'est ce que lit le widget."""
    from omniscient.group.server import load_or_create_token

    try:
        typer.echo(load_or_create_token(_home(home_dir)))
    except (OSError, RuntimeError) as exc:
        raise _fail(str(exc)) from exc


# ---- hook client (no daemon needed) -----------------------------------------------------


@group_app.command("hook")
def hook(
    source: Annotated[str, typer.Option("--source", help="claude ou copilot.")],
    fast: Annotated[bool, typer.Option("--fast", help="Suivi seul.")] = False,
    event: Annotated[str | None, typer.Option("--event")] = None,
    home_dir: HomeOpt = None,
) -> None:
    """Relayer un hook d'agent au démon (toujours code 0 ; silencieux s'il est absent)."""
    from omniscient.group.hook_client import main

    argv = ["--source", source] + (["--fast"] if fast else [])
    argv += (["--event", event] if event else []) + (
        ["--home", str(home_dir)] if home_dir else []
    )
    try:
        main(argv)
    except SystemExit as exc:  # argparse: bad --source
        if exc.code not in (0, None):
            typer.echo("omnigroup hook ignoré : --source claude|copilot", err=True)
    raise typer.Exit(code=0)


@group_app.command("statusline")
def statusline(home_dir: HomeOpt = None) -> None:
    """Ligne d'état de Claude Code (et usage transmis au démon s'il tourne)."""
    from omniscient.group.hook_client import default_context, run_statusline

    run_statusline(sys.stdin, sys.stdout, default_context(home_dir))


# ---- hooks install|uninstall|status ------------------------------------------------------

TargetOpt = Annotated[str, typer.Option("--target", help="claude, copilot ou all.")]
WriteOpt = Annotated[
    bool, typer.Option("--write", help="Écrire (sinon : aperçu du diff seulement).")
]
PathOpt = Annotated[
    Path | None, typer.Option("--path", help="Autre fichier (une seule cible).")
]


def _hooks(
    action: str, target: str, write: bool, path: Path | None, **extra: Any
) -> None:
    from omniscient.group.hook_install import HookInstallError, run_hooks_command

    try:
        run_hooks_command(
            action,
            target,
            write,
            path.expanduser() if path else None,
            say=typer.echo,
            **extra,
        )
    except HookInstallError as exc:
        raise _fail(str(exc)) from exc


@hooks_app.command("install")
def hooks_install(
    target: TargetOpt = "all",
    write: WriteOpt = False,
    path: PathOpt = None,
    with_statusline: Annotated[
        bool,
        typer.Option("--statusline", help="Aussi la statusLine de Claude (usage)."),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Remplacer une statusLine existante.")
    ] = False,
) -> None:
    """Ajouter les hooks omnigroup (les tiens sont conservés, sauvegarde avant écriture)."""
    _hooks("install", target, write, path, statusline=with_statusline, force=force)


@hooks_app.command("uninstall")
def hooks_uninstall(
    target: TargetOpt = "all", write: WriteOpt = False, path: PathOpt = None
) -> None:
    """Retirer uniquement les hooks omnigroup."""
    _hooks("uninstall", target, write, path)


@hooks_app.command("status")
def hooks_status(target: TargetOpt = "all", path: PathOpt = None) -> None:
    """Quels événements portent le hook omnigroup ?"""
    _hooks("status", target, False, path)


# ---- widget --------------------------------------------------------------------------------

WIDGET_HELP = f"""Widget Windows « Omnigroup » (coin de l'écran, toujours au-dessus) :

1. Télécharge l'installateur (.exe NSIS) :
   - versions publiées : {REPO_URL}/releases (tags widget-v*) ;
   - ou l'artefact « omnigroup-windows-nsis » du dernier run du workflow « Widget » :
     {REPO_URL}/actions/workflows/widget.yml
2. Dans WSL, lance le démon : omniscient group serve
   (ou au démarrage : omniscient schedule install --write, puis
   systemctl --user enable --now omnigroup.service)
3. Ouvre Omnigroup sous Windows : il lit le jeton via
   `wsl.exe -e omniscient group token`. Sinon, ouvre ses réglages et colle la
   sortie de `omniscient group token`.
4. Branche les agents : omniscient group hooks install --target all
   (aperçu), puis la même commande avec --write.

Le démon n'écoute que 127.0.0.1:{DEFAULT_PORT} ; le widget y accède par le
localhost de WSL (forwarding ou réseau en miroir)."""


@group_app.command("widget")
def widget() -> None:
    """Où trouver l'installateur Windows du widget et comment l'appairer."""
    typer.echo(WIDGET_HELP)


def register_group(app: typer.Typer) -> None:
    app.add_typer(group_app, name="group")
