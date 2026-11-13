"""Plan loop: promote the best debated ideas and chart a wayfinder map for each.

The map follows the local-markdown tracker in docs/agents/issue-tracker.md, so phase 2's
`/wayfinder` continues exactly where the factory leaves off. Each ticket is routed
(role, model, effort, HITL) by the deterministic router.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import frontmatter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from omniscient.brain import set_idea_status, slugify
from omniscient.plan.render import (
    render_gameplan,
    render_map,
    render_ticket,
    ticket_filename,
)
from omniscient.router import Routing, route
from omniscient.runs import (
    DEFAULT_WAIT_SECONDS,
    AgentRunClient,
    AnswerNotJson,
    extract_json,
    run_once,
)

STRATEGIST_ID = "strategist"
STRATEGIST_CAP_USD = 0.75
ACTIVE_STATUSES = ("planned", "building")

__all__ = [
    "STRATEGIST_ID",
    "PlanFailed",
    "PlanResult",
    "render_gameplan",
    "run_plan",
    "select_promotions",
]


class PlanFailed(Exception):
    """The Plan loop could not run (budget, server)."""


class _Ticket(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    title: str = Field(min_length=3, max_length=100)
    question: str = Field(min_length=5)
    type: Literal["research", "prototype", "grilling", "task"]
    effort: Literal["S", "M", "L"]
    intelligence: Literal["routine", "skilled", "deep"]
    blocked_by: tuple[int, ...] = ()


class _Map(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    destination: str = Field(min_length=10)
    notes: str = ""
    not_yet_specified: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    tickets: tuple[_Ticket, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _blockers_exist(self) -> _Map:
        count = len(self.tickets)
        for index, ticket in enumerate(self.tickets, start=1):
            bad = [b for b in ticket.blocked_by if not 1 <= b <= count or b == index]
            if bad:
                raise ValueError(f"ticket {index} blocked_by unknown tickets {bad}")
        return self


@dataclass(frozen=True)
class PlanResult:
    promoted: tuple[Path, ...]
    maps: tuple[Path, ...]
    failed: tuple[str, ...]
    cost_usd: float


def select_promotions(
    vault: Path, threshold: float, wip_limit: int
) -> tuple[Path, ...]:
    """Debated ideas the owner pursued, then those at or above the threshold, best first,
    up to the free WIP slots."""
    ideas = [(p, frontmatter.load(p).metadata) for p in (vault / "ideas").glob("*.md")]
    active = sum(1 for _, m in ideas if m.get("status") in ACTIVE_STATUSES)

    def pursued(m: dict[str, object]) -> bool:
        return m.get("owner_verdict") == "pursue"

    candidates = sorted(
        (
            (p, m)
            for p, m in ideas
            if m.get("status") == "debated"
            and (pursued(m) or float(m.get("score", 0)) >= threshold)
        ),
        key=lambda n: (
            not pursued(n[1]),
            -float(n[1].get("score", 0)),
            str(n[1].get("title")),
        ),
    )
    return tuple(p for p, _ in candidates[: max(0, wip_limit - active)])


def _strategist_prompt(idea_note: str) -> str:
    return (
        f"{idea_note}\n\nChart the wayfinder map for this idea. Answer with ONE raw JSON "
        'object: {"destination", "notes", "not_yet_specified": [..], "out_of_scope": [..], '
        '"tickets": [{"title", "question", "type": "research"|"prototype"|"grilling"|"task", '
        '"effort": "S"|"M"|"L", "intelligence": "routine"|"skilled"|"deep", '
        '"blocked_by": [1-based ticket numbers]}]}'
    )


def _write_map(
    vault: Path, idea_path: Path, plan: _Map, routing: Routing, now: datetime
) -> Path:
    title = str(frontmatter.load(idea_path).get("title"))
    map_dir = vault / "maps" / slugify(title)
    issues = map_dir / "issues"
    issues.mkdir(parents=True, exist_ok=True)
    for index, ticket in enumerate(plan.tickets, start=1):
        ticket_route = route(routing, ticket.type, ticket.intelligence)
        (issues / ticket_filename(index, ticket.title)).write_text(
            render_ticket(index, ticket, ticket_route), encoding="utf-8"
        )
    map_path = map_dir / "map.md"
    map_path.write_text(render_map(idea_path, title, plan, now), encoding="utf-8")
    return map_path


def run_plan(
    client: AgentRunClient,
    vault: Path,
    routing: Routing,
    threshold: float,
    wip_limit: int,
    budget_usd: float,
    now: datetime,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
) -> PlanResult:
    """Promote, chart maps, then regenerate vault/gameplan.md."""
    promoted: list[Path] = []
    maps: list[Path] = []
    failed: list[str] = []
    cost = 0.0
    for idea_path in select_promotions(vault, threshold, wip_limit):
        if cost + STRATEGIST_CAP_USD > budget_usd:
            raise PlanFailed(
                f"budget: ${cost:.4f} spent, strategist cap ${STRATEGIST_CAP_USD} "
                f"would exceed ${budget_usd}"
            )
        _, answer, run_cost = run_once(
            client,
            STRATEGIST_ID,
            _strategist_prompt(idea_path.read_text(encoding="utf-8")),
            STRATEGIST_CAP_USD,
            wait_seconds,
        )
        cost += run_cost
        try:
            plan = _Map.model_validate(extract_json(answer))
        except (AnswerNotJson, ValidationError) as exc:
            failed.append(f"{idea_path.stem}: {exc}")
            continue
        maps.append(_write_map(vault, idea_path, plan, routing, now))
        set_idea_status(idea_path, "planned")
        promoted.append(idea_path)
    (vault / "gameplan.md").write_text(
        render_gameplan(vault, wip_limit, threshold, budget_usd, now), encoding="utf-8"
    )
    return PlanResult(tuple(promoted), tuple(maps), tuple(failed), cost)
