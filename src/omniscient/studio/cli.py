"""`omniscient idea ...` commands: the deterministic door the atelier skills use.

Every write goes through omniscient.studio.ideas, so notes keep one valid shape.
User errors print a French message on stderr and exit 1 (no traceback).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import fields
from pathlib import Path
from typing import Annotated, Any, NoReturn, get_args

import frontmatter
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from omniscient.brain import IdeaScores
from omniscient.brain.models import BrainError, IdeaKind, IdeaStatus
from omniscient.studio import paths, todo
from omniscient.studio.ideas import (
    IdeaCard,
    StudioError,
    append_atelier,
    fork_variant,
    list_ideas,
    load_card,
    new_seed,
    record_debate,
    resolve,
)

FOLLOW_POLL_SECONDS = 1.0
VERDICTS = ("pursue", "reshape", "drop")
SCORE_KEYS = tuple(f.name for f in fields(IdeaScores))
NEW_ORIGINS = ("owner", "remix")
JSON_FIELDS = (
    "stem",
    "title",
    "status",
    "score",
    "kind",
    "origin",
    "parent",
    "variant",
    "poc_url",
)

idea_app = typer.Typer(
    no_args_is_help=True, help="Idées du studio : lire, créer, débattre."
)


def _fail(message: str) -> NoReturn:
    typer.echo(f"Erreur : {message}", err=True)
    raise typer.Exit(code=1)


def _card(stem: str) -> IdeaCard:
    try:
        return load_card(resolve(paths.VAULT, stem))
    except StudioError:
        _fail(f"aucune idée nommée « {stem} ».")


def _card_json(card: IdeaCard) -> dict[str, Any]:
    return {key: getattr(card, key) for key in JSON_FIELDS}


@idea_app.command("list")
def list_cmd(
    status: Annotated[
        list[str] | None,
        typer.Option("--status", help="Filtre par statut (répétable)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Sortie JSON.")] = False,
) -> None:
    """Liste les idées, meilleur score d'abord."""
    allowed = get_args(IdeaStatus)
    bad = [s for s in status or [] if s not in allowed]
    if bad:
        _fail(f"statut inconnu {bad[0]!r} ; valeurs possibles : {', '.join(allowed)}.")
    cards = list_ideas(paths.VAULT, frozenset(status) if status else None)
    if as_json:
        typer.echo(
            json.dumps([_card_json(c) for c in cards], ensure_ascii=False, indent=2)
        )
        return
    table = Table("score", "statut", "titre")
    table.add_column("stem", overflow="fold")
    for card in cards:
        score = f"{card.score:.2f}" if card.score is not None else "—"
        # Text: a title like "Plan [b]x" must print as-is, not as rich markup.
        table.add_row(score, card.status, Text(card.title), Text(card.stem))
    Console().print(table)


def _render(console: Console, path: Path, full: bool) -> None:
    try:
        console.print(todo.render_pane(path, full=full))
    except StudioError as exc:
        _fail(str(exc))


def _follow(console: Console, path: Path, full: bool) -> None:
    """Re-render on every change; a note caught mid-write only prints a warning."""
    last = path.stat().st_mtime
    while True:
        time.sleep(FOLLOW_POLL_SECONDS)
        if not path.is_file():
            _fail(f"la note {path.name} a disparu.")
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # replaced between the two checks: look again next tick
        if mtime == last:
            continue
        last = mtime
        try:
            pane = todo.render_pane(path, full=full)
        except StudioError as exc:
            console.print(
                Text(f"⚠ note momentanément illisible, nouvel essai : {exc}"),
                style="dark_orange",
            )
            continue
        console.clear()
        console.print(pane)


@idea_app.command()
def show(
    stem: str,
    follow: Annotated[
        bool, typer.Option("--follow", help="Ré-affiche la note à chaque changement.")
    ] = False,
    full: Annotated[
        bool, typer.Option("--full", help="Note complète au lieu du résumé.")
    ] = False,
) -> None:
    """Volet « à faire » : titre, tâches, puis la note en bref (ou --full)."""
    path = _card(stem).path
    console = Console()
    _render(console, path, full)
    if not follow:
        return
    try:
        _follow(console, path, full)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None


