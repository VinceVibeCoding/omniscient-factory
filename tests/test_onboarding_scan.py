from pathlib import Path

import frontmatter
import pytest

from omniscient.onboarding import scan
from omniscient.onboarding.prereqs import check_prereqs, missing_required, render_checks
from omniscient.onboarding.scan import (
    clean_remote,
    describe_local_project,
    first_paragraph,
    scan_projects_root,
    slugify,
)
from omniscient.registry.core import load_registry

AT = "@"  # split so the export scan does not read the URLs as e-mail addresses


def _project(root: Path) -> Path:
    proj = root / "My App"
    (proj / ".git").mkdir(parents=True)
    (proj / "docs").mkdir()
    (proj / "docs" / "HANDOFF.md").write_text("x", encoding="utf-8")
    (proj / "README.md").write_text(
        "# My App\n\n[![ci](b.svg)](x)\n\nTracks **runs** with [Strava](http://x) data.\n\nMore.\n",
        encoding="utf-8",
    )
    (proj / "package.json").write_text(
        '{"dependencies": {"next": "15"}}', encoding="utf-8"
    )
    (proj / "pyproject.toml").write_text('[project]\nname="a"\n', encoding="utf-8")
    (proj / "docker-compose.yml").write_text(
        "services:\n  db:\n    image: postgres\n    ports: ['127.0.0.1:5433:5432']\n"
        "  web:\n    ports:\n      - 3000:3000\n      - {published: 8080, target: 80}\n",
        encoding="utf-8",
    )
    (proj / ".env.example").write_text("API_PORT=8001\nSECRET=x\n", encoding="utf-8")
    (proj / ".env").write_text("WEB_PORT=9999\n", encoding="utf-8")
    return proj


def test_describe_local_project_matches_registry_schema(tmp_path: Path) -> None:
    # Arrange
    proj = _project(tmp_path)
    calls: list[list[str]] = []

    def git(cmd: list[str]) -> str | None:
        calls.append(cmd)
        return "https://user:ghp_secret" + "@" + "github.com/acme/my-app.git"

    # Act
    markdown = describe_local_project(proj, git)
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "my-app.md").write_text(markdown, encoding="utf-8")
    (project,) = load_registry(repos)

    # Assert
    assert project.name == "my-app"
    assert project.purpose == "Tracks runs with Strava data."
    assert project.stack == ("python", "node", "nextjs", "docker")
    assert [p.port for p in project.ports] == [5433, 3000, 8080, 8001]
    assert 9999 not in [p.port for p in project.ports]  # .env is never read
    assert project.services == ("db", "web")
    assert project.remote == "github.com/acme/my-app"
    assert "ghp_secret" not in markdown
    assert project.git is True
    assert project.agent_docs == ("README.md", "docs/HANDOFF.md")
    assert calls == [["git", "-C", str(proj.resolve()), "remote", "get-url", "origin"]]


def test_describe_without_readme_uses_manifest_or_placeholder(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    meta = frontmatter.loads(describe_local_project(bare, lambda _: None)).metadata
    assert meta["purpose"] == "Projet bare (à décrire)."
    assert meta["git"] is False and meta["remote"] is None

    (bare / "pyproject.toml").write_text(
        '[project]\ndescription = "A tool."\n', encoding="utf-8"
    )
    meta = frontmatter.loads(describe_local_project(bare, lambda _: None)).metadata
    assert meta["purpose"] == "A tool."


def test_describe_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        describe_local_project(tmp_path / "nope")


def test_first_paragraph_clips_to_200_chars() -> None:
    text = first_paragraph("# T\n\n" + "word " * 100)
    assert text is not None and len(text) <= 200 and text.endswith("…")
    assert first_paragraph("# only heading\n") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/x.git", "github.com/acme/x"),
        ("https://tok" + "@" + "github.com/acme/x.git", "github.com/acme/x"),
        ("ssh://git@gitlab.com/acme/x/", "gitlab.com/acme/x"),
    ],
)
def test_clean_remote_strips_credentials(url: str, expected: str) -> None:
    assert clean_remote(url) == expected


def test_slugify() -> None:
    assert slugify("My App!") == "my-app"
    assert slugify("***") == "projet"


