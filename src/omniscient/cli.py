"""Omniscient command line: `uv run omniscient <command>`."""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import frontmatter
import typer
from agentrun import AgentRunError

from omniscient.agentrun_access import (
    missing_key_message,
    readiness_problem,
    resolve_api_key,
)
from omniscient.agents import AgentDefinitionError, load_definitions, push_definition
from omniscient.build.poc import (
    BUILD_CAP_USD,
    BUILDER_ID,
    BUILDER_MODEL,
    BuildError,
    BuildResult,
    build_poc,
    format_build_reply,
)
from omniscient.cli_onboarding import register_onboarding
from omniscient.config import ConfigError, Settings, load_settings
from omniscient.home import Home
from omniscient.ops import CostRow, append_cost
from omniscient.profile import Profile, ProfileError, load_profile
from omniscient.runs import CapReached, RunFailed
from omniscient.sense.scout import (
    AGENT_ID,
    AgentRunClient,
    ScoutFailed,
    ScoutFormatError,
    ingest_report,
    run_scout,
)
from omniscient.studio.cli import idea_app
from omniscient.studio.cli import studio as _studio
from omniscient.studio.cli_panes import chat as _chat
from omniscient.studio.cli_panes import warroom as _warroom
from omniscient.studio.paths import NoHomeError, home_path, require_home

AGENTRUN_BASE_URL = os.environ.get("AGENTRUN_BASE_URL", "http://127.0.0.1:7340/v2")
AGENTRUN_TOKEN_FILE = Path.home() / ".agentrun" / ".token"
SCOUT_MODEL = "anthropic/claude-haiku-5-5"
DEFAULT_SCOUT_CAP_USD = 1.0
WORK_BUDGET_USD = 1.00  # research tickets per `work` run (inside the daily cap)
GRILL_BUDGET_USD = 1.00  # grilling rounds per `grill` run
BUILD_BUDGET_USD = BUILD_CAP_USD  # one prototype build (inside the daily cap)
AGENTRUN_DATA = Path(os.environ.get("AGENTRUN_DATA", Path.home() / ".agentrun"))
BUILD_BUSY = "Un prototype est déjà en construction, réessaie plus tard."
BUILD_FAILED_REASON_LIMIT = 300
_BUILD_LOCK = threading.Lock()  # one build at a time: each holds a Docker box and $
HOME_FREE_COMMANDS = frozenset({"init"})  # commands that run before a home exists

# The user's data lives in a home (omniscient.home), resolved at command time, never
# at import: cli.VAULT, cli.REPOS, ... (studio.paths.HOME_PATHS) are computed on access;
# tests may monkeypatch them, and the commands read them through _path().


def __getattr__(name: str) -> Path:
    return home_path(__name__, name)


def _path(name: str) -> Path:
    """A home path: a monkeypatched module attribute if set, else resolved now."""
    patched = globals().get(name)
    return patched if isinstance(patched, Path) else __getattr__(name)


def _vault() -> Path:
    return _path("VAULT")


def _settings() -> Settings:
    try:
        return load_settings(_path("CONFIG"))
    except ConfigError as exc:
        typer.echo(f"configuration illisible : {exc}")
        raise typer.Exit(code=1) from exc


