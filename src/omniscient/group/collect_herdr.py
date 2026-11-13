"""herdr collector: every herdr agent pane becomes an omnigroup session.

Polls `herdr agent list` (herdr 0.9.5, JSON envelope `{"result": {"type": "agent_list",
"agents": [AgentInfo...]}}`) every two seconds. A pane whose agent is `blocked` (waiting at
an approval or question dialog) gets a "permission" pending carrying the dialog text from
`herdr agent read <pane> --source detection`; the owner's answer is sent as key presses
(`herdr agent send-keys`). The key table is per agent kind and NOT verified against every
agent's dialog ("à vérifier", see docs/omnigroup.md).

A herdr pane running Claude Code also reports through the Claude hooks: when herdr names the
Claude session (`agent_session.value`) and that `claude:<uuid>` session exists, it is kept
and only gains a herdr jump target; the pane's own `herdr:<pane>` session is not created.

The module also holds what both collectors share: the store protocol and the poll loop.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from omniscient.group.models import (
    JumpTarget,
    Pending,
    Session,
    SessionState,
    Step,
    Usage,
    session_id,
    utcnow,
)
from omniscient.studio.herdr import HerdrError, default_runner

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
STATE_MAP: dict[str, SessionState] = {
    "blocked": "waiting",
    "working": "working",
    "idle": "idle",
    "done": "done",
    "unknown": "idle",
}
STATE_LABEL = {
    "working": "au travail",
    "waiting": "attend ta réponse",
    "idle": "au repos",
    "done": "terminé",
    "error": "en erreur",
}
# Answer → keys per agent kind (herdr's `agent` field). "à vérifier": Claude Code's dialog
# opens with "Yes" selected (enter) and esc cancels it (= refuse). Other agents fall back
# to the same keys until checked.
SEND_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {"allow": ("enter",), "deny": ("esc",)},
}
DEFAULT_KEYS: dict[str, tuple[str, ...]] = {"allow": ("enter",), "deny": ("esc",)}
PANE_ID = re.compile(r"^[A-Za-z0-9:_.-]{1,64}$")
DIALOG_MAX = 4000

Runner = Callable[[Sequence[str]], str]


class CollectorStore(Protocol):
    """The part of omniscient.group.store.Store the collectors use."""

    def get_session(self, session_id: str) -> Session | None: ...
    def upsert_session(self, session: Session) -> Session: ...
    def add_step(self, step: Step) -> Any: ...
    def open_pending(self, pending: Pending) -> Pending: ...
    def pending_for(self, session_id: str) -> Sequence[Pending]: ...
    def update_usage(self, session_id: str, usage: Usage) -> Any: ...
    def resolve_pending(
        self, pending_id: str, answer: dict[str, Any] | None, status: str
    ) -> Any: ...


# ---- shared by both collectors ---------------------------------------------------


def run_loop(
    name: str, poll: Callable[[], None], stop: threading.Event, interval_s: float
) -> None:
    """Call `poll` every `interval_s` until `stop` is set; a failed poll is logged, not fatal."""
    while not stop.is_set():
        try:
            poll()
        except Exception:
            log.exception("omnigroup collector %s: poll failed", name)
        stop.wait(interval_s)


def save_session(store: CollectorStore, fresh: Session) -> Session:
    """Upsert `fresh`; the store merges it with what it knows (start time, dismissal)."""
    return store.upsert_session(fresh)


def _same_usage(a: Usage | None, b: Usage) -> bool:
    return a is not None and replace(a, updated_at=b.updated_at) == b


def apply_usage(
    store: CollectorStore,
    sid: str,
    usage: Usage | None,
    *,
    ours: Usage | None = None,
    guard: bool = False,
) -> bool:
    """Store `usage` when it says something new; True when written.

    With `guard`, usage someone else wrote (the Claude hooks: richer) is kept: only an empty
    slot or `ours` (what this collector wrote last) is replaced.
    """
    known = store.get_session(sid)
    if usage is None or known is None or _same_usage(known.usage, usage):
        return False
    if guard and known.usage is not None and known.usage != ours:
        return False
    store.update_usage(sid, usage)
    return True


def open_pendings(store: CollectorStore, sid: str) -> list[Pending]:
    return [p for p in store.pending_for(sid) if p.status == "open"]


def cancel_open(store: CollectorStore, sid: str) -> None:
    for pending in open_pendings(store, sid):
        store.resolve_pending(pending.id, None, "cancelled")


# ---- herdr -------------------------------------------------------------------------


def _parse_result(stdout: str) -> Mapping[str, Any]:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise HerdrError(f"réponse herdr illisible : {stdout[:120]!r}") from exc
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise HerdrError(f"réponse herdr inattendue : {stdout[:120]!r}")
    return result


def parse_agents(stdout: str) -> tuple[Mapping[str, Any], ...]:
    """The AgentInfo objects of `herdr agent list`, each with a valid pane_id."""
    agents = _parse_result(stdout).get("agents")
    if not isinstance(agents, list):
        raise HerdrError("réponse herdr sans .result.agents")
    return tuple(
        a
        for a in agents
        if isinstance(a, dict) and PANE_ID.match(str(a.get("pane_id") or ""))
    )


def parse_read(stdout: str) -> str:
    """The text of `herdr agent read`: the JSON envelope's .result.read.text, or raw text."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout
    read = data.get("result", {}).get("read") if isinstance(data, dict) else None
    if isinstance(read, dict) and isinstance(read.get("text"), str):
        return read["text"]
    return stdout


