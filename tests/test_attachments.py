import hashlib
import logging
from pathlib import Path
from typing import Any

import pytest

from omniscient.registry.core import Project
from omniscient.work.attachments import (
    MAX_FILES_PER_RUN,
    AgentRunUploader,
    Attachment,
    ProjectContext,
    UploadFailed,
    attach_projects,
    attachments_section,
    idea_note,
    idea_projects,
    keywords,
)


def _project(root: Path, name: str) -> Project:
    return Project(
        name=name,
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


def _repo(tmp_path: Path, name: str, files: int = 2) -> Path:
    root = tmp_path / name
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (root / ".env").write_text("KEY=1\n", encoding="utf-8")
    for i in range(files):
        (root / "src" / f"m{i:02d}.py").write_text("x = 1\n", encoding="utf-8")
    return root


class _Uploader:
    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[tuple[Path, str]] = []
        self.fail_on = fail_on

    def upload(self, path: Path, upload_path: str) -> str:
        if self.fail_on and self.fail_on in upload_path:
            raise UploadFailed("boom")
        self.calls.append((path, upload_path))
        return f"file-{len(self.calls)}"


def test_attach_projects_uploads_selected_files_of_named_projects(
    tmp_path: Path,
) -> None:
    # Arrange
    uploader = _Uploader()
    context = ProjectContext((_project(_repo(tmp_path, "cards"), "cards"),), uploader)

    # Act
    attached = attach_projects(context, ("cards",))

    # Assert
    paths = [a.path for a in attached]
    assert paths[0] == "projects/cards/README.md"
    assert "projects/cards/src/m00.py" in paths
    assert not any(".env" in p for p in paths)
    assert attached[0] == Attachment("cards", "projects/cards/README.md", "file-1")


def test_attach_projects_interleaves_projects_under_the_run_cap(
    tmp_path: Path,
) -> None:
    # Arrange
    projects = (
        _project(_repo(tmp_path, "a", files=20), "a"),
        _project(_repo(tmp_path, "b", files=20), "b"),
    )
    context = ProjectContext(projects, _Uploader())

    # Act
    attached = attach_projects(context, ("a", "b"))

    # Assert
    assert len(attached) == MAX_FILES_PER_RUN
    assert {a.project for a in attached[:2]} == {"a", "b"}


def test_attach_projects_skips_unknown_projects_with_a_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    context = ProjectContext((), _Uploader())

    # Act
    with caplog.at_level(logging.WARNING):
        attached = attach_projects(context, ("ghost",))

    # Assert
    assert attached == ()
    assert "ghost" in caplog.text


def test_attach_projects_logs_and_drops_everything_on_upload_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    uploader = _Uploader(fail_on="src/m01.py")
    context = ProjectContext((_project(_repo(tmp_path, "cards"), "cards"),), uploader)

    # Act
    with caplog.at_level(logging.WARNING):
        attached = attach_projects(context, ("cards",))

    # Assert
    assert attached == ()
    assert "boom" in caplog.text


class _Files:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[bytes, dict[str, Any]]] = []

    def create(self, body: bytes, **kwargs: Any) -> Any:
        self.calls.append((body, kwargs))
        return self.answer


def test_agentrun_uploader_sends_bytes_in_one_call(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "a.md"
    path.write_text("hello\n", encoding="utf-8")
    files = _Files({"file": {"file_id": "f1", "status": "ready"}, "upload": {}})

    # Act
    file_id = AgentRunUploader(files).upload(path, "projects/x/a.md")

    # Assert
    assert file_id == "f1"
    body, kwargs = files.calls[0]
    assert body == b"hello\n"
    assert kwargs == {
        "path": "projects/x/a.md",
        "sha256": hashlib.sha256(b"hello\n").hexdigest(),
    }


@pytest.mark.parametrize(
    "answer",
    [
        {"file": {"file_id": "f1", "status": "pending"}},
        {"file": {"status": "ready"}},
        {},
        None,
    ],
)
def test_agentrun_uploader_rejects_unready_answers(tmp_path: Path, answer: Any) -> None:
    # Arrange
    path = tmp_path / "a.md"
    path.write_text("x", encoding="utf-8")

    # Act / Assert
    with pytest.raises(UploadFailed):
        AgentRunUploader(_Files(answer)).upload(path, "projects/x/a.md")


def _vault_with_idea(tmp_path: Path, frontmatter: str) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "ideas").mkdir(parents=True)
    (vault / "ideas" / "card.md").write_text(f"---\n{frontmatter}---\n# Idée\n")
    map_path = vault / "maps" / "m" / "map.md"
    map_path.parent.mkdir(parents=True)
    map_path.write_text("---\nidea: '[[card]]'\n---\n# Map\n")
    return vault, map_path


def test_idea_note_and_projects_are_read_from_the_vault(tmp_path: Path) -> None:
    # Arrange
    vault, map_path = _vault_with_idea(tmp_path, "projects:\n- cards\n- web\n")

    # Act
    link, note = idea_note(vault, map_path)

    # Assert
    assert link == "card"
    assert note is not None
    assert idea_projects(note) == ("cards", "web")


def test_idea_note_refuses_escapes_and_missing_links(tmp_path: Path) -> None:
    # Arrange
    vault, map_path = _vault_with_idea(tmp_path, "projects: []\n")
    escape = map_path.with_name("escape.md")
    escape.write_text("---\nidea: '[[../secret]]'\n---\n")
    nolink = map_path.with_name("nolink.md")
    nolink.write_text("---\ntype: map\n---\n")

    # Act / Assert
    assert idea_note(vault, escape) == ("../secret", None)
    assert idea_note(vault, nolink) == ("", None)


@pytest.mark.parametrize("value", ["projects: cards\n", "projects:\n- 3\n", ""])
def test_idea_projects_ignores_malformed_lists(tmp_path: Path, value: str) -> None:
    # Arrange
    vault, _ = _vault_with_idea(tmp_path, value or "kind: x\n")

    # Act / Assert
    assert idea_projects(vault / "ideas" / "card.md") == ()


def test_keywords_keep_distinctive_word_stems() -> None:
    # Act
    words = keywords("How does card_index produce buy signals?")

    # Assert
    assert "signa" in words
    assert "how" not in words


def test_attachments_section_names_projects_and_the_read_tool() -> None:
    # Arrange
    attached = (
        Attachment("cards", "projects/cards/README.md", "f1"),
        Attachment("web", "projects/web/a.py", "f2"),
    )

    # Act
    section = attachments_section(attached)

    # Assert
    assert "cards, web" in section
    assert "`read`" in section
    assert "projects/web/a.py" in section
    assert "only the files relevant to the question" in section
    assert attachments_section(()) == ""
