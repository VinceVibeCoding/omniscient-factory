"""Shared plumbing for driving AgentRun agents: one run, its answer, its cost."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

DEFAULT_WAIT_SECONDS = 900
RETRY_PAUSE_SECONDS = 10
RUN_EFFORTS = ("low", "medium", "high")
UNREACHABLE = "unreachable"  # agentrun.AgentRunUnreachable.kind
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class RunFailed(Exception):
    """The AgentRun run itself failed (billing, network, cap, ...)."""


class CapReached(RunFailed):
    """AgentRun paused the run at its cost cap (`input_required`, pending budget).

    The run is not lost: `resume_with_cap` raises the cap and continues it.
    `spent_usd` and `least_cap_usd` are None when AgentRun did not report them.
    """

    def __init__(
        self, run_id: str, spent_usd: float | None, least_cap_usd: float | None
    ) -> None:
        self.run_id = run_id
        self.spent_usd = spent_usd
        self.least_cap_usd = least_cap_usd
        super().__init__(
            f"run {run_id} paused at its cap: spent ${_usd(spent_usd)}, "
            f"needs a cap of at least ${_usd(least_cap_usd)}"
        )


class AnswerNotJson(ValueError):
    """The run's final answer holds no parseable JSON object."""


class RunsApi(Protocol):
    def start(self, agent_id: str, body: dict[str, Any]) -> Any: ...
    def wait(self, run_id: str, timeout: float) -> Any: ...
    def raise_cap(self, run_id: str, body: dict[str, Any]) -> Any: ...


class AgentRunClient(Protocol):
    @property
    def runs(self) -> RunsApi: ...


def _usd(value: float | None) -> str:
    return "?" if value is None else f"{value:.2f}"


def _number(value: Any) -> float | None:
    """A JSON number from AgentRun as a float; None when absent or not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _budget_pause(run: dict[str, Any]) -> CapReached | None:
    """The CapReached for a run paused at its cap, else None."""
    pending = run.get("pending")
    if run.get("status") != "input_required" or not isinstance(pending, dict):
        return None
    if pending.get("kind") != "budget":
        return None
    return CapReached(
        str(run.get("run_id")),
        _number(pending.get("spent_usd")),
        _number(pending.get("least_cap_usd")),
    )


def final_answer(run: dict[str, Any]) -> str:
    """The run's structured output (if the harness enforced one) or its final text answer.

    Raises CapReached for a run paused at its cap, RunFailed for any other outcome.
    """
    paused = _budget_pause(run)
    if paused is not None:
        raise paused
    if run.get("status") != "completed":
        error = run.get("error") or {}
        raise RunFailed(
            f"run {run.get('run_id')} {run.get('status')}: "
            f"{error.get('code')}: {error.get('message')}"
        )
    result = run.get("result") or {}
    output = result.get("output")
    return json.dumps(output) if output else (result.get("final_answer") or "")


def run_once(
    client: AgentRunClient,
    agent_id: str,
    content: str,
    cap_usd: float,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    effort: str | None = None,
    file_ids: tuple[str, ...] = (),
) -> tuple[str, str, float]:
    """Start a run, wait for it, return (run_id, final answer, cost in USD).

    `effort` overrides the agent's effort for this run (AgentRun RunCreate accepts
    low/medium/high; the model itself cannot be overridden per run). `file_ids` are
    ready uploads (POST /files) copied into the run's workspace (RunCreate.file_ids).
    """
    if effort is not None and effort not in RUN_EFFORTS:
        raise ValueError(f"effort must be one of {RUN_EFFORTS}, got {effort!r}")
    if any(not f.strip() for f in file_ids) or len(set(file_ids)) != len(file_ids):
        raise ValueError(f"file_ids must be unique non-empty ids, got {file_ids!r}")
    body: dict[str, Any] = {"input": {"content": content}, "max_cost_usd": cap_usd}
    if effort is not None:
        body["effort"] = effort
    if file_ids:
        body["file_ids"] = list(file_ids)
    started = client.runs.start(agent_id, body)
    run = wait_durably(client, started["run_id"], wait_seconds)
    return started["run_id"], final_answer(run), run_cost(run)


def run_cost(run: dict[str, Any]) -> float:
    return float((run.get("cost") or {}).get("usd") or 0.0)


def resume_with_cap(
    client: AgentRunClient,
    run_id: str,
    new_cap: float,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
) -> tuple[str, float]:
    """Raise a paused run's cap, wait for it again, return (final answer, cost USD).

    Raises CapReached again if the run pauses at the new cap.
    """
    if new_cap <= 0:
        raise ValueError(f"new_cap must be > 0, got {new_cap}")
    client.runs.raise_cap(run_id, {"max_cost_usd": new_cap})
    run = wait_durably(client, run_id, wait_seconds)
    return final_answer(run), run_cost(run)


def wait_durably(
    client: AgentRunClient,
    run_id: str,
    wait_seconds: float,
    retry_pause_seconds: float = RETRY_PAUSE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Wait for a run, riding out temporary 'unreachable' errors until the deadline.

    AgentRun runs are durable (they resume after a server restart), so a stalled or
    restarting server is not a failed run. Seen live: the server hung mid-council.
    """
    deadline = clock() + wait_seconds
    while True:
        remaining = deadline - clock()
        try:
            return client.runs.wait(run_id, timeout=max(remaining, 1.0))
        except Exception as exc:
            if getattr(exc, "kind", None) != UNREACHABLE or clock() >= deadline:
                raise
            sleep(retry_pause_seconds)


def extract_json(answer: str) -> dict[str, Any]:
    """Parse the JSON object in an answer, raw or inside a code fence."""
    match = _JSON_OBJECT.search(answer or "")
    if match is None:
        raise AnswerNotJson("no JSON object in the answer")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise AnswerNotJson(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AnswerNotJson("the JSON answer is not an object")
    return data