def claude_link(agent: Mapping[str, Any]) -> str | None:
    """The Claude session uuid herdr detected in this pane, if any."""
    ref = agent.get("agent_session")
    if not isinstance(ref, dict) or ref.get("kind") != "id":
        return None
    if str(ref.get("agent") or agent.get("agent") or "") != "claude":
        return None
    value = str(ref.get("value") or "")
    return value if re.fullmatch(r"[A-Za-z0-9-]{1,64}", value) else None


def _count(tokens: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        raw = str(tokens.get(name) or "").strip().replace(",", "").replace("_", "")
        if raw.isdigit():
            return int(raw)
    return None


def herdr_usage(agent: Mapping[str, Any], now: datetime) -> Usage | None:
    """Usage from the pane's display tokens, when an integration reports known names.

    herdr's `tokens` is a free NAME=VALUE map set with `pane report-metadata --token`; herdr
    itself puts no token counts there, so most panes yield None.
    """
    tokens = agent.get("tokens")
    if not isinstance(tokens, Mapping):
        return None
    counts = {
        "input_tokens": _count(tokens, "input_tokens", "input"),
        "output_tokens": _count(tokens, "output_tokens", "output"),
        "context_tokens": _count(tokens, "context_tokens", "context"),
        "context_limit": _count(tokens, "context_limit", "context_window"),
    }
    if all(v is None for v in counts.values()):
        return None
    model = tokens.get("model")
    return Usage(
        model=str(model) if model else None,
        input_tokens=counts["input_tokens"] or 0,
        output_tokens=counts["output_tokens"] or 0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=counts["context_tokens"],
        context_limit=counts["context_limit"],
        cost_usd=None,
        plan_used_pct=None,
        plan_resets_at=None,
        updated_at=now,
    )


def keys_for(agent_kind: str, decision: str) -> tuple[str, ...] | None:
    return SEND_KEYS.get(agent_kind, DEFAULT_KEYS).get(decision)


def _title(agent: Mapping[str, Any]) -> str:
    for key in ("name", "title", "terminal_title_stripped", "display_agent", "agent"):
        value = agent.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"herdr {agent['pane_id']}"


def herdr_jump(pane: str) -> JumpTarget:
    return JumpTarget(kind="herdr", value=pane, label="Ouvrir dans herdr")


@dataclass
class HerdrCollector:
    """Polls herdr; answers blocked panes with key presses. Not thread-safe across polls."""

    runner: Runner = default_runner
    interval_s: float = POLL_INTERVAL_S
    clock: Callable[[], datetime] = utcnow
    name: str = "herdr"
    _states: dict[str, str] = field(default_factory=dict)
    _usage: dict[str, Usage] = field(default_factory=dict)

    def run(self, store: CollectorStore, stop: threading.Event) -> None:
        run_loop(self.name, lambda: self.poll(store), stop, self.interval_s)

    def poll(self, store: CollectorStore) -> None:
        """One pass: refresh every pane's session; panes that vanished end as done."""
        agents = parse_agents(self.runner(["agent", "list"]))
        seen: set[str] = set()
        for agent in agents:
            seen.add(self._track(store, agent))
        for sid in [s for s in self._states if s not in seen]:
            self._vanish(store, sid)

    def _track(self, store: CollectorStore, agent: Mapping[str, Any]) -> str:
        pane = str(agent["pane_id"])
        linked = claude_link(agent)
        if linked and store.get_session(session_id("claude", linked)) is not None:
            claude_sid = session_id("claude", linked)
            self._linked_usage(store, claude_sid, herdr_usage(agent, self.clock()))
            return self._link(store, claude_sid, pane)
        state = STATE_MAP.get(str(agent.get("agent_status")), "idle")
        sid = session_id("herdr", pane)
        session = Session(
            id=sid,
            source="herdr",
            native_id=pane,
            title=_title(agent),
            cwd=agent.get("foreground_cwd") or agent.get("cwd") or None,
            state=state,
            started_at=self.clock(),
            updated_at=self.clock(),
            jump=herdr_jump(pane),
            meta={"agent": str(agent.get("agent") or ""), "pane": pane},
        )
        self._transition(store, session)
        apply_usage(store, sid, herdr_usage(agent, self.clock()))
        return sid

    def _link(self, store: CollectorStore, sid: str, pane: str) -> str:
        """Keep the hooks' Claude session; give it the herdr jump and drop our duplicate."""
        known = store.get_session(sid)
        if known is not None and (known.jump is None or known.jump.kind != "herdr"):
            meta = {**known.meta, "herdr_pane": pane}
            store.upsert_session(replace(known, jump=herdr_jump(pane), meta=meta))
        duplicate = store.get_session(session_id("herdr", pane))
        if duplicate is not None and not duplicate.dismissed:
            cancel_open(store, duplicate.id)
            store.upsert_session(replace(duplicate, dismissed=True))
        self._states.pop(session_id("herdr", pane), None)
        return session_id("herdr", pane)

    def _linked_usage(
        self, store: CollectorStore, sid: str, usage: Usage | None
    ) -> None:
        ours = self._usage.get(sid)
        if apply_usage(store, sid, usage, ours=ours, guard=True) and usage is not None:
            self._usage[sid] = usage

    def _transition(self, store: CollectorStore, session: Session) -> None:
        previous = self._states.get(session.id)
        if previous == session.state:
            save_session(
                store, replace(session, updated_at=self._kept_time(store, session))
            )
            return
        label = f"État : {STATE_LABEL[session.state]}"
        saved = save_session(store, replace(session, last_step=label))
        store.add_step(
            Step(session_id=saved.id, at=self.clock(), kind="notice", summary=label)
        )
        self._states[session.id] = session.state
        if session.state == "waiting":
            self._ask(store, saved)
        elif previous == "waiting":
            cancel_open(store, session.id)

    def _kept_time(self, store: CollectorStore, session: Session) -> datetime:
        known = store.get_session(session.id)
        return known.updated_at if known is not None else session.updated_at

    def _ask(self, store: CollectorStore, session: Session) -> None:
        if open_pendings(store, session.id):
            return
        pane = session.native_id
        text = parse_read(self.runner(["agent", "read", pane, "--source", "detection"]))
        store.open_pending(
            Pending.new(
                session.id,
                "permission",
                payload={
                    "text": text.strip()[-DIALOG_MAX:],
                    "pane": pane,
                    "agent": session.meta.get("agent", ""),
                },
                options=("allow", "deny"),
            )
        )
        store.add_step(
            Step(
                session_id=session.id,
                at=self.clock(),
                kind="permission",
                summary="L'agent attend une autorisation",
                detail=text.strip()[-DIALOG_MAX:] or None,
            )
        )

    def _vanish(self, store: CollectorStore, sid: str) -> None:
        self._states.pop(sid, None)
        known = store.get_session(sid)
        cancel_open(store, sid)
        if known is None or known.state == "done":
            return
        label = "Panneau herdr fermé"
        store.upsert_session(
            replace(known, state="done", updated_at=self.clock(), last_step=label)
        )
        store.add_step(
            Step(session_id=sid, at=self.clock(), kind="stop", summary=label)
        )

    def answer(self, pending: Pending, answer: dict[str, Any]) -> tuple[bool, str]:
        """Send the owner's allow/deny to the pane as key presses, if it is still blocked."""
        decision = answer.get("decision")
        pane = str(pending.payload.get("pane") or "")
        if pending.kind != "permission" or not PANE_ID.match(pane):
            return False, "Cette demande ne vient pas d'un panneau herdr."
        keys = keys_for(str(pending.payload.get("agent") or ""), str(decision))
        if keys is None:
            return (
                False,
                "Réponse non prise en charge pour herdr : autoriser ou refuser.",
            )
        try:
            current = _parse_result(self.runner(["agent", "get", pane])).get("agent")
            if (
                not isinstance(current, dict)
                or current.get("agent_status") != "blocked"
            ):
                return False, "L'agent n'attend plus de réponse : rien n'a été envoyé."
            self.runner(["agent", "send-keys", pane, *keys])
        except HerdrError as exc:
            return False, f"herdr a refusé : {exc}"
        verb = "autorisé" if decision == "allow" else "refusé"
        return True, f"Réponse envoyée à l'agent ({verb})."
