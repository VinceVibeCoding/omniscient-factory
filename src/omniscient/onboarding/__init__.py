"""Onboarding: `omniscient init`, `project add`, `project list` (plain functions; the CLI registers them)."""

from omniscient.onboarding.init_cmd import run_init
from omniscient.onboarding.prereqs import Check, check_prereqs
from omniscient.onboarding.projects import (
    OnboardingError,
    add_project,
    list_projects,
    write_registry_entry,
)

__all__ = [
    "Check",
    "OnboardingError",
    "add_project",
    "check_prereqs",
    "list_projects",
    "run_init",
    "write_registry_entry",
]
