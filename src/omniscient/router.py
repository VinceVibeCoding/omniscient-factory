"""Deterministic router: a wayfinder ticket (kind + intelligence) -> role, model, effort.

Reads config/routing.toml. Fable is never chosen automatically (opt-in per task only).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROVIDER = "anthropic"
INTELLIGENCE_EFFORT = {"routine": "low", "skilled": "medium", "deep": "high"}
HITL_KINDS = frozenset({"prototype", "grilling"})


class RoutingError(Exception):
    """The routing config or a routing request is invalid."""


@dataclass(frozen=True)
class Route:
    role: str
    model: str  # AgentRun form: provider/model-id
    effort: str
    hitl: bool  # the owner must take part (wayfinder HITL ticket)


@dataclass(frozen=True)
class Routing:
    roles: dict[str, dict[str, str]]
    escalation: dict[str, str]


def load_routing(path: Path) -> Routing:
    try:
        raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RoutingError(f"{path.name}: {exc}") from exc
    escalation = raw.get("escalation")
    if not isinstance(escalation, dict) or set(escalation) != set(INTELLIGENCE_EFFORT):
        raise RoutingError(
            f"{path.name}: [escalation] needs {sorted(INTELLIGENCE_EFFORT)}"
        )
    return Routing(roles=dict(raw.get("roles", {})), escalation=dict(escalation))


def _agentrun_model(model_id: str) -> str:
    return model_id if "/" in model_id else f"{PROVIDER}/{model_id}"


def route(routing: Routing, kind: str, intelligence: str) -> Route:
    if intelligence not in INTELLIGENCE_EFFORT:
        raise RoutingError(f"unknown intelligence {intelligence!r}")
    hitl = kind in HITL_KINDS
    if kind in ("research", "task"):
        role = "researcher" if kind == "research" else "operator"
        model = routing.escalation[intelligence]
        effort = INTELLIGENCE_EFFORT[intelligence]
    elif kind == "prototype":
        role = "builder"
        builder = routing.roles.get("builder", {})
        deep = intelligence == "deep"
        model = routing.escalation["deep"] if deep else builder.get("model", "")
        effort = "high" if deep else builder.get("effort", "medium")
    elif kind == "grilling":
        role = "strategist"
        strategist = routing.roles.get("strategist", {})
        model = strategist.get("model", routing.escalation["deep"])
        effort = strategist.get("effort", "high")
    else:
        raise RoutingError(f"unknown ticket kind {kind!r}")
    if not model:
        raise RoutingError(f"no model configured for {kind}/{intelligence}")
    return Route(role=role, model=_agentrun_model(model), effort=effort, hitl=hitl)
