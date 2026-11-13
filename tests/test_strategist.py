import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from omniscient.brain import Idea, IdeaScores, write_idea
from omniscient.home import packaged
from omniscient.plan.strategist import (
    STRATEGIST_ID,
    PlanFailed,
    render_gameplan,
    run_plan,
    select_promotions,
)
from omniscient.router import load_routing

NOW = datetime(2026, 11, 12, 22, 0, tzinfo=UTC)
ROUTING = load_routing(packaged("templates") / "routing.toml")


def _idea(vault: Path, title: str, score: float, status: str = "debated") -> Path:
    idea = Idea(
        title=title,
        pitch="A pitch that is long enough.",
        status=status,  # type: ignore[arg-type]
        scores=IdeaScores(
            novelty=0.5, impact=0.5, feasibility=0.5, fit=0.5, effort=0.5
        ),
        sources=(),
        projects=("run-tracker",),
        created=NOW,
    )
    return write_idea(vault, idea, score=score, council="**Verdict:** pursue")


MAP = {
    "destination": "A decision on whether weekly coaching emails help.",
    "notes": "Use /prototype for the email draft.",
    "not_yet_specified": ["How to measure helpfulness"],
    "out_of_scope": ["Mobile app"],
    "tickets": [
        {
            "title": "Which FIT fields matter",
            "question": "Which fields drive advice?",
            "type": "research",
            "effort": "S",
            "intelligence": "skilled",
            "blocked_by": [],
        },
        {
            "title": "Draft coaching email",
            "question": "What should the email look like?",
            "type": "prototype",
            "effort": "M",
            "intelligence": "skilled",
            "blocked_by": [1],
        },
    ],
}


class _Runs:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.started: list[dict[str, Any]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append({"agent_id": agent_id, **body})
        return {"run_id": f"r{len(self.started)}"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": 0.2},
            "result": {"output": None, "final_answer": self.answers.pop(0)},
        }


class _Client:
    def __init__(self, answers: list[str]) -> None:
        self.runs = _Runs(answers)


def test_select_promotions_ranks_by_score_and_respects_threshold_and_wip(
    tmp_path: Path,
) -> None:
    low = _idea(tmp_path, "Low", 0.40)
    top = _idea(tmp_path, "Top", 0.58)
    mid = _idea(tmp_path, "Mid", 0.56)
    _idea(tmp_path, "Already planned", 0.9, status="planned")

    chosen = select_promotions(tmp_path, threshold=0.55, wip_limit=2)

    assert chosen == (top,)  # one slot left: WIP 2 minus 1 already planned
    assert low not in chosen and mid not in chosen


def test_run_plan_charts_a_wayfinder_map_with_routed_tickets(tmp_path: Path) -> None:
    idea = _idea(tmp_path, "Run coach", 0.6)
    client = _Client([json.dumps(MAP)])

    result = run_plan(
        client, tmp_path, ROUTING, threshold=0.55, wip_limit=2, budget_usd=2.0, now=NOW
    )

    assert frontmatter.load(idea)["status"] == "planned"
    map_dir = tmp_path / "maps" / "run-coach"
    assert result.maps == (map_dir / "map.md",)
    map_text = (map_dir / "map.md").read_text()
    assert (
        "## Destination\nA decision on whether weekly coaching emails help." in map_text
    )
    assert "How to measure helpfulness" in map_text
    assert "[[" in map_text  # links back to the idea note
    first = (map_dir / "issues" / "01-which-fit-fields-matter.md").read_text()
    assert "Type: research" in first and "Status: open" in first
    assert "Route: researcher · anthropic/claude-sonnet-5-5 · medium" in first
    second = (map_dir / "issues" / "02-draft-coaching-email.md").read_text()
    assert "Blocked by: 01" in second
    assert "HITL" in second
    assert client.runs.started[0]["agent_id"] == STRATEGIST_ID


def test_run_plan_rejects_tickets_blocked_by_unknown_index(tmp_path: Path) -> None:
    _idea(tmp_path, "Run coach", 0.6)
    bad = {**MAP, "tickets": [{**MAP["tickets"][0], "blocked_by": [7]}]}
    client = _Client([json.dumps(bad)])

    result = run_plan(
        client, tmp_path, ROUTING, threshold=0.55, wip_limit=2, budget_usd=2.0, now=NOW
    )

    assert result.maps == ()
    assert "blocked_by" in " ".join(result.failed)


def test_run_plan_stops_when_budget_would_be_exceeded(tmp_path: Path) -> None:
    _idea(tmp_path, "A", 0.6)
    _idea(tmp_path, "B", 0.6)

    with pytest.raises(PlanFailed, match="budget"):
        run_plan(
            _Client([json.dumps(MAP)]),
            tmp_path,
            ROUTING,
            threshold=0.55,
            wip_limit=5,
            budget_usd=0.5,
            now=NOW,
        )


def test_run_plan_with_nothing_to_promote_still_renders_gameplan(
    tmp_path: Path,
) -> None:
    _idea(tmp_path, "Weak", 0.3)

    result = run_plan(
        _Client([]),
        tmp_path,
        ROUTING,
        threshold=0.55,
        wip_limit=2,
        budget_usd=2.0,
        now=NOW,
    )

    assert result.maps == ()
    assert "Weak" in (tmp_path / "gameplan.md").read_text()


def test_render_gameplan_sections_ideas_by_status(tmp_path: Path) -> None:
    _idea(tmp_path, "Backlog one", 0.5)
    _idea(tmp_path, "Planned one", 0.6, status="planned")
    _idea(tmp_path, "Dropped one", 0.2, status="dropped")

    text = render_gameplan(
        tmp_path, wip_limit=2, threshold=0.55, budget_usd=5.0, now=NOW
    )

    backlog = text.split("## Carnet")[1].split("##")[0]
    assert "Backlog one" in backlog and "0.50" in backlog
    assert "Planned one" in text.split("## Planifiées")[1].split("##")[0]
    assert "Dropped one" in text.split("## Abandonnées")[1]


def test_select_promotions_owner_pursue_bypasses_threshold_but_not_wip(
    tmp_path: Path,
) -> None:
    pursued = _idea(tmp_path, "Owner wants this", 0.30)
    post = frontmatter.load(pursued)
    post["owner_verdict"] = "pursue"
    pursued.write_text(frontmatter.dumps(post))
    _idea(tmp_path, "High scorer", 0.60)

    chosen = select_promotions(tmp_path, threshold=0.55, wip_limit=1)

    assert chosen == (pursued,)  # owner choices go first, within the WIP limit
