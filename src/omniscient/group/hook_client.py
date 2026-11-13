"""`omniscient group hook`: post one agent hook event to the daemon, wait for the owner.

Reads the hook JSON on stdin and posts it to `POST /hook` with the hook credential
(`group.token`). For a permission or a question it long-polls `GET /pending/{id}/wait`
with the single-use wait ticket /hook returned, and prints the owner's decision in the agent's
format (see `hooks.decision_output`). Fail-open: daemon down, token missing, timeout or
any network error -> exit 0 with no stdout, and the agent shows its normal prompt.

Two Claude PreToolUse entries share this command: the `^AskUserQuestion$` entry (waits)
and the `*` entry (`--fast`: track only, returns at once, skips AskUserQuestion).
Copilot also reads `.claude/settings.json`: when the Claude hook sees a Copilot payload
(camelCase `sessionId`) it reports it as Copilot, track-only, or stays silent when the
Copilot hooks file is installed (that hook then reports the event, once).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from omniscient.group.hooks import (
    ASK_TOOL,
    CONTEXT_KEY,
    MARKS,
    WAIT_S,
    HookError,
    decision_output,
    event_name,
)
from omniscient.home import Home, find_home

DEFAULT_URL = "http://127.0.0.1:7341"
URL_ENV = "OMNIGROUP_URL"
TICKET_HEADER = "X-Omnigroup-Ticket"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
TOKEN_FILE = "group.token"
POST_TIMEOUT_S = 2.0
FAST_POST_TIMEOUT_S = 1.0
LONG_POLL_MAX_S = 120
COPILOT_HOOKS_FILE = Path.home() / ".copilot" / "hooks" / "omnigroup.json"

Transport = Callable[[str, str, dict[str, str], bytes | None, float], Any]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> Any:
    """One HTTP call to the daemon, never through a proxy (the URL is loopback-only,
    see _base_url); raises OSError (incl. URLError), ValueError or HTTPException."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass(frozen=True)
class HookContext:
    """Everything the hook command depends on, injectable for tests."""

    state_dir: Path  # <home>/.omniscient
    environ: Mapping[str, str]
    transport: Transport = urllib_transport
    copilot_hooks: Path = COPILOT_HOOKS_FILE
    clock: Callable[[], float] = time.monotonic


def _base_url(environ: Mapping[str, str]) -> str:
    """OMNIGROUP_URL (default 127.0.0.1:7341): plain http to this machine, nothing else."""
    url = environ.get(URL_ENV, DEFAULT_URL).rstrip("/")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise HookError(f"{URL_ENV} invalide") from exc
    local = parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
    plain = not (parts.username or parts.password or parts.path or parts.query)
    if not (local and plain and port and not parts.fragment):
        raise HookError(f"{URL_ENV} must point to the local daemon")
    return url


