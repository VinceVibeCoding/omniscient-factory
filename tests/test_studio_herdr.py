"""Atelier layout through a fake herdr runner (no real herdr, no server)."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omniscient.studio import herdr as herdr_mod
from omniscient.studio.agents import AgentChoice
from omniscient.studio.herdr import (
    Atelier,
    HerdrError,
    default_runner,
    slugify,
    tag,
)
from omniscient.studio.ideas import IdeaCard

STEM = "2026-11-12 l-atelier-de-jean-s-idea"
RIFF_LABEL = f"atelier · L'atelier de Jean's idea #{tag('riff', STEM, 'claude')}"
RIFF_AGENT = f"riff-l-atelier-de-jea-{tag('riff', f'{STEM}\x1fclaude')}"


def make_card(stem: str = STEM, title: str = "L'atelier de Jean's idea") -> IdeaCard:
    return IdeaCard(
        stem=stem,
        path=Path(f"/vault/ideas/{stem}.md"),
        title=title,
        status="debated",
        kind="new-build",
        score=0.6,
        created="2026-11-12",
        projects=(),
        sources=(),
        origin="council",
        parent=None,
        parents=(),
        variant=1,
        poc_url=None,
        poc_verdict=None,
        owner_verdict=None,
    )


class FakeHerdr:
    """Records argv and answers like herdr 0.9.5."""

    def __init__(self, workspaces: list[dict] | None = None, running: bool = True):
        self.calls: list[list[str]] = []
        self.workspaces = workspaces or []
        self.running = running
        self.splits = 0

    def __call__(self, args: Sequence[str]) -> str:
        argv = list(args)
        self.calls.append(argv)
        key = tuple(argv[:2])
        if key == ("status", "server"):
            return json.dumps(
                {
                    "status": "running" if self.running else "not_running",
                    "running": self.running,
                }
            )
        if key == ("workspace", "list"):
            return json.dumps(
                {
                    "id": "cli:workspace:list",
                    "result": {"type": "workspace_list", "workspaces": self.workspaces},
                }
            )
        if key == ("workspace", "create"):
            return json.dumps(
                {
                    "id": "x",
                    "result": {
                        "type": "workspace_created",
                        "workspace": {
                            "workspace_id": "w7",
                            "label": argv[argv.index("--label") + 1],
                        },
                        "tab": {"tab_id": "w7:t1"},
                        "root_pane": {"pane_id": "w7:p1"},
                    },
                }
            )
        if key == ("pane", "split"):
            self.splits += 1
            return json.dumps(
                {
                    "id": "x",
                    "result": {
                        "type": "pane_info",
                        "pane": {"pane_id": f"w7:p{self.splits + 1}"},
                    },
                }
            )
        return json.dumps({"id": "x", "result": {"type": "ok"}})

    def commands(self) -> list[tuple[str, str]]:
        return [(c[0], c[1]) for c in self.calls]


def test_open_idea_builds_two_by_two_layout(tmp_path: Path) -> None:
    fake = FakeHerdr()
    workspace = Atelier(tmp_path, runner=fake).open_idea(make_card(), "riff")

    assert workspace == "w7"
    assert fake.commands() == [
        ("workspace", "list"),
        ("workspace", "create"),
        ("agent", "start"),
        ("agent", "prompt"),
        ("pane", "rename"),
        ("pane", "split"),
        ("pane", "run"),
        ("pane", "rename"),
        ("pane", "split"),
        ("pane", "run"),
        ("pane", "rename"),
        ("pane", "split"),
        ("pane", "run"),
        ("pane", "rename"),
    ]
    create, start, prompt, rename_agent = fake.calls[1:5]
    right, run_todo, rename_todo, down, run_ui, rename_ui = fake.calls[5:11]
    war, run_war, rename_war = fake.calls[11:]
    assert create == [
        "workspace",
        "create",
        "--cwd",
        str(tmp_path),
        "--label",
        RIFF_LABEL,
        "--focus",
    ]
    assert start[:3] == ["agent", "start", RIFF_AGENT]
    assert start[3:7] == ["--kind", "claude", "--pane", "w7:p1"]
    assert prompt == [
        "agent",
        "prompt",
        RIFF_AGENT,
        f"/omniscient-riff {shlex.quote(STEM)}",
    ]
    assert "--wait" not in prompt
    assert rename_agent == ["pane", "rename", "w7:p1", "agent"]
    assert right[:5] == ["pane", "split", "w7:p1", "--direction", "right"]
    assert right[right.index("--ratio") + 1] == "0.42"
    assert run_todo == [
        "pane",
        "run",
        "w7:p2",
        f"uv run omniscient idea show '{STEM}' --follow",
    ]
    assert rename_todo == ["pane", "rename", "w7:p2", "à faire"]
    assert down[:5] == ["pane", "split", "w7:p2", "--direction", "down"]
    assert "--ratio" not in down
    assert run_ui == ["pane", "run", "w7:p3", f"uv run omniscient idea ui '{STEM}'"]
    assert rename_ui == ["pane", "rename", "w7:p3", "UI"]
    assert war[:5] == ["pane", "split", "w7:p1", "--direction", "down"]
    assert war[war.index("--ratio") + 1] == "0.3"
    assert run_war == [
        "pane",
        "run",
        "w7:p4",
        f"uv run omniscient warroom --idea '{STEM}' --follow",
    ]
    assert rename_war == ["pane", "rename", "w7:p4", "salle de crise"]
    assert all(c[:2] != ["pane", "send-text"] for c in fake.calls)
    assert not any("omniscient build" in " ".join(c) for c in fake.calls)


def test_variant_move_names_agent_and_skill(tmp_path: Path) -> None:
    fake = FakeHerdr()
    Atelier(tmp_path, runner=fake).open_idea(make_card(), "variant")
    start, prompt = fake.calls[2], fake.calls[3]
    assert start[2].startswith("variant-") and len(start[2]) <= 32
    assert prompt[3] == f"/omniscient-variant {shlex.quote(STEM)}"


def test_existing_workspace_is_focused_not_duplicated(tmp_path: Path) -> None:
    fake = FakeHerdr(
        workspaces=[
            {"workspace_id": "w1", "label": "autre"},
            {"workspace_id": "w3", "label": RIFF_LABEL},
        ]
    )
    workspace = Atelier(tmp_path, runner=fake).open_idea(make_card(), "riff")
    assert workspace == "w3"
    assert fake.calls == [["workspace", "list"], ["workspace", "focus", "w3"]]


def test_stem_with_quote_is_shell_quoted(tmp_path: Path) -> None:
    stem = "2026-11-12 jean's idea"
    fake = FakeHerdr()
    Atelier(tmp_path, runner=fake).open_idea(make_card(stem=stem), "riff")
    quoted = "'2026-11-12 jean'\"'\"'s idea'"
    assert fake.calls[6][3] == f"uv run omniscient idea show {quoted} --follow"
    assert fake.calls[9][3] == f"uv run omniscient idea ui {quoted}"
    assert fake.calls[12][3] == f"uv run omniscient warroom --idea {quoted} --follow"


def test_remix_needs_two_or_three_cards(tmp_path: Path) -> None:
    atelier = Atelier(tmp_path, runner=FakeHerdr())
    with pytest.raises(HerdrError):
        atelier.open_remix([make_card()])
    with pytest.raises(HerdrError):
        atelier.open_remix([make_card()] * 4)


def test_remix_prompts_with_quoted_stems(tmp_path: Path) -> None:
    a = make_card(stem="2026-11-12 alpha idea", title="Alpha très longue idée")
    b = make_card(stem="2026-11-13 beta", title="Beta")
    fake = FakeHerdr()
    assert Atelier(tmp_path, runner=fake).open_remix([a, b]) == "w7"
    create = fake.calls[1]
    assert create[create.index("--label") + 1].startswith(
        "remix · Alpha très l + Beta #"
    )
    assert fake.calls[3] == [
        "agent",
        "prompt",
        fake.calls[2][2],
        "/omniscient-remix '2026-11-12 alpha idea' '2026-11-13 beta'",
    ]
    assert ("pane", "split") not in fake.commands()


def test_seed_rejects_empty_text(tmp_path: Path) -> None:
    fake = FakeHerdr()
    with pytest.raises(HerdrError):
        Atelier(tmp_path, runner=fake).open_seed("   ")
    assert fake.calls == []


def test_seed_opens_claude_with_raw_text(tmp_path: Path) -> None:
    fake = FakeHerdr()
    Atelier(tmp_path, runner=fake).open_seed(
        "  Un éditeur de GIF pour retours design  "
    )
    create = fake.calls[1]
    assert create[create.index("--label") + 1].startswith(
        "graine · Un éditeur de GIF pour r #"
    )
    assert fake.calls[2][2].startswith("seed-un-editeur-de-gi-")
    assert len(fake.calls[2][2]) <= 32
    assert fake.calls[3][3] == "/omniscient-seed Un éditeur de GIF pour retours design"


def test_runner_error_propagates_as_herdr_error(tmp_path: Path) -> None:
    def broken(args: Sequence[str]) -> str:
        raise HerdrError("no herdr server is running")

    with pytest.raises(HerdrError, match="no herdr server"):
        Atelier(tmp_path, runner=broken).open_idea(make_card(), "riff")


def test_unexpected_shape_is_a_clear_error(tmp_path: Path) -> None:
    def odd(args: Sequence[str]) -> str:
        if list(args[:2]) == ["workspace", "list"]:
            return json.dumps({"result": {"workspaces": []}})
        return json.dumps({"result": {"workspace": {}}})

    with pytest.raises(HerdrError, match="workspace_id"):
        Atelier(tmp_path, runner=odd).open_idea(make_card(), "riff")


def test_non_json_output_is_a_herdr_error(tmp_path: Path) -> None:
    with pytest.raises(HerdrError, match="illisible"):
        Atelier(tmp_path, runner=lambda args: "oops").open_seed("x")


def test_unknown_move_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(HerdrError):
        Atelier(tmp_path, runner=FakeHerdr()).open_idea(make_card(), "remix")  # type: ignore[arg-type]


def test_available_reflects_server_status(tmp_path: Path) -> None:
    assert Atelier(tmp_path, runner=FakeHerdr(running=True)).available() is True
    assert Atelier(tmp_path, runner=FakeHerdr(running=False)).available() is False

    def broken(args: Sequence[str]) -> str:
        raise HerdrError("boom")

    assert Atelier(tmp_path, runner=broken).available() is False
    assert (
        Atelier(tmp_path, runner=lambda a: "status: not running").available() is False
    )


def test_slugify_is_ascii_and_bounded() -> None:
    assert slugify("Éditeur  GIF — v2!") == "editeur-gif-v2"
    assert slugify("!!!") == "idee"
    assert len(slugify("a" * 80)) == 24


def _completed(
    code: int, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["herdr"], code, stdout=out, stderr=err)


def test_default_runner_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(0, out='{"result": {}}')

    monkeypatch.setattr(herdr_mod.subprocess, "run", fake_run)
    assert default_runner(["workspace", "list"]) == '{"result": {}}'
    assert seen == [["herdr", "workspace", "list"]]


def test_default_runner_raises_json_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = json.dumps(
        {"id": "x", "error": {"code": "server_not_running", "message": "no server"}}
    )
    monkeypatch.setattr(
        herdr_mod.subprocess, "run", lambda *a, **k: _completed(1, err=err)
    )
    with pytest.raises(HerdrError, match="no server"):
        default_runner(["workspace", "list"])


def test_default_runner_wraps_missing_binary_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*a: object, **k: object) -> None:
        raise FileNotFoundError("herdr")

    monkeypatch.setattr(herdr_mod.subprocess, "run", missing)
    with pytest.raises(HerdrError, match="introuvable"):
        default_runner(["status", "server"])

    def slow(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired("herdr", 60)

    monkeypatch.setattr(herdr_mod.subprocess, "run", slow)
    with pytest.raises(HerdrError, match="délai"):
        default_runner(["status", "server"])


def test_variant_and_riff_get_distinct_workspaces(tmp_path: Path) -> None:
    fake = FakeHerdr()
    atelier = Atelier(tmp_path, runner=fake)
    atelier.open_idea(make_card(), "riff")
    atelier.open_idea(make_card(), "variant")

    labels = [
        c[c.index("--label") + 1]
        for c in fake.calls
        if c[:2] == ["workspace", "create"]
    ]
    assert len(set(labels)) == 2 and labels[1].startswith("variante · ")


class FailingStart(FakeHerdr):
    def __call__(self, args):  # type: ignore[override]
        if list(args[:2]) == ["agent", "start"]:
            self.calls.append(list(args))
            raise HerdrError("claude n'a pas démarré")
        return super().__call__(args)


def test_half_built_workspace_is_closed_on_failure(tmp_path: Path) -> None:
    fake = FailingStart()

    with pytest.raises(HerdrError, match="démarré"):
        Atelier(tmp_path, runner=fake).open_idea(make_card(), "riff")

    assert fake.calls[-1] == ["workspace", "close", "w7"]


COPILOT = AgentChoice("copilot", "GitHub Copilot CLI", "herdr", "copilot", None)
ORACLE = AgentChoice(
    "or-debater", "Omniscient · debater (crédit API)", "omniscient", None, "debater"
)


def test_copilot_agent_gets_french_context_prompt(tmp_path: Path) -> None:
    fake = FakeHerdr()
    Atelier(tmp_path, runner=fake).open_idea(make_card(), "variant", COPILOT)
    start, prompt = fake.calls[2], fake.calls[3]
    assert start[3:5] == ["--kind", "copilot"]
    assert len(start[2]) <= 32 and start[2] != RIFF_AGENT
    text = prompt[3]
    assert text.startswith(
        f"Lis le contexte avec `uv run omniscient idea context '{STEM}'`"
    )
    assert "faire une variante de cette idée" in text
    assert f"uv run omniscient idea todo-add '{STEM}'" in text
    assert "N'exécute jamais `omniscient build`" in text
    label = fake.calls[1][fake.calls[1].index("--label") + 1]
    assert label.endswith(f"#{tag('variant', STEM, 'copilot')}")


def test_each_agent_gets_its_own_workspace(tmp_path: Path) -> None:
    fake = FakeHerdr(workspaces=[{"workspace_id": "w3", "label": RIFF_LABEL}])
    workspace = Atelier(tmp_path, runner=fake).open_idea(make_card(), "riff", COPILOT)
    assert workspace == "w7"  # Claude's riff atelier is not reused for Copilot


def test_omniscient_agent_runs_chat_in_agent_pane(tmp_path: Path) -> None:
    fake = FakeHerdr()
    Atelier(tmp_path, runner=fake).open_idea(make_card(), "riff", ORACLE)
    assert ("agent", "start") not in fake.commands()
    assert fake.calls[2] == [
        "pane",
        "run",
        "w7:p1",
        f"uv run omniscient chat debater --idea '{STEM}'",
    ]
    assert fake.calls[3] == ["pane", "rename", "w7:p1", "agent"]
    assert fake.calls[-1] == ["pane", "rename", "w7:p4", "salle de crise"]


def test_skill_creator_asks_claude_for_a_project_skill(tmp_path: Path) -> None:
    fake = FakeHerdr()
    Atelier(tmp_path, runner=fake).open_skill_creator("  résumer\nles PR  ")
    label = fake.calls[1][fake.calls[1].index("--label") + 1]
    assert label.startswith("skill · résumer les PR #")
    assert fake.calls[2][4] == "claude"
    prompt = fake.calls[3][3]
    assert "skill-creator" in prompt and ".claude/skills/<nom>/SKILL.md" in prompt
    assert prompt.endswith("Besoin du propriétaire : résumer les PR")
    assert ("pane", "split") not in fake.commands()


def test_skill_creator_rejects_empty_or_huge_text(tmp_path: Path) -> None:
    fake = FakeHerdr()
    atelier = Atelier(tmp_path, runner=fake)
    with pytest.raises(HerdrError):
        atelier.open_skill_creator("  ")
    with pytest.raises(HerdrError, match="trop longue"):
        atelier.open_skill_creator("x" * 301)
    assert fake.calls == []


def test_warroom_opens_once_then_is_reused(tmp_path: Path) -> None:
    fake = FakeHerdr()
    assert Atelier(tmp_path, runner=fake).open_warroom() == "w7"
    assert fake.calls[1][fake.calls[1].index("--label") + 1] == "salle de crise"
    assert fake.calls[2] == [
        "pane",
        "run",
        "w7:p1",
        "uv run omniscient warroom --follow",
    ]
    assert ("agent", "start") not in fake.commands()

    again = FakeHerdr(workspaces=[{"workspace_id": "w2", "label": "salle de crise"}])
    assert Atelier(tmp_path, runner=again).open_warroom() == "w2"
    assert again.calls[-1] == ["workspace", "focus", "w2"]


class FailingSplit(FakeHerdr):
    def __call__(self, args):  # type: ignore[override]
        if list(args[:2]) == ["pane", "split"] and self.splits == 1:
            self.calls.append(list(args))
            raise HerdrError("split refusé")
        return super().__call__(args)


def test_layout_failure_closes_workspace(tmp_path: Path) -> None:
    fake = FailingSplit()
    with pytest.raises(HerdrError, match="split refusé"):
        Atelier(tmp_path, runner=fake).open_idea(make_card(), "riff")
    assert fake.calls[-1] == ["workspace", "close", "w7"]