app = typer.Typer(no_args_is_help=True, help="Omniscient: the agent software factory.")
app.add_typer(idea_app, name="idea")
app.command("studio")(_studio)
app.command("warroom")(_warroom)
app.command("chat")(_chat)
register_onboarding(app)


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Omniscient: the agent software factory."""
    if ctx.invoked_subcommand in HOME_FREE_COMMANDS or ctx.resilient_parsing:
        return
    try:
        require_home()
    except NoHomeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def make_client() -> AgentRunClient:
    """The AgentRun client (key: env, home .env, then the server's token file).

    A missing key is a setup problem, not a usage error: one French line, exit 1,
    no usage banner and no traceback.
    """
    from agentrun import AgentRun

    api_key = resolve_api_key(_path("ENV_FILE"), AGENTRUN_TOKEN_FILE, os.environ)
    if api_key is None:
        typer.echo(missing_key_message(AGENTRUN_TOKEN_FILE), err=True)
        raise typer.Exit(code=1)
    return AgentRun(base_url=AGENTRUN_BASE_URL, api_key=api_key)


def _agentrun_problem() -> str | None:
    """Why AgentRun cannot be used right now (no key, server down), or None."""
    return readiness_problem(
        AGENTRUN_BASE_URL, _path("ENV_FILE"), AGENTRUN_TOKEN_FILE, os.environ
    )


def _definitions() -> tuple[dict[str, Any], ...]:
    try:
        return load_definitions(_path("AGENTS_DIR"))
    except AgentDefinitionError as exc:
        typer.echo(f"Définitions d'agents invalides : {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _profile() -> Profile:
    """The owner's profile (interests, language); defaults when the file is absent."""
    try:
        return load_profile(Home(_path("REPO")).profile)
    except ProfileError as exc:
        typer.echo(f"Profil invalide : {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("agents-push")
def agents_push() -> None:
    """Push agent definitions from agentrun/*.json to the AgentRun server."""
    from omniscient.prompting import render_instructions

    client = make_client()
    profile = _profile()
    for raw in _definitions():
        definition = {
            **raw["definition"],
            "instructions": render_instructions(
                raw["definition"].get("instructions", ""), profile
            ),
        }
        body = {**raw, "definition": definition}
        action = push_definition(client, body)  # type: ignore[arg-type]
        typer.echo(f"{definition['id']} v{definition['version']}: {action}")


@app.command("learn-sync")
def learn_sync() -> None:
    """Mirror every agent's kept AgentRun learnings into vault/tech/learned/."""
    from omniscient.learn.sync import sync_learnings

    agent_ids = tuple(b["definition"]["id"] for b in _definitions())
    result = sync_learnings(make_client(), agent_ids, _vault())  # type: ignore[arg-type]
    typer.echo(
        f"learnings: {len(result.written)} written, {len(result.unchanged)} unchanged, "
        f"{len(result.removed)} removed, {len(result.rejected)} rejected"
    )
    for name in result.rejected:
        typer.echo(f"  ! rejected unsafe filename: {name!r}")


@app.command()
def think() -> None:
    """Think loop: the visionary proposes ideas, the council debates and scores them."""
    from omniscient.registry import RegistryError, load_registry
    from omniscient.think.ideation import ThinkFailed, run_think

    settings = _settings()
    try:
        registry = load_registry(_path("REPOS"))
    except RegistryError as exc:
        typer.echo(f"Registre des projets invalide : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    now = datetime.now(UTC)
    try:
        result = run_think(
            make_client(),
            _vault(),
            projects=tuple(p.name for p in registry),
            weights=settings.scoring_weights,
            max_ideas=settings.gameplan.ideas_per_cycle,
            budget_usd=settings.budget.per_cycle_usd,
            now=now,
            project_purposes={p.name: p.purpose for p in registry},
            tracks=dict(settings.gameplan.tracks),
            profile=_profile(),
        )
    except (ThinkFailed, AgentRunError) as exc:
        typer.echo(f"think failed: {exc}")
        raise typer.Exit(code=1) from exc
    append_cost(
        _vault(),
        CostRow(
            at=now,
            task="think",
            role="visionary+council",
            model="anthropic/claude-opus-5-5",
            effort="high/medium",
            usd=result.cost_usd,
        ),
    )
    typer.echo(f"{len(result.written)} ideas written | cost: ${result.cost_usd:.4f}")
    for path in result.written:
        meta = frontmatter.load(path).metadata
        typer.echo(f"  + [{meta['score']:.2f}] {meta['status']:8} {meta['title']}")
    for reason in result.dropped:
        typer.echo(f"  - dropped: {reason}")


@app.command()
def plan() -> None:
    """Plan loop: promote top ideas, chart wayfinder maps, regenerate the game plan."""
    from omniscient.plan.strategist import PlanFailed, run_plan
    from omniscient.router import RoutingError, load_routing

    settings = _settings()
    now = datetime.now(UTC)
    try:
        routing = load_routing(_path("ROUTING"))
    except RoutingError as exc:
        typer.echo(f"Table de routage invalide : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        result = run_plan(
            make_client(),
            _vault(),
            routing,
            threshold=settings.gameplan.promote_threshold,
            wip_limit=settings.gameplan.wip_limit,
            budget_usd=settings.budget.per_cycle_usd,
            now=now,
        )
    except (PlanFailed, AgentRunError) as exc:
        typer.echo(f"plan failed: {exc}")
        raise typer.Exit(code=1) from exc
    if result.cost_usd:
        append_cost(
            _vault(),
            CostRow(
                at=now,
                task="plan",
                role="strategist",
                model="anthropic/claude-opus-5-5",
                effort="high",
                usd=result.cost_usd,
            ),
        )
    typer.echo(
        f"{len(result.promoted)} promoted | {len(result.maps)} maps | "
        f"cost: ${result.cost_usd:.4f}"
    )
    for path in result.maps:
        typer.echo(f"  + {path.relative_to(_vault())}")
    for reason in result.failed:
        typer.echo(f"  ! {reason}")


def _slack_env() -> dict[str, str]:
    from omniscient.slack.core import load_env_file

    env = load_env_file(_path("ENV_FILE"))
    missing = [
        k
        for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL_ID")
        if k not in env
    ]
    if missing:
        typer.echo(f"missing in .env: {', '.join(missing)} (see slack/manifest.yaml)")
        raise typer.Exit(code=2)
    return env


@app.command("slack-digest")
def slack_digest() -> None:
    """Post every new debated idea to Slack as its own thread, with verdict buttons."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    from omniscient.slack import core

    env = _slack_env()
    try:
        posted = core.post_digest(
            WebClient(token=env["SLACK_BOT_TOKEN"]), env["SLACK_CHANNEL_ID"], _vault()
        )
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        hint = SLACK_HINTS.get(str(error), "")
        typer.echo(f"slack-digest failed: {error}{' | ' + hint if hint else ''}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"posted {len(posted)} idea(s) to Slack")


@app.command("slack-refresh")
def slack_refresh() -> None:
    """Rewrite every posted idea's Slack message from its current note (after edits)."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    from omniscient.slack import core

    env = _slack_env()
    try:
        done = core.refresh_posts(WebClient(token=env["SLACK_BOT_TOKEN"]), _vault())
    except SlackApiError as exc:
        error = exc.response.get("error") if exc.response else str(exc)
        typer.echo(f"slack-refresh failed: {error}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"refreshed {len(done)} Slack post(s)")


SLACK_HINTS = {
    "channel_not_found": (
        "check SLACK_CHANNEL_ID (channel details > bottom, starts with C) "
        "and run /invite @omniscient in that channel"
    ),
    "not_in_channel": "run /invite @omniscient in the channel",
    "invalid_auth": "SLACK_BOT_TOKEN is wrong: use the Bot User OAuth Token (xoxb-...)",
}


@app.command()
def slack() -> None:
    """Run the Slack bot (Socket Mode): buttons and thread debates. Blocks until stopped."""
    import logging

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from omniscient.slack.app import build_app
    from omniscient.slack.core import DEBATER_ID, Reply

    logging.basicConfig(level=logging.INFO)
    env = _slack_env()

    def log_cost(reply: Reply) -> None:
        append_cost(
            _vault(),
            CostRow(
                at=datetime.now(UTC),
                task="slack reply",
                role=DEBATER_ID,
                model="anthropic/claude-opus-5-5",
                effort="medium",
                usd=reply.cost_usd,
            ),
        )

    def start_build(stem: str, channel: str, thread_ts: str) -> bool:
        return _launch_build(bolt.client, stem, channel, thread_ts)

    bolt = build_app(
        env["SLACK_BOT_TOKEN"],
        env["SLACK_CHANNEL_ID"],
        _vault(),
        make_client,
        log_cost,
        owner_id=env.get("SLACK_OWNER_ID", ""),
        start_build=start_build,
    )
    typer.echo("Omniscient Slack bot running (Socket Mode). Ctrl-C to stop.")
    SocketModeHandler(bolt, env["SLACK_APP_TOKEN"]).start()


class BuildRefused(Exception):
    """A build would push today's spend over the daily budget."""


def _log_build_cost(stem: str, usd: float) -> None:
    append_cost(
        _vault(),
        CostRow(
            at=datetime.now(UTC),
            task=f"build {stem}",
            role=BUILDER_ID,
            model=BUILDER_MODEL,
            effort="medium",
            usd=usd,
        ),
    )


def _run_build(stem: str) -> BuildResult:
    """Build one idea's prototype inside the daily budget, and log what it cost."""
    daily = _settings().budget.daily_usd
    spent = _spent_today()
    if spent + BUILD_BUDGET_USD > daily:
        raise BuildRefused(
            f"daily budget has ${max(daily - spent, 0):.2f} left, "
            f"a build can cost up to ${BUILD_BUDGET_USD:.2f}"
        )
    try:
        result = build_poc(make_client(), _vault(), stem, _path("POCS"), AGENTRUN_DATA)
    except CapReached as exc:
        if exc.spent_usd is not None:
            _log_build_cost(stem, exc.spent_usd)
        raise
    except BuildError as exc:
        if exc.cost_usd is not None:  # the run finished, then harvesting failed
            _log_build_cost(stem, exc.cost_usd)
        raise
    _log_build_cost(stem, result.cost_usd)
    return _publish(result)


def _publish(result: BuildResult) -> BuildResult:
    """Put the prototype's web demo online; a publish failure never fails the build."""
    from dataclasses import replace

    from omniscient.build.poc import poc_slug
    from omniscient.build.publish import PublishError, publish_web_demo, record_url

    site = _settings().publish
    if site.site_url is None:  # no [publish] site_url: publishing is disabled
        return result
    try:
        url = publish_web_demo(
            result.poc_dir,
            poc_slug(result.idea_path),
            _path("SITE_CLONE"),
            site.site_url,
        )
    except PublishError as exc:
        logging.getLogger("omniscient.build").warning("not published: %s", exc)
        return result
    record_url(result.idea_path, url)
    return replace(result, url=url)


def _launch_build(web: Any, stem: str, channel: str, thread_ts: str) -> bool:
    """Start a build in a daemon thread and report it in the idea's thread when done.

    Returns False (after saying so in the thread) when another build is running.
    """
    if not _BUILD_LOCK.acquire(blocking=False):
        web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=BUILD_BUSY)
        return False

    def job() -> None:
        try:
            text = format_build_reply(_run_build(stem))
        except Exception as exc:  # report every failure in the thread, then log it
            logging.getLogger("omniscient.build").exception("build %s failed", stem)
            reason = str(exc)[:BUILD_FAILED_REASON_LIMIT] or type(exc).__name__
            text = f":x: Le prototype a échoué : {reason}"
        finally:
            _BUILD_LOCK.release()
        web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

    threading.Thread(target=job, name=f"build {stem}", daemon=True).start()
    return True


@app.command()
def build(idea_stem: str) -> None:
    """Build loop: turn one idea's smallest version into a prototype under pocs/."""
    try:
        result = _run_build(idea_stem)
    except BuildRefused as exc:
        typer.echo(f"build refused: {exc}")
        raise typer.Exit(code=2) from exc
    except (BuildError, RunFailed, AgentRunError) as exc:
        typer.echo(f"build failed: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(format_build_reply(result))


@app.command()
def work(max_tickets: int = 5) -> None:
    """Work loop: resolve AFK research tickets on every wayfinder map."""
    from omniscient.registry import RegistryError, load_registry
    from omniscient.work.attachments import AgentRunUploader, ProjectContext
    from omniscient.work.engine import run_afk

    now = datetime.now(UTC)
    try:
        projects = load_registry(_path("REPOS"))
    except RegistryError as exc:  # research still runs, without project files
        typer.echo(f"registry unreadable, no project files attached: {exc}")
        projects = ()
    client = make_client()
    context = ProjectContext(projects, AgentRunUploader(client.files))  # type: ignore[attr-defined]
    try:
        result = run_afk(
            client,
            _vault(),
            budget_usd=WORK_BUDGET_USD,
            now=now,
            max_tickets=max_tickets,
            context=context,
        )
    except AgentRunError as exc:
        typer.echo(f"work failed: {exc}")
        raise typer.Exit(code=1) from exc
    if result.cost_usd:
        append_cost(
            _vault(),
            CostRow(
                at=now,
                task="work",
                role="researcher",
                model="anthropic/claude-*-5-5",
                effort="per-tier",
                usd=result.cost_usd,
            ),
        )
    typer.echo(
        f"{len(result.resolved)} ticket(s) resolved | cost: ${result.cost_usd:.4f}"
    )
    for o in result.failed:
        typer.echo(f"  ! {o.path.name}: {o.reason}")
    for path in result.waiting:
        typer.echo(f"  ? waiting for owner: {path.name}")


@app.command()
def grill() -> None:
    """Post one grilling round per frontier grilling ticket to Slack."""
    from slack_sdk import WebClient

    from omniscient.work import grilling

    env = _slack_env()

    def log_cost(r: grilling.GrillingRound) -> None:
        append_cost(
            _vault(),
            CostRow(
                at=datetime.now(UTC),
                task=f"grill {r.ticket.path.stem}",
                role=grilling.GRILLER_ID,
                model="anthropic/claude-opus-5-5",
                effort="high",
                usd=r.cost_usd,
            ),
        )

    posted = grilling.post_rounds(
        make_client(),
        WebClient(token=env["SLACK_BOT_TOKEN"]),
        env["SLACK_CHANNEL_ID"],
        _vault(),
        GRILL_BUDGET_USD,
        on_error=lambda p, e: typer.echo(f"grill failed {p.name}: {e}"),
        on_cost=log_cost,
    )
    typer.echo(f"posted {len(posted)} grilling round(s)")


CYCLE_STAGES = (
    "ingest",
    "learn-sync",
    "think",
    "plan",
    "work",
    "grill",
    "slack-digest",
)


@app.command()
def cycle() -> None:
    """One full factory cycle: ingest, learn-sync, think, plan, slack-digest."""
    stages = {
        "ingest": ingest,
        "learn-sync": learn_sync,
        "think": think,
        "plan": plan,
        "work": work,
        "grill": grill,
        "slack-digest": slack_digest,
    }
    settings = _settings()
    problem = _agentrun_problem()
    if problem is not None:  # every stage needs AgentRun: say it once, run nothing
        typer.echo(f"Cycle annulé. {problem}", err=True)
        raise typer.Exit(code=1)
    if not _claim_cycle(settings.schedule.max_cycles_per_day):
        return
    worst_case = _stage_worst_case_usd(settings.gameplan.wip_limit)
    failed: list[str] = []
    skipped: list[str] = []
    for name in CYCLE_STAGES:
        typer.echo(f"== {name}")
        left = settings.budget.daily_usd - _spent_today()
        if worst_case[name] > left:
            typer.echo(
                f"skipped: daily budget has ${max(left, 0):.2f} left, "
                f"{name} can cost up to ${worst_case[name]:.2f}"
            )
            skipped.append(name)
            continue
        try:
            if name == "ingest":
                stages[name](max_cost_usd=DEFAULT_SCOUT_CAP_USD)
            else:
                stages[name]()
        except typer.Exit as exc:
            if exc.exit_code:
                failed.append(name)
        except Exception:  # a stage crash must not stop the cycle; log and report it
            logging.getLogger("omniscient.cycle").exception("%s crashed", name)
            typer.echo(f"{name} crashed (traceback logged)")
            failed.append(name)
    typer.echo(
        f"cycle done | failed stages: {', '.join(failed) or 'none'} | "
        f"skipped (daily budget): {', '.join(skipped) or 'none'}"
    )
    if failed:
        raise typer.Exit(code=1)


def _claim_cycle(max_per_day: int) -> bool:
    """Record a cycle start, or refuse it if today's limit is already reached."""
    from datetime import date

    from omniscient.ops import cycles_on, record_cycle

    done = cycles_on(_vault(), date.today())
    if done >= max_per_day:
        typer.echo(f"cycle refused: {done}/{max_per_day} cycles already ran today")
        return False
    record_cycle(_vault(), datetime.now().astimezone())
    return True


def _spent_today() -> float:
    from datetime import date

    from omniscient.ops import spent_on

    return spent_on(_vault(), date.today())


def _stage_worst_case_usd(wip_limit: int) -> dict[str, float]:
    """The most each stage can spend, from the caps enforced in code."""
    from omniscient.plan.strategist import STRATEGIST_CAP_USD
    from omniscient.sense.scout import DEFAULT_MAX_REPAIRS, FORMATTER_CAP_USD
    from omniscient.think.ideation import COUNCIL_CAP_USD, VISIONARY_CAP_USD

    return {
        "ingest": DEFAULT_SCOUT_CAP_USD + DEFAULT_MAX_REPAIRS * FORMATTER_CAP_USD,
        "learn-sync": 0.0,
        "think": VISIONARY_CAP_USD + COUNCIL_CAP_USD,
        "plan": STRATEGIST_CAP_USD * wip_limit,
        "work": WORK_BUDGET_USD,
        "grill": GRILL_BUDGET_USD,
        "slack-digest": 0.0,
    }


def _seen_urls(vault: Path) -> list[str]:
    return [str(frontmatter.load(p).get("url")) for p in (vault / "news").glob("*.md")]


@app.command()
def ingest(
    max_cost_usd: Annotated[
        float, typer.Option(help="Spend cap for this scout run, in USD.")
    ] = DEFAULT_SCOUT_CAP_USD,
) -> None:
    """Sense loop: run the news scout and write new items into vault/news/."""
    settings = _settings()
    if max_cost_usd > settings.budget.per_cycle_usd:
        typer.echo(
            f"refused: cap ${max_cost_usd} exceeds budget.per_cycle_usd "
            f"${settings.budget.per_cycle_usd}"
        )
        raise typer.Exit(code=2)
    try:
        result = run_scout(
            make_client(),
            seen_urls=_seen_urls(_vault()),
            lookback_days=settings.sources.news_lookback_days,
            max_cost_usd=max_cost_usd,
            interests=_profile().interests,
        )
    except (ScoutFailed, ScoutFormatError, AgentRunError) as exc:
        typer.echo(f"ingest failed: {exc}")
        raise typer.Exit(code=1) from exc
    now = datetime.now(UTC)
    created = ingest_report(
        _vault(), result.report, ingested=now, avoid=_profile().avoid
    )
    append_cost(
        _vault(),
        CostRow(
            at=now,
            task=f"ingest {result.run_id}",
            role=AGENT_ID,
            model=SCOUT_MODEL,
            effort="low",
            usd=result.cost_usd,
        ),
    )
    typer.echo(
        f"{len(created)} new / {len(result.report.items)} found | "
        f"repairs: {result.repairs} | cost: ${result.cost_usd:.4f}"
    )
    for path in created:
        typer.echo(f"  + {path.name}")
    for gap in result.report.unreachable:
        typer.echo(f"  ! unreachable {gap.source}: {gap.reason}")
