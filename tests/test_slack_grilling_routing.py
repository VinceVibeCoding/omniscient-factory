"""Slack thread replies on a grilling round: only the owner resolves, never the debater."""

import json
from pathlib import Path
from typing import Any

from omniscient.slack.app import on_message
from omniscient.work.grilling import post_rounds
from omniscient.work.tickets import parse_ticket

MAP = "---\ntype: map\n---\n# Coach\n\n## Destination\nD.\n\n## Notes\nN.\n\n## Decisions so far\n\n## Not yet specified\n- f\n"
TICKET = (
    "# Choix du fournisseur\n\nType: grilling\nStatus: open\nEffort: S\nIntelligence: skilled\n"
    "Route: griller · anthropic/claude-opus-5-5 · high (HITL: the owner takes part)\n\n"
    "## Question\nQuel fournisseur ?\n"
)
ROUND = json.dumps(
    {
        "questions": [
            {"title": "Fournisseur", "question": "Lequel ?", "recommended": "Anthropic"}
        ]
    }
)


class _Runs:
    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"run_id": "r"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": 0.1},
            "result": {"final_answer": ROUND},
        }


class _Client:
    runs = _Runs()


class _Slack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.reactions: list[str] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1700000001.0001"}

    def reactions_add(self, **kwargs: Any) -> None:
        self.reactions.append(kwargs["name"])


def _posted_round(tmp_path: Path) -> Path:
    d = tmp_path / "maps" / "coach"
    (d / "issues").mkdir(parents=True)
    (d / "map.md").write_text(MAP)
    ticket = d / "issues" / "01-choix.md"
    ticket.write_text(TICKET)
    post_rounds(_Client(), _Slack(), "C1", tmp_path, budget_usd=5.0)
    return ticket


def _debater_must_not_run() -> Any:
    raise AssertionError("a grilling thread must never reach the debater")


def _event(user: str, channel: str = "C1") -> dict[str, Any]:
    return {
        "channel": channel,
        "ts": "2.2",
        "thread_ts": "1700000001.0001",
        "text": "oui",
        "user": user,
    }


def test_owner_reply_resolves_the_grilling_ticket(tmp_path: Path) -> None:
    ticket = _posted_round(tmp_path)
    slack = _Slack()

    on_message(
        tmp_path,
        "C1",
        _debater_must_not_run,
        lambda r: None,
        _event("U_OWNER"),
        slack,
        owner_id="U_OWNER",
    )

    assert parse_ticket(ticket).status == "resolved"
    assert slack.posts and slack.reactions == []


def test_non_owner_reply_is_ignored_and_never_reaches_the_debater(
    tmp_path: Path,
) -> None:
    ticket = _posted_round(tmp_path)
    slack = _Slack()

    on_message(
        tmp_path,
        "C1",
        _debater_must_not_run,
        lambda r: None,
        _event("U_SOMEONE"),
        slack,
        owner_id="U_OWNER",
    )

    assert parse_ticket(ticket).status == "claimed"
    assert slack.posts == []


def test_missing_owner_id_means_nobody_can_resolve(tmp_path: Path) -> None:
    ticket = _posted_round(tmp_path)

    on_message(
        tmp_path,
        "C1",
        _debater_must_not_run,
        lambda r: None,
        _event("U_OWNER"),
        _Slack(),
        owner_id="",
    )

    assert parse_ticket(ticket).status == "claimed"
