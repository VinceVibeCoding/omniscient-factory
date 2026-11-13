"""`omniscient chat AGENT_ID [--idea STEM]`: talk to an Omniscient AgentRun agent.

A terminal REPL for the atelier's agent pane. Each owner message (plus the idea note,
re-read every time) is ONE AgentRun run capped at the agent's max_cost_usd; it spends
API credit, so the cap is shown first and a message is refused when it could push the
day over config budget.daily_usd. Exchanges land in the note's `## Discussion` and every
cost in vault/ops/costs.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from omniscient import i18n
from omniscient.config import ConfigError, load_settings
from omniscient.ops import CostRow, append_cost, spent_on
from omniscient.studio.agents import (
    CHAT_AGENTS,
    definition_files,
    is_chat_agent,
    read_definition,
)
from omniscient.studio.ideas import StudioError, resolve, safe_text

QUIT = frozenset({"/quit", "/exit", "/q"})
MESSAGE_MAX = 4000
CONFIG_PATH = Path("config") / "omniscient.toml"

Ask = Callable[[str], str]
Say = Callable[[str], None]
ClientFactory = Callable[[], Any]
Today = Callable[[], date]


@dataclass(frozen=True)
class ChatAgent:
    """What the REPL needs from an agentrun/<id>.json definition."""

    agent_id: str
    cap_usd: float
    model: str
    effort: str


def load_agent(repo: Path, agent_id: str) -> ChatAgent:
    """The agent's definition (home agentrun/ or packaged), validated (French errors)."""
    if not is_chat_agent(agent_id):
        raise StudioError(
            f"l'agent {agent_id!r} ne sait pas discuter "
            f"(agents de chat : {', '.join(CHAT_AGENTS)})"
        )
    for path in definition_files(repo):
        definition = read_definition(path)
        if definition is None or definition.get("id") != agent_id:
            continue
        cap = definition.get("max_cost_usd")
        if not isinstance(cap, int | float) or isinstance(cap, bool) or cap <= 0:
            raise StudioError(f"l'agent {agent_id} n'a pas de max_cost_usd valide")
        return ChatAgent(
            agent_id=agent_id,
            cap_usd=float(cap),
            model=str(definition.get("model") or "-"),
            effort=str(definition.get("thinking") or "-"),
        )
    raise StudioError(f"agent Omniscient inconnu : {agent_id!r} (voir agentrun/*.json)")


def daily_budget(repo: Path) -> float:
    try:
        return load_settings(repo / CONFIG_PATH).budget.daily_usd
    except ConfigError as exc:
        raise StudioError(f"configuration illisible : {exc}") from exc


def agent_line(agent_id: str) -> str:
    """The bold speaker prefix used in `## Discussion`."""
    return i18n.L_DEBATER if agent_id == "debater" else f"**{agent_id} :**"


def build_prompt(message: str, note: str | None) -> str:
    if note is None:
        return f"Le propriétaire t'écrit :\n\n{message}\n\nRéponds-lui en français."
    return (
        f"{note}\n\n---\nLe propriétaire discute de cette idée avec toi :\n\n"
        f"{message}\n\nRéponds-lui en français, brièvement."
    )


def append_discussion(path: Path, lines: tuple[str, ...]) -> None:
    """Add lines under `## Discussion` (created at the end of the note if missing)."""
    from omniscient.slack.core import _append_discussion

    for line in lines:
        _append_discussion(path, line)


def _default_client() -> Any:
    from omniscient.cli import make_client  # big module: import only when sending

    return make_client()


def _send_errors() -> tuple[type[BaseException], ...]:
    """Errors one send can raise; each is reported and the REPL keeps going."""
    import typer
    from agentrun import AgentRunError

    from omniscient.runs import RunFailed

    return (RunFailed, AgentRunError, typer.BadParameter, ValueError, OSError, KeyError)


@dataclass(frozen=True)
class ChatReply:
    """One answered message: the reply, its cost, and a French warning if any."""

    text: str
    cost: float
    warning: str | None = None


def _reported_cost(run: Any) -> float | None:
    """The cost AgentRun reported for a run, or None when it reported none."""
    cost = run.get("cost") if isinstance(run, dict) else None
    usd = cost.get("usd") if isinstance(cost, dict) else None
    if isinstance(usd, bool) or not isinstance(usd, int | float):
        return None
    return float(usd)


