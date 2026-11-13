"""Read-only widget routes that need no request data: usage totals and the theme."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omniscient.group.models import to_json

if TYPE_CHECKING:
    from omniscient.group.server import App

USAGE_SUMS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")  # fmt: skip


def usage_totals(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    """Token and cost totals across the visible sessions (for the widget's pill)."""
    usages = [s.usage for s in app.store.list_sessions() if s.usage is not None]
    totals: dict[str, Any] = {k: sum(getattr(u, k) for u in usages) for k in USAGE_SUMS}
    costs = [u.cost_usd for u in usages if u.cost_usd is not None]
    plans = [u for u in usages if u.plan_used_pct is not None]
    latest_plan = max(plans, key=lambda u: u.updated_at, default=None)
    totals.update(
        sessions=len(usages),
        cost_usd=round(sum(costs), 4) if costs else None,
        plan_used_pct=latest_plan.plan_used_pct if latest_plan else None,
        plan_resets_at=to_json(latest_plan)["plan_resets_at"] if latest_plan else None,
    )
    return 200, totals


def theme(app: App, ids: dict, query: dict, body: dict) -> tuple[int, dict]:
    from omniscient.theme import load_palette

    palette, warning = load_palette(app.home)
    return 200, {
        "name": palette.name,
        "label": palette.label,
        "dark": palette.dark,
        "colors": palette.colors(),
        "ansi": list(palette.ansi),
        "warning": warning,
    }
