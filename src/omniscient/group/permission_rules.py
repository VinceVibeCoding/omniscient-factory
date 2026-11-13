"""« Toujours (cette session) »: which permission rule a Claude Code answer adds (M2).

Order of preference:
1. the event's own `permission_suggestions` of type addRules / allow / destination
   "session" (other destinations or types, such as setMode, are never used);
2. for Bash, Write, Edit, MultiEdit and NotebookEdit: one exact rule, the command or the
   absolute file path as `ruleContent` (paths written `//abs/path`, Claude's absolute
   form); refused when it holds a wildcard character, since a rule would then match more;
3. other tools: the tool name alone.
The tools of 2. never get a bare tool-name rule: without exact content, no « Toujours ».
Pure functions: the daemon shows the rule, the hook client recomputes it from its own
event (it never trusts the daemon for this).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EXACT_TOOLS = {
    "Bash": "command",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
WILDCARDS = frozenset("*?[]")
RULE_CONTENT_MAX = 2000


def _session_rule(rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "addRules",
        "rules": rules,
        "behavior": "allow",
        "destination": "session",
    }


def _rule_ok(rule: Any) -> bool:
    if not isinstance(rule, dict) or not isinstance(rule.get("toolName"), str):
        return False
    content = rule.get("ruleContent")
    if content is None:
        return rule["toolName"] not in EXACT_TOOLS
    return isinstance(content, str) and 0 < len(content) <= RULE_CONTENT_MAX


def _from_suggestions(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = event.get("permission_suggestions")
    picked: list[dict[str, Any]] = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict) or s.get("type") != "addRules":
            continue
        if s.get("behavior") != "allow" or s.get("destination") != "session":
            continue
        rules = s.get("rules")
        if isinstance(rules, list) and rules and all(_rule_ok(r) for r in rules):
            picked.append(_session_rule([dict(r) for r in rules]))
    return picked


def _exact_content(tool: str, args: Any) -> str | None:
    value = args.get(EXACT_TOOLS[tool]) if isinstance(args, dict) else None
    if not isinstance(value, str) or not value.strip() or len(value) > RULE_CONTENT_MAX:
        return None
    if WILDCARDS & set(value) or "\n" in value:
        return None
    if tool == "Bash":
        return value
    return "/" + value if value.startswith("/") else None  # absolute paths only


def always_permissions(
    tool: str, args: Any, event: Mapping[str, Any]
) -> list[dict[str, Any]] | None:
    """The `updatedPermissions` for « Toujours », or None when it is not offered."""
    suggested = _from_suggestions(event)
    if suggested:
        return suggested
    if not tool:
        return None
    if tool in EXACT_TOOLS:
        content = _exact_content(tool, args)
        if content is None:
            return None
        return [_session_rule([{"toolName": tool, "ruleContent": content}])]
    return [_session_rule([{"toolName": tool}])]


def rule_labels(updates: list[dict[str, Any]] | None) -> list[str]:
    """Human-readable rules, as Claude Code writes them: `Bash(git status)`."""
    labels = []
    for update in updates or []:
        for rule in update.get("rules") or []:
            content = rule.get("ruleContent")
            name = rule.get("toolName", "?")
            labels.append(f"{name}({content})" if content else str(name))
    return labels
