"""Resume a research run AgentRun paused at its cost cap, once and within bounds.

Seen live: an Opus researcher with project files attached reached its $0.75 cap.
AgentRun pauses such a run (`input_required`) instead of failing it, so the money
spent is kept if the cap is raised. The engine raises it ONCE, to the least cap
AgentRun asks for, and only when that stays within MAX_RAISE_FACTOR x the tier
cap and the cycle budget left covers the extra. Otherwise, or if the resumed run
fails or pauses again, the ticket is released with the run id in the reason so
the owner can resume that run by hand.
"""

from __future__ import annotations

from omniscient.runs import AgentRunClient, CapReached, RunFailed, resume_with_cap
from omniscient.work.answer import failure_reason

MAX_RAISE_FACTOR = 2.0


def refusal(paused: CapReached, cap_usd: float, room_usd: float) -> str:
    """Why the cap must not be raised ('' when it may). `room_usd` includes the cap."""
    least = paused.least_cap_usd
    if least is None:
        return "least cap unknown"
    if least > MAX_RAISE_FACTOR * cap_usd:
        return f"${least:.2f} is over {MAX_RAISE_FACTOR:g}x the ${cap_usd:.2f} tier cap"
    extra = least - cap_usd
    left = room_usd - cap_usd
    if extra > left:
        return f"cycle budget left ${left:.2f} does not cover the extra ${extra:.2f}"
    return ""


def resume_once(
    client: AgentRunClient,
    paused: CapReached,
    cap_usd: float,
    room_usd: float,
    wait_seconds: float,
) -> str:
    """The resumed run's final answer; RunFailed (naming the run) when not resumed."""
    why = refusal(paused, cap_usd, room_usd)
    if why or paused.least_cap_usd is None:
        raise RunFailed(
            f"{paused}; cap not raised ({why}); resume run {paused.run_id} by hand"
        ) from paused
    try:
        answer, _ = resume_with_cap(
            client, paused.run_id, paused.least_cap_usd, wait_seconds
        )
    except Exception as again:
        raise RunFailed(
            f"run {paused.run_id} failed after its cap was raised to "
            f"${paused.least_cap_usd:.2f}: {failure_reason(again)}"
        ) from again
    return answer
