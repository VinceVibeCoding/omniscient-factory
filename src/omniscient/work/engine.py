"""AFK ticket engine: resolve frontier research tickets of every wayfinder map.

Each ticket is claimed before any work, answered by a researcher agent picked from
its route's model tier, then resolved (or released on any failure, never left
claimed). AgentRun's run body has no model override (only `effort`), so each model
tier is its own agent definition: agentrun/researcher-{haiku,sonnet,opus}.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omniscient.runs import (
    DEFAULT_WAIT_SECONDS,
    RUN_EFFORTS,
    AgentRunClient,
    CapReached,
    extract_json,
    run_once,
)
from omniscient.work.answer import REASON_MAX_CHARS, Answer, failure_reason
from omniscient.work.attachments import ProjectContext, attach_for_map
from omniscient.work.cap_raise import resume_once
from omniscient.work.metering import MeteredClient, MeteredRuns
from omniscient.work.prompt import build_prompt
from omniscient.work.researchers import (
    HAIKU_CAP_USD,
    OPUS_CAP_USD,
    RESEARCHERS,
    SONNET_CAP_USD,
    Researcher,
    researcher_for,
)
from omniscient.work.schedule import Candidate, Step, run_all, scan, validate
from omniscient.work.tickets import Ticket, TicketError, claim, release, resolve

__all__ = [
    "HAIKU_CAP_USD",
    "OPUS_CAP_USD",
    "REASON_MAX_CHARS",
    "RESEARCHERS",
    "SONNET_CAP_USD",
    "Researcher",
    "WorkResult",
    "build_prompt",
    "researcher_for",
    "run_afk",
]

_ANSWER_HEADING = re.compile(r"^## Answer\s*$", re.MULTILINE)
DEFAULT_MAX_TICKETS = 5


@dataclass(frozen=True)
class Outcome:
    path: Path
    reason: str


@dataclass(frozen=True)
class WorkResult:
    ran_at: datetime
    resolved: tuple[Path, ...]
    failed: tuple[Outcome, ...]
    skipped: tuple[Outcome, ...]
    waiting: tuple[Path, ...]  # AFK task tickets left for the owner (need local access)
    cost_usd: float


def run_effort(route_effort: str) -> str | None:
    """The route's effort as AgentRun accepts it (low/medium/high), else None."""
    return route_effort if route_effort in RUN_EFFORTS else None


def _answer_written(path: Path) -> bool:
    """True once `resolve()` has appended an `## Answer` (sanitised answers can't fake one)."""
    return bool(_ANSWER_HEADING.search(path.read_text(encoding="utf-8")))


def _give_back(claimed: Ticket) -> str:
    """Release a claimed ticket unless its answer is already written; '' or a note."""
    try:
        if _answer_written(claimed.path):
            return " (answer already written; ticket not released, needs review)"
        release(claimed)
    except Exception as exc:  # noqa: BLE001 -- recorded in the reason, never dropped
        return f" (release failed: {failure_reason(exc)})"
    return ""


def _answer(
    client: AgentRunClient,
    vault: Path,
    candidate: Candidate,
    claimed: Ticket,
    researcher: Researcher,
    wait_seconds: float,
    room_usd: float,
    context: ProjectContext | None = None,
) -> None:
    """Run the researcher on a claimed ticket and resolve it with the answer.

    A run paused at its cap is resumed once if `cap_raise` allows it.
    """
    attachments = (
        attach_for_map(context, vault, claimed.map_path, claimed.question)
        if context is not None
        else ()
    )
    prompt = build_prompt(vault, claimed, candidate.siblings, attachments)
    try:
        _, answer, _ = run_once(
            client,
            researcher.agent_id,
            prompt,
            researcher.cap_usd,
            wait_seconds,
            effort=run_effort(claimed.route.effort),
            file_ids=tuple(a.file_id for a in attachments),
        )
    except CapReached as paused:
        answer = resume_once(client, paused, researcher.cap_usd, room_usd, wait_seconds)
    parsed = Answer.model_validate(extract_json(answer))
    resolve(claimed, parsed.answer, parsed.gist)


def _research(
    client: AgentRunClient,
    vault: Path,
    candidate: Candidate,
    researcher: Researcher,
    wait_seconds: float,
    room_usd: float,
    context: ProjectContext | None = None,
) -> Step:
    """Claim, run, resolve; on failure give the ticket back and record the cost.

    Any Exception becomes a failed step (cost unknown: the cap in force, raised or
    not, counts against the budget). KeyboardInterrupt and SystemExit propagate, after the release.
    """
    ticket = candidate.ticket
    try:
        claimed = claim(ticket)
    except TicketError as exc:
        return Step(ticket.path, "skipped", str(exc))
    metered = MeteredClient(MeteredRuns(client.runs))
    try:
        _answer(
            metered,
            vault,
            candidate,
            claimed,
            researcher,
            wait_seconds,
            room_usd,
            context,
        )
    except Exception as exc:  # noqa: BLE001 -- any error fails this ticket only
        reason = failure_reason(exc) + _give_back(claimed)
        cost = metered.runs.total_cost()
        cap = metered.runs.cap_in_force(researcher.cap_usd)
        return Step(
            ticket.path, "failed", reason, cost or 0.0, cap if cost is None else cost
        )
    except BaseException:
        _give_back(claimed)
        raise
    cost = metered.runs.total_cost() or 0.0
    return Step(ticket.path, "resolved", cost_usd=cost, spent_usd=cost)


def run_afk(
    client: AgentRunClient,
    vault: Path,
    budget_usd: float,
    now: datetime,
    max_tickets: int = DEFAULT_MAX_TICKETS,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    context: ProjectContext | None = None,
) -> WorkResult:
    """Resolve the AFK research tickets on every map's frontier, within budget.

    With a `context`, each run gets the files of the projects its idea touches.
    """
    validate(budget_usd, max_tickets, wait_seconds)
    candidates, waiting, broken = scan(vault)
    steps = (
        *broken,
        *run_all(
            candidates,
            budget_usd,
            max_tickets,
            lambda c, r, room: _research(
                client, vault, c, r, wait_seconds, room, context
            ),
        ),
    )
    return WorkResult(
        ran_at=now,
        resolved=tuple(s.path for s in steps if s.kind == "resolved"),
        failed=tuple(Outcome(s.path, s.reason) for s in steps if s.kind == "failed"),
        skipped=tuple(Outcome(s.path, s.reason) for s in steps if s.kind == "skipped"),
        waiting=waiting,
        cost_usd=round(sum(s.cost_usd for s in steps), 6),
    )
