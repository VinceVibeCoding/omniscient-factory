"""Build loop: turn one idea's smallest version into a runnable prototype.

The `builder` agent works inside an AgentRun Docker sandbox box (builder.json: place
sandbox/docker; its prompt carries no keys). Network egress is whatever the AgentRun box
allows; the agent is only *told* to stay on PyPI, npm and GitHub. Everything it makes
lands in its run's `work/` folder on this machine. We copy
that folder into `pocs/<idea>/`, make it its own git repository on branch
`prototype/<idea>`, and record the verdict on the idea note.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import frontmatter

from omniscient.brain.notes import find_news, set_idea_status
from omniscient.runs import AgentRunClient, RunsApi, extract_json, run_once

BUILDER_ID = "builder"
BUILDER_MODEL = "anthropic/claude-sonnet-5-5"
BUILD_CAP_USD = 1.50
BUILD_WAIT_SECONDS = 2400  # a build installs packages and runs the demo: allow 40 min
MAX_POC_BYTES = 20 * 1024 * 1024
SKIPPED_DIRS = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".git", ".cache", ".learning"}
)
DEMO_OUTPUT_LIMIT = 800
H_PROTOTYPE = "## Prototype"
RUN_ID_KEY = "build_run_id"  # frontmatter: the builder run the studio UI pane follows
_log = logging.getLogger("omniscient.build")

Verdict = Literal["works", "partial", "fails"]
VERDICT_FR = {"works": "ça marche", "partial": "partiel", "fails": "ça ne marche pas"}
_REPORT_FIELDS = ("summary", "run_command", "demo_output", "question", "verdict_detail")


class BuildError(Exception):
    """A build could not start, finish, or be harvested.

    `cost_usd` is what the builder run spent when it finished before the failure.
    """

    def __init__(self, message: str, cost_usd: float | None = None) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


@dataclass(frozen=True)
class BuildReport:
    summary: str
    run_command: str
    demo_output: str
    question: str
    verdict: Verdict
    verdict_detail: str
    simulated: str


@dataclass(frozen=True)
class BuildResult:
    idea_path: Path
    poc_dir: Path
    branch: str
    run_id: str
    cost_usd: float
    report: BuildReport
    url: str | None = None  # public web demo, once published


def parse_report(answer: str) -> BuildReport:
    """Validate the builder's final JSON answer."""
    data: dict[str, Any] = extract_json(answer)
    missing = [k for k in _REPORT_FIELDS if not str(data.get(k, "")).strip()]
    if missing:
        raise BuildError(f"builder answer lacks {missing}")
    verdict = str(data.get("verdict", ""))
    if verdict not in get_args(Verdict):
        raise BuildError(
            f"builder verdict must be one of {get_args(Verdict)}: {verdict!r}"
        )
    return BuildReport(
        summary=str(data["summary"]).strip(),
        run_command=str(data["run_command"]).strip(),
        demo_output=str(data["demo_output"]).strip()[:DEMO_OUTPUT_LIMIT],
        question=str(data["question"]).strip(),
        verdict=verdict,  # type: ignore[arg-type]
        verdict_detail=str(data["verdict_detail"]).strip(),
        simulated=str(data.get("simulated", "")).strip(),
    )


def resolve_idea(vault: Path, stem: str) -> Path:
    """Find an idea note by file stem, refusing anything outside vault/ideas."""
    ideas = (vault / "ideas").resolve()
    path = (ideas / f"{stem}.md").resolve()
    if path.parent != ideas or not path.is_file():
        raise BuildError(f"no idea note named {stem!r}")
    return path


def poc_slug(idea_path: Path) -> str:
    """'2026-11-13 my-idea' -> 'my-idea' (the stem minus its date)."""
    parts = idea_path.stem.split(" ", 1)
    return parts[-1][:60].strip("-") or "poc"


