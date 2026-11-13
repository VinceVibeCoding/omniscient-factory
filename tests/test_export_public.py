"""scripts/export_public.py: allowlist copy + leak scan (fixtures build secrets at runtime
so this file never trips the scanner itself)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_public.py"
_spec = importlib.util.spec_from_file_location("export_public", _SCRIPT)
assert _spec is not None and _spec.loader is not None
export_public = importlib.util.module_from_spec(_spec)
sys.modules["export_public"] = export_public
_spec.loader.exec_module(export_public)

FAKE_SLACK = "xo" + "xb-" + "1234567890-abcdefghij"
FAKE_HOME = "/ho" + "me/alice/projects"
FAKE_EMAIL = "alice" + "@" + "corp.io"


def _write(root: Path, rel: str, text: str = "ok\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    for rel in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        ".env.example",
        "src/omniscient/__init__.py",
        "tests/test_x.py",
        "docs/agents/domain.md",
        "docs/AGENTS.public.md",
        "AGENTS.md",
        "scripts/export_public.py",
        "scripts/spike.py",
        ".env",
        ".env.local",
        "vault/ideas/a.md",
        "repos/x.md",
        "pocs/p/run.sh",
        "config/omniscient.toml",
        "agentrun/builder.json",
        ".claude/skills/s/SKILL.md",
        ".omniscient/state.json",
        "slack/manifest.yaml",
        "ressources_repo.txt",
        "src/omniscient/__pycache__/x.cpython-312.pyc",
        ".venv/lib/x.py",
    ):
        _write(root, rel)
    _write(root, "docs/AGENTS.public.md", "public agents\n")
    return root


def test_plan_copies_only_the_allowlist(src: Path) -> None:
    # Act
    rels = {rel for _, rel in export_public.plan_export(src)}

    # Assert
    assert rels == {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        ".env.example",
        "src/omniscient/__init__.py",
        "tests/test_x.py",
        "docs/agents/domain.md",
        "AGENTS.md",
        "scripts/export_public.py",
        "slack/manifest.yaml",
    }


def test_public_agents_doc_replaces_the_private_one(src: Path, tmp_path: Path) -> None:
    # Act
    _, hits = export_public.export(src, tmp_path / "out", markers=[])

    # Assert
    assert hits == []
    assert (tmp_path / "out" / "AGENTS.md").read_text() == "public agents\n"
    assert not (tmp_path / "out" / "vault").exists()
    assert not (tmp_path / "out" / ".env").exists()


def test_dry_run_writes_nothing(src: Path, tmp_path: Path) -> None:
    # Act
    plan, _ = export_public.export(src, tmp_path / "out", markers=[], dry_run=True)

    # Assert
    assert plan and not (tmp_path / "out").exists()


def test_refuses_non_empty_destination_and_dest_inside_src(
    src: Path, tmp_path: Path
) -> None:
    # Arrange
    _write(tmp_path / "busy", "x.txt")

    # Act / Assert
    with pytest.raises(export_public.ExportError):
        export_public.export(src, tmp_path / "busy", markers=[])
    with pytest.raises(export_public.ExportError):
        export_public.export(src, src / "out", markers=[])


def test_scan_finds_secrets_home_paths_markers_and_emails() -> None:
    # Arrange
    text = f"token = '{FAKE_SLACK}'\npath = '{FAKE_HOME}'\nby AcmeOwner\nmail {FAKE_EMAIL}\n"

    # Act
    hits = export_public.scan_text("a.py", text, markers=["acmeowner"])

    # Assert
    kinds = {(h.line, h.kind.split()[0]) for h in hits}
    assert kinds == {(1, "secret"), (2, "secret"), (3, "marker"), (4, "e-mail")}
    assert all(FAKE_SLACK not in h.render() for h in hits)


def test_scan_allows_noreply_example_and_ssh_remote_emails() -> None:
    # Arrange
    text = "noreply@anthropic.com bob@example.com git@github.com:o/r.git\n"

    # Act / Assert
    assert export_public.scan_text("a.md", text, markers=[]) == []


def test_allowance_only_applies_to_its_file() -> None:
    # Arrange
    allow = [export_public.parse_allowance("LICENSE:Copyright (c) 2026 Acme Owner")]
    text = "Copyright (c) 2026 Acme Owner\n"

    # Act
    in_license = export_public.scan_text("LICENSE", text, ["Acme Owner"], allow)
    elsewhere = export_public.scan_text("README.md", text, ["Acme Owner"], allow)

    # Assert
    assert in_license == [] and len(elsewhere) == 1


def test_parse_allowance_rejects_missing_text() -> None:
    with pytest.raises(export_public.ExportError):
        export_public.parse_allowance("LICENSE")


def test_file_names_are_scanned_too(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "acmeowner.md"
    path.write_text("ok\n")

    # Act
    hits = export_public.scan_plan([(path, "docs/acmeowner.md")], ["AcmeOwner"])

    # Assert
    assert [h.line for h in hits] == [0]


def test_main_exit_codes(
    src: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    _write(src, "README.md", "made by AcmeOwner\n")

    # Act
    dirty = export_public.main([str(src), str(tmp_path / "a"), "--marker", "AcmeOwner"])
    clean = export_public.main(
        [
            str(src),
            str(tmp_path / "b"),
            "--marker",
            "AcmeOwner",
            "--allow",
            "README.md:AcmeOwner",
        ]
    )
    bad = export_public.main([str(src), str(tmp_path / "c"), "--allow", "nocolon"])

    # Assert
    assert (dirty, clean, bad) == (1, 0, 2)
    assert "README.md:1: marker" in capsys.readouterr().out
