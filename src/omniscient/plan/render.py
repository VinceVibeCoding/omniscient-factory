"""Markdown rendering for wayfinder maps, tickets and the game plan (local-markdown tracker)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

from omniscient import i18n
from omniscient.brain import slugify
from omniscient.router import Route

GOALS = (
    "1. Transformer l'actualité IA et tech en un flux constant d'idées créatives et réalisables.\n"
    "2. Prouver vite les meilleures idées par des prototypes (phase 2).\n"
    "3. Réinjecter les réussites dans les projets existants, avec l'accord du propriétaire."
)


def _bullets(items: tuple[str, ...] | list[str], empty: str = "_none_") -> str:
    return "\n".join(f"- {i}" for i in items) or empty


def render_map(idea_path: Path, idea_title: str, plan: Any, now: datetime) -> str:
    meta = {"type": "map", "idea": f"[[{idea_path.stem}]]", "created": now.isoformat()}
    body = (
        f"# {idea_title}\n\nIdée source : [[{idea_path.stem}]]\n\n"
        f"## Destination\n{plan.destination}\n\n"
        f"## Notes\n{plan.notes or '_none_'}\n\n"
        "## Decisions so far\n\n<!-- one line per resolved ticket: gist + [[link]] -->\n\n"
        f"## Not yet specified\n{_bullets(plan.not_yet_specified)}\n\n"
        f"## Out of scope\n{_bullets(plan.out_of_scope)}"
    )
    return frontmatter.dumps(frontmatter.Post(body, **meta)) + "\n"


def ticket_filename(index: int, title: str) -> str:
    return f"{index:02d}-{slugify(title)}.md"


def render_ticket(index: int, ticket: Any, route: Route) -> str:
    lines = [f"# {ticket.title}", "", f"Type: {ticket.type}", "Status: open"]
    if ticket.blocked_by:
        lines.append("Blocked by: " + ", ".join(f"{b:02d}" for b in ticket.blocked_by))
    hitl = " (HITL: the owner takes part)" if route.hitl else ""
    lines += [
        f"Effort: {ticket.effort}",
        f"Intelligence: {ticket.intelligence}",
        f"Route: {route.role} · {route.model} · {route.effort}{hitl}",
        "",
        "## Question",
        ticket.question,
        "",
    ]
    return "\n".join(lines)


def _ideas(vault: Path) -> list[tuple[Path, dict[str, Any]]]:
    notes = [(p, frontmatter.load(p).metadata) for p in (vault / "ideas").glob("*.md")]
    return sorted(
        notes, key=lambda n: (-float(n[1].get("score", 0)), str(n[1].get("title")))
    )


def _row(path: Path, meta: dict[str, Any], with_map: bool = False) -> str:
    projects = ", ".join(f"[[{p}]]" for p in meta.get("projects", [])) or i18n.NEW_BUILD
    link = (
        f" · [[maps/{slugify(str(meta.get('title')))}/map\\|map]]" if with_map else ""
    )
    title = str(meta.get("title", path.stem)).replace("|", "-")
    score = float(meta.get("score", 0))
    return f"| {score:.2f} | [[{path.stem}\\|{title}]]{link} | {projects} |"


def render_gameplan(
    vault: Path, wip_limit: int, threshold: float, budget_usd: float, now: datetime
) -> str:
    ideas = _ideas(vault)
    table = "| score | idea | projects |\n|---|---|---|\n"

    def section(status: str, with_map: bool = False) -> str:
        rows = [_row(p, m, with_map) for p, m in ideas if m.get("status") == status]
        return table + "\n".join(rows) if rows else "_Aucune._"

    meta = {"type": "gameplan", "updated": now.isoformat(), "owner": "strategist"}
    body = (
        "# Plan de match\n\n_Régénéré par `omniscient plan` à partir du vault. Modifie les idées, "
        "pas ce fichier._\n\n"
        f"## Objectifs\n{GOALS}\n\n"
        f"## Limites\n- Limite de travail en cours (planifiées + en construction) : {wip_limit}\n"
        f"- Seuil de promotion : {threshold:.2f}\n- Budget par cycle : {budget_usd:.2f} $\n\n"
        f"## Carnet (débattues, classées)\n{section('debated')}\n\n"
        f"## Planifiées\n{section('planned', with_map=True)}\n\n"
        f"## En construction\n{section('building', with_map=True)}\n\n"
        f"## Livrées\n{section('shipped')}\n\n"
        f"## Abandonnées\n{section('dropped')}"
    )
    return frontmatter.dumps(frontmatter.Post(body, **meta)) + "\n"