def build_prompt(vault: Path, idea_path: Path) -> str:
    post = frontmatter.load(idea_path)
    sections = [f"# Idée : {post.get('title')}", "", _body_before_council(post.content)]
    sources = []
    for item_id in post.get("sources", []):
        news = find_news(vault, str(item_id))
        if news is not None:
            meta = frontmatter.load(news).metadata
            sources.append(
                f"- {meta.get('title')} ({meta.get('url')}): {meta.get('summary', '')}"
            )
    if sources:
        sections += ["", "## Articles sources", *sources]
    sections += [
        "",
        "## Ta mission",
        (
            "Construis la PLUS PETITE VERSION ci-dessus (section Conseil, « plus petite version »)"
            " comme prototype jetable qui démarre avec ./run.sh. Réponds avec le JSON demandé."
        ),
    ]
    return "\n".join(sections)


def _body_before_council(content: str) -> str:
    """The pitch, projects and council text; drop the discussion log, keep it short."""
    body = content.split("## Discussion", 1)[0]
    return body.strip()[:6000]


def run_work_dir(agentrun_data: Path, agent_id: str, run_id: str) -> Path:
    """AgentRun keeps a run's files in disk/agents/<id>-<sha256(id)[:16]>/runs/<run>/work."""
    digest = hashlib.sha256(agent_id.lower().encode()).hexdigest()[:16]
    return (
        agentrun_data
        / "disk"
        / "agents"
        / f"{agent_id.lower()}-{digest}"
        / "runs"
        / run_id
        / "work"
    )


def harvest(work: Path, dest: Path) -> None:
    """Copy the run's work/ into dest: regular files only, no deps, no symlinks, size-capped."""
    if not work.is_dir():
        raise BuildError(f"the run left no work folder at {work}")
    if dest.exists():
        raise BuildError(f"{dest} already exists; remove it to rebuild")
    files = list(_regular_files(work))
    if not files:
        raise BuildError("the builder produced no files")
    total = sum(f.stat().st_size for f in files)
    if total > MAX_POC_BYTES:
        raise BuildError(f"prototype is {total} bytes, over the {MAX_POC_BYTES} cap")
    for src in files:
        target = dest / src.relative_to(work)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _regular_files(root: Path):  # type: ignore[no-untyped-def]
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIPPED_DIRS for part in rel.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue  # symlinks could point outside the box's folder
        yield path


def init_repo(poc_dir: Path, branch: str, message: str) -> None:
    """Make the prototype its own git repo with one commit on `branch`."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=poc_dir, check=True, capture_output=True)

    git("init", "-q", "-b", branch)
    git("add", "-A")
    git(
        "-c",
        "user.name=Omniscient",
        "-c",
        "user.email=omniscient@localhost",
        "commit",
        "-q",
        "-m",
        message,
    )


def record_prototype(
    idea_path: Path, result_dir: Path, branch: str, report: BuildReport
) -> None:
    """Write the prototype's verdict into the idea note (section + frontmatter)."""
    post = frontmatter.load(idea_path)
    post["poc_dir"] = str(result_dir)
    post["poc_branch"] = branch
    post["poc_verdict"] = report.verdict
    section = "\n".join(
        [
            H_PROTOTYPE,
            f"**Question :** {report.question}",
            f"**Verdict :** {VERDICT_FR[report.verdict]} — {report.verdict_detail}",
            f"**Lancer :** `cd {result_dir} && {report.run_command}`",
            f"**Simulé :** {report.simulated or 'rien'}",
            "",
            report.summary,
        ]
    )
    post.content = _replace_section(post.content, section)
    idea_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def _replace_section(content: str, section: str) -> str:
    """Put `section` in place of an existing Prototype section, keeping later sections."""
    head, sep, rest = content.partition(f"\n{H_PROTOTYPE}\n")
    if not sep:
        return f"{content.rstrip()}\n\n{section}\n"
    _, nxt, tail = rest.partition("\n## ")
    after = f"\n\n## {tail}" if nxt else "\n"
    return f"{head.rstrip()}\n\n{section}{after}"


