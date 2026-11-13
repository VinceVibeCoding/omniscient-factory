from pathlib import Path

import pytest

from omniscient.registry import (
    Port,
    Project,
    RegistryError,
    find_port_conflicts,
    load_registry,
)


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / f"{name}.md").write_text(body, encoding="utf-8")


VALID = """---
name: alpha
path: /tmp/alpha
purpose: Test project alpha.
stack: [python]
ports:
  - {port: 3000, service: next dev, source: assumed}
services: []
remote: null
git: true
agent_docs: [README.md]
---
# alpha
"""


def test_load_registry_parses_project_frontmatter(tmp_path: Path) -> None:
    # Arrange
    _write(tmp_path, "alpha", VALID)

    # Act
    projects = load_registry(tmp_path)

    # Assert
    assert projects == (
        Project(
            name="alpha",
            path=Path("/tmp/alpha"),
            purpose="Test project alpha.",
            stack=("python",),
            ports=(Port(port=3000, service="next dev", source="assumed"),),
            services=(),
            remote=None,
            git=True,
            agent_docs=("README.md",),
            notes="",
        ),
    )


def test_load_registry_skips_readme_and_sorts_by_name(tmp_path: Path) -> None:
    _write(tmp_path, "README", "# index, no frontmatter")
    _write(tmp_path, "zeta", VALID.replace("alpha", "zeta"))
    _write(tmp_path, "alpha", VALID)

    names = [p.name for p in load_registry(tmp_path)]

    assert names == ["alpha", "zeta"]


def test_load_registry_raises_with_filename_when_required_field_missing(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "broken", VALID.replace("purpose: Test project alpha.\n", ""))

    with pytest.raises(RegistryError, match="broken.md.*purpose"):
        load_registry(tmp_path)


def test_load_registry_rejects_invalid_port(tmp_path: Path) -> None:
    _write(tmp_path, "badport", VALID.replace("port: 3000", "port: 70000"))

    with pytest.raises(RegistryError, match="badport.md"):
        load_registry(tmp_path)


def test_load_registry_wraps_malformed_yaml_in_registry_error(tmp_path: Path) -> None:
    _write(
        tmp_path, "badyaml", VALID.replace("purpose: Test", 'purpose: "Quoted" then')
    )

    with pytest.raises(RegistryError, match="badyaml.md.*cannot parse"):
        load_registry(tmp_path)


def test_load_registry_reports_missing_frontmatter(tmp_path: Path) -> None:
    _write(tmp_path, "nofm", "# just a heading\n")

    with pytest.raises(RegistryError, match="nofm.md: missing frontmatter"):
        load_registry(tmp_path)


def test_load_registry_reports_non_mapping_frontmatter(tmp_path: Path) -> None:
    _write(tmp_path, "listfm", "---\n- a\n- b\n---\n")

    with pytest.raises(RegistryError, match="listfm.md: .+"):
        load_registry(tmp_path)


def test_load_registry_rejects_duplicate_project_names(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", VALID)
    _write(tmp_path, "alpha-copy", VALID)

    with pytest.raises(RegistryError, match="duplicate project name: alpha"):
        load_registry(tmp_path)


def test_load_registry_rejects_relative_path(tmp_path: Path) -> None:
    _write(tmp_path, "rel", VALID.replace("path: /tmp/alpha", "path: alpha"))

    with pytest.raises(RegistryError, match="rel.md.*path"):
        load_registry(tmp_path)


def test_load_registry_rejects_unknown_and_coerced_fields(tmp_path: Path) -> None:
    _write(tmp_path, "extra", VALID.replace("git: true", "git: true\nfoo: 1"))
    _write(tmp_path, "coerce", VALID.replace("git: true", 'git: "yes"'))

    with pytest.raises(RegistryError, match="coerce.md.*git"):
        load_registry(tmp_path)


def test_load_registry_raises_when_directory_missing(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "nope")


def test_projects_are_immutable(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", VALID)
    project = load_registry(tmp_path)[0]

    with pytest.raises(AttributeError):
        project.name = "changed"  # type: ignore[misc]


def test_find_port_conflicts_groups_projects_sharing_a_port(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", VALID)
    _write(tmp_path, "beta", VALID.replace("alpha", "beta"))
    _write(
        tmp_path,
        "gamma",
        VALID.replace("alpha", "gamma").replace("port: 3000", "port: 4566"),
    )

    conflicts = find_port_conflicts(load_registry(tmp_path))

    assert conflicts == {3000: ("alpha", "beta")}


def test_find_port_conflicts_includes_proposed_project() -> None:
    existing = (
        Project(
            name="alpha",
            path=Path("/a"),
            purpose="a",
            stack=(),
            ports=(Port(8000, "fastapi", "assumed"),),
            services=(),
            remote=None,
            git=True,
            agent_docs=(),
            notes="",
        ),
    )
    proposed = Project(
        name="poc-x",
        path=Path("/p"),
        purpose="poc",
        stack=(),
        ports=(Port(8000, "api", "config"),),
        services=(),
        remote=None,
        git=False,
        agent_docs=(),
        notes="",
    )

    assert find_port_conflicts(existing, proposed=proposed) == {
        8000: ("alpha", "poc-x")
    }


def test_find_port_conflicts_proposed_replaces_same_named_project(
    tmp_path: Path,
) -> None:
    # Arrange: re-validating an updated "alpha" must not conflict with old "alpha"
    _write(tmp_path, "alpha", VALID)
    projects = load_registry(tmp_path)
    updated = projects[0]

    # Act
    conflicts = find_port_conflicts(projects, proposed=updated)

    # Assert
    assert conflicts == {}


def test_find_port_conflicts_ignores_duplicate_ports_within_one_project() -> None:
    project = Project(
        name="solo",
        path=Path("/s"),
        purpose="s",
        stack=(),
        ports=(Port(3000, "a", "config"), Port(3000, "b", "config")),
        services=(),
        remote=None,
        git=True,
        agent_docs=(),
        notes="",
    )

    assert find_port_conflicts((project,)) == {}


def test_registry_fixture_loads_and_flags_shared_dev_ports(tmp_path: Path) -> None:
    # Arrange: a small registry shaped like a real home's repos/ folder.
    _write(tmp_path, "README", "# index, no frontmatter")
    _write(tmp_path, "run-tracker", VALID.replace("alpha", "run-tracker"))
    _write(tmp_path, "card-index", VALID.replace("alpha", "card-index"))
    other = VALID.replace("alpha", "weekly-report").replace("port: 3000", "port: 8080")
    _write(tmp_path, "weekly-report", other)

    # Act
    projects = load_registry(tmp_path)
    conflicts = find_port_conflicts(projects)

    # Assert
    assert {p.name for p in projects} == {"run-tracker", "card-index", "weekly-report"}
    assert set(conflicts) == {3000}
