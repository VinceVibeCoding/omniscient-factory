"""Cost metering for AgentRun runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omniscient.runs import RunsApi


@dataclass(frozen=True)
class MeteredRuns:
    """Delegates to the client's runs API, recording each waited run's cost.

    `run_once` raises on a failed run before returning its cost; this keeps the
    actual cost of a failed run known. A run's reported cost is its running total,
    so the latest wait replaces the earlier one (a paused, then resumed run is not
    counted twice). After `raise_cap` the run is live again and its cost unknown
    until the next wait; `raised` keeps each run's new cap for budget accounting.
    """

    runs: RunsApi
    costs: dict[str, float] = field(default_factory=dict)
    raised: dict[str, float] = field(default_factory=dict)

    def start(self, agent_id: str, body: dict[str, Any]) -> Any:
        return self.runs.start(agent_id, body)

    def wait(self, run_id: str, timeout: float) -> Any:
        run = self.runs.wait(run_id, timeout=timeout)
        usd = (run.get("cost") or {}).get("usd")
        if usd is not None:
            self.costs[run_id] = float(usd)
        return run

    def raise_cap(self, run_id: str, body: dict[str, Any]) -> Any:
        result = self.runs.raise_cap(run_id, body)
        self.raised[run_id] = float(body["max_cost_usd"])
        self.costs.pop(run_id, None)
        return result

    def total_cost(self) -> float | None:
        """The known cost of every run so far; None when no cost is known."""
        return sum(self.costs.values()) if self.costs else None

    def cap_in_force(self, cap_usd: float) -> float:
        """The highest cap any run was given: the tier cap or a raised one."""
        return max((cap_usd, *self.raised.values()))


@dataclass(frozen=True)
class MeteredClient:
    runs: MeteredRuns