def record_run_id(idea_path: Path, run_id: str) -> None:
    """Write the builder's run id into the idea frontmatter as soon as the run starts."""
    post = frontmatter.load(idea_path)
    post[RUN_ID_KEY] = run_id
    idea_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


class _RecordingRuns:
    """Pass-through runs API that records the started run's id on the idea note."""

    def __init__(self, runs: RunsApi, idea_path: Path) -> None:
        self._runs = runs
        self._idea_path = idea_path

    def start(self, agent_id: str, body: dict[str, Any]) -> Any:
        started = self._runs.start(agent_id, body)
        try:
            record_run_id(self._idea_path, str(started["run_id"]))
        except Exception as exc:  # noqa: BLE001 - any failure here is bookkeeping only
            # The run is already spending: never abort it over a bookkeeping write.
            _log.warning(
                "could not record %s on %s: %s", RUN_ID_KEY, self._idea_path, exc
            )
        return started

    def wait(self, run_id: str, timeout: float) -> Any:
        return self._runs.wait(run_id, timeout=timeout)

    def raise_cap(self, run_id: str, body: dict[str, Any]) -> Any:
        return self._runs.raise_cap(run_id, body)


class _RecordingClient:
    def __init__(self, client: AgentRunClient, idea_path: Path) -> None:
        self.runs = _RecordingRuns(client.runs, idea_path)


def build_poc(
    client: AgentRunClient,
    vault: Path,
    idea_stem: str,
    pocs_dir: Path,
    agentrun_data: Path,
    cap_usd: float = BUILD_CAP_USD,
    wait_seconds: float = BUILD_WAIT_SECONDS,
) -> BuildResult:
    """Run the builder on one idea, harvest its prototype, record the verdict."""
    idea_path = resolve_idea(vault, idea_stem)
    slug = poc_slug(idea_path)
    poc_dir = pocs_dir / slug
    if poc_dir.exists():
        raise BuildError(f"a prototype already exists at {poc_dir}")
    previous_status = str(frontmatter.load(idea_path).get("status", "debated"))
    set_idea_status(idea_path, "building")
    cost: float | None = None
    try:
        run_id, answer, cost = run_once(
            _RecordingClient(client, idea_path),
            BUILDER_ID,
            build_prompt(vault, idea_path),
            cap_usd,
            wait_seconds=wait_seconds,
        )
        report = parse_report(answer)
        branch = f"prototype/{slug}"
        harvest(run_work_dir(agentrun_data, BUILDER_ID, run_id), poc_dir)
        init_repo(poc_dir, branch, f"prototype: {slug} ({report.verdict})")
        record_prototype(idea_path, poc_dir, branch, report)
    except Exception as exc:
        # Undo: the idea goes back to its status and no half-copied prototype blocks a retry.
        set_idea_status(idea_path, previous_status)
        shutil.rmtree(poc_dir, ignore_errors=True)
        if cost is None:
            raise
        raise BuildError(str(exc) or type(exc).__name__, cost_usd=cost) from exc
    return BuildResult(idea_path, poc_dir, branch, run_id, cost, report)


def format_build_reply(result: BuildResult) -> str:
    """The French Slack message posted in the idea's thread when a build ends."""
    r = result.report
    icon = {
        "works": ":white_check_mark:",
        "partial": ":large_yellow_circle:",
        "fails": ":x:",
    }
    lines = [
        f"{icon[r.verdict]} *Prototype prêt : {VERDICT_FR[r.verdict]}*",
        *([f":globe_with_meridians: *Essayer :* {result.url}"] if result.url else []),
        f"*Question :* {r.question}",
        f"*Verdict :* {r.verdict_detail}",
        "",
        r.summary,
        "",
        f"*Lancer :* `cd {result.poc_dir} && {r.run_command}`",
        f"*Branche :* `{result.branch}` · *Coût :* {result.cost_usd:.2f} $",
    ]
    if r.simulated:
        lines.append(f"*Simulé :* {r.simulated}")
    if r.demo_output:
        lines += ["*Sortie réelle :*", f"```{r.demo_output}```"]
    return "\n".join(lines)
