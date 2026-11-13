"""Install, remove or inspect the omnigroup hooks in Claude Code and Copilot settings.

`omniscient group hooks install|uninstall|status [--target claude|copilot|all] [--write]
[--path P]`. Without --write nothing is written: the merged file is shown as a unified
diff. With --write: timestamped backup first (never overwriting an earlier one), then an
atomic replace (a 0600 mkstemp file next to the real file, a symlinked settings.json is
written through to its target). Our entries are recognised by their exact command
(`omniscient-hook …`, `<python> -m omniscient.group.hook_client …` or `omniscient group
hook …`); every other hook is kept. In the Claude settings only `hooks` (and `statusLine`,
`permissions` when asked) is rewritten, the rest stays byte for byte. `--protect` adds
Claude deny rules around the state folder (hook_protect.py); uninstall removes them.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from omniscient.group.hook_match import (
    STATUS_FLAG,
    is_ours,
    is_status_command,
    without_ours,
)
from omniscient.group.hook_protect import (
    ProtectError,
    deny_rules,
    is_protected,
    updated_permissions,
)
from omniscient.group.hooks import HOOK_SCRIPT
from omniscient.group.json_edit import REMOVE, JsonEditError, rewrite
from omniscient.home import DEFAULT_HOME, Home, find_home
from omniscient.theme.export import backup

Target = Literal["claude", "copilot"]
TARGETS: tuple[Target, ...] = ("claude", "copilot")
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
COPILOT_HOOKS = Path.home() / ".copilot" / "hooks" / "omnigroup.json"
WAIT_TIMEOUT_S = 110  # Claude: the hook long-polls up to 100 s
FAST_TIMEOUT_S = 5
COPILOT_TIMEOUT_S = 30  # Copilot: the hook long-polls up to 25 s
TOOL_MATCH = "*"
ASK_MATCH = "^AskUserQuestion$"
# Claude event -> (matcher or None, waits for the owner?)
CLAUDE_EVENTS: tuple[tuple[str, str | None, bool], ...] = (
    ("SessionStart", None, False),
    ("SessionEnd", None, False),
    ("UserPromptSubmit", None, False),
    ("PreToolUse", ASK_MATCH, True),
    ("PreToolUse", TOOL_MATCH, False),
    ("PermissionRequest", TOOL_MATCH, True),
    ("PostToolUse", TOOL_MATCH, False),
    ("PostToolUseFailure", TOOL_MATCH, False),
    ("Notification", None, False),
    ("Stop", None, False),
    ("SubagentStop", None, False),
)
COPILOT_EVENTS: tuple[tuple[str, bool], ...] = (
    ("sessionStart", False),
    ("sessionEnd", False),
    ("userPromptSubmitted", False),
    ("preToolUse", False),
    ("postToolUse", False),
    ("permissionRequest", True),
    ("agentStop", False),
)


class HookInstallError(Exception):
    """The settings file cannot be read, understood or written."""


@dataclass(frozen=True)
class HookPlan:
    target: Target
    path: Path
    before: str | None  # None: the file does not exist
    after: str | None  # None: the file is to be deleted

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        old = (self.before or "").splitlines(keepends=True)
        new = (self.after or "").splitlines(keepends=True)
        name = str(self.path)
        return "".join(difflib.unified_diff(old, new, name, name + " (nouveau)"))


@dataclass(frozen=True)
class HookStatus:
    target: Target
    path: Path
    installed: tuple[str, ...]  # events carrying our hook
    missing: tuple[str, ...]
    statusline: bool = False  # Claude's statusLine runs `group statusline`
    protected: bool = False  # our deny rules are all present (Claude, --protect)


def base_command(
    home: Home | None = None,
    which: Callable[[str], str | None] = shutil.which,
    python: str = sys.executable,
) -> str:
    """The hook command: the absolute `omniscient-hook` script (fast start: it imports
    only the hook client, not the whole CLI), else `<python> -m omniscient.group.
    hook_client`; plus `--home` when the home is not the default."""
    exe = which(HOOK_SCRIPT)
    if exe:
        cmd = shlex.quote(str(Path(exe).resolve()))
    elif python:
        cmd = f"{shlex.quote(python)} -m omniscient.group.hook_client"
    else:
        raise HookInstallError(
            "Commande « omniscient-hook » introuvable dans le PATH : installez "
            "Omniscient d'abord (uv tool install ...)."
        )
    if home is not None and home.root.resolve() != DEFAULT_HOME.resolve():
        cmd += f" --home {shlex.quote(str(home.root))}"
    return cmd


def claude_hooks(command: str) -> dict[str, list[dict[str, Any]]]:
    """Our Claude hook groups, by event."""
    out: dict[str, list[dict[str, Any]]] = {}
    for event, matcher, waits in CLAUDE_EVENTS:
        cmd = f"{command} --source claude" + ("" if waits else " --fast")
        hook = {
            "type": "command",
            "command": cmd,
            "timeout": WAIT_TIMEOUT_S if waits else FAST_TIMEOUT_S,
        }
        group: dict[str, Any] = {} if matcher is None else {"matcher": matcher}
        out.setdefault(event, []).append({**group, "hooks": [hook]})
    return out


def copilot_hooks(command: str) -> dict[str, list[dict[str, Any]]]:
    """Our Copilot hook entries, by event (the event name is passed as --event)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for event, waits in COPILOT_EVENTS:
        cmd = f"{command} --source copilot --event {event}" + (
            "" if waits else " --fast"
        )
        entry = {"type": "command", "bash": cmd, "timeoutSec": COPILOT_TIMEOUT_S}
        out[event] = [entry]
    return out


