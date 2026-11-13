"""Open an "atelier" for an idea: a herdr workspace laid out 2x2 around one agent.

  agent (chat)  | à faire + note (`idea show --follow`)
  --------------+---------------------------------------
  salle de crise| UI (`idea ui`: live prototype; it confirms any build itself)

Talks to herdr 0.9.5 through its CLI only. Control commands print a JSON envelope on
stdout ({"id": ..., "result": {...}}); server errors print {"id": ..., "error": {...}}
on stderr with exit status 1. Nothing here starts a paid build: the UI pane asks first.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from omniscient.studio.agents import CLAUDE, AgentChoice, chat_command, prime_prompt
from omniscient.studio.ideas import IdeaCard

HERDR_BIN = "herdr"
RUNNER_TIMEOUT_S = 60
AGENT_START_TIMEOUT_MS = 45_000
NOTE_RATIO = 0.42  # right column width
WARROOM_RATIO = 0.3  # war room height under the agent
SLUG_MAX = 24
AGENT_SLUG_MAX = 16  # "variant-" + 16 + "-" + 6 = 31 <= 32 (herdr limit)
TAG_LEN = 6
REMIX_MIN, REMIX_MAX = 2, 3
_NOT_SLUG = re.compile(r"[^a-z0-9-]+")

Runner = Callable[[Sequence[str]], str]
Move = Literal["riff", "variant"]
MOVES: tuple[str, ...] = ("riff", "variant")
MOVE_LABEL = {"riff": "atelier", "variant": "variante"}
PANE_AGENT, PANE_TODO, PANE_UI, PANE_WAR = "agent", "à faire", "UI", "salle de crise"
WARROOM_LABEL = "salle de crise"
SKILL_TEXT_MAX = 300


class HerdrError(Exception):
    """herdr is missing, refused a command, or answered with an unexpected shape."""


def _error_message(stderr: str) -> str:
    """The `.error.message` of herdr's JSON error, or the raw stderr."""
    text = stderr.strip()
    try:
        message = json.loads(text)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return text or "erreur inconnue"
    return str(message)


def default_runner(args: Sequence[str]) -> str:
    """Run `herdr <args>` and return stdout; any failure becomes a HerdrError."""
    try:
        proc = subprocess.run(
            [HERDR_BIN, *args],
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HerdrError("herdr est introuvable dans le PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise HerdrError(f"herdr {' '.join(args[:2])} : délai dépassé") from exc
    except OSError as exc:
        raise HerdrError(f"herdr n'a pas pu être lancé : {exc}") from exc
    if proc.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args[:2])} : {_error_message(proc.stderr)}")
    return proc.stdout


def _parse(stdout: str) -> dict[str, Any]:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise HerdrError(f"réponse herdr illisible : {stdout[:120]!r}") from exc
    if not isinstance(data, dict):
        raise HerdrError(f"réponse herdr inattendue : {stdout[:120]!r}")
    return data


def _dig(data: Any, *path: str) -> Any:
    """Follow a key path (e.g. result.root_pane.pane_id) or fail clearly."""
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise HerdrError(f"réponse herdr sans .{'.'.join(path)}")
        node = node[key]
    return node


def _dig_str(data: Any, *path: str) -> str:
    value = _dig(data, *path)
    if not isinstance(value, str) or not value:
        raise HerdrError(f"réponse herdr : .{'.'.join(path)} n'est pas un identifiant")
    return value


