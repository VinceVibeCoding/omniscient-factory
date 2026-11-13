"""Every test runs against an isolated, throwaway Omniscient home.

Tests must never read or write the developer's real home (vault, .env, registry), and must
pass in a fresh clone that has no home at all (CI, contributors). Tests that need a
specific layout still override OMNISCIENT_HOME or monkeypatch paths themselves.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omniscient.home import ENV_VAR, packaged


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    root = tmp_path_factory.mktemp("omniscient-home")
    (root / "config").mkdir()
    shutil.copy(
        packaged("templates") / "omniscient.toml", root / "config" / "omniscient.toml"
    )
    for folder in ("vault/news", "vault/ideas", "vault/ops", "repos", "pocs"):
        (root / folder).mkdir(parents=True)
    monkeypatch.setenv(ENV_VAR, str(root))
    return root
