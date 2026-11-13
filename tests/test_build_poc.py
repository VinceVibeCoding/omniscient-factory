import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import yaml

from omniscient.brain import Idea, IdeaScores, write_idea
from omniscient.brain.models import NewsItem
from omniscient.brain.notes import write_news
from omniscient.build import poc
from omniscient.build.poc import (
    BUILDER_ID,
    DEMO_OUTPUT_LIMIT,
    H_PROTOTYPE,
    BuildError,
    BuildReport,
    BuildResult,
    build_poc,
    build_prompt,
    format_build_reply,
    harvest,
    parse_report,
    poc_slug,
    record_prototype,
    resolve_idea,
    run_work_dir,
)
from omniscient.runs import RunFailed

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
COUNCIL = "**Verdict:** pursue\n\n**Simplifier (smallest version):** One CLI that prints a chart."
SOURCE_URL = "https://example.com/rain-api"


def _report_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "summary": "A CLI that fetches rain data.",
        "run_command": "./run.sh",
        "demo_output": "rain: 3.2 mm",
        "question": "Can we prove rain per hour?",
        "verdict": "works",
        "verdict_detail": "real data fetched",
        "simulated": "",
    }
    data.update(overrides)
    return data


def _report(**overrides: Any) -> BuildReport:
    return parse_report(json.dumps(_report_data(**overrides)))


def _idea(vault: Path, title: str = "Rain proof", with_source: bool = True) -> Path:
    sources: tuple[str, ...] = ()
    if with_source:
        news = NewsItem(
            title="Rain API opens",
            url=SOURCE_URL,
            source="hn",
            published=NOW.date(),
            ingested=NOW,
            summary="Hourly precipitation API.",
            tags=("api",),
        )
        write_news(vault, news)
        sources = (news.id,)
    idea = Idea(
        title=title,
        pitch="Hourly rain proof for a construction site.",
        status="debated",
        scores=IdeaScores(
            novelty=0.5, impact=0.5, feasibility=0.5, fit=0.5, effort=0.5
        ),
        sources=sources,
        projects=(),
        created=NOW,
    )
    return write_idea(vault, idea, score=0.5, council=COUNCIL)


class FakeRuns:
    def __init__(
        self, answer: str, usd: float = 0.42, status: str = "completed"
    ) -> None:
        self.answer = answer
        self.usd = usd
        self.status = status
        self.starts: list[tuple[str, dict[str, Any]]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.starts.append((agent_id, body))
        return {"run_id": "r1"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": self.status,
            "result": {"final_answer": self.answer},
            "error": {"code": "boom", "message": "box crashed"},
            "cost": {"usd": self.usd},
        }


class FakeClient:
    def __init__(
        self, answer: str, usd: float = 0.42, status: str = "completed"
    ) -> None:
        self.runs = FakeRuns(answer, usd, status)


# parse_report


def test_parse_report_accepts_valid_json() -> None:
    report = parse_report("```json\n" + json.dumps(_report_data()) + "\n```")

    assert report.verdict == "works"
    assert report.run_command == "./run.sh"
    assert report.simulated == ""


def test_parse_report_rejects_missing_field() -> None:
    data = _report_data()
    del data["question"]

    with pytest.raises(BuildError, match="question"):
        parse_report(json.dumps(data))


def test_parse_report_rejects_blank_field() -> None:
    with pytest.raises(BuildError, match="summary"):
        parse_report(json.dumps(_report_data(summary="   ")))


def test_parse_report_rejects_bad_verdict() -> None:
    with pytest.raises(BuildError, match="verdict"):
        parse_report(json.dumps(_report_data(verdict="great")))


def test_parse_report_truncates_demo_output() -> None:
    report = _report(demo_output="x" * (DEMO_OUTPUT_LIMIT + 100))

    assert len(report.demo_output) == DEMO_OUTPUT_LIMIT


# resolve_idea / poc_slug


def test_resolve_idea_finds_an_existing_note(tmp_path: Path) -> None:
    path = _idea(tmp_path)

    assert resolve_idea(tmp_path, path.stem) == path.resolve()


@pytest.mark.parametrize("stem", ["../x", "../../AGENTS", "sub/../../x", "unknown"])
def test_resolve_idea_refuses_traversal_and_unknown(tmp_path: Path, stem: str) -> None:
    _idea(tmp_path)
    (tmp_path / "x.md").write_text("outside", encoding="utf-8")

    with pytest.raises(BuildError, match="no idea note"):
        resolve_idea(tmp_path, stem)


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("2026-11-13 rain-proof.md", "rain-proof"),
        ("nodate.md", "nodate"),
        ("2026-11-13 ---.md", "poc"),
    ],
)
def test_poc_slug_strips_date_prefix(name: str, slug: str) -> None:
    assert poc_slug(Path(name)) == slug


