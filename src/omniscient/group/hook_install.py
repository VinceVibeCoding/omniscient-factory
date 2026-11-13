"""Install, remove or inspect the omnigroup hooks in Claude Code and Copilot settings.

`omniscient group hooks install|uninstall|status [--target claude|copilot|all] [--write]
[--path P]`. Without --write nothing is written: the merged file is shown as a unified
diff. With --write: timestamped backup first (never overwriting an earlier one), then an
atomic replace. Our entries are recognised by their command (`MARK`); every other hook
is kept. In the Claude settings only the `hooks` value is rewritten, the rest of the file
stays byte for byte; uninstall removes only our entries.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from omniscient.group.hooks import HOOK_SCRIPT, MARKS
from omniscient.group.json_edit import REMOVE, JsonEditError, rewrite
from omniscient.home import DEFAULT_HOME, Home, find_home
from omniscient.theme.export import backup

Target = Literal["claude", "copilot"]
TARGETS: tuple[Target, ...] = ("claude", "copilot")
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
COPILOT_HOOKS = Path.home() / ".copilot" / "hooks" / "omnigroup.json"
STATUS_FLAG = "--statusline"
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


def is_status_command(command: str) -> bool:
    """Claude's statusLine runs our client (`omniscient-hook --statusline` or legacy)."""
    legacy = "omniscient group statusline" in command
    return legacy or (STATUS_FLAG in command and any(m in command for m in MARKS))


def is_ours(entry: Any) -> bool:
    """A hook entry (or a Claude matcher group) that runs our command."""
    if not isinstance(entry, dict):
        return False
    for key in ("command", "bash", "powershell"):
        if isinstance(entry.get(key), str) and any(m in entry[key] for m in MARKS):
            return True
    return False


def _without_ours(hooks: dict[str, Any]) -> dict[str, Any]:
    """Claude `hooks` with our commands removed (emptied groups and events dropped)."""
    out: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept = []
        for group in groups:
            if is_ours(group):  # a flat Copilot entry
                continue
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                kept.append(group)
                continue
            mine = [h for h in inner if is_ours(h)]
            if not mine:
                kept.append(group)
            elif len(mine) < len(inner):
                kept.append({**group, "hooks": [h for h in inner if not is_ours(h)]})
        if kept:
            out[event] = kept
    return out


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
) -> HookPlan:
    """The file as it is and as it would be after install/uninstall.

    `statusline`: the `group statusline` command to install as Claude's statusLine
    (opt-in; an existing foreign statusLine is replaced only with `force`).
    """
    text, data = _load(path)
    current = _without_ours(data.get("hooks", {}))
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
        new = {k: v for k, v in new.items() if k != "statusLine"}
        new = new if line is REMOVE else {**new, "statusLine": line}
    if text is not None and new == data:
        return HookPlan(target, path, text, text)  # nothing to do: keep the bytes
    try:
        return HookPlan(target, path, text, rewrite(text, data, new))
    except JsonEditError as exc:
        raise HookInstallError(f"{path} : {exc}") from exc


def apply(plan_: HookPlan, now: datetime | None = None) -> Path | None:
    """Write the plan (backup first); returns the backup path, if a file existed."""
    if not plan_.changed:
        return None
    path = plan_.path
    try:
        saved = backup(path, now) if plan_.before is not None else None
        if plan_.after is None:
            path.unlink()
            return saved
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.omnigroup.tmp")
        tmp.write_bytes(plan_.after.encode("utf-8"))
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError as exc:
        raise HookInstallError(f"écriture impossible de {path} : {exc}") from exc
    return saved


def status(target: Target, path: Path) -> HookStatus:
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
    return line + (" — statusLine omnigroup" if st.statusline else "")


def run_hooks_command(
    action: str,
    target: str = "all",
    write: bool = False,
    path: Path | None = None,
    command: str | None = None,
    say: Callable[[str], None] = print,
    statusline: bool = False,
    force: bool = False,
) -> None:
    """The `group hooks` CLI body (French output); raises HookInstallError.

    `statusline` (install, Claude only, opt-in) also sets Claude's statusLine to
    `omniscient group statusline`; an existing foreign one needs `force`.
    """
    if action not in ("install", "uninstall", "status"):
        raise HookInstallError(f"action inconnue « {action} »")
    for tgt in resolve_targets(target, path):
        file = path or default_path(tgt)
        if action == "status":
            say(_status_line(status(tgt, file)))
            continue
        hook_cmd = command or base_command(find_home())
        line_cmd = None
        if statusline and tgt == "claude":
            line_cmd = f"{hook_cmd} {STATUS_FLAG}"
        p = plan(tgt, action, file, hook_cmd, line_cmd, force)  # type: ignore[arg-type]
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
