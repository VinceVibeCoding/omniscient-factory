"""Which agents the owner can bring into an atelier, and how each one is primed.

Three families: Claude Code (the owner's subscription, always first), the other
interactive CLIs herdr can start (found on PATH), and Omniscient's AgentRun agents
(<home>/agentrun/*.json overrides, else the packaged definitions), which spend API
credit through `omniscient chat`.
"""

from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omniscient.home import Home

AgentFamily = Literal["herdr", "omniscient"]
Which = Callable[[str], str | None]
CLAUDE_KEY = "claude"
MOVE_ASK = {"riff": "riffer sur", "variant": "faire une variante de"}
# AgentRun agents that answer free-form conversation (the rest expect a pipeline
# input such as a council brief or a build spec). A trailing "*" is a prefix.
CHAT_AGENTS: tuple[str, ...] = ("debater", "griller", "researcher-*")


@dataclass(frozen=True)
class AgentChoice:
    """One entry of the agent picker."""

    key: str  # stable, [a-z0-9-]: used in herdr tags and agent names
    label: str  # French, shown to the owner
    kind: AgentFamily
    herdr_kind: str | None  # `herdr agent start --kind` value
    agent_id: str | None  # AgentRun agent id


CLAUDE = AgentChoice(CLAUDE_KEY, "Claude Code", "herdr", "claude", None)

# (herdr kind, executable on PATH, label), in picker order after Claude.
HERDR_CLIS: tuple[tuple[str, str, str], ...] = (
    ("copilot", "copilot", "GitHub Copilot CLI"),
    ("codex", "codex", "OpenAI Codex CLI"),
    ("gemini", "gemini", "Gemini CLI"),
    ("opencode", "opencode", "opencode"),
    ("amp", "amp", "Amp"),
    ("qwen", "qwen", "Qwen Code"),
    ("cursor", "cursor-agent", "Cursor Agent"),
    ("droid", "droid", "Factory Droid"),
    ("kimi", "kimi", "Kimi CLI"),
    ("grok", "grok", "Grok CLI"),
    ("kiro", "kiro-cli", "Kiro CLI"),
    ("cline", "cline", "Cline"),
    ("pi", "pi", "pi"),
)


def _herdr_clis(which: Which) -> tuple[AgentChoice, ...]:
    return tuple(
        AgentChoice(kind, label, "herdr", kind, None)
        for kind, exe, label in HERDR_CLIS
        if which(exe)
    )


def read_definition(path: Path) -> dict[str, object] | None:
    """The `definition` object of an agentrun/*.json file, or None if unusable."""
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))["definition"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(definition, dict) or not isinstance(definition.get("id"), str):
        return None
    return definition


def definition_files(repo: Path) -> tuple[Path, ...]:
    """The agent definition files of a home: its agentrun/ folder, else the packaged ones."""
    return tuple(sorted(Home(repo).agents_dir.glob("*.json")))


def is_chat_agent(agent_id: str) -> bool:
    """True when `omniscient chat` may talk to this AgentRun agent."""
    return any(
        agent_id.startswith(allowed[:-1])
        if allowed.endswith("*")
        else agent_id == allowed
        for allowed in CHAT_AGENTS
    )


def _omniscient_agents(repo: Path) -> tuple[AgentChoice, ...]:
    choices = []
    for path in definition_files(repo):
        definition = read_definition(path)
        if definition is None:
            continue
        agent_id = str(definition["id"])
        if not is_chat_agent(agent_id):
            continue
        choices.append(
            AgentChoice(
                key=f"or-{agent_id}",
                label=f"Omniscient · {agent_id} (crédit API)",
                kind="omniscient",
                herdr_kind=None,
                agent_id=agent_id,
            )
        )
    return tuple(choices)


def available_agents(repo: Path, which: Which | None = None) -> tuple[AgentChoice, ...]:
    """Claude Code, then herdr CLIs on PATH, then Omniscient's AgentRun agents."""
    found = which or shutil.which
    return (CLAUDE, *_herdr_clis(found), *_omniscient_agents(repo))


def prime_prompt(agent: AgentChoice, move: str, stem: str) -> str:
    """The first message a herdr agent gets in an atelier."""
    if move not in MOVE_ASK:
        raise ValueError(f"unknown move {move!r}")
    quoted = shlex.quote(stem)
    if agent.herdr_kind == CLAUDE.herdr_kind:
        return f"/omniscient-{move} {quoted}"
    return (
        f"Lis le contexte avec `uv run omniscient idea context {quoted}`, puis aide-moi "
        f"à {MOVE_ASK[move]} cette idée. Note les conclusions avec "
        f"`uv run omniscient idea append {quoted}` et les tâches avec "
        f'`uv run omniscient idea todo-add {quoted} "…"`. '
        "N'exécute jamais `omniscient build`."
    )


def chat_command(agent: AgentChoice, stem: str) -> str:
    """The pane command that talks to an Omniscient agent about one idea."""
    if agent.kind != "omniscient" or not agent.agent_id:
        raise ValueError(f"{agent.key} is not an Omniscient agent")
    return (
        f"uv run omniscient chat {shlex.quote(agent.agent_id)} "
        f"--idea {shlex.quote(stem)}"
    )