# build_prompt


def test_build_prompt_has_title_source_url_and_no_discussion(tmp_path: Path) -> None:
    path = _idea(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n## Discussion\nSECRET-CHATTER from the thread\n")

    prompt = build_prompt(tmp_path, path)

    assert "# Idée : Rain proof" in prompt
    assert SOURCE_URL in prompt and "Rain API opens" in prompt
    assert "## Articles sources" in prompt
    assert "## Discussion" not in prompt
    assert "SECRET-CHATTER" not in prompt
    assert "./run.sh" in prompt


def test_build_prompt_skips_unknown_sources(tmp_path: Path) -> None:
    path = _idea(tmp_path, with_source=False)
    post = frontmatter.load(path)
    post["sources"] = ["missing-id"]
    path.write_text(frontmatter.dumps(post), encoding="utf-8")

    prompt = build_prompt(tmp_path, path)

    assert "## Articles sources" not in prompt


# run_work_dir


def test_run_work_dir_layout(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"builder").hexdigest()[:16]

    work = run_work_dir(tmp_path, "builder", "r1")

    assert work == (
        tmp_path / "disk" / "agents" / f"builder-{digest}" / "runs" / "r1" / "work"
    )


def test_run_work_dir_lowercases_agent_id(tmp_path: Path) -> None:
    assert run_work_dir(tmp_path, "Builder", "r1") == run_work_dir(
        tmp_path, "builder", "r1"
    )


# harvest


def test_harvest_copies_regular_files_and_skips_deps_and_symlinks(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (work / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    for skipped in ("node_modules", ".venv", "__pycache__", ".git"):
        (work / skipped).mkdir()
        (work / skipped / "junk.txt").write_text("junk", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside", encoding="utf-8")
    (work / "link.txt").symlink_to(secret)
    dest = tmp_path / "dest"

    harvest(work, dest)

    copied = sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()
    )
    assert copied == ["run.sh", "src/app.py"]


def test_harvest_refuses_missing_work(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="no work folder"):
        harvest(tmp_path / "nope", tmp_path / "dest")


def test_harvest_refuses_existing_dest(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "run.sh").write_text("x", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(BuildError, match="already exists"):
        harvest(work, dest)


def test_harvest_refuses_empty_work(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "node_modules").mkdir(parents=True)
    (work / "node_modules" / "dep.js").write_text("x", encoding="utf-8")

    with pytest.raises(BuildError, match="no files"):
        harvest(work, tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


def test_harvest_refuses_over_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(poc, "MAX_POC_BYTES", 10)
    work = tmp_path / "work"
    work.mkdir()
    (work / "big.bin").write_bytes(b"0" * 11)

    with pytest.raises(BuildError, match="over the 10 cap"):
        harvest(work, tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


# build_poc end to end


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_build_poc_end_to_end(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idea_path = _idea(vault)
    agentrun_data = tmp_path / "agentrun"
    work = run_work_dir(agentrun_data, BUILDER_ID, "r1")
    work.mkdir(parents=True)
    (work / "run.sh").write_text("#!/bin/sh\necho rain\n", encoding="utf-8")
    (work / "README.md").write_text("# Rain proof\n", encoding="utf-8")
    pocs = tmp_path / "pocs"
    client = FakeClient(json.dumps(_report_data(verdict="partial")), usd=0.42)

    result = build_poc(client, vault, idea_path.stem, pocs, agentrun_data)  # type: ignore[arg-type]

    slug = "rain-proof"
    assert result.poc_dir == pocs / slug
    assert result.branch == f"prototype/{slug}"
    assert result.run_id == "r1" and result.cost_usd == pytest.approx(0.42)
    agent_id, body = client.runs.starts[0]
    assert agent_id == BUILDER_ID
    assert "Rain proof" in body["input"]["content"]
    assert body["max_cost_usd"] == poc.BUILD_CAP_USD
    assert (result.poc_dir / "run.sh").is_file()
    assert (result.poc_dir / ".git").is_dir()
    assert _git(result.poc_dir, "branch", "--show-current") == f"prototype/{slug}"
    assert _git(result.poc_dir, "rev-list", "--count", "HEAD") == "1"
    assert "partial" in _git(result.poc_dir, "log", "-1", "--format=%s")
    note = frontmatter.load(idea_path)
    assert note["status"] == "building"
    assert note["poc_verdict"] == "partial"
    assert note["poc_branch"] == f"prototype/{slug}"
    assert note["poc_dir"] == str(result.poc_dir)
    assert note.content.count(H_PROTOTYPE) == 1
    assert "partiel" in note.content

    with pytest.raises(BuildError, match="already exists"):
        build_poc(client, vault, idea_path.stem, pocs, agentrun_data)  # type: ignore[arg-type]
    assert len(client.runs.starts) == 1


def test_build_poc_rejects_bad_answer_before_harvest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idea_path = _idea(vault)
    client = FakeClient(json.dumps(_report_data(verdict="nope")))

    with pytest.raises(BuildError, match="verdict"):
        build_poc(client, vault, idea_path.stem, tmp_path / "pocs", tmp_path / "ar")  # type: ignore[arg-type]
    assert not (tmp_path / "pocs").exists()
    assert frontmatter.load(idea_path)["status"] == "debated"


def test_build_poc_restores_status_when_the_run_fails(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idea_path = _idea(vault)
    client = FakeClient("", status="failed")

    with pytest.raises(RunFailed, match="boom"):
        build_poc(client, vault, idea_path.stem, tmp_path / "pocs", tmp_path / "ar")  # type: ignore[arg-type]

    note = frontmatter.load(idea_path)
    assert note["status"] == "debated"
    assert "poc_verdict" not in note.metadata
    assert not (tmp_path / "pocs").exists()


# record_prototype


def test_record_prototype_replaces_existing_section(tmp_path: Path) -> None:
    path = _idea(tmp_path)
    first = _report(verdict="fails", verdict_detail="first try")
    second = _report(verdict="works", verdict_detail="second try", simulated="GPS")

    record_prototype(path, tmp_path / "p", "prototype/p", first)
    record_prototype(path, tmp_path / "p", "prototype/p", second)

    post = frontmatter.load(path)
    assert post.content.count(H_PROTOTYPE) == 1
    assert "second try" in post.content and "first try" not in post.content
    assert "**Simulé :** GPS" in post.content
    assert post["poc_verdict"] == "works"
    assert "## Conseil" in post.content


def test_record_prototype_says_nothing_simulated(tmp_path: Path) -> None:
    path = _idea(tmp_path)

    record_prototype(path, tmp_path / "p", "prototype/p", _report())

    assert "**Simulé :** rien" in frontmatter.load(path).content


# format_build_reply


def _result(tmp_path: Path, **overrides: Any) -> BuildResult:
    return BuildResult(
        idea_path=tmp_path / "idea.md",
        poc_dir=tmp_path / "pocs" / "rain-proof",
        branch="prototype/rain-proof",
        run_id="r1",
        cost_usd=0.4234,
        report=_report(**overrides),
    )


@pytest.mark.parametrize(
    ("verdict", "fr", "icon"),
    [
        ("works", "ça marche", ":white_check_mark:"),
        ("partial", "partiel", ":large_yellow_circle:"),
        ("fails", "ça ne marche pas", ":x:"),
    ],
)
def test_format_build_reply(tmp_path: Path, verdict: str, fr: str, icon: str) -> None:
    result = _result(tmp_path, verdict=verdict, simulated="GPS feed")

    text = format_build_reply(result)

    assert text.startswith(f"{icon} *Prototype prêt : {fr}*")
    assert f"cd {result.poc_dir} && ./run.sh" in text
    assert "0.42 $" in text
    assert "```rain: 3.2 mm```" in text
    assert "*Simulé :* GPS feed" in text
    assert "prototype/rain-proof" in text


def test_format_build_reply_omits_empty_simulated(tmp_path: Path) -> None:
    text = format_build_reply(_result(tmp_path))

    assert "Simulé" not in text


def test_record_prototype_keeps_sections_written_after_it(tmp_path: Path) -> None:
    from omniscient.build.poc import _replace_section

    content = "intro\n\n## Prototype\nold\n\n## Discussion\n- hi\n"

    out = _replace_section(content, "## Prototype\nnew")

    assert "old" not in out and "new" in out
    assert out.count("## Prototype") == 1 and "## Discussion\n- hi" in out


def test_build_error_after_a_finished_run_carries_its_cost(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idea_path = _idea(vault)
    pocs = tmp_path / "pocs"
    client = FakeClient(
        json.dumps(_report_data()), usd=0.9
    )  # run ok, but no work/ left

    with pytest.raises(BuildError) as caught:
        build_poc(client, vault, idea_path.stem, pocs, tmp_path / "ar")  # type: ignore[arg-type]

    assert caught.value.cost_usd == pytest.approx(0.9)
    assert not (pocs / "rain-proof").exists()
    assert frontmatter.load(idea_path)["status"] == "debated"


# build_run_id (followed by the studio UI pane)


def test_build_poc_records_the_run_id_when_the_run_starts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idea_path = _idea(vault)
    seen: list[object] = []
    client = FakeClient("", status="failed")
    original_wait = client.runs.wait

    def wait(run_id: str, timeout: float) -> dict[str, Any]:
        seen.append(frontmatter.load(idea_path).get(poc.RUN_ID_KEY))
        return original_wait(run_id, timeout)

    client.runs.wait = wait  # type: ignore[method-assign]

    with pytest.raises(RunFailed):
        build_poc(client, vault, idea_path.stem, tmp_path / "pocs", tmp_path / "ar")  # type: ignore[arg-type]

    assert seen == ["r1"]  # written before the wait, while the run is in progress
    assert frontmatter.load(idea_path)[poc.RUN_ID_KEY] == "r1"


@pytest.mark.parametrize(
    "error",
    [OSError("disk full"), yaml.YAMLError("bad frontmatter"), RuntimeError("boom")],
)
def test_build_poc_keeps_running_when_the_run_id_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    vault = tmp_path / "vault"
    idea_path = _idea(vault)
    client = FakeClient(json.dumps(_report_data(verdict="nope")))

    def broken(path: Path, run_id: str) -> None:
        raise error

    monkeypatch.setattr(poc, "record_run_id", broken)

    with pytest.raises(BuildError, match="verdict"):  # got past start to the answer
        build_poc(client, vault, idea_path.stem, tmp_path / "pocs", tmp_path / "ar")  # type: ignore[arg-type]
    assert len(client.runs.starts) == 1
    assert "could not record build_run_id" in caplog.text