def _copilot_hooks_installed(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
        return any(mark in text for mark in MARKS)
    except OSError:
        return False


def _prepare(
    source: str, text: str, fast: bool, event_arg: str | None, ctx: HookContext
) -> tuple[str, dict[str, Any]] | None:
    """(source, event to post), or None when this invocation must stay silent."""
    event = json.loads(text)
    if not isinstance(event, dict):
        raise HookError("hook stdin must be a JSON object")
    if event_arg and not (event.get("hook_event_name") or event.get("hookEventName")):
        event = {**event, "hookEventName": event_arg}
    if source == "claude" and "sessionId" in event and "session_id" not in event:
        if _copilot_hooks_installed(ctx.copilot_hooks):
            return None  # Copilot reading .claude/settings.json: its own hook reports
        source, fast = "copilot", True
    name = event_name(event)
    is_ask = name == "PreToolUse" and event.get("tool_name") == ASK_TOOL
    if name == "PreToolUse" and source == "claude" and is_ask == fast:
        return None  # each PreToolUse is handled by exactly one of the two entries
    entry = ctx.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    return source, {**event, CONTEXT_KEY: {"entrypoint": entry, "fast": fast}}


def _wait(
    ctx: HookContext,
    call: Callable[..., Any],
    pending_id: str,
    wait_s: float,
    ticket: str,
) -> Mapping[str, Any] | None:
    deadline = ctx.clock() + wait_s
    extra = {TICKET_HEADER: ticket}
    while (remaining := deadline - ctx.clock()) > 0:
        poll = int(min(max(remaining, 1), LONG_POLL_MAX_S))
        pending = call(
            "GET", f"/pending/{pending_id}/wait?timeout={poll}", None, poll + 5, extra
        )
        status = pending.get("status") if isinstance(pending, dict) else None
        if status == "answered" and isinstance(pending.get("answer"), dict):
            return pending["answer"]
        if status != "open":
            return None
    return None


def _caller(ctx: HookContext, base: str, token: str) -> Callable[..., Any]:
    """call(method, path, body, timeout, extra headers) with the hook credential."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def call(
        method: str,
        path: str,
        body: Any,
        timeout: float,
        extra: dict[str, str] | None = None,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        hdrs = {**headers, **(extra or {})}
        return ctx.transport(method, base + path, hdrs, data, timeout)

    return call


def run_hook(
    source: str,
    stdin: TextIO,
    stdout: TextIO,
    ctx: HookContext,
    fast: bool = False,
    event_arg: str | None = None,
) -> int:
    """`omniscient group hook`: always exits 0; prints a decision only when answered."""
    token_file = ctx.state_dir / TOKEN_FILE
    if not token_file.is_file():
        return 0  # omnigroup not set up: stay out of the way, silently
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            return 0
        prepared = _prepare(source, stdin.read(), fast, event_arg, ctx)
        if prepared is None:
            return 0
        source, event = prepared
        call = _caller(ctx, _base_url(ctx.environ), token)
        timeout = FAST_POST_TIMEOUT_S if event[CONTEXT_KEY]["fast"] else POST_TIMEOUT_S
        reply = call("POST", "/hook", {"source": source, "event": event}, timeout)
        reply = reply if isinstance(reply, dict) else {}
        pending_id, ticket = reply.get("pending_id"), reply.get("wait_ticket")
        if not pending_id or not isinstance(ticket, str) or event[CONTEXT_KEY]["fast"]:
            return 0  # tracking only, or no widget to answer (wait_s 0)
        pending_id = str(uuid.UUID(str(pending_id)))  # never a path fragment
        wait = min(float(reply.get("wait_s") or 0), WAIT_S[source])
        answer = _wait(ctx, call, pending_id, wait, ticket) if wait > 0 else None
        output = decision_output(source, event, answer) if answer else None
        if output is not None:
            stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    except (OSError, ValueError, http.client.HTTPException) as exc:  # fail open
        print(f"omnigroup hook ignoré : {exc}", file=sys.stderr)
    return 0


STATUS_POST_TIMEOUT_S = 0.5  # the status line redraws often: never make it wait


def status_text(event: Mapping[str, Any]) -> str:
    """A short French status line from Claude Code's statusLine JSON."""
    parts = ["omnigroup"]
    model = event.get("model")
    if isinstance(model, dict) and isinstance(model.get("display_name"), str):
        parts.append(model["display_name"])
    ctx = event.get("context_window")
    pct = ctx.get("used_percentage") if isinstance(ctx, dict) else None
    if isinstance(pct, int | float):
        parts.append(f"contexte {pct:.0f} %")
    limits = event.get("rate_limits")
    for key, label in (("five_hour", "5 h"), ("seven_day", "7 j")):
        win = limits.get(key) if isinstance(limits, dict) else None
        used = win.get("used_percentage") if isinstance(win, dict) else None
        if isinstance(used, int | float):
            parts.append(f"{label} {used:.0f} %")
    return " · ".join(parts)


def run_statusline(stdin: TextIO, stdout: TextIO, ctx: HookContext) -> int:
    """`omniscient group statusline`: print the line, forward the JSON (best effort)."""
    try:
        event = json.loads(stdin.read())
        if not isinstance(event, dict):
            raise HookError("statusLine stdin must be a JSON object")
    except ValueError as exc:
        print(f"omnigroup statusline : {exc}", file=sys.stderr)
        stdout.write("omnigroup\n")
        return 0
    stdout.write(status_text(event) + "\n")
    stdout.flush()
    token_file = ctx.state_dir / TOKEN_FILE
    if not token_file.is_file():
        return 0
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        url = _base_url(ctx.environ) + "/hook"
        body = {"source": "claude", "event": {**event, "hook_event_name": "StatusLine"}}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8")
        ctx.transport("POST", url, headers, data, STATUS_POST_TIMEOUT_S)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"omnigroup statusline ignoré : {exc}", file=sys.stderr)
    return 0


def default_context(home_root: Path | None = None) -> HookContext:
    """The real context: the home's state folder (`--home`, else `find_home()`)."""
    home = Home(home_root.expanduser().resolve()) if home_root else find_home()
    return HookContext(state_dir=home.state, environ=os.environ)


def main(argv: list[str] | None = None) -> int:
    """`omniscient-hook` (console script) or `python -m omniscient.group.hook_client`."""
    parser = argparse.ArgumentParser(prog="omniscient group hook")
    parser.add_argument("--source", choices=sorted(WAIT_S))
    parser.add_argument("--statusline", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--event")
    parser.add_argument("--home", type=Path)
    args = parser.parse_args(argv)
    ctx = default_context(args.home)
    if args.statusline:
        return run_statusline(sys.stdin, sys.stdout, ctx)
    if args.source is None:
        parser.error("--source est requis")
    return run_hook(args.source, sys.stdin, sys.stdout, ctx, args.fast, args.event)


if __name__ == "__main__":
    raise SystemExit(main())