def _print_todos(stem: str) -> None:
    items = todo.read_todos(_card(stem).path)
    Console().print(todo.checklist(items))


@idea_app.command("todo")
def todo_cmd(stem: str) -> None:
    """Liste les tâches « À faire » d'une idée."""
    try:
        _print_todos(stem)
    except StudioError as exc:
        _fail(str(exc))


@idea_app.command("todo-add")
def todo_add(stem: str, text: str) -> None:
    """Ajoute une tâche à la liste « À faire »."""
    _card(stem)
    try:
        item = todo.add_todo(paths.VAULT, stem, text)
    except StudioError as exc:
        _fail(f"tâche refusée : {exc}")
    typer.echo(f"Tâche n°{item.index} ajoutée : {item.text}")


def _set(stem: str, index: int, done: bool) -> None:
    _card(stem)
    try:
        item = todo.set_done(paths.VAULT, stem, index, done)
    except StudioError as exc:
        _fail(str(exc))
    state = "faite" if done else "à faire"
    typer.echo(f"Tâche n°{item.index} marquée {state} : {item.text}")


@idea_app.command("todo-done")
def todo_done(stem: str, index: int) -> None:
    """Coche la tâche n°N."""
    _set(stem, index, True)


@idea_app.command("todo-undo")
def todo_undo(stem: str, index: int) -> None:
    """Décoche la tâche n°N."""
    _set(stem, index, False)


@idea_app.command("todo-rm")
def todo_rm(stem: str, index: int) -> None:
    """Supprime la tâche n°N (les suivantes sont renumérotées)."""
    _card(stem)
    try:
        todo.remove_todo(paths.VAULT, stem, index)
    except StudioError as exc:
        _fail(str(exc))
    typer.echo(f"Tâche n°{index} supprimée.")


def _source_sections(card: IdeaCard) -> list[str]:
    from omniscient.brain.notes import find_news

    out = []
    for source in card.sources:
        news = find_news(paths.VAULT, source)
        if news is not None:
            out.append(
                f"## Source {source}\n\n{frontmatter.load(news).content.strip()}"
            )
    return out


def _project_sections(card: IdeaCard) -> list[str]:
    out = []
    for project in card.projects:
        pointer = (paths.REPOS / f"{project}.md").resolve()
        if pointer.parent == paths.REPOS.resolve() and pointer.is_file():
            out.append(
                f"## Projet {project}\n\n{pointer.read_text(encoding='utf-8').strip()}"
            )
    return out


@idea_app.command()
def context(stem: str) -> None:
    """Contexte Markdown pour amorcer un agent : la note, ses sources, ses projets."""
    card = _card(stem)
    parts = [
        f"# Idée {card.stem}\n\n{card.path.read_text(encoding='utf-8').strip()}",
        *_source_sections(card),
        *_project_sections(card),
    ]
    typer.echo("\n\n---\n\n".join(parts))


@idea_app.command()
def new(
    title: Annotated[str, typer.Option("--title", help="Titre de l'idée.")],
    pitch: Annotated[
        str, typer.Option("--pitch", help="Pitch en une ou deux phrases.")
    ],
    project: Annotated[
        list[str] | None, typer.Option("--project", help="Projet lié (répétable).")
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Id de news source (répétable)."),
    ] = None,
    kind: Annotated[
        str, typer.Option("--kind", help="showcase | new-build | upgrade")
    ] = "new-build",
    origin: Annotated[str, typer.Option("--origin", help="owner | remix")] = "owner",
    parent: Annotated[
        list[str] | None, typer.Option("--parent", help="Idée remixée (répétable).")
    ] = None,
) -> None:
    """Crée une graine (idée non débattue) ; affiche son stem."""
    if kind not in get_args(IdeaKind):
        _fail(
            f"type inconnu {kind!r} ; valeurs possibles : {', '.join(get_args(IdeaKind))}."
        )
    if origin not in NEW_ORIGINS:
        _fail(
            f"origine inconnue {origin!r} ; valeurs possibles : {', '.join(NEW_ORIGINS)}."
        )
    for stem in parent or []:
        _card(stem)
    try:
        path = new_seed(
            paths.VAULT,
            title=title,
            pitch=pitch,
            projects=tuple(project or ()),
            sources=tuple(source or ()),
            kind=kind,
            origin=origin,
            parents=tuple(parent or ()),
        )
    except StudioError as exc:
        _fail(f"idée refusée : {exc}")
    typer.echo(path.stem)


