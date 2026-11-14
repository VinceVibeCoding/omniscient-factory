"""One headless Claude Code turn for the widget chat: `claude -p --output-format
stream-json --verbose [--resume <id>]`, the prompt on stdin, events parsed line by line.

stream-json (code.claude.com/docs/en/headless): `system/init` carries the session id,
`assistant` messages carry text and tool_use blocks (subagent messages have a non-null
`parent_tool_use_id` and are skipped), the last line is a `result` with `is_error`,
`result` and `total_cost_usd` (the conversation's total when resuming). Unknown lines are
ignored: the format belongs to Claude Code.

No shell anywhere: argv lists only. The process runs in its own session so stop/timeout
reach its children: SIGINT first (ends the turn cleanly), SIGTERM after a grace delay.

`--permission-mode default` is always passed, so an ambient `bypassPermissions` or
`acceptEdits` default (user or project settings) never applies to a widget chat: every
tool call that needs approval goes through the PermissionRequest hook to the widget, and
is denied when no widget answers. stdout lines longer than MAX_LINE_CHARS are skipped;
a failure's stderr goes to the daemon log only, the widget gets a generic message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniscient.redact import redact_secrets

log = logging.getLogger(__name__)

TOOL_SUMMARY_MAX = 200
STDERR_KEEP = 2000
MAX_LINE_CHARS = 1024 * 1024  # one stream-json line; longer ones are skipped
RESULT_TEXT_MAX = 300  # Claude's own error text shown to the widget
PERMISSION_MODE = "default"
GRACE_S = 5.0
STDERR_JOIN_S = 2.0
DEFAULT_TIMEOUT_S = 30 * 60
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SUMMARY_KEYS = (
    "command",
    "file_path",
    "path",
    "pattern",
    "url",
    "description",
    "prompt",
)


@dataclass(frozen=True)
class RunnerEvent:
    kind: str  # init | text | tool | result
    text: str | None = None
    tool: str | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    is_error: bool = False


@dataclass(frozen=True)
class TurnSpec:
    argv: Sequence[str]
    cwd: Path
    text: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    env: Mapping[str, str] | None = None


@dataclass(frozen=True)
class TurnOutcome:
    ok: bool
    session_id: str | None = None
    cost_usd: float | None = None
    error: str = ""
    stopped: bool = False


@dataclass
class TurnHandle:
    """Lets another thread stop a running turn (mutable by design: process control)."""

    process: subprocess.Popen[str] | None = None
    stop_requested: threading.Event = field(default_factory=threading.Event)

    def stop(self) -> None:
        self.stop_requested.set()
        _signal(self.process, signal.SIGINT)


def build_argv(
    claude: Sequence[str],
    resume: str | None = None,
    max_budget_usd: float | None = None,
) -> list[str]:
    argv = [*claude, "-p", "--output-format", "stream-json", "--verbose"]
    argv += ["--permission-mode", PERMISSION_MODE]
    if resume is not None:
        if not SESSION_ID.match(resume):
            raise ValueError("identifiant de session Claude invalide")
        argv += ["--resume", resume]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{float(max_budget_usd):g}"]
    return argv


# ---- parsing ---------------------------------------------------------------------------


def _tool_summary(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    value = next((raw[k] for k in _SUMMARY_KEYS if isinstance(raw.get(k), str)), "")
    if not value:
        value = next((v for v in raw.values() if isinstance(v, str)), "")
    text = redact_secrets(" ".join(value.split()), keep_paths=True)
    return text if len(text) <= TOOL_SUMMARY_MAX else text[:TOOL_SUMMARY_MAX] + "…"


def _assistant(data: dict[str, Any]) -> tuple[RunnerEvent, ...]:
    if data.get("parent_tool_use_id") is not None:
        return ()  # a subagent's message
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ()
    events: list[RunnerEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            events.append(RunnerEvent("text", text=block["text"]))
        elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
            summary = _tool_summary(block.get("input"))
            events.append(RunnerEvent("tool", text=summary, tool=block["name"]))
    return tuple(events)


def _cost(raw: Any) -> float | None:
    return (
        float(raw)
        if isinstance(raw, int | float) and not isinstance(raw, bool)
        else None
    )


def parse_line(line: str) -> tuple[RunnerEvent, ...]:
    """The chat events of one stream-json line (none for noise or unknown types)."""
    try:
        data = json.loads(line)
    except ValueError:
        return ()
    if not isinstance(data, dict):
        return ()
    sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    kind = data.get("type")
    if kind == "system" and data.get("subtype") == "init":
        return (RunnerEvent("init", session_id=sid),)
    if kind == "assistant":
        return _assistant(data)
    if kind == "result":
        text = data.get("result") if isinstance(data.get("result"), str) else None
        return (
            RunnerEvent(
                "result",
                text=text,
                cost_usd=_cost(data.get("total_cost_usd")),
                session_id=sid,
                is_error=bool(data.get("is_error")),
            ),
        )
    return ()


# ---- one turn --------------------------------------------------------------------------


def _signal(proc: subprocess.Popen[str] | None, sig: int) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        return
    except OSError:
        log.exception("omnigroup chat: signal %s failed", sig)


def _watchdog(
    proc: subprocess.Popen[str],
    spec: TurnSpec,
    handle: TurnHandle,
    grace_s: float,
    timed_out: threading.Event,
) -> None:
    """SIGINT at timeout (or after a stop request), SIGTERM then SIGKILL after grace."""
    if not handle.stop_requested.wait(spec.timeout_s):
        timed_out.set()
        _signal(proc, signal.SIGINT)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            proc.wait(grace_s)
            return
        except subprocess.TimeoutExpired:
            _signal(proc, sig)


def _start(spec: TurnSpec) -> subprocess.Popen[str]:
    return subprocess.Popen(  # argv list, no shell
        list(spec.argv),
        cwd=spec.cwd,
        env=dict(spec.env) if spec.env is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )


def _drain_stderr(proc: subprocess.Popen[str], keep: list[str]) -> None:
    assert proc.stderr is not None
    for chunk in proc.stderr:
        keep.append(chunk)
        while sum(len(c) for c in keep) > STDERR_KEEP and len(keep) > 1:
            keep.pop(0)


def _send_prompt(proc: subprocess.Popen[str], text: str) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        log.warning("omnigroup chat: claude closed its input early")


def _lines(stream: Any) -> Any:
    """Complete lines of `stream`; a line over MAX_LINE_CHARS is skipped (logged)."""
    while line := stream.readline(MAX_LINE_CHARS + 1):
        if len(line) <= MAX_LINE_CHARS:
            yield line
            continue
        log.warning("omnigroup chat: stream-json line too long, skipped")
        while line and not line.endswith("\n"):  # drop the rest of that line
            line = stream.readline(MAX_LINE_CHARS + 1)


def run_turn(
    spec: TurnSpec,
    on_event: Callable[[RunnerEvent], None],
    handle: TurnHandle,
    grace_s: float = GRACE_S,
) -> TurnOutcome:
    """Run one turn to completion, calling `on_event` for each parsed event."""
    try:
        proc = _start(spec)
    except FileNotFoundError:
        return TurnOutcome(
            False, error="Commande « claude » introuvable dans le PATH du démon."
        )
    except OSError as exc:
        return TurnOutcome(False, error=f"Impossible de lancer Claude Code : {exc}")
    handle.process = proc
    if handle.stop_requested.is_set():
        _signal(proc, signal.SIGINT)
    timed_out = threading.Event()
    errors: list[str] = []
    reader = threading.Thread(target=_drain_stderr, args=(proc, errors), daemon=True)
    reader.start()
    threading.Thread(target=_watchdog, args=(proc, spec, handle, grace_s, timed_out), daemon=True).start()  # fmt: skip
    _send_prompt(proc, spec.text)
    session_id, cost, result = None, None, None
    assert proc.stdout is not None
    for line in _lines(proc.stdout):
        for event in parse_line(line):
            session_id = event.session_id or session_id
            if event.kind == "result":
                cost, result = event.cost_usd, event
            on_event(event)
    code = proc.wait()
    reader.join(STDERR_JOIN_S)
    asked_to_stop = handle.stop_requested.is_set()
    handle.stop_requested.set()  # releases the watchdog
    stderr = "".join(errors)
    return _outcome(
        code, session_id, cost, result, stderr, timed_out.is_set(), asked_to_stop
    )


def _outcome(
    code: int,
    session_id: str | None,
    cost: float | None,
    result: RunnerEvent | None,
    stderr: str,
    timed_out: bool,
    asked_to_stop: bool,
) -> TurnOutcome:
    stopped = asked_to_stop and not timed_out and result is None
    if timed_out:
        return TurnOutcome(False, session_id, cost, "Tour arrêté : délai dépassé.")
    if result is not None and not result.is_error and code == 0:
        return TurnOutcome(True, session_id, cost)
    if stopped:
        return TurnOutcome(False, session_id, cost, "Tour arrêté.", stopped=True)
    if stderr.strip():
        log.warning(
            "omnigroup chat: claude exited with code %s: %s",
            code,
            redact_secrets(stderr.strip()[-STDERR_KEEP:], keep_paths=True),
        )
    message = f"Claude Code a échoué (code {code}). Détails dans le journal du démon."
    said = result.text.strip() if result is not None and result.text else ""
    if said:  # Claude's own result text (not stderr), redacted and short
        said = redact_secrets(" ".join(said.split()))
        said = said if len(said) <= RESULT_TEXT_MAX else said[:RESULT_TEXT_MAX] + "…"
        message = f"Claude Code a échoué (code {code}) : {said}"
    return TurnOutcome(False, session_id, cost, message)
