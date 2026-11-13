"""The idea's « À faire » checklist and the compact to-do pane of an atelier.

Items live in the note's `## À faire` section as `- [ ] texte` / `- [x] texte`,
numbered 1.. in display order. Other lines of the section are kept as they are.
Writes go through the ideas helpers so the note keeps one valid shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from omniscient import i18n
from omniscient.studio.ideas import (
    H_ATELIER,
    StudioError,
    _section,
    _set_section,
    _update,
    load_card,
    resolve,
    safe_text,
)
from omniscient.studio.screens import STATUS_FR
from omniscient.theme.apply import current_look

H_TODO = "## À faire"
MAX_TODO_CHARS = 200
ATELIER_SHOWN = 3
_ITEM = re.compile(r"^- \[([ xX])\] (.*)$")
_STAMP = re.compile(r"^\*\*\d{2}-\d{2}-\d{4} \d{2}:\d{2}\*\*[ \t]*$", re.MULTILINE)
_TITLE = re.compile(r"^# .*$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^## ", re.MULTILINE)


@dataclass(frozen=True)
class TodoItem:
    """One checklist entry; index is 1-based, in display order."""

    index: int
    text: str
    done: bool


def _content(path: Path) -> str:
    try:
        return frontmatter.load(path).content
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise StudioError(f"note illisible {path.name} : {exc}") from exc


def _lines(content: str) -> list[str]:
    bounds = _section(content, H_TODO)
    return content[bounds[0] : bounds[1]].splitlines() if bounds else []


def _items(lines: list[str]) -> list[tuple[int, TodoItem]]:
    """(line position, item) for every checklist line of the section."""
    out: list[tuple[int, TodoItem]] = []
    for pos, line in enumerate(lines):
        match = _ITEM.match(line.rstrip())
        if match:
            item = TodoItem(len(out) + 1, match.group(2).strip(), match.group(1) != " ")
            out.append((pos, item))
    return out


def read_todos(path: Path) -> tuple[TodoItem, ...]:
    """The note's checklist (empty when the section is missing)."""
    return tuple(item for _, item in _items(_lines(_content(path))))


def clean_text(text: str) -> str:
    """One safe line of at most MAX_TODO_CHARS characters, or StudioError."""
    line = " ".join(safe_text(" ".join(text.split())).split())
    if not line:
        raise StudioError("le texte de la tâche est vide")
    if len(line) > MAX_TODO_CHARS:
        raise StudioError(f"tâche trop longue ({len(line)} > {MAX_TODO_CHARS} car.)")
    return line


def _line(text: str, done: bool) -> str:
    return f"- [{'x' if done else ' '}] {text}"


def _write(path: Path, content: str, lines: list[str]) -> None:
    _update(path, _set_section(content, H_TODO, "\n".join(lines)))


def _locate(lines: list[str], index: int) -> tuple[int, TodoItem]:
    items = _items(lines)
    if not 1 <= index <= len(items):
        span = f"1..{len(items)}" if items else "aucune tâche"
        raise StudioError(f"tâche n°{index} introuvable ({span})")
    return items[index - 1]


def add_todo(vault: Path, stem: str, text: str) -> TodoItem:
    """Append an open item at the end of the checklist (section created if missing)."""
    line = clean_text(text)
    path = resolve(vault, stem)
    content = _content(path)
    lines = [*_lines(content), _line(line, False)]
    _write(path, content, lines)
    return TodoItem(len(_items(lines)), line, False)


def set_done(vault: Path, stem: str, index: int, done: bool = True) -> TodoItem:
    """Tick (or untick) item number `index`."""
    path = resolve(vault, stem)
    content = _content(path)
    lines = _lines(content)
    pos, item = _locate(lines, index)
    updated = TodoItem(item.index, item.text, done)
    if item.done != done:
        new_lines = [*lines[:pos], _line(item.text, done), *lines[pos + 1 :]]
        _write(path, content, new_lines)
    return updated


def remove_todo(vault: Path, stem: str, index: int) -> None:
    """Delete item number `index`; the following items are renumbered."""
    path = resolve(vault, stem)
    content = _content(path)
    lines = _lines(content)
    pos, _ = _locate(lines, index)
    _write(path, content, [*lines[:pos], *lines[pos + 1 :]])


# --- the to-do pane (`idea show`) ---------------------------------------------


def _body(content: str, heading: str) -> str:
    bounds = _section(content, heading)
    return content[bounds[0] : bounds[1]].strip() if bounds else ""


def pitch(content: str) -> str:
    """The text between the '# title' line and the first '## ' section."""
    title = _TITLE.search(content)
    start = title.end() if title else 0
    nxt = _NEXT_HEADING.search(content, start)
    return content[start : nxt.start() if nxt else len(content)].strip()


def council_summary(content: str) -> str:
    """The council's verdict line and its Simplificateur paragraph."""
    paragraphs = re.split(r"\n\s*\n", _body(content, i18n.H_COUNCIL))
    keep = [
        p.strip()
        for p in paragraphs
        if p.strip().startswith((i18n.L_VERDICT, i18n.L_SIMPLIFIER))
    ]
    return "\n\n".join(keep)


def last_atelier(content: str, count: int = ATELIER_SHOWN) -> tuple[str, ...]:
    """The last `count` dated entries of '## Atelier', oldest first."""
    body = _body(content, H_ATELIER)
    starts = [m.start() for m in _STAMP.finditer(body)]
    if not starts:
        return (body,) if body else ()
    ends = [*starts[1:], len(body)]
    entries = [body[s:e].strip() for s, e in zip(starts, ends, strict=True)]
    return tuple(entries[-count:])


def _header(path: Path) -> Text:
    card = load_card(path)
    score = f"{card.score:.2f}" if card.score is not None else "—"
    header = Text(card.title, style="bold")
    header.append(
        f"  ·  {STATUS_FR.get(card.status, card.status)}  ·  score {score}",
        style=current_look().signal,
    )
    return header


def checklist(items: tuple[TodoItem, ...]) -> Text:
    """« À faire 2/5 » then one ☐ / ☑ line per item, done ones dimmed."""
    done = sum(item.done for item in items)
    text = Text(f"À faire  {done}/{len(items)}", style=current_look().heading)
    if not items:
        text.append("\n  (aucune tâche — idea todo-add)", style="dim")
    for item in items:
        mark = "☑" if item.done else "☐"
        style = "dim strike" if item.done else ""
        text.append(f"\n {item.index:>2}. {mark} {item.text}", style=style)
    return text


def _compact(content: str) -> str:
    parts = [pitch(content)]
    council = council_summary(content)
    if council:
        parts.append(f"{i18n.H_COUNCIL}\n\n{council}")
    entries = last_atelier(content)
    if entries:
        parts.append(f"{H_ATELIER}\n\n" + "\n\n".join(entries))
    return "\n\n".join(p for p in parts if p)


def render_pane(path: Path, full: bool = False) -> RenderableType:
    """The to-do pane: header, checklist, compact note — or the full note body."""
    content = _content(path)
    if full:  # the whole note body (no frontmatter), checklist section included
        return Markdown(content.strip())
    return Group(
        _header(path),
        Text(""),
        checklist(read_todos(path)),
        Text(""),
        Markdown(_compact(content)),
    )
