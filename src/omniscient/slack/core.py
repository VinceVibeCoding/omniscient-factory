"""Slack logic, kept free of slack-bolt so it is unit-testable.

Each new idea is posted as its own thread root with Build / Pursue / Reshape / Drop buttons; its
message timestamp is stored in the idea note, so a reply in that thread finds the idea.
Replies are recorded in the note's Discussion and answered by the debater agent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import frontmatter

from omniscient import i18n
from omniscient.brain.notes import find_news
from omniscient.envfile import parse_env
from omniscient.runs import AgentRunClient, run_once

DEBATER_ID = "debater"
DEBATER_CAP_USD = 0.25
OWNER_VERDICTS = ("pursue", "reshape", "drop")
SLACK_TEXT_LIMIT = 2900
PITCH_LIMIT = 400
MESSAGE_GONE = "message_not_found"
# Accept the French labels and the English ones written before 2026-11-12.
_VERDICT = re.compile(r"\*\*Verdict ?:\*\*\s*(\w+)")
_SMALLEST = re.compile(
    r"\*\*(?:Simplificateur \(plus petite version\) ?|Simplifier \(smallest version\)):\*\*\s*(.+)"
)


class SlackWeb(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...


class SlackWebUpdate(SlackWeb, Protocol):
    def chat_update(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Reply:
    text: str
    cost_usd: float


def _ideas(vault: Path) -> list[Path]:
    return sorted((vault / "ideas").glob("*.md"))


def _update_meta(path: Path, **fields: Any) -> None:
    post = frontmatter.load(path)
    for key, value in fields.items():
        post[key] = value
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def _first_paragraph(content: str) -> str:
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    body = [p for p in paragraphs if not p.startswith("#")]
    return body[0] if body else ""


def idea_blocks(path: Path) -> list[dict[str, Any]]:
    post = frontmatter.load(path)
    verdict = _VERDICT.search(post.content)
    smallest = _SMALLEST.search(post.content)
    projects = ", ".join(post.get("projects", [])) or i18n.NEW_BUILD
    buttons = [
        ("Lancer le PoC", "idea_build", "primary"),
        ("Poursuivre", "idea_pursue", None),
        ("Remodeler", "idea_reshape", None),
        ("Abandonner", "idea_drop", "danger"),
    ]
    elements = []
    for label, action_id, style in buttons:
        button: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": action_id,
            "value": path.stem,
        }
        if style:
            button["style"] = style
        elements.append(button)
    kind = i18n.KIND_FR.get(str(post.get("kind", "new-build")), "Idée")
    poc = smallest.group(1).strip() if smallest else "n/d"
    pitch = _first_sentence(_first_paragraph(post.content))
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": str(post["title"])[:150]},
        },
        _mrkdwn(_source_line(path, post.get("sources", []))),
        _mrkdwn(
            f":test_tube: *Projet PoC :* {poc}\n:dart: *Projet ciblé :* {projects}"
        ),
        _mrkdwn(f"*L'idée :* {pitch}"),
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{kind} · Score {float(post.get('score', 0)):.2f} · Conseil : "
                    f"{verdict.group(1) if verdict else 'n/d'} · "
                    "Réponds dans ce fil pour la challenger.",
                }
            ],
        },
        {"type": "actions", "elements": elements},
    ]


def _mrkdwn(text: str) -> dict[str, Any]:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text[:SLACK_TEXT_LIMIT]},
    }


def _first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(\s|$)", text, re.DOTALL)
    sentence = match.group(1) if match else text
    return sentence[:PITCH_LIMIT]


def _source_line(idea_path: Path, source_ids: list[str]) -> str:
    """Link each source article; the vault is two levels above the idea note."""
    vault = idea_path.parent.parent
    links = []
    for item_id in source_ids:
        news = find_news(vault, str(item_id))
        if news is None:
            continue
        meta = frontmatter.load(news).metadata
        title = str(meta.get("title", "article")).replace("|", "-").replace(">", "")
        links.append(f"<{meta.get('url')}|{title}>")
    label = "Source" if len(links) <= 1 else "Sources"
    return f":newspaper: *{label} :* " + (" · ".join(links) if links else "n/d")


def refresh_posts(web: SlackWebUpdate, vault: Path) -> tuple[Path, ...]:
    """Rewrite every posted idea's Slack message in place from its current note."""
    refreshed: list[Path] = []
    for path in _ideas(vault):
        meta = frontmatter.load(path).metadata
        if not meta.get("slack_ts") or not meta.get("slack_channel"):
            continue
        try:
            web.chat_update(
                channel=str(meta["slack_channel"]),
                ts=str(meta["slack_ts"]),
                text=str(meta.get("title")),
                blocks=idea_blocks(path),
            )
        except Exception as exc:
            response = getattr(exc, "response", None) or {}
            if response.get("error") != MESSAGE_GONE:
                raise
            _unlink_slack(path)  # the post was deleted: the next digest reposts it
            continue
        refreshed.append(path)
    return tuple(refreshed)