def _merge(current: dict[str, Any], ours: dict[str, list[Any]]) -> dict[str, Any]:
    merged = {k: list(v) if isinstance(v, list) else v for k, v in current.items()}
    for event, entries in ours.items():
        existing = merged.get(event)
        merged[event] = (existing if isinstance(existing, list) else []) + entries
    return merged


def _load(path: Path) -> tuple[str | None, dict[str, Any]]:
    try:
        text = path.read_bytes().decode("utf-8") if path.exists() else None
        data = json.loads(text) if text and text.strip() else {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise HookInstallError(f"{path} illisible ou JSON invalide : {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise HookInstallError(f"{path} : objet JSON avec « hooks » objet attendu")
    return text, data


def _statusline(current: Any, action: str, command: str | None, force: bool) -> Any:
    """The statusLine value after the action (REMOVE when there is none)."""
    ours = isinstance(current, dict) and is_status_command(
        str(current.get("command", ""))
    )
    if action == "uninstall":
        return REMOVE if ours else current
    if command is None:  # install without --statusline: leave it alone
        return current
    if current is not REMOVE and not ours and not force:
        raise HookInstallError(
            "Une statusLine existe déjà dans ce fichier : relancez avec --force pour la "
            "remplacer (une sauvegarde est faite)."
        )
    return {"type": "command", "command": command, "padding": 0}


def plan(
    target: Target,
    action: Literal["install", "uninstall"],
    path: Path,
    command: str,
    statusline: str | None = None,
    force: bool = False,
    protect: list[str] | None = None,
) -> HookPlan:
    """The file as it is and as it would be after install/uninstall.

    `statusline`: the `group statusline` command to install as Claude's statusLine
    (opt-in; an existing foreign statusLine is replaced only with `force`).
    `protect`: our Claude deny rules, added on install, removed on uninstall.
    """
    text, data = _load(path)
    current = without_ours(data.get("hooks", {}))
    ours = claude_hooks(command) if target == "claude" else copilot_hooks(command)
    hooks = _merge(current, ours) if action == "install" else current
    if action == "uninstall" and text is None:
        return HookPlan(target, path, None, None)
    if target == "copilot" and not hooks and set(data) <= {"version", "hooks"}:
        return HookPlan(target, path, text, None)  # our own file, now empty: delete
    new = {**data, "hooks": hooks}
    if target == "copilot":
        new = {"version": data.get("version", 1), **new}
    else:
        line = _statusline(data.get("statusLine", REMOVE), action, statusline, force)
        new = _with(new, "statusLine", line)
        if protect:
            perms = _permissions(data.get("permissions", REMOVE), action, protect, path)
            new = _with(new, "permissions", perms)
    if text is not None and new == data:
        return HookPlan(target, path, text, text)  # nothing to do: keep the bytes
    try:
        return HookPlan(target, path, text, rewrite(text, data, new))
    except JsonEditError as exc:
        raise HookInstallError(f"{path} : {exc}") from exc


def _with(data: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    rest = {k: v for k, v in data.items() if k != key}
    return rest if value is REMOVE else {**rest, key: value}


def _permissions(current: Any, action: str, rules: list[str], path: Path) -> Any:
    try:
        return updated_permissions(current, action, rules)
    except ProtectError as exc:
        raise HookInstallError(f"{path} : {exc}") from exc


def _write_atomic(real: Path, text: str) -> None:
    """A unique 0600 temp file in the target's folder, the target's mode, then replace."""
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        if real.exists():
            shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply(plan_: HookPlan, now: datetime | None = None) -> Path | None:
    """Write the plan (backup first); returns the backup path, if a file existed. A
    symlinked settings file is written through to its real target (the link stays)."""
    if not plan_.changed:
        return None
    real = Path(os.path.realpath(plan_.path))
    try:
        saved = backup(real, now) if plan_.before is not None else None
        if plan_.after is None:
            real.unlink()
            return saved
        _write_atomic(real, plan_.after)
    except OSError as exc:
        raise HookInstallError(f"écriture impossible de {real} : {exc}") from exc
    return saved


def status(target: Target, path: Path, protect: list[str] | None = None) -> HookStatus:
    _, data = _load(path)
    hooks = data.get("hooks", {})
    wanted = (
        [e for e, *_ in CLAUDE_EVENTS]
        if target == "claude"
        else [e for e, _ in COPILOT_EVENTS]
    )
    found = {
        event
        for event, groups in hooks.items()
        if isinstance(groups, list)
        and any(
            is_ours(h)
            for g in groups
            if isinstance(g, dict)
            for h in (g.get("hooks") or [g])
        )
    }
    events = tuple(dict.fromkeys(wanted))
    line = (
        str(data.get("statusLine", {}).get("command", ""))
        if isinstance(data.get("statusLine"), dict)
        else ""
    )
    return HookStatus(
        target,
        path,
        tuple(e for e in events if e in found),
        tuple(e for e in events if e not in found),
        is_status_command(line),
        bool(protect) and target == "claude" and is_protected(data, protect),
    )


def default_path(target: Target) -> Path:
    return CLAUDE_SETTINGS if target == "claude" else COPILOT_HOOKS


def resolve_targets(target: str, path: Path | None) -> tuple[Target, ...]:
    if target not in (*TARGETS, "all"):
        raise HookInstallError(f"cible inconnue « {target} » (claude, copilot ou all)")
    targets = TARGETS if target == "all" else (target,)  # type: ignore[assignment]
    if path is not None and len(targets) > 1:
        raise HookInstallError(
            "--path demande une seule cible (--target claude|copilot)"
        )
    return targets  # type: ignore[return-value]


def _status_line(st: HookStatus) -> str:
    total = len(st.installed) + len(st.missing)
    line = f"{st.target} ({st.path}) : {len(st.installed)}/{total} événements"
    line += f" — manquants : {', '.join(st.missing)}" if st.missing else ""
    line += " — statusLine omnigroup" if st.statusline else ""
    return line + (" — protection --protect active" if st.protected else "")


def run_hooks_command(
    action: str,
    target: str = "all",
    write: bool = False,
    path: Path | None = None,
    command: str | None = None,
    say: Callable[[str], None] = print,
    statusline: bool = False,
    force: bool = False,
    protect: bool = False,
    state_dir: Path | None = None,
) -> None:
    """The `group hooks` CLI body (French output); raises HookInstallError.

    `statusline` (install, Claude only, opt-in) also sets Claude's statusLine to
    `omniscient group statusline`; an existing foreign one needs `force`.
    `protect` (install, Claude) adds deny rules around `state_dir`; uninstall always
    removes them.
    """
    if action not in ("install", "uninstall", "status"):
        raise HookInstallError(f"action inconnue « {action} »")
    rules = deny_rules(state_dir or find_home().state)
    for tgt in resolve_targets(target, path):
        file = path or default_path(tgt)
        if action == "status":
            say(_status_line(status(tgt, file, rules)))
            continue
        hook_cmd = command or base_command(find_home())
        line_cmd = None
        if statusline and tgt == "claude":
            line_cmd = f"{hook_cmd} {STATUS_FLAG}"
        guard = (
            rules if (protect or action == "uninstall") and tgt == "claude" else None
        )
        p = plan(tgt, action, file, hook_cmd, line_cmd, force, guard)  # type: ignore[arg-type]
        if not p.changed:
            say(f"{tgt} ({file}) : déjà à jour.")
        elif not write:
            say(p.diff() + f"\n(aperçu — relancez avec --write pour modifier {file})")
        else:
            saved = apply(p)
            say(
                f"{tgt} : {file} mis à jour"
                + (f" (sauvegarde : {saved})" if saved else "")
            )
