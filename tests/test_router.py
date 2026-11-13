from pathlib import Path

import pytest

from omniscient.home import packaged
from omniscient.router import Route, RoutingError, load_routing, route

ROUTING = load_routing(packaged("templates") / "routing.toml")


@pytest.mark.parametrize(
    ("kind", "intelligence", "expected"),
    [
        (
            "research",
            "routine",
            Route("researcher", "anthropic/claude-haiku-5-5", "low", False),
        ),
        (
            "research",
            "skilled",
            Route("researcher", "anthropic/claude-sonnet-5-5", "medium", False),
        ),
        ("task", "deep", Route("operator", "anthropic/claude-opus-5-5", "high", False)),
        (
            "prototype",
            "skilled",
            Route("builder", "anthropic/claude-sonnet-5-5", "medium", True),
        ),
        (
            "prototype",
            "deep",
            Route("builder", "anthropic/claude-opus-5-5", "high", True),
        ),
        (
            "grilling",
            "routine",
            Route("strategist", "anthropic/claude-opus-5-5", "high", True),
        ),
    ],
)
def test_route_maps_ticket_kind_and_intelligence(
    kind: str, intelligence: str, expected: Route
) -> None:
    assert route(ROUTING, kind, intelligence) == expected


def test_route_never_picks_fable_automatically() -> None:
    models = {
        route(ROUTING, k, i).model
        for k in ("research", "task", "prototype", "grilling")
        for i in ("routine", "skilled", "deep")
    }

    assert not any("fable" in m for m in models)


def test_route_rejects_unknown_kind() -> None:
    with pytest.raises(RoutingError, match="kind"):
        route(ROUTING, "vibes", "routine")


def test_load_routing_rejects_missing_escalation(tmp_path: Path) -> None:
    path = tmp_path / "r.toml"
    path.write_text('[roles.builder]\nmodel = "claude-sonnet-5-5"\neffort = "medium"\n')

    with pytest.raises(RoutingError, match="escalation"):
        load_routing(path)