def _unlink_slack(path: Path) -> None:
    post = frontmatter.load(path)
    for key in ("slack_ts", "slack_channel"):
        post.metadata.pop(key, None)
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def post_digest(web: SlackWeb, channel: str, vault: Path) -> tuple[Path, ...]:
    """Post every debated idea not yet in Slack; remember each idea's thread."""
    new = [
        p
        for p in _ideas(vault)
        if frontmatter.load(p).get("status") == "debated"
        and not frontmatter.load(p).get("slack_ts")
    ]
    if not new:
        return ()
    web.chat_postMessage(
        channel=channel,
        text=f":bulb: {len(new)} nouvelle(s) idée(s) d'Omniscient. Challenge-les dans leurs fils.",
    )
    for path in new:
        title = str(frontmatter.load(path).get("title"))
        response = web.chat_postMessage(
            channel=channel, text=title, blocks=idea_blocks(path)
        )
        _update_meta(path, slack_ts=response["ts"], slack_channel=channel)
    return tuple(new)


def _idea_path(vault: Path, stem: str) -> Path:
    ideas_dir = (vault / "ideas").resolve()
    path = (ideas_dir / f"{stem}.md").resolve()
    if path.parent != ideas_dir or not path.is_file():
        raise ValueError(f"unknown idea: {stem!r}")
    return path


def apply_owner_verdict(vault: Path, stem: str, verdict: str) -> Path:
    """Record the owner's button press. Drop ends the idea; pursue bypasses the threshold."""
    if verdict not in OWNER_VERDICTS:
        raise ValueError(f"verdict must be one of {OWNER_VERDICTS}")
    path = _idea_path(vault, stem)
    fields: dict[str, Any] = {"owner_verdict": verdict}
    if verdict == "drop":
        fields["status"] = "dropped"
    _update_meta(path, **fields)
    return path


def find_idea_by_thread(vault: Path, thread_ts: str) -> Path | None:
    for path in _ideas(vault):
        if str(frontmatter.load(path).get("slack_ts")) == thread_ts:
            return path
    return None


def _append_discussion(path: Path, line: str) -> None:
    text = path.read_text(encoding="utf-8").rstrip("\n")
    if f"\n{i18n.H_DISCUSSION}" not in text:
        text += f"\n\n{i18n.H_DISCUSSION}"
    path.write_text(f"{text}\n\n{line}\n", encoding="utf-8")


def handle_thread_reply(
    client: AgentRunClient, vault: Path, thread_ts: str, text: str
) -> Reply | None:
    """Record the owner's message on the idea and get the debater's answer."""
    path = find_idea_by_thread(vault, thread_ts)
    if path is None:
        return None
    _append_discussion(path, f"{i18n.L_OWNER} {text.strip()}")
    prompt = (
        f"{path.read_text(encoding='utf-8')}\n\n---\nLe propriétaire vient d'écrire dans le "
        f"fil de cette idée :\n\n{text.strip()}\n\nRéponds-lui en français."
    )
    _, answer, cost = run_once(client, DEBATER_ID, prompt, DEBATER_CAP_USD)
    reply = answer.strip()[:SLACK_TEXT_LIMIT]
    _append_discussion(path, f"{i18n.L_DEBATER} {reply}")
    return Reply(text=reply, cost_usd=cost)


def load_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines (omniscient.envfile dialect); process environment wins."""
    parsed = parse_env(path.read_text(encoding="utf-8")) if path.is_file() else {}
    values = {key: value for key, value in parsed.items() if value}
    return {key: os.environ.get(key, value) for key, value in values.items()}
