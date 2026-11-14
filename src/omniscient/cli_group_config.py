"""`omniscient group config get|set`: the widget's display settings, from a terminal.

Writes `~/.config/omnigroup/config.json` directly (no daemon needed); a running daemon
notices the change within a few seconds and the widget follows live.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from omniscient.group.config_store import (
    DEFAULTS,
    ConfigError,
    ConfigPaths,
    effective,
    update,
)

config_app = typer.Typer(
    help="Réglages d'affichage du widget (thème, vue OSD, ticker).",
    no_args_is_help=True,
)

KeyArg = Annotated[str, typer.Argument(help=f"Réglage : {', '.join(DEFAULTS)}.")]


def _describe(view: dict, key: str) -> str:
    source = view["sources"][key]
    suffix = {"settings": " (fixé dans settings.json)", "default": " (défaut)"}.get(
        source, ""
    )
    value = view["config"][key]
    shown = json.dumps(value) if isinstance(value, bool) else value
    return f"{key} = {shown}{suffix}"


@config_app.command("get")
def config_get(
    key: Annotated[
        str | None, typer.Argument(help="Un seul réglage (défaut : tous).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Sortie JSON.")] = False,
) -> None:
    """Affiche les réglages effectifs et leur origine."""
    paths = ConfigPaths.default()
    view = effective(paths)
    if key is not None and key not in DEFAULTS:
        typer.echo(
            f"Erreur : réglage inconnu : {key} (connus : {', '.join(DEFAULTS)})",
            err=True,
        )
        raise typer.Exit(code=1)
    if as_json:
        typer.echo(json.dumps(view if key is None else view["config"][key]))
        return
    for name in [key] if key else list(DEFAULTS):
        typer.echo(_describe(view, name))
    if key is None:
        typer.echo(f"Fichier : {paths.config}")


@config_app.command("set")
def config_set(
    key: KeyArg,
    value: Annotated[
        str,
        typer.Argument(
            help="theme : default|terminal|dashboard ; osd_view : bars|gauge ; show_ticker : true|false."
        ),
    ],
) -> None:
    """Change un réglage ; le widget suit en quelques secondes."""
    paths = ConfigPaths.default()
    try:
        view = update(paths, {key: value})
    except ConfigError as exc:
        typer.echo(f"Erreur : {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(_describe(view, key))


def register(group_app: typer.Typer) -> None:
    """Adds `config` under `omniscient group`."""
    group_app.add_typer(config_app, name="config")
