"""`omniscient schedule`: renders systemd user units, writes only with --write, never
runs systemctl."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omniscient import cli, schedule
from omniscient.schedule import (
    UNITS,
    ScheduleError,
    omniscient_executable,
    render_units,
    unit_dir,
)


@pytest.fixture
def exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "bin" / "omniscient"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(schedule.shutil, "which", lambda _: str(binary))
    return binary


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("schedule must never run a command")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return tmp_path / "xdg" / "systemd" / "user"


def test_render_units_uses_the_absolute_executable_and_home(
    tmp_path: Path, exe: Path
) -> None:
    home = tmp_path / "my home"

    units = {u.name: u.text for u in render_units(home, omniscient_executable())}

    assert set(units) == set(UNITS)
    cycle = units["omniscient-cycle.service"]
    assert f'ExecStart="{exe.resolve()}" cycle' in cycle
    assert f"WorkingDirectory={home}" in cycle
    assert f'Environment="OMNISCIENT_HOME={home}"' in cycle
    assert "uv run" not in "".join(units.values())
    assert "{" not in "".join(units.values())


def test_render_units_escapes_percent_and_refuses_unsafe_paths(
    tmp_path: Path, exe: Path
) -> None:
    text = render_units(tmp_path / "100%", exe)[0].text
    assert "100%%" in text

    with pytest.raises(ScheduleError, match="systemd"):
        render_units(tmp_path / 'bad"name', exe)
    with pytest.raises(ScheduleError):
        render_units(Path("relative"), exe)


def test_executable_falls_back_to_argv0_then_explains(tmp_path: Path) -> None:
    script = tmp_path / "omniscient"
    script.write_text("x", encoding="utf-8")

    assert omniscient_executable(lambda _: None, str(script)) == script.resolve()
    with pytest.raises(ScheduleError, match="introuvable"):
        omniscient_executable(lambda _: None, str(tmp_path / "python"))


def test_unit_dir_follows_xdg_config_home(tmp_path: Path) -> None:
    assert unit_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "systemd/user"


def test_install_without_write_only_prints(exe: Path, config_home: Path) -> None:
    result = CliRunner().invoke(cli.app, ["schedule", "install"])

    assert result.exit_code == 0, result.output
    assert f'ExecStart="{exe.resolve()}" cycle' in result.output
    assert "--write" in result.output
    assert not config_home.exists()


def test_install_write_writes_units_and_prints_the_systemctl_command(
    exe: Path, config_home: Path
) -> None:
    result = CliRunner().invoke(cli.app, ["schedule", "install", "--write"])

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in config_home.iterdir()) == sorted(UNITS)
    assert (
        "systemctl --user daemon-reload && systemctl --user enable --now "
        "omniscient-cycle.timer"
    ) in result.output

    status = CliRunner().invoke(cli.app, ["schedule", "status"])
    assert status.output.count("à jour") == len(UNITS)


def test_uninstall_prints_disable_then_removes_only_with_write(
    exe: Path, config_home: Path
) -> None:
    CliRunner().invoke(cli.app, ["schedule", "install", "--write"])
    (config_home / "other.service").write_text("keep", encoding="utf-8")

    dry = CliRunner().invoke(cli.app, ["schedule", "uninstall"])
    assert "systemctl --user disable --now" in dry.output
    assert len(list(config_home.iterdir())) == len(UNITS) + 1

    done = CliRunner().invoke(cli.app, ["schedule", "uninstall", "--write"])
    assert done.exit_code == 0
    assert [p.name for p in config_home.iterdir()] == ["other.service"]


def test_status_reports_missing_units(exe: Path, config_home: Path) -> None:
    result = CliRunner().invoke(cli.app, ["schedule", "status"])

    assert result.output.count("absent") == len(UNITS)
