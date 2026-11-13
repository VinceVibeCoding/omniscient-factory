"""Onboarding commands: `omniscient init`, `project add|list`, `theme …`, `schedule …`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import typer

from omniscient.home import find_home

project_app = typer.Typer(
    help="Projets que connaît Omniscient (registre).", no_args_is_help=True
)


def _ask(question: str, default: str) -> str:
    return str(typer.prompt(question, default=default, show_default=bool(default)))


def init(
    home_dir: Annotated[
        str | None,
        typer.Option("--home", help="Dossier Omniscient (défaut : ~/.omniscient)."),
    ] = None,
) -> None:
    """Créer ou mettre à jour ton dossier Omniscient : profil, thème, projets, clés."""
    from omniscient.home import Home
    from omniscient.onboarding import OnboardingError, run_init

    home = Home(Path(home_dir).expanduser().resolve()) if home_dir else find_home()
    try:
        run_init(home, _ask, typer.echo)
    except OnboardingError as exc:
        typer.echo(f"Erreur : {exc}", err=True)
        raise typer.Exit(code=1) from exc


@project_app.command("add")
def project_add(
    source: Annotated[
        str,
        typer.Argument(
            help=(
                "Dossier local, URL GitHub, owner/nom, ou utilisateur GitHub. Un NOM "
                "seul est un dossier local s'il contient .git ou un manifeste, sinon "
                "un utilisateur GitHub (./NOM force le dossier)."
            )
        ),
    ],
) -> None:
    """Ajouter un projet (local ou GitHub, en lecture seule) au registre."""
    from omniscient.onboarding import OnboardingError, add_project
    from omniscient.onboarding.secrets import read_env

    home = find_home()
    token = os.environ.get("GITHUB_TOKEN") or read_env(home.env_file).get(
        "GITHUB_TOKEN"
    )
    try:
        written = add_project(
            home, source, ask=_ask, say=typer.echo, token=token or None
        )
    except OnboardingError as exc:
        typer.echo(f"Erreur : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for path in written:
        typer.echo(f"  + {path.name}")


@project_app.command("list")
def project_list() -> None:
    """Lister les projets du registre."""
    from omniscient.onboarding import list_projects

    rows = list_projects(find_home())
    if not rows:
        typer.echo(
            "Aucun projet. Ajoute-en avec `omniscient project add <dossier|github>`."
        )
    for name, purpose in rows:
        typer.echo(f"{name:<28} {purpose}")


theme_app = typer.Typer(
    help="Thèmes du terminal (studio, volets, Windows Terminal, herdr).",
    no_args_is_help=True,
)


def _theme_call(fn_name: str, *args: object, **kwargs: object) -> None:
    from omniscient import theme

    try:
        getattr(theme, fn_name)(*args, **kwargs)
    except theme.ThemeError as exc:
        typer.echo(f"Erreur : {exc}", err=True)
        raise typer.Exit(code=1) from exc


@theme_app.command("list")
def theme_list() -> None:
    """Lister les thèmes (● = actuel)."""
    _theme_call("theme_list")


@theme_app.command("set")
def theme_set(name: str) -> None:
    """Choisir le thème du studio et des volets."""
    _theme_call("theme_set", name)


@theme_app.command("preview")
def theme_preview(name: Annotated[str | None, typer.Argument()] = None) -> None:
    """Aperçu : couleurs, bande ANSI, salle de crise et à faire."""
    _theme_call("theme_preview", name)


@theme_app.command("export")
def theme_export(
    target: Annotated[str, typer.Argument(help="windows-terminal | herdr")],
    name: str,
    write: Annotated[
        bool, typer.Option(help="Écrire (avec sauvegarde) au lieu d'afficher.")
    ] = False,
    path: Annotated[
        str | None, typer.Option(help="Fichier de réglages à modifier.")
    ] = None,
) -> None:
    """Exporter un thème vers Windows Terminal (WSL) ou herdr."""
    _theme_call(
        "theme_export",
        target,
        name,
        write,
        path=Path(path).expanduser() if path else None,
    )


schedule_app = typer.Typer(
    help="Minuteur systemd (utilisateur) du cycle quotidien et du bot Slack.",
    no_args_is_help=True,
)
WriteFlag = Annotated[
    bool,
    typer.Option("--write", help="Écrire les fichiers (sinon : aperçu seulement)."),
]


def _rendered_units() -> tuple[Any, ...]:
    from omniscient.schedule import ScheduleError, omniscient_executable, render_units

    try:
        return render_units(find_home().root, omniscient_executable())
    except ScheduleError as exc:
        typer.echo(f"Erreur : {exc}", err=True)
        raise typer.Exit(code=1) from exc


@schedule_app.command("install")
def schedule_install(write: WriteFlag = False) -> None:
    """Afficher (et avec --write, écrire) les unités systemd ; ne lance jamais systemctl."""
    from omniscient import schedule as sch

    units, target = _rendered_units(), sch.unit_dir()
    for unit in units:
        typer.echo(f"# ---- {target / unit.name}\n{unit.text}")
    if not write:
        typer.echo("Aperçu seulement : relance avec --write pour écrire ces fichiers.")
        return
    try:
        written = sch.write_units(units, target)
    except sch.ScheduleError as exc:
        typer.echo(f"Erreur : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{len(written)} unités écrites dans {target}. Active-les toi-même :")
    typer.echo(f"  {sch.ENABLE_CMD}")
    typer.echo(f"  {sch.SLACK_CMD}   # optionnel : le bot Slack")


@schedule_app.command("uninstall")
def schedule_uninstall(write: WriteFlag = False) -> None:
    """Désactiver puis (avec --write) supprimer les unités ; ne lance jamais systemctl."""
    from omniscient import schedule as sch

    target = sch.unit_dir()
    typer.echo(f"D'abord, désactive-les toi-même :\n  {sch.DISABLE_CMD}")
    if not write:
        typer.echo(
            f"Puis relance avec --write pour supprimer les fichiers de {target}."
        )
        return
    try:
        removed = sch.remove_units(target)
    except sch.ScheduleError as exc:
        typer.echo(f"Erreur : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{len(removed)} unité(s) supprimée(s). Puis : {sch.RELOAD_CMD}")


@schedule_app.command("status")
def schedule_status() -> None:
    """Les unités sont-elles écrites et à jour ? (fichiers seulement)"""
    from omniscient import schedule as sch

    target = sch.unit_dir()
    for name, state in sch.unit_states(_rendered_units(), target):
        typer.echo(f"{name:<28} {state}")
    typer.echo(f"État du minuteur : systemctl --user list-timers {sch.TIMER}")


def register_onboarding(app: typer.Typer) -> None:
    app.command("init")(init)
    app.add_typer(project_app, name="project")
    app.add_typer(theme_app, name="theme")
    app.add_typer(schedule_app, name="schedule")
