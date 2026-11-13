"""`omniscient group …`: the omnigroup daemon, its hooks and the Windows widget.

The agents' hooks do NOT go through this module: the installer writes the lightweight
`omniscient-hook` script (omniscient.group.hook_client), which starts without importing
the whole CLI. `omniscient group hook` is the same client, for manual use and old
installs. Only serve and status talk to a running daemon; hook and statusline stay
silent (exit 0) without one, and without an Omniscient home.
"""

from __future__ import annotations

import json
import sqlite3
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
    allow_terminal_pairing: Annotated[
        bool,
        typer.Option(
            "--allow-terminal-pairing",
            help="Autoriser `group pair --show-code` (plus faible, voir la doc).",
        ),
    ] = False,
    home_dir: HomeOpt = None,
) -> None:
    """Lancer le démon omnigroup (127.0.0.1 seulement ; Ctrl-C pour arrêter)."""
    from omniscient.group.server import serve as run_server

    try:
        run_server(
            _home(home_dir),
            port,
            () if no_collectors else None,
            allow_terminal_pairing=allow_terminal_pairing,
        )
    except (OSError, RuntimeError) as exc:
        raise _fail(f"le démon n'a pas pu démarrer ({exc})") from exc


def _call(url: str, token: str | None = None, body: Any = None) -> Any:
    """One local call to the daemon (never through a proxy)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=STATUS_TIMEOUT_S) as resp:
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
    """État du démon et nombre de sessions suivies (lues dans group.db)."""
    from omniscient.group.credentials import db_path
    from omniscient.group.models import to_json
    from omniscient.group.store import Store

    base = f"http://127.0.0.1:{port}"
    try:
        health = _call(base + "/health")
    except (OSError, ValueError) as exc:
        typer.echo(
            f"omnigroup ne répond pas sur {base} : lance `omniscient group serve`."
        )
        raise typer.Exit(code=1) from exc
    typer.echo(f"omnigroup {health.get('version', '?')} répond sur {base}.")
    try:
        store = Store(db_path(_home(home_dir)))
        try:
            sessions = []
            for s in store.list_sessions():
                data = to_json(s)
                data["pending"] = [to_json(p) for p in store.pending_for(s.id)]
                sessions.append(data)
        finally:
            store.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise _fail(f"sessions illisibles ({exc}).") from exc
    for line in summary(sessions):
        typer.echo(line)


@group_app.command("token")
def token(home_dir: HomeOpt = None) -> None:
    """Où est le jeton des hooks (jamais affiché ; le widget se couple autrement)."""
    from omniscient.group.credentials import hook_token_path, read_hook_token

    path = hook_token_path(_home(home_dir))
    if read_hook_token(_home(home_dir)) is None:
        typer.echo(f"Jeton des hooks absent ({path}) : lance `omniscient group serve`.")
        return
    mode = path.stat().st_mode & 0o777
    typer.echo(f"Jeton des hooks : {path} (droits {mode:o}).")
    typer.echo(
        "Il ne sert qu'aux hooks des agents. Pour le widget : `omniscient group pair`."
    )


@group_app.command("pair")
def pair(
    show_code: Annotated[
        bool,
        typer.Option("--show-code", help="Afficher le code ici (repli plus faible)."),
    ] = False,
    revoke: Annotated[
        bool, typer.Option("--revoke", help="Oublier tous les widgets couplés.")
    ] = False,
    port: PortOpt = DEFAULT_PORT,
    home_dir: HomeOpt = None,
) -> None:
    """Coupler le widget Windows (code à 6 chiffres), ou révoquer les widgets."""
    from omniscient.group.credentials import WidgetTokens, db_path, read_hook_token

    home = _home(home_dir)
    if revoke:
        tokens = WidgetTokens(db_path(home))
        try:
            count = tokens.revoke_all()
        finally:
            tokens.close()
        typer.echo(
            f"{count} widget(s) découplé(s) : recouple-les depuis leurs réglages."
        )
        return
    if not show_code:
        typer.echo(PAIR_HELP)
        return
    hook_token = read_hook_token(home)
    if hook_token is None:
        raise _fail("jeton des hooks absent : lance `omniscient group serve`.")
    try:
        reply = _call(
            f"http://127.0.0.1:{port}/pair/start-terminal", hook_token, body={}
        )
    except urllib.error.HTTPError as exc:
        raise _fail(_http_error(exc)) from exc
    except (OSError, ValueError) as exc:
        raise _fail(f"le démon ne répond pas ({exc}).") from exc
    typer.echo(f"Code de couplage : {reply.get('code')} (valable 2 minutes).")
    typer.echo("Tape-le dans les réglages du widget. Ne le donne à personne.")


def _http_error(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read().decode("utf-8")).get("error") or exc)
    except (OSError, ValueError):
        return str(exc)


PAIR_HELP = """Couplage du widget Windows :
1. Dans les réglages du widget : « Demander un code ».
2. Un code à 6 chiffres s'affiche sur le bureau Windows (jamais dans WSL).
3. Tape-le dans le widget (2 minutes, 5 essais).
Si Windows ne peut pas l'afficher : relance le démon avec --allow-terminal-pairing,
puis, dans un terminal que TU ouvres, `omniscient group pair --show-code`.
`omniscient group pair --revoke` découple tous les widgets."""


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
    protect: Annotated[
        bool,
        typer.Option(
            "--protect", help="Règles deny Claude autour du dossier d'état (conseillé)."
        ),
    ] = False,
    home_dir: HomeOpt = None,
) -> None:
    """Ajouter les hooks omnigroup (les tiens sont conservés, sauvegarde avant écriture)."""
    _hooks(
        "install",
        target,
        write,
        path,
        statusline=with_statusline,
        force=force,
        protect=protect,
        state_dir=_home(home_dir).state,
    )


@hooks_app.command("uninstall")
def hooks_uninstall(
    target: TargetOpt = "all", write: WriteOpt = False, path: PathOpt = None
) -> None:
    """Retirer uniquement les hooks omnigroup (et nos règles --protect)."""
    _hooks("uninstall", target, write, path, state_dir=find_home().state)


@hooks_app.command("status")
def hooks_status(target: TargetOpt = "all", path: PathOpt = None) -> None:
    """Quels événements portent le hook omnigroup ?"""
    _hooks("status", target, False, path, state_dir=find_home().state)


# ---- widget --------------------------------------------------------------------------------

WIDGET_HELP = f"""Widget Windows « Omnigroup » (coin de l'écran, toujours au-dessus) :

1. Télécharge l'installateur (.exe NSIS) :
   - versions publiées : {REPO_URL}/releases (tags widget-v*) ;
   - ou l'artefact « omnigroup-windows-nsis » du dernier run du workflow « Widget » :
     {REPO_URL}/actions/workflows/widget.yml
2. Dans WSL, lance le démon : omniscient group serve
   (ou au démarrage : omniscient schedule install --write, puis
   systemctl --user enable --now omnigroup.service)
3. Ouvre Omnigroup sous Windows, puis ses réglages : « Demander un code ». Le
   code s'affiche sur le bureau Windows ; tape-le dans le widget
   (voir `omniscient group pair`).
4. Branche les agents : omniscient group hooks install --target all --protect
   (aperçu), puis la même commande avec --write.

Le démon n'écoute que 127.0.0.1:{DEFAULT_PORT} ; le widget y accède par le
localhost de WSL (forwarding ou réseau en miroir)."""


@group_app.command("widget")
def widget() -> None:
    """Où trouver l'installateur Windows du widget et comment l'appairer."""
    typer.echo(WIDGET_HELP)


def register_group(app: typer.Typer) -> None:
    app.add_typer(group_app, name="group")
