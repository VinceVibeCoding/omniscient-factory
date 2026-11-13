"""The researcher's run message: question, map context, idea, files, prior answers."""

from __future__ import annotations

import re
from pathlib import Path

from omniscient.work.attachments import Attachment, attachments_section, idea_note
from omniscient.work.tickets import Ticket

_OUTPUT_RULE = (
    "## Output\nAnswer with ONE raw JSON object and nothing else: "
    '{"answer": "<detailed answer in French markdown, citing source URLs>", '
    '"gist": "<one French line summarising the answer>"}'
)


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    return match.group(1).strip() if match else ""


def _idea_text(vault: Path, map_path: Path) -> str:
    link, note = idea_note(vault, map_path)
    if not link:
        return "(note d'idée introuvable : aucun lien `idea` dans la carte)"
    if note is None:
        return f"(note d'idée introuvable : {link})"
    return note.read_text(encoding="utf-8").strip()


def _blocker_answers(ticket: Ticket, siblings: tuple[Ticket, ...]) -> str:
    blockers = [s for s in siblings if s.number in ticket.blocked_by]
    parts = [
        f"### {b.number:02d} {b.title}\n"
        f"{_section(b.path.read_text(encoding='utf-8'), '## Answer')}"
        for b in blockers
    ]
    return "\n\n".join(parts) or "(aucun)"


def build_prompt(
    vault: Path,
    ticket: Ticket,
    siblings: tuple[Ticket, ...],
    attachments: tuple[Attachment, ...] = (),
) -> str:
    """The researcher's run message: question, map context, idea, files, prior answers."""
    map_text = ticket.map_path.read_text(encoding="utf-8")
    parts = (
        f"# Research ticket: {ticket.title}",
        f"## Question\n{ticket.question}",
        f"## Map destination\n{_section(map_text, '## Destination')}",
        f"## Map notes and constraints\n{_section(map_text, '## Notes')}",
        f"## Source idea\n{_idea_text(vault, ticket.map_path)}",
        attachments_section(attachments),
        "## Answers of the tickets this one was blocked by\n"
        + _blocker_answers(ticket, siblings),
        _OUTPUT_RULE,
    )
    return "\n\n".join(p for p in parts if p)
