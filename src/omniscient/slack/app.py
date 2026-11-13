"""slack-bolt wiring (Socket Mode). Handlers are plain functions so they are testable."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from omniscient.build.poc import BUILD_CAP_USD, BuildError, resolve_idea
from omniscient.runs import AgentRunClient
from omniscient.slack.core import Reply, apply_owner_verdict, handle_thread_reply
from omniscient.work.grilling import find_grilling_by_thread, handle_grilling_reply

log = logging.getLogger("omniscient.slack")

VERDICT_ACK = {
    "pursue": "a choisi *poursuivre* : l'idée sera planifiée au prochain cycle.",
    "reshape": "veut la *remodeler*. Réponds dans ce fil avec ce qui doit changer.",
    "drop": "a *abandonné* cette idée.",
}
HELP = (
    "Je publie ici les nouvelles idées. Utilise les boutons pour poursuivre, remodeler ou "
    "abandonner une idée, et réponds dans son fil pour la challenger : le débatteur du "
    "conseil te répondra."
)
DEBATER_FAILED = "Le débatteur n'a pas pu répondre (voir les journaux d'Omniscient)."
BUILD_OWNER_ONLY = "Seul le propriétaire peut lancer un prototype."
BUILD_STARTED = (
    ":hammer_and_wrench: Construction du prototype lancée "
    f"(environ 10 à 30 min, plafond {BUILD_CAP_USD:.2f} $)…"
)


def on_verdict(
    vault: Path, ack: Any, body: dict[str, Any], action: dict[str, Any], client: Any
) -> None:
    ack()
    verdict = action["action_id"].removeprefix("idea_")
    try:
        apply_owner_verdict(vault, action["value"], verdict)
    except ValueError as exc:
        log.warning("verdict ignored: %s", exc)
        return
    client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body["message"]["ts"],
        text=f"<@{body['user']['id']}> {VERDICT_ACK[verdict]}",
    )


def on_build(
    vault: Path,
    owner_id: str,
    start_build: Callable[[str, str, str], bool],
    ack: Any,
    body: dict[str, Any],
    action: dict[str, Any],
    client: Any,
) -> None:
    """«Lancer le PoC»: only the owner may start a build; it runs in the background."""
    ack()
    channel = body["channel"]["id"]
    thread_ts = body["message"]["ts"]
    if not owner_id or body["user"]["id"] != owner_id:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=BUILD_OWNER_ONLY
        )
        return
    try:
        resolve_idea(vault, action["value"])
    except BuildError as exc:
        log.warning("build ignored: %s", exc)
        return
    if start_build(action["value"], channel, thread_ts):
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=BUILD_STARTED
        )


def on_message(
    vault: Path,
    channel_id: str,
    agentrun: Callable[[], AgentRunClient],
    on_reply_cost: Callable[[Reply], None],
    event: dict[str, Any],
    client: Any,
    owner_id: str = "",
) -> None:
    thread_ts = event.get("thread_ts")
    if (
        event.get("channel") != channel_id
        or event.get("bot_id")
        or event.get("subtype")
        or not thread_ts
        or thread_ts == event.get("ts")
    ):
        return
    if find_grilling_by_thread(vault, thread_ts, channel=channel_id) is not None:
        # Grilling threads never reach the debater; only the owner can resolve them.
        is_owner = bool(owner_id) and event.get("user") == owner_id
        answer = handle_grilling_reply(
            vault, thread_ts, event.get("text", ""), is_owner, channel=channel_id
        )
        if answer is not None:
            client.chat_postMessage(
                channel=event["channel"], thread_ts=thread_ts, text=answer.text
            )
        return
    client.reactions_add(channel=event["channel"], timestamp=event["ts"], name="eyes")
    try:
        reply = handle_thread_reply(agentrun(), vault, thread_ts, event["text"])
    except Exception:  # report every failure in the thread, then log it with traceback
        log.exception("debater failed")
        client.chat_postMessage(
            channel=event["channel"], thread_ts=thread_ts, text=DEBATER_FAILED
        )
        return
    if reply is not None:
        client.chat_postMessage(
            channel=event["channel"], thread_ts=thread_ts, text=reply.text
        )
        on_reply_cost(reply)


def on_mention(event: dict[str, Any], client: Any) -> None:
    client.chat_postMessage(
        channel=event["channel"], thread_ts=event.get("ts"), text=HELP
    )


def build_app(
    bot_token: str,
    channel_id: str,
    vault: Path,
    agentrun: Callable[[], AgentRunClient],
    on_reply_cost: Callable[[Reply], None],
    owner_id: str = "",
    start_build: Callable[[str, str, str], bool] | None = None,
) -> Any:
    from slack_bolt import App

    app = App(token=bot_token)
    app.action(re.compile(r"^idea_(pursue|reshape|drop)$"))(partial(on_verdict, vault))
    if start_build is not None:
        app.action("idea_build")(partial(on_build, vault, owner_id, start_build))
    app.event("message")(
        partial(
            on_message, vault, channel_id, agentrun, on_reply_cost, owner_id=owner_id
        )
    )
    app.event("app_mention")(on_mention)
    return app
