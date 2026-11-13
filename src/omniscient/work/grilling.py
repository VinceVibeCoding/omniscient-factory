"""Grilling tickets in Slack (HITL): one round of questions per frontier grilling ticket.

The `griller` agent drafts the round (every frontier question at once, each with a
recommended answer); the round is posted as one Slack message whose thread is recorded
in the ticket file as `Slack: <channel> <ts>`. Only the owner's own reply in that thread
resolves the ticket (AGENTS.md rule 9): no model call, the owner's words are the decision.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Protocol

import frontmatter
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from omniscient import i18n
from omniscient.runs import AgentRunClient, AnswerNotJson, extract_json, run_once
from omniscient.work.tickets import (
    QUESTION_HEADING,
    Ticket,
    TicketError,
    all_maps,
    claim,
    frontier,
    load_map,
    parse_ticket,
    release,
    resolve,
    sanitize_untrusted,
)

GRILLER_ID = "griller"
GRILLER_CAP_USD = 0.50
GIST_MAX_CHARS = 160
TITLE_MAX_CHARS = 120
FIELD_MAX_CHARS = 500
SLACK_TEXT_LIMIT = 2900
ROUND_HEADING = "## Grilling round"
ACCEPTED_GIST = "Recommandations acceptées"
REPLY_HINT = "Réponds dans ce fil (« oui » accepte toutes les recommandations)"
NO_IDEA = "_aucune note d'idée_"
_ACCEPT_WORDS = frozenset({"oui", "yes", "ok", "okay", "d'accord", "go", "parfait"})
_SLACK_LINE = re.compile(r"^Slack:\s*(\S+)\s+(\S+)\s*$", re.MULTILINE)
_CHANNEL_ID = re.compile(r"^[A-Z0-9]+$")
_SLACK_TS = re.compile(r"^\d+\.\d+$")
_PARTIAL_ENTITY = re.compile(r"&[a-z]*$")
# One process runs the bot: parse, resolve and write of a reply happen under this lock.
_REPLY_LOCK = threading.Lock()
_ROUTE_LINE = re.compile(r"^(Route:.*)$", re.MULTILINE)
_IDEA_LINK = re.compile(r"\[\[([^\]|]+)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s|\n")

log = logging.getLogger("omniscient.grilling")


class GrillingError(Exception):
    """The griller's answer is unusable, or a grilling ticket cannot be handled."""


