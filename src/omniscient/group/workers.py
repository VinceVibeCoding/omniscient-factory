"""Background work of the omnigroup daemon: collectors and the janitor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from omniscient.home import Home

if TYPE_CHECKING:
    from omniscient.group.server import App

log = logging.getLogger(__name__)

JANITOR_S = 5.0
PRUNE_EVERY_S = 3600.0


def run_collector(collector: Any, app: App) -> None:
    try:
        collector.run(app.store, app.stopping)
    except Exception:
        log.exception("omnigroup collector %s stopped", collector.name)


def janitor(app: App) -> None:
    """Expire pendings whose hook gave up; prune old sessions at start, then hourly."""
    since_prune = PRUNE_EVERY_S
    while not app.stopping.is_set():
        try:
            app.store.expire_overdue()
            if since_prune >= PRUNE_EVERY_S:
                app.store.prune()
                since_prune = 0.0
        except Exception:
            log.exception("omnigroup janitor failed")
        app.stopping.wait(JANITOR_S)
        since_prune += JANITOR_S


def default_collectors(home: Home) -> tuple[Any, ...]:
    """herdr + AgentRun collectors (agent C's modules, imported lazily)."""
    from omniscient.group.collect_agentrun import AgentRunCollector
    from omniscient.group.collect_herdr import HerdrCollector

    return (HerdrCollector(), AgentRunCollector.for_home(home))
