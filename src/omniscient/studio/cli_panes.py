"""Commands that run inside atelier panes: the live UI, the war room and agent chat."""

from __future__ import annotations

from typing import Annotated

import typer

from omniscient.studio import paths
from omniscient.studio.cli import _fail, idea_app
from omniscient.studio.ideas import StudioError


@idea_app.command("ui")
def ui(stem: str) -> None:
    """Volet UI : état de la construction et prototype en direct (localhost)."""
    from omniscient.studio.uipane import run_ui

    try:
        run_ui(paths.VAULT, paths.REPO, stem)
    except StudioError as exc:
        _fail(str(exc))


def warroom(
    idea: Annotated[str | None, typer.Option(help="Limiter à une idée (stem).")] = None,
    follow: Annotated[bool, typer.Option(help="Rafraîchir en continu.")] = False,
) -> None:
    """Salle de crise : qui a fait quoi, sur quel sujet, et les relais entre agents."""
    from omniscient.studio.warroom import run_warroom

    try:
        run_warroom(paths.VAULT, paths.REPO, idea, follow)
    except StudioError as exc:
        _fail(str(exc))


def chat(
    agent_id: str,
    idea: Annotated[str | None, typer.Option(help="Idée à discuter (stem).")] = None,
) -> None:
    """Discuter avec un agent Omniscient (AgentRun, crédit API plafonné par message)."""
    from omniscient.studio.chat import run_chat

    try:
        run_chat(paths.VAULT, paths.REPO, agent_id, idea)
    except StudioError as exc:
        _fail(str(exc))
