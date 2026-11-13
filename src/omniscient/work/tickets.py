"""Shared contract for phase 2: wayfinder tickets in the vault's local-markdown tracker.

Format (docs/agents/issue-tracker.md): vault/maps/<effort>/map.md plus
vault/maps/<effort>/issues/NN-<slug>.md, each ticket with `Type:`, `Status:`,
optional `Blocked by: NN, NN`, `Effort:`, `Intelligence:`, `Route:` lines and a
`## Question` section. Resolving appends `## Answer`, sets `Status: resolved`, and
adds a context pointer to the map's `## Decisions so far`.

Both the AFK ticket engine and Slack grilling build on this module only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

TicketType = Literal["research", "prototype", "grilling", "task"]
TicketStatus = Literal["open", "claimed", "resolved"]
TICKET_TYPES = ("research", "prototype", "grilling", "task")
TICKET_STATUSES = ("open", "claimed", "resolved")
AFK_TYPES = frozenset({"research", "task"})
DECISIONS_HEADING = "## Decisions so far"
_LINE = re.compile(
    r"^(Type|Status|Blocked by|Effort|Intelligence|Route):\s*(.*)$", re.MULTILINE
)
_ROUTE = re.compile(
    r"^(?P<role>\S+) · (?P<model>\S+) · (?P<effort>\S+)(?P<hitl>.*HITL.*)?$"
)
_NUMBER = re.compile(r"^(\d{2})-")
QUESTION_HEADING = "## Question"
GIST_MAX_CHARS = 200
# Lines with structural meaning in a ticket or map; untrusted text must never start one.
_CONTROL_LINE = re.compile(
    r"^\s*(#|>|(Type|Status|Blocked by|Effort|Intelligence|Route|Slack)\s*:)",
    re.IGNORECASE,
)


def sanitize_untrusted(text: str) -> str:
    """Quote every line of model or Slack text that could pass for a control line."""
    return "\n".join(
        f"> {line}" if _CONTROL_LINE.match(line) else line
        for line in text.strip().splitlines()
    )


def _header(text: str) -> str:
    """The ticket's metadata block: everything before `## Question`."""
    return text.split(f"\n{QUESTION_HEADING}", 1)[0]


class TicketError(Exception):
    """A ticket or map file is malformed, or an operation is not allowed."""


@dataclass(frozen=True)
class Route:
    role: str
    model: str  # AgentRun form: provider/model-id
    effort: str
    hitl: bool


@dataclass(frozen=True)
class Ticket:
    path: Path
    number: int
    title: str
    type: TicketType
    status: TicketStatus
    blocked_by: tuple[int, ...]
    effort: str
    intelligence: str
    route: Route
    question: str

    @property
    def map_path(self) -> Path:
        return self.path.parent.parent / "map.md"

    @property
    def is_afk(self) -> bool:
        """Resolvable by an agent alone (research, task) and not routed as HITL."""
        return self.type in AFK_TYPES and not self.route.hitl


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    return match.group(1).strip() if match else ""


def parse_ticket(path: Path) -> Ticket:
    text = path.read_text(encoding="utf-8")
    fields = dict(_LINE.findall(_header(text)))  # body text can never change state
    number = _NUMBER.match(path.name)
    title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "")
    route = _ROUTE.match(fields.get("Route", "").strip())
    problems = [
        name
        for name, ok in (
            ("NN- filename", number),
            ("# title", title),
            ("Type", fields.get("Type") in TICKET_TYPES),
            ("Status", fields.get("Status") in TICKET_STATUSES),
            ("Route", route),
            ("## Question", _section(text, "## Question")),
        )
        if not ok
    ]
    if problems:
        raise TicketError(f"{path.name}: missing or invalid {', '.join(problems)}")
    assert number is not None and route is not None  # for the type checker
    blocked = fields.get("Blocked by", "").strip()
    return Ticket(
        path=path,
        number=int(number.group(1)),
        title=title,
        type=fields["Type"],  # type: ignore[arg-type]
        status=fields["Status"],  # type: ignore[arg-type]
        blocked_by=tuple(int(b) for b in re.findall(r"\d+", blocked)),
        effort=fields.get("Effort", "").strip(),
        intelligence=fields.get("Intelligence", "").strip(),
        route=Route(
            role=route.group("role"),
            model=route.group("model"),
            effort=route.group("effort"),
            hitl=bool(route.group("hitl")),
        ),
        question=_section(text, "## Question"),
    )


