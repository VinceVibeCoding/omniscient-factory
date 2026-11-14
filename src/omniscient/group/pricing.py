"""USD cost of one Claude Code assistant turn, for the OSD's per-turn ticker.

An estimate at Anthropic first-party API list prices (per million tokens), whatever the
owner's plan: it shows what each turn would cost pay-as-you-go. Rates come from the
claude-api skill's model table (cached 2026-11-03); review them when models change.
Cache writes cost 1.25x input (5-minute TTL) or 2x input (1-hour TTL); cache reads use
the published per-model rate when there is one, else 0.1x input. An unknown model costs
None (the turn is still listed). Inspired by bozdemir/claude-usage-widget (MIT).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MTOK = 1_000_000
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0
CACHE_READ_DEFAULT = 0.1


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float | None = None  # None → CACHE_READ_DEFAULT x input

    @property
    def read(self) -> float:
        return (
            self.cache_read
            if self.cache_read is not None
            else self.input * CACHE_READ_DEFAULT
        )


# Model id prefix → rates. Longest prefix wins ("claude-opus-5-5" before "claude-opus-5").
RATES: dict[str, Rates] = {
    "claude-fable-5-2": Rates(8.0, 40.0, 0.20),
    "claude-mythos-5-2": Rates(8.0, 40.0, 0.20),
    "claude-fable-5-1": Rates(10.0, 50.0),
    "claude-mythos-5-1": Rates(10.0, 50.0),
    "claude-fable-5": Rates(10.0, 50.0),
    "claude-opus-5-5": Rates(4.0, 20.0, 0.20),
    "claude-opus-5": Rates(5.0, 25.0),
    "claude-opus-4-8": Rates(5.0, 25.0),
    "claude-opus-4-7": Rates(5.0, 25.0),
    "claude-opus-4-6": Rates(5.0, 25.0),
    "claude-sonnet-5-5": Rates(2.0, 10.0, 0.10),
    "claude-sonnet-5": Rates(2.0, 10.0),
    "claude-sonnet-4-6": Rates(3.0, 15.0),
    "claude-haiku-5-5": Rates(0.10, 0.50),  # prompts up to 100k tokens
    "claude-haiku-4-5": Rates(1.0, 5.0),
}
_BY_LENGTH = sorted(RATES, key=len, reverse=True)


def rates_for(model: str | None) -> Rates | None:
    """Rates for a model id ("claude-opus-5-5", "claude-opus-5-5[1m]", dated ids…)."""
    if not isinstance(model, str):
        return None
    name = model.strip().lower()
    for prefix in _BY_LENGTH:
        if name == prefix or name.startswith((prefix + "-", prefix + "[")):
            return RATES[prefix]
    return None


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return 0
    return int(value)


def turn_cost(model: str | None, usage: Mapping[str, Any]) -> float | None:
    """Cost of a transcript `message.usage` block, or None for an unknown model."""
    rates = rates_for(model)
    if rates is None:
        return None
    write_total = _count(usage, "cache_creation_input_tokens")
    split = usage.get("cache_creation")
    write_1h = (
        _count(split, "ephemeral_1h_input_tokens") if isinstance(split, Mapping) else 0
    )
    write_1h = min(write_1h, write_total)
    write_5m = write_total - write_1h
    dollars = (
        _count(usage, "input_tokens") * rates.input
        + _count(usage, "output_tokens") * rates.output
        + _count(usage, "cache_read_input_tokens") * rates.read
        + write_5m * rates.input * CACHE_WRITE_5M
        + write_1h * rates.input * CACHE_WRITE_1H
    )
    return round(dollars / MTOK, 6)
