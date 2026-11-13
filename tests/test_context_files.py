import os
from pathlib import Path

import pytest

from omniscient.registry.core import Project
from omniscient.work.context_files import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    is_excluded,
    select_project_files,
    upload_path,
)


def _project(root: Path, agent_docs: tuple[str, ...] = ()) -> Project:
    return Project(
        name="cards",
        path=root,
        purpose="Card prices.",
        stack=(),
        ports=(),
        services=(),
        remote=None,
        git=True,
        agent_docs=agent_docs,
        notes="",
    )


def _write(root: Path, rel: str, text: str = "x\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "cards"
    for rel in (
        "app/CLAUDE.md",
        "README.md",
        "AGENTS.md",
        "docs/guide.md",
        "docs/deep/signals.md",
        "src/engine.py",
        "src/signals/buy.py",
        "app/page.tsx",
        "main.py",
    ):
        _write(root, rel)
    # Planted secrets and data: none of these may ever be selected.
    for rel in (
        ".env",
        ".env.local",
        "api_keys.txt",
        "ebay.token",
        "server.key",
        "cert.pem",
        "src/my_secret_config.py",
        "src/credentials.py",
        "data/prices.py",
        "data/README.md",
        "node_modules/lib/index.ts",
        ".venv/lib/site.py",
        "dist/bundle.py",
        "build/out.py",
        ".git/HEAD.md",
        "docs/data/notes.md",
        "cards.db",
        "cards.sqlite",
        "run.fit",
    ):
        _write(root, rel, "SECRET=1\n")
    return root


def _rels(root: Path, paths: tuple[Path, ...]) -> list[str]:
    return [p.relative_to(root.resolve()).as_posix() for p in paths]


def test_selects_docs_first_then_sources_in_order(repo: Path) -> None:
    # Arrange
    project = _project(repo, agent_docs=("app/CLAUDE.md", "missing.md"))

    # Act
    rels = _rels(repo, select_project_files(project))

    # Assert
    assert rels[:5] == [
        "app/CLAUDE.md",
        "README.md",
        "AGENTS.md",
        "docs/guide.md",
        "docs/deep/signals.md",
    ]
    assert set(rels[5:]) == {
        "src/engine.py",
        "src/signals/buy.py",
        "app/page.tsx",
        "main.py",
    }


def test_never_selects_secrets_data_or_vendor_dirs(repo: Path) -> None:
    # Act
    rels = _rels(repo, select_project_files(_project(repo)))

    # Assert
    for path in rels:
        assert "SECRET" not in (repo / path).read_text()
    assert not any(r.startswith(("data/", ".env", ".git/", "docs/data/")) for r in rels)


def test_agent_docs_cannot_bypass_hard_exclusions(repo: Path) -> None:
    # Arrange
    project = _project(repo, agent_docs=(".env", "data/README.md", "../outside.md"))
    _write(repo.parent, "outside.md")

    # Act
    rels = _rels(repo, select_project_files(project))

    # Assert
    assert ".env" not in rels and "data/README.md" not in rels
    assert all(not r.startswith("..") for r in rels)


def test_keywords_rank_matching_sources_first(repo: Path) -> None:
    # Act
    rels = _rels(repo, select_project_files(_project(repo), keywords=("buy",)))

    # Assert
    sources = [r for r in rels if not r.endswith(".md")]
    assert sources[0] == "src/signals/buy.py"


def test_sources_are_at_most_two_levels_deep(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "p"
    _write(root, "src/a/b/deep.py")
    _write(root, "src/a/ok.py")

    # Act
    rels = _rels(root, select_project_files(_project(root)))

    # Assert
    assert rels == ["src/a/ok.py"]


def test_caps_file_count_size_and_total(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "p"
    _write(root, "README.md", "a" * (MAX_FILE_BYTES + 1))  # too big alone
    for i in range(30):
        _write(root, f"src/m{i:02d}.py", "b" * 1000)

    # Act
    picked = select_project_files(_project(root))

    # Assert
    assert len(picked) == MAX_FILES
    assert "README.md" not in _rels(root, picked)
    assert sum(p.stat().st_size for p in picked) <= MAX_TOTAL_BYTES


def test_attachment_limits_are_300_kb_total_and_60_kb_per_file() -> None:
    # Assert: seen live, 1 MB of files re-read on every Opus step cost ~$0.45 each
    assert MAX_TOTAL_BYTES == 300_000
    assert MAX_FILE_BYTES == 60_000


def test_total_budget_stops_selection_once_reached(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "p"
    big = MAX_FILE_BYTES - 10
    for i in range(6):
        _write(root, f"docs/d{i}.md", "c" * big)
    _write(root, "main.py", "small\n")

    # Act
    picked = select_project_files(_project(root))

    # Assert: docs fill the budget first; nothing after the overflow is added
    total = sum(p.stat().st_size for p in picked)
    assert total <= MAX_TOTAL_BYTES
    assert "main.py" not in _rels(root, picked)
    assert len(picked) == MAX_TOTAL_BYTES // big


def test_an_oversized_file_is_skipped_without_stopping(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "p"
    _write(root, "README.md", "a" * (MAX_FILE_BYTES + 1))
    _write(root, "main.py", "small\n")

    # Act
    rels = _rels(root, select_project_files(_project(root)))

    # Assert
    assert rels == ["main.py"]


def test_skips_binary_and_non_utf8_files(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    (root / "src" / "blob.py").write_bytes(b"abc\x00def")
    (root / "src" / "latin.py").write_bytes("café".encode("latin-1"))
    _write(root, "src/ok.py")

    # Act
    rels = _rels(root, select_project_files(_project(root)))

    # Assert
    assert rels == ["src/ok.py"]


def test_never_follows_symlinks_outside_the_root(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "p"
    outside = tmp_path / "outside"
    _write(outside, "leak.py")
    _write(outside, "README.md")
    _write(root, "src/ok.py")
    os.symlink(outside / "leak.py", root / "src" / "leak.py")
    os.symlink(outside / "README.md", root / "README.md")
    os.symlink(outside, root / "src" / "linked")

    # Act
    rels = _rels(root, select_project_files(_project(root, ("README.md",))))

    # Assert
    assert rels == ["src/ok.py"]


def test_missing_project_root_selects_nothing(tmp_path: Path) -> None:
    # Act / Assert
    assert select_project_files(_project(tmp_path / "gone")) == ()


@pytest.mark.parametrize(
    "rel",
    [
        ".env",
        ".env.production",
        "x/api_keys.txt",
        "a.token",
        "k.key",
        "c.pem",
        "MySecrets.md",
        "aws_Credentials.py",
        ".git/config",
        "node_modules/a.ts",
        ".venv/a.py",
        "dist/a.py",
        "build/a.py",
        "data/a.py",
        "a.db",
        "a.sqlite",
        "a.fit",
        "id_rsa",
    ],
)
def test_is_excluded_hard_rules(rel: str) -> None:
    assert is_excluded(Path(rel))


@pytest.mark.parametrize("rel", ["README.md", "src/app.py", "docs/a/b.md"])
def test_is_excluded_lets_ordinary_files_through(rel: str) -> None:
    assert not is_excluded(Path(rel))


def test_upload_path_is_relative_and_never_hidden(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "cards"
    path = root / ".obsidian-vault" / "CLAUDE.md"

    # Act / Assert
    assert (
        upload_path("cards", root, path) == "projects/cards/_obsidian-vault/CLAUDE.md"
    )
    assert upload_path(".x", root, root / "a.md") == "projects/_x/a.md"
