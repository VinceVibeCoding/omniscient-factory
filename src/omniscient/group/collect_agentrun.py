"""AgentRun collector: live and recently finished runs become omnigroup sessions.

Polls GET /v2/runs (next_cursor) every 5 s: queued/working/paused/input_required runs and
runs ended within the hour. A run's `pending` becomes a Pending — question (answered by
POST /runs/{id}/messages), budget (raise = PATCH /runs/{id} max_cost_usd, stop = POST
/runs/{id}/stop, only on the owner's choice) or approval_external (only the owner's console
session may answer: a bearer token gets 403). Steps come from GET /runs/{id}/timeline?after=
(one cursor per run); usage is the run's cost (AgentRun reports no token counts). The API
key comes from omniscient.agentrun_access and is never logged or shown.
"""

from __future__ import annotations

import os
import string
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from omniscient.agentrun_access import TOKEN_FILE, resolve_api_key
from omniscient.group.collect_common import (
    CollectorStore,
    apply_usage,
    cancel_open,
    open_pendings,
    run_loop,
    save_session,
)
from omniscient.group.models import (
    JumpTarget,
    ModelError,
    Pending,
    Session,
    SessionState,
    Step,
    StepKind,
    Usage,
    parse_iso,
    session_id,
    utcnow,
)
from omniscient.home import Home

POLL_INTERVAL_S = 5.0
RECENT = timedelta(hours=1)
MAX_PAGES = 10
HTTP_TIMEOUT_S = 10.0
DEFAULT_BASE_URL = "http://127.0.0.1:7340/v2"
LIVE = ("queued", "working", "paused", "input_required")
ENDED: dict[str, SessionState] = {"completed": "done", "failed": "error", "cancelled": "done"}  # fmt: skip
STEP_KIND: dict[str, StepKind] = {
    "user_message": "prompt", "tool_use": "tool", "tool_result": "tool_result",
    "text_block": "message", "final_answer": "message", "error": "error",
    "tool_approval_requested": "permission", "waiting": "question", "phase": "notice",
    "decision": "notice", "subagent_started": "notice", "subagent_completed": "notice",
}  # fmt: skip
RUN_ID_CHARS = frozenset(string.ascii_letters + string.digits + "_-")


class AgentRunHttpError(Exception):
    """AgentRun refused or could not be reached (message is safe to show; no token)."""