def load_map(map_dir: Path) -> tuple[Ticket, ...]:
    """Every ticket of one map, by number."""
    if not (map_dir / "map.md").is_file():
        raise TicketError(f"not a map directory: {map_dir}")
    tickets = [parse_ticket(p) for p in (map_dir / "issues").glob("*.md")]
    return tuple(sorted(tickets, key=lambda t: t.number))


def all_maps(vault: Path) -> tuple[Path, ...]:
    return tuple(sorted(p.parent for p in (vault / "maps").glob("*/map.md")))


def frontier(tickets: tuple[Ticket, ...]) -> tuple[Ticket, ...]:
    """Open tickets whose blockers are all resolved, lowest number first."""
    resolved = {t.number for t in tickets if t.status == "resolved"}
    return tuple(
        t for t in tickets if t.status == "open" and set(t.blocked_by) <= resolved
    )


def _set_status(path: Path, status: TicketStatus) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r"^Status:.*$", f"Status: {status}", text, count=1, flags=re.MULTILINE)
    )


def claim(ticket: Ticket) -> Ticket:
    """Mark an open ticket claimed (before any work), so concurrent workers skip it."""
    if ticket.status != "open":
        raise TicketError(f"{ticket.path.name}: cannot claim a {ticket.status} ticket")
    _set_status(ticket.path, "claimed")
    return replace(ticket, status="claimed")


def release(ticket: Ticket) -> Ticket:
    """Give a claimed ticket back (e.g. its run failed), so it returns to the frontier."""
    if ticket.status != "claimed":
        raise TicketError(f"{ticket.path.name}: only a claimed ticket can be released")
    _set_status(ticket.path, "open")
    return replace(ticket, status="open")


def resolve(ticket: Ticket, answer: str, gist: str) -> Ticket:
    """Record the answer, set resolved, and add a context pointer to the map."""
    if ticket.status != "claimed":
        raise TicketError(f"{ticket.path.name}: claim a ticket before resolving it")
    if not answer.strip() or not gist.strip():
        raise TicketError(f"{ticket.path.name}: answer and gist are required")
    map_text = ticket.map_path.read_text(encoding="utf-8")
    if DECISIONS_HEADING not in map_text:  # check before writing anything
        raise TicketError(f"{ticket.map_path}: no '{DECISIONS_HEADING}' section")
    text = ticket.path.read_text(encoding="utf-8").rstrip("\n")
    safe_answer = sanitize_untrusted(answer)
    ticket.path.write_text(f"{text}\n\n## Answer\n{safe_answer}\n", encoding="utf-8")
    _set_status(ticket.path, "resolved")
    short = (
        " ".join(gist.split())[:GIST_MAX_CHARS].replace("[[", "[").replace("]]", "]")
    )
    pointer = f"- [[{ticket.path.stem}|{ticket.title}]] : {short}"
    head, tail = map_text.split(DECISIONS_HEADING, 1)
    next_heading = tail.find("\n## ")
    body, rest = (
        (tail, "") if next_heading == -1 else (tail[:next_heading], tail[next_heading:])
    )
    ticket.map_path.write_text(
        f"{head}{DECISIONS_HEADING}{body.rstrip()}\n{pointer}\n{rest}", encoding="utf-8"
    )
    return replace(ticket, status="resolved")