def slugify(text: str, limit: int = SLUG_MAX) -> str:
    """ASCII [a-z0-9-] slug, at most `limit` characters, never empty."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _NOT_SLUG.sub("-", ascii_text.lower()).strip("-")
    return slug[:limit].strip("-") or "idee"


def tag(*parts: str) -> str:
    """Short stable hash: keeps labels and agent names unique per idea and move."""
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:TAG_LEN]


def agent_name(move: str, text: str, key: str) -> str:
    """herdr agent names: [a-z][a-z0-9_-]{0,31}; '<move>-<slug>-<tag>'."""
    return f"{move}-{slugify(text, AGENT_SLUG_MAX)}-{tag(move, key)}"


def date_less(stem: str) -> str:
    """'2026-11-13 my-idea' -> 'my-idea'."""
    return stem.split(" ", 1)[-1]


class Atelier:
    """Builds herdr workspaces for the studio's creative moves."""

    def __init__(self, repo: Path, runner: Runner = default_runner) -> None:
        self.repo = repo
        self._runner = runner

    def _call(self, *args: str) -> dict[str, Any]:
        return _parse(self._runner(list(args)))

    def _send(self, *args: str) -> None:
        """A command whose output we do not need (it still fails loudly)."""
        self._runner(list(args))

    def available(self) -> bool:
        """True when a herdr server is reachable. Never raises."""
        try:
            status = self._call("status", "server", "--json")
        except HerdrError:
            return False
        return status.get("running") is True

    def _find_workspace(self, label: str) -> str | None:
        workspaces = _dig(self._call("workspace", "list"), "result", "workspaces")
        if not isinstance(workspaces, list):
            raise HerdrError("réponse herdr : .result.workspaces n'est pas une liste")
        for item in workspaces:
            if isinstance(item, dict) and item.get("label") == label:
                return _dig_str(item, "workspace_id")
        return None

    def _create_workspace(self, label: str) -> tuple[str, str]:
        """(workspace id, root pane id) of a new focused workspace in the repo."""
        created = self._call(
            "workspace", "create", "--cwd", str(self.repo), "--label", label, "--focus"
        )
        return (
            _dig_str(created, "result", "workspace", "workspace_id"),
            _dig_str(created, "result", "root_pane", "pane_id"),
        )

    def _start_agent(self, pane: str, kind: str, name: str, prompt: str) -> None:
        self._send(
            "agent",
            "start",
            name,
            "--kind",
            kind,
            "--pane",
            pane,
            "--timeout",
            str(AGENT_START_TIMEOUT_MS),
        )
        self._send("agent", "prompt", name, prompt)

    def _claude(self, name: str, prompt: str) -> Callable[[str], None]:
        return lambda pane: self._start_agent(pane, "claude", name, prompt)

    def _run_in(self, pane: str, command: str, name: str) -> None:
        self._send("pane", "run", pane, command)
        self._send("pane", "rename", pane, name)

    def _split(self, pane: str, direction: str, ratio: float | None) -> str:
        args = ["pane", "split", pane, "--direction", direction]
        args += ["--cwd", str(self.repo)]
        if ratio is not None:
            args += ["--ratio", str(ratio)]
        args.append("--no-focus")
        return _dig_str(self._call(*args), "result", "pane", "pane_id")

    def _open(
        self,
        label: str,
        start: Callable[[str], None],
        layout: Callable[[str], None] | None = None,
    ) -> str:
        """Focus an existing workspace by label, or create it, start its agent, lay out.

        A half-built workspace is closed on failure so the next try starts clean."""
        existing = self._find_workspace(label)
        if existing is not None:
            self._send("workspace", "focus", existing)
            return existing
        workspace, root = self._create_workspace(label)
        try:
            start(root)
            if layout is not None:
                layout(root)
        except (HerdrError, OSError):
            try:
                self._send("workspace", "close", workspace)
            except HerdrError:
                pass  # the original error matters more; the owner can close it by hand
            raise
        return workspace

    def open_idea(
        self, card: IdeaCard, move: Move, agent: AgentChoice | None = None
    ) -> str:
        """2x2 atelier for one idea around the chosen agent (Claude Code by default)."""
        if move not in MOVES:
            raise HerdrError(f"mouvement inconnu : {move!r}")
        chosen = agent or CLAUDE
        label = (
            f"{MOVE_LABEL[move]} · {card.title[:24]} "
            f"#{tag(move, card.stem, chosen.key)}"
        )
        return self._open(
            label,
            lambda root: self._start_idea_agent(root, card, move, chosen),
            lambda root: self._layout(card, root),
        )

    def _start_idea_agent(
        self, root: str, card: IdeaCard, move: Move, agent: AgentChoice
    ) -> None:
        if agent.kind == "omniscient":
            try:
                command = chat_command(agent, card.stem)
            except ValueError as exc:
                raise HerdrError(f"agent inutilisable : {agent.label}") from exc
            self._run_in(root, command, PANE_AGENT)
            return
        if not agent.herdr_kind:
            raise HerdrError(f"agent sans type herdr : {agent.label}")
        name = agent_name(move, date_less(card.stem), f"{card.stem}\x1f{agent.key}")
        prompt = prime_prompt(agent, move, card.stem)
        self._start_agent(root, agent.herdr_kind, name, prompt)
        self._send("pane", "rename", root, PANE_AGENT)

    def _layout(self, card: IdeaCard, root: str) -> None:
        """À faire (top right), UI (bottom right), war room (under the agent)."""
        stem = shlex.quote(card.stem)
        todo = self._split(root, "right", NOTE_RATIO)
        self._run_in(todo, f"uv run omniscient idea show {stem} --follow", PANE_TODO)
        ui = self._split(todo, "down", None)
        self._run_in(ui, f"uv run omniscient idea ui {stem}", PANE_UI)
        war = self._split(root, "down", WARROOM_RATIO)
        self._run_in(war, f"uv run omniscient warroom --idea {stem} --follow", PANE_WAR)

    def open_remix(self, cards: Sequence[IdeaCard]) -> str:
        """Atelier where Claude combines 2 or 3 ideas into new concepts."""
        if not REMIX_MIN <= len(cards) <= REMIX_MAX:
            raise HerdrError("un remix combine 2 ou 3 idées")
        first, second = cards[0], cards[1]
        key = "\x1f".join(c.stem for c in cards)
        label = f"remix · {first.title[:12]} + {second.title[:12]} #{tag('remix', key)}"
        agent = agent_name("remix", date_less(first.stem), key)
        stems = " ".join(shlex.quote(c.stem) for c in cards)
        return self._open(label, self._claude(agent, f"/omniscient-remix {stems}"))

    def open_seed(self, text: str) -> str:
        """Atelier where Claude turns the owner's raw idea into a seed note."""
        idea = text.strip()
        if not idea:
            raise HerdrError("l'idée est vide")
        label = f"graine · {idea[:24]} #{tag('seed', idea)}"
        prompt = f"/omniscient-seed {idea}"
        return self._open(label, self._claude(agent_name("seed", idea, idea), prompt))

    def open_skill_creator(self, text: str) -> str:
        """Atelier where Claude writes a new PROJECT skill with the skill-creator skill."""
        need = " ".join(text.split())
        if not need:
            raise HerdrError("décris le skill à créer")
        if len(need) > SKILL_TEXT_MAX:
            raise HerdrError(f"description trop longue (max {SKILL_TEXT_MAX} car.)")
        label = f"skill · {need[:24]} #{tag('skill', need)}"
        prompt = (
            "Avec le skill skill-creator (anthropic-skills:skill-creator), crée un "
            "skill de PROJET dans .claude/skills/<nom>/SKILL.md de ce dépôt "
            "(nom en minuscules avec tirets ; pas dans ~/.claude). "
            f"Besoin du propriétaire : {need}"
        )
        return self._open(label, self._claude(agent_name("skill", need, need), prompt))

    def open_warroom(self) -> str:
        """The global war room workspace (reused by label)."""
        return self._open(
            WARROOM_LABEL,
            lambda root: self._run_in(
                root, "uv run omniscient warroom --follow", PANE_WAR
            ),
        )
