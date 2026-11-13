from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from omniscient.brain import Idea, IdeaScores, write_idea
from omniscient.slack.app import (
    BUILD_OWNER_ONLY,
    BUILD_STARTED,
    DEBATER_FAILED,
    HELP,
    on_build,
    on_mention,
    on_message,
    on_verdict,
)
from omniscient.slack.core import Reply

NOW = datetime(2026, 11, 12, tzinfo=UTC)


class _Slack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.reactions: list[str] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True}

    def reactions_add(self, **kwargs: Any) -> None:
        self.reactions.append(kwargs["name"])


def _idea(vault: Path, slack_ts: str = "1.1") -> Path:
    idea = Idea(
        "Coach",
        "A pitch long enough.",
        "debated",
        IdeaScores(novelty=0, impact=0, feasibility=0, fit=0, effort=0),
        (),
        (),
        NOW,
    )
    path = write_idea(vault, idea, 0.5)
    post = frontmatter.load(path)
    post["slack_ts"] = slack_ts
    path.write_text(frontmatter.dumps(post))
    return path


def test_on_verdict_records_choice_and_answers_in_thread(tmp_path: Path) -> None:
    path = _idea(tmp_path)
    slack = _Slack()
    acked: list[bool] = []
    body = {"channel": {"id": "C1"}, "message": {"ts": "1.1"}, "user": {"id": "U1"}}

    on_verdict(
        tmp_path,
        lambda: acked.append(True),
        body,
        {"action_id": "idea_drop", "value": path.stem},
        slack,
    )

    assert acked == [True]
    assert frontmatter.load(path)["status"] == "dropped"
    assert (
        slack.posts[0]["thread_ts"] == "1.1" and "abandonné" in slack.posts[0]["text"]
    )


def test_on_verdict_ignores_unknown_idea(tmp_path: Path) -> None:
    slack = _Slack()
    body = {"channel": {"id": "C1"}, "message": {"ts": "1"}, "user": {"id": "U1"}}

    on_verdict(
        tmp_path, lambda: None, body, {"action_id": "idea_drop", "value": "nope"}, slack
    )

    assert slack.posts == []


class _Runs:
    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"run_id": "r"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": 0.03},
            "result": {"final_answer": "Good challenge."},
        }


class _Agentrun:
    runs = _Runs()


def _event(**overrides: Any) -> dict[str, Any]:
    return {
        "channel": "C1",
        "ts": "2.2",
        "thread_ts": "1.1",
        "text": "Why now?",
        **overrides,
    }


def test_on_message_answers_thread_replies_and_reports_cost(tmp_path: Path) -> None:
    _idea(tmp_path)
    slack = _Slack()
    costs: list[Reply] = []

    on_message(tmp_path, "C1", _Agentrun, costs.append, _event(), slack)

    assert slack.reactions == ["eyes"]
    assert slack.posts == [
        {"channel": "C1", "thread_ts": "1.1", "text": "Good challenge."}
    ]
    assert costs[0].cost_usd == 0.03


def test_on_message_ignores_other_channels_bots_and_top_level(tmp_path: Path) -> None:
    _idea(tmp_path)
    slack = _Slack()
    for event in (
        _event(channel="C2"),
        _event(bot_id="B1"),
        _event(subtype="message_changed"),
        _event(thread_ts=None),
        _event(thread_ts="2.2"),
    ):
        on_message(tmp_path, "C1", _Agentrun, lambda r: None, event, slack)

    assert slack.posts == [] and slack.reactions == []


def test_on_message_reports_debater_failure_in_thread(tmp_path: Path) -> None:
    _idea(tmp_path)
    slack = _Slack()

    def broken() -> Any:
        raise RuntimeError("server down")

    on_message(tmp_path, "C1", broken, lambda r: None, _event(), slack)

    assert slack.posts[0]["text"] == DEBATER_FAILED


def test_on_mention_posts_help() -> None:
    slack = _Slack()

    on_mention({"channel": "C1", "ts": "3.3"}, slack)

    assert slack.posts[0]["text"] == HELP


def _build_body(user: str) -> dict[str, Any]:
    return {"channel": {"id": "C1"}, "message": {"ts": "1.1"}, "user": {"id": user}}


def test_on_build_lets_the_owner_start_a_background_build(tmp_path: Path) -> None:
    path = _idea(tmp_path)
    slack = _Slack()
    acked: list[bool] = []
    started: list[tuple[str, str, str]] = []

    on_build(
        tmp_path,
        "UOWNER",
        lambda *args: started.append(args) or True,
        lambda: acked.append(True),
        _build_body("UOWNER"),
        {"action_id": "idea_build", "value": path.stem},
        slack,
    )

    assert acked == [True]
    assert started == [(path.stem, "C1", "1.1")]
    assert slack.posts == [{"channel": "C1", "thread_ts": "1.1", "text": BUILD_STARTED}]
    assert "1.50 $" in BUILD_STARTED


@pytest.mark.parametrize(("owner_id", "user"), [("UOWNER", "USTRANGER"), ("", "")])
def test_on_build_refuses_anyone_but_the_owner(
    tmp_path: Path, owner_id: str, user: str
) -> None:
    path = _idea(tmp_path)
    slack = _Slack()
    started: list[tuple[str, str, str]] = []

    on_build(
        tmp_path,
        owner_id,
        lambda *args: started.append(args),
        lambda: None,
        _build_body(user),
        {"action_id": "idea_build", "value": path.stem},
        slack,
    )

    assert started == []
    assert slack.posts[0]["text"] == BUILD_OWNER_ONLY


def test_on_build_ignores_unknown_idea(tmp_path: Path) -> None:
    slack = _Slack()
    started: list[tuple[str, str, str]] = []

    on_build(
        tmp_path,
        "UOWNER",
        lambda *args: started.append(args),
        lambda: None,
        _build_body("UOWNER"),
        {"action_id": "idea_build", "value": "../secrets"},
        slack,
    )

    assert started == [] and slack.posts == []


def test_on_build_stays_quiet_when_another_build_is_running(tmp_path: Path) -> None:
    path = _idea(tmp_path)
    slack = _Slack()

    on_build(
        tmp_path,
        "UOWNER",
        lambda *args: False,  # busy: start_build already said so in the thread
        lambda: None,
        _build_body("UOWNER"),
        {"action_id": "idea_build", "value": path.stem},
        slack,
    )

    assert slack.posts == []