class Api(Protocol):
    def __call__(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any: ...


def default_api(env_file: Path, environ: Mapping[str, str] | None = None) -> Api:
    """JSON over HTTP to AgentRun /v2 with the owner's key (resolved on each call)."""
    env = os.environ if environ is None else environ
    base = env.get("AGENTRUN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def call(method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        key = resolve_api_key(env_file, TOKEN_FILE, env)
        if key is None:
            raise AgentRunHttpError("pas de clé AgentRun")
        headers = {"Authorization": f"Bearer {key}"}
        try:
            resp = httpx.request(
                method, base + path, json=body, headers=headers, timeout=HTTP_TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            raise AgentRunHttpError(
                f"AgentRun injoignable ({type(exc).__name__})"
            ) from exc
        if resp.status_code >= 400:
            raise AgentRunHttpError(
                f"AgentRun a répondu {resp.status_code} à {method} {path}"
            )
        return resp.json() if resp.content else {}

    return call


def run_path(run_id: str, suffix: str = "") -> str:
    if not run_id or not set(run_id) <= RUN_ID_CHARS:
        raise AgentRunHttpError("identifiant de run invalide")
    return f"/runs/{quote(run_id)}{suffix}"


def list_runs(api: Api, max_pages: int = MAX_PAGES) -> list[Mapping[str, Any]]:
    """Every run on the first `max_pages` pages of GET /runs (no limit param: default page)."""
    runs: list[Mapping[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page = api("GET", "/runs" + (f"?cursor={quote(cursor)}" if cursor else ""))
        items = page.get("runs") if isinstance(page, Mapping) else None
        if not isinstance(items, list):
            raise AgentRunHttpError("réponse AgentRun sans liste de runs")
        runs.extend(r for r in items if isinstance(r, Mapping) and r.get("run_id"))
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return runs


def _ended_at(run: Mapping[str, Any]) -> datetime | None:
    try:
        return parse_iso(str(run.get("ended_at"))) if run.get("ended_at") else None
    except ModelError:
        return None


def is_relevant(run: Mapping[str, Any], now: datetime) -> bool:
    if run.get("status") in LIVE:
        return True
    ended = _ended_at(run)
    return run.get("status") in ENDED and ended is not None and now - ended <= RECENT


def run_state(run: Mapping[str, Any]) -> SessionState:
    status = str(run.get("status"))
    if status in ENDED:
        return ENDED[status]
    if run.get("state") == "waiting" or isinstance(run.get("pending"), Mapping):
        return "waiting"
    return "idle" if run.get("state") == "idle" else "working"


def pending_key(raw: Mapping[str, Any]) -> str:
    """Identifies one pending of a run, so a poll does not open it twice."""
    kind = str(raw.get("kind"))
    marker = raw.get("id") or raw.get("asked_at") or raw.get("at") or ""
    return f"{kind}:{marker}"


def to_pending(
    sid: str, raw: Mapping[str, Any], console_url: str | None
) -> Pending | None:
    kind = raw.get("kind")
    if kind == "question":
        return Pending.new(
            sid, "question", payload={"question": str(raw.get("question", ""))}
        )
    if kind == "budget":
        figures = (
            "cap_usd",
            "spent_usd",
            "needs_usd",
            "least_cap_usd",
            "suggested_cap_usd",
        )
        payload = {k: raw[k] for k in figures if isinstance(raw.get(k), int | float)}
        payload["said"] = str(raw.get("said", ""))
        return Pending.new(sid, "budget", payload=payload, options=("raise", "stop"))
    if kind == "approval":
        payload = {
            "args_preview": str(raw.get("args_preview", "")),
            "url": console_url or "",
        }
        return Pending.new(
            sid, "approval_external", tool=str(raw.get("tool", "")), payload=payload
        )
    return None


def to_step(sid: str, row: Mapping[str, Any], now: datetime) -> Step | None:
    kind = STEP_KIND.get(str(row.get("kind")))
    if kind is None:
        return None
    tool = row.get("tool_name")
    summary = row.get("summary") or row.get("text") or (f"Outil {tool}" if tool else "")
    detail = (
        row.get("tool_input_preview")
        or row.get("tool_result_preview")
        or row.get("text")
    )
    try:
        at = parse_iso(str(row.get("created_at") or row.get("timestamp")))
    except ModelError:
        at = now
    return Step(
        session_id=sid,
        at=at,
        kind=kind,
        summary=str(summary or row.get("kind")),
        detail=str(detail) if detail else None,
    )


def _meta(run: Mapping[str, Any]) -> dict[str, str]:
    meta = {"agent_id": str(run.get("agent_id", "")), "status": str(run.get("status"))}
    cost = run.get("cost")
    cap = cost.get("cap_usd") if isinstance(cost, Mapping) else None
    if isinstance(cap, int | float) and not isinstance(cap, bool):
        meta["cap_usd"] = f"{cap:g}"
    return meta


def cost_usage(cost: Any, now: datetime) -> Usage | None:
    """Usage from an AgentRun Cost: only dollars (AgentRun exposes no token counts)."""
    if not isinstance(cost, Mapping):
        return None
    usd = cost.get("usd")
    if usd is None:
        usd = cost.get("so_far_usd")  # known part while the total is unknown
    if isinstance(usd, bool) or not isinstance(usd, int | float) or usd < 0:
        return None
    return Usage(
        model=None,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=None,
        context_limit=None,
        cost_usd=float(usd),
        plan_used_pct=None,
        plan_resets_at=None,
        updated_at=now,
    )


def _console_jump(url: Any) -> JumpTarget | None:
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return JumpTarget(kind="url", value=url, label="Ouvrir la console AgentRun")
    return None


@dataclass
class AgentRunCollector:
    """Polls AgentRun runs and their timelines; answers questions and budget stops."""

    api: Api
    interval_s: float = POLL_INTERVAL_S
    clock: Callable[[], datetime] = utcnow
    name: str = "agentrun"
    _cursors: dict[str, int] = field(default_factory=dict)
    _final: set[str] = field(default_factory=set)
    _pendings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_home(cls, home: Home) -> AgentRunCollector:
        """The collector the daemon starts: the owner's AgentRun, key from the home's .env."""
        return cls(api=default_api(home.env_file))

    def run(self, store: CollectorStore, stop: threading.Event) -> None:
        run_loop(self.name, lambda: self.poll(store), stop, self.interval_s)

    def poll(self, store: CollectorStore) -> None:
        now = self.clock()
        for run in list_runs(self.api):
            if is_relevant(run, now):
                self._track(store, run, now)

    def _track(
        self, store: CollectorStore, run: Mapping[str, Any], now: datetime
    ) -> None:
        run_id = str(run["run_id"])
        sid = session_id("agentrun", run_id)
        known = store.get_session(sid)
        state = run_state(run)
        changed = known is None or known.state != state
        session = Session(
            id=sid,
            source="agentrun",
            native_id=run_id,
            title=str(run.get("title") or run.get("agent_id") or run_id),
            cwd=None,
            state=state,
            started_at=_created_at(run, now),
            updated_at=now if changed else known.updated_at,
            jump=_console_jump(run.get("console_url")),
            meta=_meta(run),
        )
        save_session(store, session)
        apply_usage(store, sid, cost_usage(run.get("cost"), now))
        self._sync_pending(store, sid, run)
        if run_id not in self._final:
            self._read_timeline(store, sid, run_id, now)

    def _sync_pending(
        self, store: CollectorStore, sid: str, run: Mapping[str, Any]
    ) -> None:
        raw = run.get("pending")
        key = pending_key(raw) if isinstance(raw, Mapping) else None
        if self._pendings.get(sid) == key and (
            key is None or open_pendings(store, sid)
        ):
            return
        cancel_open(store, sid)
        self._pendings.pop(sid, None)
        if not isinstance(raw, Mapping) or key is None:
            return
        pending = to_pending(sid, raw, run.get("console_url"))
        if pending is not None:
            store.open_pending(pending)
            self._pendings[sid] = key

    def _read_timeline(
        self, store: CollectorStore, sid: str, run_id: str, now: datetime
    ) -> None:
        after = self._cursors.get(run_id, 0)
        page = self.api("GET", run_path(run_id, f"/timeline?after={after}"))
        if not isinstance(page, Mapping):
            raise AgentRunHttpError("réponse AgentRun sans timeline")
        steps = [
            to_step(sid, r, now)
            for r in page.get("rows") or []
            if isinstance(r, Mapping)
        ]
        kept = [s for s in steps if s is not None]
        for step in kept:
            store.add_step(step)
        apply_usage(store, sid, cost_usage(page.get("cost"), now))
        if isinstance(page.get("next_after"), int):
            self._cursors[run_id] = page["next_after"]
        if page.get("final") is True and not page.get("has_more"):
            self._final.add(run_id)

    def answer(self, pending: Pending, answer: dict[str, Any]) -> tuple[bool, str]:
        """Carry out the owner's answer on the run. Errors come back as a French message."""
        run_id = pending.session_id.removeprefix("agentrun:")
        try:
            if pending.kind == "question":
                return send_message(self.api, run_id, answer_text(answer))
            if pending.kind == "budget":
                return self._budget(pending, run_id, answer.get("choice"))
        except AgentRunHttpError as exc:
            return False, f"AgentRun : {exc}"
        if pending.kind == "approval_external":
            url = pending.payload.get("url") or "la console AgentRun"
            return (
                False,
                f"Seule ta session dans la console peut approuver : ouvre {url}.",
            )
        return False, "Type de demande inconnu pour AgentRun."

    def _budget(self, pending: Pending, run_id: str, choice: Any) -> tuple[bool, str]:
        if choice == "stop":
            self.api("POST", run_path(run_id, "/stop"))
            return True, "Run arrêté."
        if choice != "raise":
            return False, "Choix attendu : augmenter le plafond ou arrêter."
        cap = pending.payload.get("suggested_cap_usd")
        if not isinstance(cap, int | float) or cap <= 0:
            return (
                False,
                "AgentRun ne propose pas de nouveau plafond : ouvre la console.",
            )
        self.api("PATCH", run_path(run_id), {"max_cost_usd": cap})
        return True, f"Plafond relevé à {cap} $."


def _created_at(run: Mapping[str, Any], now: datetime) -> datetime:
    try:
        return parse_iso(str(run.get("created_at")))
    except ModelError:
        return now


def answer_text(answer: Mapping[str, Any]) -> str:
    """The message text of a question answer: {"message"} or {"answers": {q: label}}."""
    answers = answer.get("answers")
    if isinstance(answers, Mapping) and answers:
        return "\n".join(str(v) for v in answers.values())
    return str(answer.get("message") or answer.get("text") or "")


def send_message(api: Api, run_id: str, text: str) -> tuple[bool, str]:
    """POST a follow-up message to a run (it answers a question or reopens a finished run)."""
    content = text.strip()
    if not content:
        return False, "Message vide : rien n'a été envoyé."
    api("POST", run_path(run_id, "/messages"), {"content": content})
    return True, "Message envoyé au run."
