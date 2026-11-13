"""Scheduling of AFK tickets: scan the maps, validate limits, run within budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omniscient.work.researchers import Researcher, researcher_for
from omniscient.work.tickets import Ticket, TicketError, all_maps, frontier, load_map


@dataclass(frozen=True)
class Candidate:
    ticket: Ticket
    siblings: tuple[Ticket, ...]


@dataclass(frozen=True)
class Step:
    path: Path
    kind: str  # resolved | failed | skipped
    reason: str = ""
    cost_usd: float = 0.0
    spent_usd: float = 0.0  # what counts against the budget (the cap if cost unknown)


def scan(
    vault: Path,
) -> tuple[tuple[Candidate, ...], tuple[Path, ...], tuple[Step, ...]]:
    """(research candidates, waiting task tickets, malformed maps) across all maps."""
    candidates: list[Candidate] = []
    waiting: list[Path] = []
    broken: list[Step] = []
    for map_dir in all_maps(vault):
        try:
            tickets = load_map(map_dir)
        except TicketError as exc:
            broken.append(Step(map_dir, "failed", str(exc)))
            continue
        for ticket in frontier(tickets):
            if not ticket.is_afk:
                continue
            if ticket.type == "research":
                candidates.append(Candidate(ticket, tickets))
            else:
                waiting.append(ticket.path)
    return tuple(candidates), tuple(waiting), tuple(broken)


def validate(budget_usd: float, max_tickets: int, wait_seconds: float) -> None:
    if budget_usd < 0:
        raise ValueError(f"budget_usd must be >= 0, got {budget_usd}")
    if max_tickets < 1:
        raise ValueError(f"max_tickets must be >= 1, got {max_tickets}")
    if wait_seconds <= 0:
        raise ValueError(f"wait_seconds must be > 0, got {wait_seconds}")


def run_all(
    candidates: tuple[Candidate, ...],
    budget_usd: float,
    max_tickets: int,
    work: Callable[[Candidate, Researcher, float], Step],
) -> tuple[Step, ...]:
    """Run candidates in order; once the budget or max_tickets stops us, skip the rest.

    `work` gets the budget left for this candidate (its own cap included).
    """
    steps: tuple[Step, ...] = ()
    spent = 0.0
    stop_reason = ""
    for candidate in candidates:
        path = candidate.ticket.path
        researcher = researcher_for(candidate.ticket.route.model)
        runs = sum(1 for s in steps if s.kind != "skipped")
        if not stop_reason and runs >= max_tickets:
            stop_reason = f"max_tickets ({max_tickets}) reached"
        if not stop_reason and researcher and spent + researcher.cap_usd > budget_usd:
            stop_reason = (
                f"budget: ${spent:.2f} spent + ${researcher.cap_usd:.2f} cap"
                f" > ${budget_usd:.2f}"
            )
        if stop_reason:
            steps = (*steps, Step(path, "skipped", stop_reason))
        elif researcher is None:
            reason = f"no researcher agent for model {candidate.ticket.route.model}"
            steps = (*steps, Step(path, "skipped", reason))
        else:
            step = work(candidate, researcher, budget_usd - spent)
            spent += step.spent_usd
            steps = (*steps, step)
    return steps