class SlackWeb(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...


def _clip(text: str, limit: int) -> str:
    """One line (whitespace collapsed), at most limit characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _one_line(limit: int) -> BeforeValidator:
    return BeforeValidator(lambda v: _clip(v, limit) if isinstance(v, str) else v)


class _Question(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    title: Annotated[str, _one_line(TITLE_MAX_CHARS), Field(min_length=2)]
    question: Annotated[str, _one_line(FIELD_MAX_CHARS), Field(min_length=5)]
    recommended: Annotated[str, _one_line(FIELD_MAX_CHARS), Field(min_length=2)]


class _Round(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    questions: tuple[_Question, ...] = Field(min_length=1, max_length=5)


@dataclass(frozen=True)
class Question:
    title: str
    question: str
    recommended: str


@dataclass(frozen=True)
class GrillingRound:
    ticket: Ticket
    map_title: str
    questions: tuple[Question, ...]
    run_id: str
    cost_usd: float


@dataclass(frozen=True)
class GrillingReply:
    ticket_path: Path
    gist: str
    text: str


# --- prompt ---------------------------------------------------------------------


def _header(text: str) -> str:
    """The ticket's metadata block (above `## Question`): the only place state lives."""
    return text.split(f"\n{QUESTION_HEADING}", 1)[0]


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    return match.group(1).strip() if match else ""


def _map_title(map_text: str) -> str:
    return next(
        (line[2:].strip() for line in map_text.splitlines() if line.startswith("# ")),
        "",
    )


def _idea_note(vault: Path, map_meta: dict[str, Any]) -> str:
    link = _IDEA_LINK.search(str(map_meta.get("idea") or ""))
    if link is None:
        return NO_IDEA
    ideas_dir = (vault / "ideas").resolve()
    path = (ideas_dir / f"{link.group(1).strip()}.md").resolve()
    if path.parent != ideas_dir or not path.is_file():
        return NO_IDEA
    return frontmatter.load(path).content.strip() or NO_IDEA


def _blocker_answers(ticket: Ticket) -> str:
    issues = ticket.path.parent
    answers = []
    for number in ticket.blocked_by:
        for path in sorted(issues.glob(f"{number:02d}-*.md")):
            answer = _section(path.read_text(encoding="utf-8"), "## Answer")
            if answer:
                answers.append(f"### {path.stem}\n{answer}")
    return "\n\n".join(answers) or i18n.NONE


def build_prompt(ticket: Ticket, vault: Path) -> str:
    """The griller's input: the ticket, the map's frame, the idea, earlier decisions."""
    post = frontmatter.load(ticket.map_path)
    decided = _section(post.content, "## Decisions so far") or i18n.NONE
    return "\n\n".join(
        (
            f"# Ticket de grilling : {ticket.title}\n{ticket.question}",
            f"# Carte : {_map_title(post.content)}",
            f"## Destination\n{_section(post.content, '## Destination') or i18n.NONE}",
            f"## Notes\n{_section(post.content, '## Notes') or i18n.NONE}",
            f"## Décisions déjà prises\n{decided}",
            f"# Note d'idée source\n{_idea_note(vault, dict(post.metadata))}",
            f"# Réponses des tickets bloquants\n{_blocker_answers(ticket)}",
        )
    )


def prepare_round(client: AgentRunClient, ticket: Ticket, vault: Path) -> GrillingRound:
    """Ask the griller for this ticket's round of questions (1 to 5, in French)."""
    run_id, answer, cost = run_once(
        client, GRILLER_ID, build_prompt(ticket, vault), GRILLER_CAP_USD
    )
    try:
        parsed = _Round.model_validate(extract_json(answer))
    except (AnswerNotJson, ValidationError) as exc:
        raise GrillingError(f"{ticket.path.name}: bad griller answer: {exc}") from exc
    map_text = frontmatter.load(ticket.map_path).content
    return GrillingRound(
        ticket=ticket,
        map_title=_map_title(map_text),
        questions=tuple(
            Question(q.title, q.question, q.recommended) for q in parsed.questions
        ),
        run_id=run_id,
        cost_usd=cost,
    )


# --- rendering and posting --------------------------------------------------------


def render_question(number: int, question: Question, bold: str = "*") -> str:
    return (
        f"❓ {bold}Q{number} - {question.title}{bold} : {question.question}\n"
        f"➡️ {question.recommended}"
    )


def round_text(grilling: GrillingRound, bold: str = "*") -> str:
    return "\n\n".join(
        render_question(i, q, bold) for i, q in enumerate(grilling.questions, start=1)
    )


def slack_escape(text: str, limit: int = SLACK_TEXT_LIMIT) -> str:
    """Escape &, < and > (no model-made mentions like <!channel>), then bound it."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(escaped) <= limit:
        return escaped
    return _PARTIAL_ENTITY.sub("", escaped[:limit])


def round_blocks(grilling: GrillingRound) -> list[dict[str, Any]]:
    header = f"Grilling : {grilling.map_title or grilling.ticket.title}"
    intro = (
        f"*{slack_escape(grilling.ticket.title)}* · "
        f"{len(grilling.questions)} question(s)"
    )
    questions = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": slack_escape(render_question(i, q))},
        }
        for i, q in enumerate(grilling.questions, start=1)
    ]
    return [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": intro}},
        *questions,
        {"type": "context", "elements": [{"type": "mrkdwn", "text": REPLY_HINT}]},
    ]


def _record_post(grilling: GrillingRound, channel: str, ts: str) -> None:
    """Link the thread in the header and append the round, sanitized, to the body."""
    path = grilling.ticket.path
    text = path.read_text(encoding="utf-8")
    header = _header(text)
    linked = _ROUTE_LINE.sub(
        lambda m: f"{m.group(1)}\nSlack: {channel} {ts}", header, count=1
    )
    body = sanitize_untrusted(round_text(grilling, bold="**"))
    section = f"{ROUND_HEADING}\n{body}\n"
    full = f"{linked}{text[len(header) :]}"
    path.write_text(f"{full.rstrip()}\n\n{section}", encoding="utf-8")


def _post_one(
    client: AgentRunClient, web: SlackWeb, channel: str, vault: Path, claimed: Ticket
) -> GrillingRound:
    """Run, post and record one round; any failure (even an interrupt) releases."""
    try:
        grilling = prepare_round(client, claimed, vault)
        response = web.chat_postMessage(
            channel=channel,
            text=slack_escape(f"{grilling.ticket.title}\n\n{round_text(grilling)}"),
            blocks=round_blocks(grilling),
        )
        ts = str((response or {}).get("ts") or "")
        if not _SLACK_TS.match(ts):
            raise GrillingError(f"{claimed.path.name}: bad Slack message ts {ts!r}")
        _record_post(grilling, channel, ts)
    except BaseException:  # KeyboardInterrupt and SystemExit are re-raised too
        release(claimed)
        raise
    return grilling


def _pending(map_dir: Path) -> tuple[Ticket, ...]:
    return tuple(
        t
        for t in frontier(load_map(map_dir))
        if t.type == "grilling"
        and not _SLACK_LINE.search(_header(t.path.read_text(encoding="utf-8")))
    )


def _log_error(path: Path, exc: Exception) -> None:
    log.error("grilling failed for %s: %s", path, exc, exc_info=exc)


def post_rounds(
    client: AgentRunClient,
    web: SlackWeb,
    channel: str,
    vault: Path,
    budget_usd: float,
    on_error: Callable[[Path, Exception], None] = _log_error,
    on_cost: Callable[[GrillingRound], None] | None = None,
) -> tuple[Path, ...]:
    """Post one round per unposted frontier grilling ticket, within budget_usd.

    A failed round releases its ticket (back to the frontier) and is reported through
    on_error; the other tickets still get their round.
    """
    if budget_usd < 0:
        raise ValueError("budget_usd must be >= 0")
    if not _CHANNEL_ID.match(channel):
        raise ValueError(f"channel must be a Slack channel id, got {channel!r}")
    posted: list[Path] = []
    spent = 0.0
    for map_dir in all_maps(vault):
        try:
            pending = _pending(map_dir)
        except TicketError as exc:
            on_error(map_dir, exc)
            continue
        for ticket in pending:
            if spent + GRILLER_CAP_USD > budget_usd:
                return tuple(posted)
            try:  # re-read: another worker may have claimed it since _pending
                claimed = claim(parse_ticket(ticket.path))
            except TicketError as exc:
                on_error(ticket.path, exc)
                continue  # lost the claim race: no run started, nothing spent
            try:
                grilling = _post_one(client, web, channel, vault, claimed)
            # Any failure is reported and its ticket released; other tickets still run.
            except Exception as exc:  # noqa: BLE001
                on_error(ticket.path, exc)
                spent += GRILLER_CAP_USD  # a failed run may still have spent its cap
                continue
            spent += grilling.cost_usd
            posted.append(ticket.path)
            if on_cost is not None:
                on_cost(grilling)
    return tuple(posted)


# --- owner replies ------------------------------------------------------------------


def _thread_of(path: Path) -> tuple[str, str] | None:
    """The (channel, ts) recorded in the ticket's header, if any."""
    match = _SLACK_LINE.search(_header(path.read_text(encoding="utf-8")))
    return (match.group(1), match.group(2)) if match else None


def find_grilling_by_thread(
    vault: Path, thread_ts: str, *, channel: str
) -> Path | None:
    """The claimed grilling ticket whose round was posted as (channel, thread_ts)."""
    if not thread_ts.strip() or not channel.strip():
        return None
    for map_dir in all_maps(vault):
        for path in sorted((map_dir / "issues").glob("*.md")):
            if _thread_of(path) != (channel, thread_ts):
                continue
            ticket = parse_ticket(path)
            if ticket.type == "grilling" and ticket.status == "claimed":
                return path
    return None


def gist_of(text: str) -> str:
    """One deterministic line from the owner's reply: a bare 'oui' accepts everything."""
    words = text.strip().lower().strip(" .!…")
    if words in _ACCEPT_WORDS:
        return ACCEPTED_GIST
    return _clip(_SENTENCE_END.split(text.strip(), maxsplit=1)[0], GIST_MAX_CHARS)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.strip().splitlines())


def handle_grilling_reply(
    vault: Path, thread_ts: str, text: str, author_is_owner: bool, *, channel: str
) -> GrillingReply | None:
    """Resolve the thread's grilling ticket with the owner's reply, quoted.

    Returns None when the thread is not a claimed grilling round in this channel, the
    author is not the owner, or the reply is blank (never resolve twice: rule 9).
    """
    if not author_is_owner or not text.strip():
        return None
    with _REPLY_LOCK:
        path = find_grilling_by_thread(vault, thread_ts, channel=channel)
        if path is None:
            return None
        ticket = parse_ticket(path)
        questions = _section(path.read_text(encoding="utf-8"), ROUND_HEADING)
        answer = f"{questions}\n\n{i18n.L_OWNER}\n{_quote(text)}".strip()
        gist = gist_of(text)
        resolve(ticket, answer, gist)
    return GrillingReply(
        ticket_path=path,
        gist=gist,
        text=slack_escape(f"✅ Décision enregistrée pour *{ticket.title}* : {gist}"),
    )
