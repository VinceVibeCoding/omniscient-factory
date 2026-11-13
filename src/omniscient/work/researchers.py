"""Researcher routing: which agent (and spend cap) answers a ticket's model tier."""

from __future__ import annotations

from dataclasses import dataclass

HAIKU_CAP_USD = 0.10
SONNET_CAP_USD = 0.50
OPUS_CAP_USD = 0.75


@dataclass(frozen=True)
class Researcher:
    agent_id: str
    cap_usd: float


RESEARCHERS: dict[str, Researcher] = {
    "anthropic/claude-haiku-5-5": Researcher("researcher-haiku", HAIKU_CAP_USD),
    "anthropic/claude-sonnet-5-5": Researcher("researcher-sonnet", SONNET_CAP_USD),
    "anthropic/claude-opus-5-5": Researcher("researcher-opus", OPUS_CAP_USD),
}


def researcher_for(model: str) -> Researcher | None:
    return RESEARCHERS.get(model)