@idea_app.command()
def variant(
    stem: str,
    pitch: Annotated[str, typer.Option("--pitch", help="Pitch de la variante.")],
    title: Annotated[
        str | None, typer.Option("--title", help="Titre (par défaut : vN · titre).")
    ] = None,
) -> None:
    """Crée une variante d'une idée ; affiche son stem."""
    _card(stem)
    try:
        path = fork_variant(paths.VAULT, stem, pitch=pitch, title=title)
    except StudioError as exc:
        _fail(f"variante refusée : {exc}")
    typer.echo(path.stem)


@idea_app.command()
def append(stem: str) -> None:
    """Ajoute le texte lu sur l'entrée standard à la section Atelier."""
    _card(stem)
    text = sys.stdin.read()
    if not text.strip():
        _fail("rien à ajouter : l'entrée standard est vide.")
    try:
        append_atelier(paths.VAULT, stem, text)
    except StudioError as exc:
        _fail(f"ajout refusé : {exc}")
    typer.echo(f"Atelier mis à jour : {stem}")


def _parse_debate(raw: str) -> tuple[str, IdeaScores]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(f"JSON invalide sur l'entrée standard ({exc.msg}, ligne {exc.lineno}).")
    if not isinstance(payload, dict):
        _fail('le JSON doit être un objet {"council": ..., "scores": {...}}.')
    council = payload.get("council")
    scores = payload.get("scores")
    if not isinstance(council, str) or not council.strip():
        _fail("le champ « council » (texte Markdown) est manquant ou vide.")
    if not isinstance(scores, dict):
        _fail("le champ « scores » (objet) est manquant.")
    missing = [k for k in SCORE_KEYS if k not in scores]
    extra = [k for k in scores if k not in SCORE_KEYS]
    if missing or extra:
        _fail(
            f"scores attendus exactement : {', '.join(SCORE_KEYS)} (manquants : {missing}, en trop : {extra})."
        )
    values = [scores[k] for k in SCORE_KEYS]
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in values):
        _fail("chaque score doit être un nombre entre 0 et 1.")
    try:
        return council, IdeaScores(*(float(v) for v in values))
    except BrainError as exc:
        _fail(f"score invalide : {exc}")


@idea_app.command()
def debate(stem: str) -> None:
    """Enregistre le débat du conseil (JSON sur l'entrée standard) ; affiche le score."""
    from omniscient.config import load_settings

    _card(stem)
    council, scores = _parse_debate(sys.stdin.read())
    weights = load_settings(paths.CONFIG).scoring_weights
    try:
        path = record_debate(
            paths.VAULT, stem, council=council, scores=scores, weights=weights
        )
    except StudioError as exc:
        _fail(f"débat refusé : {exc}")
    score = load_card(path).score
    typer.echo(f"{score:.4f}" if score is not None else "—")


@idea_app.command()
def verdict(
    stem: str, choice: Annotated[str, typer.Argument(help="pursue | reshape | drop")]
) -> None:
    """Enregistre le verdict du propriétaire."""
    from omniscient.slack.core import apply_owner_verdict

    if choice not in VERDICTS:
        _fail(
            f"verdict inconnu {choice!r} ; valeurs possibles : {', '.join(VERDICTS)}."
        )
    _card(stem)
    try:
        apply_owner_verdict(paths.VAULT, stem, choice)
    except ValueError as exc:
        _fail(f"verdict refusé : {exc}")
    typer.echo(f"Verdict « {choice} » enregistré pour {stem}")


def studio() -> None:
    """Lance le studio (TUI) : le tableau des idées, à ouvrir dans herdr."""
    from omniscient.studio.app import run_studio

    run_studio(paths.VAULT, paths.REPO)
