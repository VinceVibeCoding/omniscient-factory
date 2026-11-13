"""`--protect`: Claude Code `permissions.deny` rules around the omnigroup state folder.

Defense in depth only. They stop the obvious ways a supervised Claude session could read
or change the hook credential and group.db (Read/Edit/Write of the folder, `cat` and
friends, `omniscient group pair`), not an agent that runs arbitrary Bash as the same
user: see docs/omnigroup.md, « Modèle de menace ». Our rules are added once, in order,
and uninstall removes exactly them; the owner's own rules are never touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniscient.group.json_edit import REMOVE

READERS = ("cat", "less", "more", "head", "tail", "cp", "sqlite3", "strings", "xxd")
OUR_COMMANDS = ("omniscient group pair*", "omniscient group token*")


class ProtectError(ValueError):
    """The settings' `permissions` value is not an object with a `deny` list."""


def _forms(state_dir: Path, home_dir: Path) -> tuple[str, ...]:
    """How rules name the folder: `~/…` under the home, and the absolute path."""
    absolute = str(state_dir)
    try:
        under_home = "~/" + str(state_dir.relative_to(home_dir))
    except ValueError:
        return (absolute,)
    return (under_home, absolute)


def deny_rules(state_dir: Path, home_dir: Path | None = None) -> list[str]:
    """Our deny rules for `state_dir` (Claude's absolute path form is `//abs/path`)."""
    home = home_dir if home_dir is not None else Path.home()
    forms = _forms(state_dir, home)
    file_form = forms[0] if forms[0].startswith("~/") else "/" + forms[0]
    rules = [f"{tool}({file_form}/**)" for tool in ("Read", "Edit", "Write")]
    for form in forms:
        rules += [f"Bash({cmd} {form}/*)" for cmd in READERS]
    rules += [f"Bash({cmd})" for cmd in OUR_COMMANDS]
    return rules


def updated_permissions(current: Any, action: str, rules: list[str]) -> Any:
    """The `permissions` value after install (add ours) or uninstall (drop ours);
    REMOVE when nothing is left of a value we created."""
    if current is REMOVE:
        perms: dict[str, Any] = {}
    elif isinstance(current, dict):
        perms = dict(current)
    else:
        raise ProtectError("« permissions » doit être un objet JSON")
    deny = perms.get("deny", [])
    if not isinstance(deny, list):
        raise ProtectError("« permissions.deny » doit être une liste")
    if action == "install":
        new_deny = deny + [r for r in rules if r not in deny]
    else:
        new_deny = [r for r in deny if r not in rules]
    if new_deny == deny:
        return current  # nothing of ours to add or remove: keep the bytes
    if new_deny:
        return {**perms, "deny": new_deny}
    rest = {k: v for k, v in perms.items() if k != "deny"}
    return rest or REMOVE


def is_protected(data: dict[str, Any], rules: list[str]) -> bool:
    perms = data.get("permissions")
    deny = perms.get("deny") if isinstance(perms, dict) else None
    return isinstance(deny, list) and all(r in deny for r in rules)