class ChatSession:
    """One REPL session; every collaborator is injectable for tests."""

    def __init__(
        self,
        vault: Path,
        agent: ChatAgent,
        daily_usd: float,
        idea_path: Path | None,
        client_factory: ClientFactory = _default_client,
        today: Today = date.today,
    ) -> None:
        self.vault = vault
        self.agent = agent
        self.daily_usd = daily_usd
        self.idea_path = idea_path
        self._client_factory = client_factory
        self._client: Any = None
        self._today = today

    def banner(self) -> str:
        spent = spent_on(self.vault, self._today())
        about = f" · idée : {self.idea_path.stem}" if self.idea_path else ""
        return (
            f"Chat avec l'agent Omniscient « {self.agent.agent_id} »{about}\n"
            f"Chaque message coûte du crédit API : jusqu'à {self.agent.cap_usd:.2f} $ "
            f"par message · dépensé aujourd'hui {spent:.2f} $ / {self.daily_usd:.2f} $.\n"
            "Tape ton message puis Entrée ; /quit pour sortir."
        )

    def refusal(self) -> str | None:
        """Why a send would break the daily budget, or None when it fits."""
        spent = spent_on(self.vault, self._today())
        if spent + self.agent.cap_usd > self.daily_usd:
            return (
                f"Refusé : {spent:.2f} $ dépensés aujourd'hui + {self.agent.cap_usd:.2f} $ "
                f"de plafond dépasseraient le budget du jour ({self.daily_usd:.2f} $)."
            )
        return None

    def send(self, message: str) -> ChatReply:
        """One paid run. The cost is logged first (even when the run fails), then
        the exchange goes to the note's `## Discussion`."""
        if self._client is None:
            self._client = self._client_factory()
        note = self.idea_path.read_text(encoding="utf-8") if self.idea_path else None
        try:
            answer, cost = self._run(build_prompt(message, note))
        except KeyboardInterrupt:
            # The run may still be billing on AgentRun: count its whole cap.
            self._log_cost(self.agent.cap_usd)
            raise
        reply = safe_text(answer) or "(réponse vide)"
        self._log_cost(cost)
        return ChatReply(reply, cost, self._write_discussion(message, reply))

    def _run(self, content: str) -> tuple[str, float]:
        """Start and wait for one run; log a conservative cost if it fails."""
        from omniscient.runs import (
            DEFAULT_WAIT_SECONDS,
            CapReached,
            RunFailed,
            final_answer,
            run_cost,
            wait_durably,
        )

        cap = self.agent.cap_usd
        started = self._client.runs.start(
            self.agent.agent_id, {"input": {"content": content}, "max_cost_usd": cap}
        )
        try:
            run = wait_durably(self._client, started["run_id"], DEFAULT_WAIT_SECONDS)
        except Exception:
            self._log_cost(cap)  # started but its outcome is unknown
            raise
        try:
            return final_answer(run), run_cost(run)
        except CapReached as exc:
            self._log_cost(cap if exc.spent_usd is None else exc.spent_usd)
            raise
        except RunFailed:
            reported = _reported_cost(run)
            self._log_cost(cap if reported is None else reported)
            raise

    def _log_cost(self, usd: float) -> None:
        task = f"chat {self.idea_path.stem if self.idea_path else '-'}"
        append_cost(
            self.vault,
            CostRow(
                at=datetime.now().astimezone(),
                task=task.replace("|", "/"),
                role=self.agent.agent_id,
                model=self.agent.model,
                effort=self.agent.effort,
                usd=usd,
            ),
        )

    def _write_discussion(self, message: str, reply: str) -> str | None:
        """Append the exchange to the note; a French warning if that failed."""
        if self.idea_path is None:
            return None
        try:
            append_discussion(
                self.idea_path,
                (
                    f"{i18n.L_OWNER} {message}",
                    f"{agent_line(self.agent.agent_id)} {reply}",
                ),
            )
        except (OSError, ValueError) as exc:
            return f"Attention : échange non écrit dans la note ({exc})."
        return None


def _read(ask: Ask) -> str | None:
    """One owner message (single line, trimmed), or None to stop."""
    try:
        text = ask("vous › ")
    except (EOFError, KeyboardInterrupt):
        return None
    return " ".join(text.split())


def repl(session: ChatSession, ask: Ask, say: Say) -> None:
    say(session.banner())
    while True:
        message = _read(ask)
        if message is None or message.lower() in QUIT:
            say("Au revoir.")
            return
        if not message:
            continue
        if len(message) > MESSAGE_MAX:
            say(f"Message trop long (max {MESSAGE_MAX} caractères).")
            continue
        refusal = session.refusal()
        if refusal is not None:
            say(refusal)
            continue
        say(
            f"… {session.agent.agent_id} réfléchit (plafond {session.agent.cap_usd:.2f} $)"
        )
        try:
            answer = session.send(message)
        except KeyboardInterrupt:
            say(
                "\nInterrompu. Le run peut encore consommer du crédit sur AgentRun : "
                f"son plafond ({session.agent.cap_usd:.2f} $) a été compté."
            )
            continue
        except _send_errors() as exc:
            say(f"Échec de l'envoi : {exc}")
            continue
        say(f"\n{session.agent.agent_id} › {answer.text}\n({answer.cost:.4f} $)\n")
        if answer.warning is not None:
            say(answer.warning)


def run_chat(
    vault: Path,
    repo: Path,
    agent_id: str,
    idea: str | None,
    *,
    ask: Ask = input,
    say: Say = print,
    client_factory: ClientFactory = _default_client,
    today: Today = date.today,
) -> None:
    """Entry point for `omniscient chat`; raises StudioError (French) on bad input."""
    agent = load_agent(repo, agent_id)
    idea_path = resolve(vault, idea) if idea else None
    session = ChatSession(
        vault, agent, daily_budget(repo), idea_path, client_factory, today
    )
    repl(session, ask, say)