def test_scan_projects_root_depth_one_skips_hidden(tmp_path: Path) -> None:
    (tmp_path / "a" / ".git").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "go.mod").write_text("module b", encoding="utf-8")
    (tmp_path / "c").mkdir()
    (tmp_path / ".hidden" / ".git").mkdir(parents=True)
    (tmp_path / "a" / "nested" / ".git").mkdir(parents=True)

    assert [p.name for p in scan_projects_root(tmp_path)] == ["a", "b"]
    assert scan_projects_root(tmp_path / "missing") == ()


def test_check_prereqs_reports_without_installing() -> None:
    present = {
        "uv": "/bin/uv",
        "git": "/bin/git",
        "docker": "/bin/docker",
        "agentrun": "/bin/agentrun",
    }
    checks = check_prereqs(present.get, run=lambda cmd: 1, port_open=lambda h, p: False)
    by_name = {c.name: c for c in checks}

    assert [c.name for c in checks] == [
        "uv",
        "git",
        "docker",
        "herdr",
        "claude",
        "agentrun",
    ]
    assert by_name["docker"].ok is False and "démon" in by_name["docker"].detail
    assert by_name["agentrun"].ok is True
    assert by_name["herdr"].ok is False and by_name["herdr"].required is False
    assert missing_required(checks) == ()
    assert "herdr" in render_checks(checks)


def test_check_prereqs_flags_missing_required_and_live_agentrun() -> None:
    checks = check_prereqs(
        lambda _: None, run=lambda cmd: 0, port_open=lambda h, p: p == 7340
    )
    assert [c.name for c in missing_required(checks)] == ["uv", "git"]
    assert {c.name: c.ok for c in checks}["agentrun"] is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:pa/ss" + AT + "github.com/acme/x.git", "github.com/acme/x"),
        (
            "https://user:p:a" + AT + "ss" + AT + "github.com/acme/x",
            "github.com/acme/x",
        ),
        ("ssh://git" + AT + "gitlab.com:2222/acme/x.git", "gitlab.com/acme/x"),
        ("user:tok" + AT + "github.com:acme/x.git", "github.com/acme/x"),
        ("github.com/acme/x", "github.com/acme/x"),
    ],
)
def test_clean_remote_strips_tricky_userinfo(url: str, expected: str) -> None:
    cleaned = clean_remote(url)

    assert cleaned == expected
    assert "pa" not in cleaned.split("/")[0] and "tok" not in cleaned


def test_describe_never_follows_symlinks_outside_the_project(tmp_path: Path) -> None:
    # Arrange: README and compose file are links to files outside the project.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("Leaked private plan.\n", encoding="utf-8")
    (outside / "compose.yml").write_text(
        "services:\n  x:\n    ports: ['4444:4444']\n", encoding="utf-8"
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").symlink_to(outside / "secret.md")
    (proj / "compose.yml").symlink_to(outside / "compose.yml")

    # Act
    markdown = describe_local_project(proj, lambda _: None)

    # Assert
    assert "Leaked" not in markdown
    assert "4444" not in markdown
    assert frontmatter.loads(markdown).metadata["agent_docs"] == []


def test_describe_skips_symlinked_project_folders(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(real)

    assert scan_projects_root(root) == ()


def test_describe_caps_reads_and_masks_secrets_in_purpose(tmp_path: Path) -> None:
    # Arrange: a README whose first paragraph leaks a key, and a huge manifest.
    proj = tmp_path / "proj"
    proj.mkdir()
    key = "sk-ant-" + "b" * 30
    (proj / "README.md").write_text(f"# P\n\nUses {key} here.\n", encoding="utf-8")
    huge = '{"description": "' + "y" * (scan.MAX_READ_BYTES * 2) + '"}'
    (proj / "package.json").write_text(huge, encoding="utf-8")

    # Act
    markdown = describe_local_project(proj, lambda _: None)

    # Assert
    assert key not in markdown
    assert frontmatter.loads(markdown).metadata["purpose"] == "Uses [masqué] here."
    assert scan._package_json(proj) == {}  # truncated at the cap -> not parsed
