"""omnigroup data model: sessions, their steps, pending decisions and jump targets.

Every value is a frozen dataclass validated at construction; datetimes are timezone-aware
and normalised to UTC. Step texts are truncated and secret-redacted here, so every
producer (hooks, collectors) gets the same guarantees. JSON helpers turn the objects into
plain dicts (ISO-8601 UTC datetimes) and back.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from omniscient.redact import redact_secrets

Source = Literal["claude", "copilot", "herdr", "agentrun"]
SessionState = Literal["working", "waiting", "idle", "done", "error"]
StepKind = Literal[
    "prompt",
    "tool",
    "tool_result",
    "message",
    "permission",
    "question",
    "notice",
    "stop",
    "error",
]
PendingKind = Literal["permission", "question", "budget", "approval_external"]
PendingStatus = Literal["open", "answered", "expired", "cancelled"]
JumpKind = Literal["vscode_session", "vscode_folder", "herdr", "url", "terminal"]

SOURCES: tuple[str, ...] = get_args(Source)
SESSION_STATES: tuple[str, ...] = get_args(SessionState)
STEP_KINDS: tuple[str, ...] = get_args(StepKind)
PENDING_KINDS: tuple[str, ...] = get_args(PendingKind)
PENDING_STATUSES: tuple[str, ...] = get_args(PendingStatus)
JUMP_KINDS: tuple[str, ...] = get_args(JumpKind)

SUMMARY_MAX = 300
DETAIL_MAX = 4000
TITLE_MAX = 200
ID_MAX = 200
ELLIPSIS = "…"


class ModelError(ValueError):
    """A value does not fit the omnigroup data model."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime, name: str = "datetime") -> datetime:
    """`value` in UTC; naive datetimes are refused (their zone is unknown)."""
    if not isinstance(value, datetime):
        raise ModelError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ModelError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    """ISO-8601 UTC with microseconds: same width everywhere, so strings sort by time."""
    return as_utc(value).isoformat(timespec="microseconds")


def parse_iso(text: str, name: str = "datetime") -> datetime:
    if not isinstance(text, str):
        raise ModelError(f"{name} must be an ISO-8601 string")
    try:
        return as_utc(datetime.fromisoformat(text), name)
    except ValueError as exc:
        raise ModelError(f"{name}: {exc}") from exc


def clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters (ending with an ellipsis when cut)."""
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS


def clean_text(text: str, limit: int) -> str:
    """Secret-redacted then clipped (redaction first: a cut must not hide a secret)."""
    return clip(redact_secrets(text), limit)


def _check_choice(value: str, choices: tuple[str, ...], name: str) -> None:
    if value not in choices:
        raise ModelError(f"{name} must be one of {', '.join(choices)} (got {value!r})")


def _check_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > ID_MAX:
        raise ModelError(f"{name} must be a non-empty string of at most {ID_MAX} chars")


def _set(obj: object, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


@dataclass(frozen=True)
class JumpTarget:
    kind: JumpKind
    value: str
    label: str

    def __post_init__(self) -> None:
        _check_choice(self.kind, JUMP_KINDS, "jump.kind")
        if not isinstance(self.value, str) or not self.value:
            raise ModelError("jump.value must be a non-empty string")
        if not isinstance(self.label, str):
            raise ModelError("jump.label must be a string")


@dataclass(frozen=True)
class Usage:
    """Token use and cost of one session (context_* = the live context window)."""

    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    context_tokens: int | None
    context_limit: int | None
    cost_usd: float | None
    plan_used_pct: float | None
    plan_resets_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in _USAGE_COUNTS:
            _check_count(getattr(self, name), f"usage.{name}", optional=False)
        for name in ("context_tokens", "context_limit"):
            _check_count(getattr(self, name), f"usage.{name}", optional=True)
        for name in ("cost_usd", "plan_used_pct"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or value < 0
            ):
                raise ModelError(f"usage.{name} must be a non-negative number")
        if self.model is not None and not isinstance(self.model, str):
            raise ModelError("usage.model must be a string or None")
        _set(self, "updated_at", as_utc(self.updated_at, "usage.updated_at"))
        if self.plan_resets_at is not None:
            _set(self, "plan_resets_at", as_utc(self.plan_resets_at, "plan_resets_at"))


_USAGE_COUNTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")  # fmt: skip


def _check_count(value: Any, name: str, *, optional: bool) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelError(f"{name} must be a non-negative integer")


def context_pct(usage: Usage | None) -> float | None:
    """Share of the context window in use (0-100), None when unknown."""
    if usage is None or usage.context_tokens is None or not usage.context_limit:
        return None
    return round(100 * usage.context_tokens / usage.context_limit, 1)


@dataclass(frozen=True)
class Session:
    id: str
    source: Source
    native_id: str
    title: str
    cwd: str | None
    state: SessionState
    started_at: datetime
    updated_at: datetime
    last_step: str = ""
    dismissed: bool = False
    jump: JumpTarget | None = None
    meta: dict[str, str] = field(default_factory=dict)
    usage: Usage | None = None

    def __post_init__(self) -> None:
        _check_choice(self.source, SOURCES, "session.source")
        if self.usage is not None and not isinstance(self.usage, Usage):
            raise ModelError("session.usage must be a Usage or None")
        _check_choice(self.state, SESSION_STATES, "session.state")
        _check_id(self.native_id, "session.native_id")
        if self.id != session_id(self.source, self.native_id):
            raise ModelError("session.id must be '<source>:<native_id>'")
        if self.cwd is not None and not isinstance(self.cwd, str):
            raise ModelError("session.cwd must be a string or None")
        if not all(
            isinstance(k, str) and isinstance(v, str) for k, v in self.meta.items()
        ):
            raise ModelError("session.meta must map strings to strings")
        _set(self, "title", clean_text(str(self.title), TITLE_MAX))
        _set(self, "last_step", clean_text(str(self.last_step), SUMMARY_MAX))
        _set(self, "started_at", as_utc(self.started_at, "session.started_at"))
        _set(self, "updated_at", as_utc(self.updated_at, "session.updated_at"))
        _set(self, "dismissed", bool(self.dismissed))
        _set(self, "meta", dict(self.meta))


def session_id(source: str, native_id: str) -> str:
    return f"{source}:{native_id}"


def merge_sessions(old: Session, new: Session) -> Session:
    """`new` over `old`: empty values keep the old ones, the start stays the earliest,
    meta is merged, and a dismissed session stays dismissed until its state changes."""
    return replace(
        new,
        title=new.title or old.title,
        cwd=new.cwd or old.cwd,
        jump=new.jump or old.jump,
        last_step=new.last_step or old.last_step,
        started_at=min(old.started_at, new.started_at),
        updated_at=max(old.updated_at, new.updated_at),
        meta={**old.meta, **new.meta},
        dismissed=new.dismissed or (old.dismissed and old.state == new.state),
        usage=new.usage or old.usage,
    )


@dataclass(frozen=True)
class Step:
    session_id: str
    at: datetime
    kind: StepKind
    summary: str
    detail: str | None = None

    def __post_init__(self) -> None:
        _check_id(self.session_id, "step.session_id")
        _check_choice(self.kind, STEP_KINDS, "step.kind")
        _set(self, "at", as_utc(self.at, "step.at"))
        _set(self, "summary", clean_text(str(self.summary), SUMMARY_MAX))
        if self.detail is not None:
            _set(self, "detail", clean_text(str(self.detail), DETAIL_MAX))


@dataclass(frozen=True)
class Pending:
    id: str
    session_id: str
    kind: PendingKind
    created_at: datetime
    expires_at: datetime | None = None
    tool: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    options: tuple[str, ...] = ()
    status: PendingStatus = "open"
    answer: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _check_id(self.id, "pending.id")
        _check_id(self.session_id, "pending.session_id")
        _check_choice(self.kind, PENDING_KINDS, "pending.kind")
        _check_choice(self.status, PENDING_STATUSES, "pending.status")
        if not isinstance(self.payload, dict):
            raise ModelError("pending.payload must be an object")
        if self.answer is not None and not isinstance(self.answer, dict):
            raise ModelError("pending.answer must be an object or None")
        options = tuple(self.options)
        if not all(isinstance(o, str) for o in options):
            raise ModelError("pending.options must be strings")
        _set(self, "options", options)
        _set(self, "created_at", as_utc(self.created_at, "pending.created_at"))
        if self.expires_at is not None:
            _set(self, "expires_at", as_utc(self.expires_at, "pending.expires_at"))

    @classmethod
    def new(cls, session_id: str, kind: PendingKind, **fields: Any) -> Pending:
        """A fresh open pending with a uuid4 id, created now."""
        return cls(
            id=str(uuid.uuid4()),
            session_id=session_id,
            kind=kind,
            created_at=fields.pop("created_at", None) or utcnow(),
            **fields,
        )

    @property
    def is_open(self) -> bool:
        return self.status == "open"


# ---- JSON ----------------------------------------------------------------------


def to_json(obj: Session | Step | Pending | JumpTarget | Usage) -> dict[str, Any]:
    """A plain, JSON-serialisable dict (datetimes as ISO-8601 UTC)."""
    if isinstance(obj, Usage):
        return _usage_json(obj)
    if isinstance(obj, JumpTarget):
        return {"kind": obj.kind, "value": obj.value, "label": obj.label}
    if isinstance(obj, Session):
        return _session_json(obj)
    if isinstance(obj, Step):
        return {
            "session_id": obj.session_id,
            "at": iso(obj.at),
            "kind": obj.kind,
            "summary": obj.summary,
            "detail": obj.detail,
        }
    if isinstance(obj, Pending):
        return _pending_json(obj)
    raise TypeError(f"not an omnigroup model: {type(obj).__name__}")


def _session_json(s: Session) -> dict[str, Any]:
    return {
        "id": s.id,
        "source": s.source,
        "native_id": s.native_id,
        "title": s.title,
        "cwd": s.cwd,
        "state": s.state,
        "started_at": iso(s.started_at),
        "updated_at": iso(s.updated_at),
        "last_step": s.last_step,
        "dismissed": s.dismissed,
        "jump": to_json(s.jump) if s.jump else None,
        "meta": dict(s.meta),
        "usage": to_json(s.usage) if s.usage else None,
    }


def _usage_json(u: Usage) -> dict[str, Any]:
    data = {k: v for k, v in u.__dict__.items() if not isinstance(v, datetime)}
    data["plan_resets_at"] = iso(u.plan_resets_at) if u.plan_resets_at else None
    data["updated_at"] = iso(u.updated_at)
    data["context_pct"] = context_pct(u)
    return data


def _pending_json(p: Pending) -> dict[str, Any]:
    return {
        "id": p.id,
        "session_id": p.session_id,
        "kind": p.kind,
        "created_at": iso(p.created_at),
        "expires_at": iso(p.expires_at) if p.expires_at else None,
        "tool": p.tool,
        "payload": p.payload,
        "options": list(p.options),
        "status": p.status,
        "answer": p.answer,
    }


_DATETIMES = frozenset(
    {"started_at", "updated_at", "at", "created_at", "expires_at", "plan_resets_at"}
)


def _decode(cls: type, data: Any, name: str, **nested: Callable[[Any], Any]) -> Any:
    """Build `cls` from its JSON form (unknown keys ignored, datetimes parsed)."""
    if not isinstance(data, dict):
        raise ModelError(f"{name} must be a JSON object")
    kwargs: dict[str, Any] = {}
    for key in {f.name for f in fields(cls)} & data.keys():
        value = data[key]
        if value is not None and key in _DATETIMES:
            value = parse_iso(value, f"{name}.{key}")
        elif value is not None and key in nested:
            value = nested[key](value)
        kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:  # a required field is missing
        raise ModelError(f"{name}: {exc}") from exc


def jump_from_json(data: Any) -> JumpTarget:
    return _decode(JumpTarget, data, "jump")


def usage_from_json(data: Any) -> Usage:
    return _decode(Usage, data, "usage")


def session_from_json(data: Any) -> Session:
    return _decode(Session, data, "session", jump=jump_from_json, usage=usage_from_json)


def step_from_json(data: Any) -> Step:
    return _decode(Step, data, "step")


def pending_from_json(data: Any) -> Pending:
    return _decode(Pending, data, "pending")
