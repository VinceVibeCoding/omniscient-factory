from typing import Any

import pytest

from omniscient.runs import (
    CapReached,
    RunFailed,
    final_answer,
    resume_with_cap,
    wait_durably,
)


class _Unreachable(Exception):
    kind = "unreachable"


class _Refused(Exception):
    kind = "refused"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Runs:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def wait(self, run_id: str, timeout: float) -> Any:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes: list[Any]) -> None:
        self.runs = _Runs(outcomes)


DONE = {"run_id": "r", "status": "completed", "result": {"final_answer": "ok"}}


def test_wait_durably_rides_out_unreachable_server() -> None:
    clock = _Clock()
    client = _Client([_Unreachable(), _Unreachable(), DONE])

    run = wait_durably(
        client, "r", 120, retry_pause_seconds=10, clock=clock, sleep=clock.sleep
    )

    assert run is DONE
    assert client.runs.calls == 3


def test_wait_durably_gives_up_at_deadline() -> None:
    clock = _Clock()
    client = _Client([_Unreachable()] * 10)

    with pytest.raises(_Unreachable):
        wait_durably(
            client, "r", 25, retry_pause_seconds=10, clock=clock, sleep=clock.sleep
        )


def test_wait_durably_does_not_retry_refusals() -> None:
    clock = _Clock()
    client = _Client([_Refused(), DONE])

    with pytest.raises(_Refused):
        wait_durably(client, "r", 120, clock=clock, sleep=clock.sleep)

    assert client.runs.calls == 1


def test_final_answer_prefers_structured_output() -> None:
    run = {"status": "completed", "result": {"output": {"a": 1}, "final_answer": "x"}}

    assert final_answer(run) == '{"a": 1}'


def test_final_answer_raises_on_failed_run() -> None:
    with pytest.raises(RunFailed, match="cap"):
        final_answer(
            {
                "run_id": "r",
                "status": "failed",
                "error": {"code": "cap", "message": "m"},
            }
        )


class _StartRuns:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        return {"run_id": "r"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return {
            "run_id": "r",
            "status": "completed",
            "cost": {"usd": 0.01},
            "result": {"final_answer": "ok"},
        }


class _StartClient:
    def __init__(self) -> None:
        self.runs = _StartRuns()


def test_run_once_sends_effort_only_when_given() -> None:
    from omniscient.runs import run_once

    client = _StartClient()

    run_once(client, "a", "c", 0.5)
    run_once(client, "a", "c", 0.5, effort="high")

    assert "effort" not in client.runs.bodies[0]
    assert client.runs.bodies[1]["effort"] == "high"


def test_run_once_rejects_unknown_effort() -> None:
    from omniscient.runs import run_once

    with pytest.raises(ValueError, match="effort"):
        run_once(_StartClient(), "a", "c", 0.5, effort="xhigh")


def test_run_once_sends_file_ids_only_when_given() -> None:
    from omniscient.runs import run_once

    client = _StartClient()

    run_once(client, "a", "c", 0.5)
    run_once(client, "a", "c", 0.5, file_ids=())
    run_once(client, "a", "c", 0.5, file_ids=("f1", "f2"))

    assert "file_ids" not in client.runs.bodies[0]
    assert "file_ids" not in client.runs.bodies[1]
    assert client.runs.bodies[2]["file_ids"] == ["f1", "f2"]


@pytest.mark.parametrize("file_ids", [("f1", "f1"), ("",), ("f1", " ")])
def test_run_once_rejects_invalid_file_ids(file_ids: tuple[str, ...]) -> None:
    from omniscient.runs import run_once

    client = _StartClient()

    with pytest.raises(ValueError, match="file_ids"):
        run_once(client, "a", "c", 0.5, file_ids=file_ids)

    assert client.runs.bodies == []


PAUSED = {
    "run_id": "r",
    "status": "input_required",
    "pending": {
        "kind": "budget",
        "cap_usd": 0.75,
        "spent_usd": 0.395,
        "needs_usd": 0.386,
        "least_cap_usd": 0.79,
        "suggested_cap_usd": 5,
    },
}


def test_final_answer_raises_cap_reached_on_a_budget_pause() -> None:
    # Act
    with pytest.raises(CapReached) as caught:
        final_answer(PAUSED)

    # Assert
    assert isinstance(caught.value, RunFailed)
    assert caught.value.run_id == "r"
    assert caught.value.spent_usd == pytest.approx(0.395)
    assert caught.value.least_cap_usd == pytest.approx(0.79)
    assert "r" in str(caught.value) and "0.79" in str(caught.value)


@pytest.mark.parametrize(
    "pending",
    [
        {"kind": "budget", "spent_usd": "x", "least_cap_usd": None},
        {"kind": "budget"},
        {"kind": "budget", "least_cap_usd": True},
    ],
)
def test_cap_reached_tolerates_missing_or_bad_numbers(
    pending: dict[str, Any],
) -> None:
    # Act
    with pytest.raises(CapReached) as caught:
        final_answer({**PAUSED, "pending": pending})

    # Assert
    assert caught.value.spent_usd is None
    assert caught.value.least_cap_usd is None


@pytest.mark.parametrize("pending", [{"kind": "question"}, None, "budget"])
def test_final_answer_other_input_required_is_a_plain_failure(pending: Any) -> None:
    # Act
    with pytest.raises(RunFailed) as caught:
        final_answer({**PAUSED, "pending": pending})

    # Assert
    assert not isinstance(caught.value, CapReached)
    assert "input_required" in str(caught.value)


class _ResumeRuns:
    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, Any]] = []

    def raise_cap(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("raise_cap", (run_id, body)))
        return {"run_id": run_id}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        self.calls.append(("wait", run_id))
        return self.outcome


class _ResumeClient:
    def __init__(self, outcome: dict[str, Any]) -> None:
        self.runs = _ResumeRuns(outcome)


def test_resume_with_cap_raises_the_cap_then_waits_for_the_answer() -> None:
    # Arrange
    client = _ResumeClient({**DONE, "cost": {"usd": 0.7}})

    # Act
    answer, cost = resume_with_cap(client, "r", 0.79, 60)

    # Assert
    assert (answer, cost) == ("ok", pytest.approx(0.7))
    assert client.runs.calls == [
        ("raise_cap", ("r", {"max_cost_usd": 0.79})),
        ("wait", "r"),
    ]


def test_resume_with_cap_raises_cap_reached_when_it_pauses_again() -> None:
    # Act / Assert
    with pytest.raises(CapReached):
        resume_with_cap(_ResumeClient(PAUSED), "r", 0.79, 60)


@pytest.mark.parametrize("new_cap", [0, -1.0])
def test_resume_with_cap_rejects_a_non_positive_cap(new_cap: float) -> None:
    # Arrange
    client = _ResumeClient(DONE)

    # Act / Assert
    with pytest.raises(ValueError, match="cap"):
        resume_with_cap(client, "r", new_cap, 60)
    assert client.runs.calls == []
