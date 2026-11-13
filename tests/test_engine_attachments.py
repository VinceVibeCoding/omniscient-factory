import logging
from pathlib import Path

import pytest

from omniscient.registry.core import Project
from omniscient.work.attachments import ProjectContext, UploadFailed
from omniscient.work.engine import run_afk
from omniscient.work.tickets import parse_ticket
from tests.test_engine import NOW, _Client, _good, _issue, _ticket_md, _vault


def _project(root: Path) -> Project:
    return Project(
        name="cards",
        path=root,
        purpose="p",
        stack=(),
        ports=(),
        services=(),
        remote=None,
        git=True,
        agent_docs=(),
        notes="",
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "cards"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# cards\n", encoding="utf-8")
    (root / "src" / "signals.py").write_text("BUY = 1\n", encoding="utf-8")
    (root / ".env").write_text("KEY=1\n", encoding="utf-8")
    return root


def _vault_on(tmp_path: Path, projects: str) -> Path:
    vault = _vault(tmp_path, {"01-apis.md": _ticket_md("APIs")})
    (vault / "ideas" / "2026-11-12 card-verifier.md").write_text(
        f"---\nprojects: {projects}\n---\n# Idée\nUn vérificateur.\n",
        encoding="utf-8",
    )
    return vault


class _Uploader:
    def __init__(self, error: Exception | None = None) -> None:
        self.paths: list[str] = []
        self.error = error

    def upload(self, path: Path, upload_path: str) -> str:
        if self.error is not None:
            raise self.error
        self.paths.append(upload_path)
        return f"file-{len(self.paths)}"


def test_run_afk_attaches_the_idea_projects_files(tmp_path: Path) -> None:
    # Arrange
    vault = _vault_on(tmp_path, "[cards]")
    uploader = _Uploader()
    context = ProjectContext((_project(_repo(tmp_path)),), uploader)
    client = _Client([_good()])

    # Act
    result = run_afk(client, vault, budget_usd=1.0, now=NOW, context=context)

    # Assert
    assert len(result.resolved) == 1
    started = client.runs.started[0]
    assert started["file_ids"] == ["file-1", "file-2"]
    assert uploader.paths == [
        "projects/cards/README.md",
        "projects/cards/src/signals.py",
    ]
    prompt = started["input"]["content"]
    assert "## Attached project files" in prompt
    assert "(cards)" in prompt and "`read`" in prompt
    assert "projects/cards/src/signals.py" in prompt


def test_run_afk_researches_without_files_when_upload_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    vault = _vault_on(tmp_path, "[cards]")
    context = ProjectContext(
        (_project(_repo(tmp_path)),), _Uploader(UploadFailed("server down"))
    )
    client = _Client([_good()])

    # Act
    with caplog.at_level(logging.WARNING):
        result = run_afk(client, vault, budget_usd=1.0, now=NOW, context=context)

    # Assert
    assert parse_ticket(_issue(vault, "01-apis.md")).status == "resolved"
    assert len(result.resolved) == 1
    started = client.runs.started[0]
    assert "file_ids" not in started
    assert "Attached project files" not in started["input"]["content"]
    assert "server down" in caplog.text


def test_run_afk_attaches_nothing_when_the_idea_names_no_project(
    tmp_path: Path,
) -> None:
    # Arrange
    vault = _vault_on(tmp_path, "[]")
    uploader = _Uploader()
    context = ProjectContext((_project(_repo(tmp_path)),), uploader)
    client = _Client([_good()])

    # Act
    run_afk(client, vault, budget_usd=1.0, now=NOW, context=context)

    # Assert
    assert uploader.paths == []
    assert "file_ids" not in client.runs.started[0]


def test_run_afk_attaches_nothing_without_an_idea_note(tmp_path: Path) -> None:
    # Arrange
    vault = _vault(tmp_path, {"01-apis.md": _ticket_md("APIs")}, idea=False)
    uploader = _Uploader()
    context = ProjectContext((_project(_repo(tmp_path)),), uploader)
    client = _Client([_good()])

    # Act
    run_afk(client, vault, budget_usd=1.0, now=NOW, context=context)

    # Assert
    assert uploader.paths == []
    assert "file_ids" not in client.runs.started[0]
