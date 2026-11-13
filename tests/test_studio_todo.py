import io
from pathlib import Path

import frontmatter
import pytest
from rich.console import Console
from typer.testing import CliRunner, Result

from omniscient import cli
from omniscient.studio import paths
from omniscient.studio.ideas import (
    StudioError,
    _set_section,
    append_atelier,
    load_card,
    new_seed,
)
from omniscient.studio.todo import (
    H_TODO,
    MAX_TODO_CHARS,
    TodoItem,
    add_todo,
    council_summary,
    last_atelier,
    pitch,
    read_todos,
    remove_todo,
    render_pane,
    set_done,
)

runner = CliRunner()
COUNCIL = (
    "## Conseil\n**Verdict :** poursuivre\n\n**Challengeur :** Déjà vu.\n\n"
    "**Simplificateur (plus petite version) :** Un script.\n\n"
    "**Gardien des risques :**\n- RGPD\n"
)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    monkeypatch.setattr(paths, "VAULT", vault)
    return vault


@pytest.fixture
def stem(vault: Path) -> str:
    return new_seed(vault, title="Radar des modèles", pitch="Un pitch court.").stem


def _note(vault: Path, stem: str) -> Path:
    return vault / "ideas" / f"{stem}.md"


def _invoke(*args: str) -> Result:
    return runner.invoke(cli.app, ["idea", *args])


def _plain(renderable: object) -> str:
    """Render at a fixed size, ignoring the real terminal and COLUMNS/LINES."""
    console = Console(
        file=io.StringIO(),
        width=120,
        height=50,
        record=True,
        color_system=None,
        force_terminal=False,
        force_jupyter=False,
        legacy_windows=False,
        soft_wrap=False,
        _environ={},
    )
    console.print(renderable)
    return console.export_text()


def test_add_creates_the_section_and_numbers_items(vault: Path, stem: str) -> None:
    first = add_todo(vault, stem, "Écrire le script")
    second = add_todo(vault, stem, "  Tester\nsur 3 cas  ")

    assert first == TodoItem(1, "Écrire le script", False)
    assert second == TodoItem(2, "Tester sur 3 cas", False)
    content = frontmatter.load(_note(vault, stem)).content
    assert content.count(H_TODO) == 1
    assert "- [ ] Écrire le script\n- [ ] Tester sur 3 cas" in content
    assert read_todos(_note(vault, stem)) == (first, second)


def test_add_keeps_frontmatter_and_other_sections(vault: Path, stem: str) -> None:
    add_todo(vault, stem, "Une tâche")
    append_atelier(vault, stem, "Une idée en plus.")
    add_todo(vault, stem, "Deux")

    card = load_card(_note(vault, stem))
    assert (card.title, card.status) == ("Radar des modèles", "seed")
    assert [t.text for t in read_todos(_note(vault, stem))] == ["Une tâche", "Deux"]
    assert "Une idée en plus." in _note(vault, stem).read_text("utf-8")


@pytest.mark.parametrize("text", ["", "   \n ", "x" * (MAX_TODO_CHARS + 1)])
def test_add_rejects_empty_or_too_long(vault: Path, stem: str, text: str) -> None:
    with pytest.raises(StudioError):
        add_todo(vault, stem, text)
    assert read_todos(_note(vault, stem)) == ()


def test_add_demotes_headings(vault: Path, stem: str) -> None:
    item = add_todo(vault, stem, "## pas une section")

    assert item.text == "### pas une section"
    assert frontmatter.load(_note(vault, stem)).content.count("\n## ") >= 1
    assert read_todos(_note(vault, stem))[0].text == "### pas une section"


def test_set_done_and_undo(vault: Path, stem: str) -> None:
    add_todo(vault, stem, "A")
    add_todo(vault, stem, "B")

    assert set_done(vault, stem, 2) == TodoItem(2, "B", True)
    assert "- [x] B" in _note(vault, stem).read_text("utf-8")
    assert set_done(vault, stem, 2, done=False).done is False
    assert [t.done for t in read_todos(_note(vault, stem))] == [False, False]


def test_remove_renumbers(vault: Path, stem: str) -> None:
    for text in ("A", "B", "C"):
        add_todo(vault, stem, text)

    remove_todo(vault, stem, 2)

    assert read_todos(_note(vault, stem)) == (
        TodoItem(1, "A", False),
        TodoItem(2, "C", False),
    )


@pytest.mark.parametrize("index", [0, 2, -1])
def test_bad_index_raises(vault: Path, stem: str, index: int) -> None:
    add_todo(vault, stem, "A")

    with pytest.raises(StudioError, match="introuvable"):
        set_done(vault, stem, index)
    with pytest.raises(StudioError):
        remove_todo(vault, stem, index)


def test_unknown_idea_raises(vault: Path, stem: str) -> None:
    with pytest.raises(StudioError):
        add_todo(vault, "ghost", "A")


def test_read_ignores_non_item_lines(vault: Path, stem: str) -> None:
    path = _note(vault, stem)
    post = frontmatter.load(path)
    post.content += (
        f"\n\n{H_TODO}\nUne remarque\n- [X] fait\n- pas une case\n- [ ] ouvert\n"
    )
    path.write_text(frontmatter.dumps(post), "utf-8")

    assert read_todos(path) == (TodoItem(1, "fait", True), TodoItem(2, "ouvert", False))
    remove_todo(vault, stem, 1)
    assert "Une remarque" in path.read_text("utf-8")


def test_compact_helpers() -> None:
    content = (
        "# Titre\n\nLe pitch.\n\n**Pourquoi :** maintenant.\n\n"
        + COUNCIL
        + "\n## Atelier\n"
        + "\n\n".join(f"**0{i}-11-2026 10:00**\nEntrée {i}" for i in range(1, 6))
    )

    assert pitch(content) == "Le pitch.\n\n**Pourquoi :** maintenant."
    summary = council_summary(content)
    assert "poursuivre" in summary and "Un script." in summary
    assert "Déjà vu" not in summary and "RGPD" not in summary
    entries = last_atelier(content)
    assert len(entries) == 3
    assert entries[0].endswith("Entrée 3") and entries[-1].endswith("Entrée 5")


def test_render_pane_compact_has_no_frontmatter(vault: Path, stem: str) -> None:
    path = _note(vault, stem)
    post = frontmatter.load(path)
    post.content = _set_section(post.content, "## Conseil", COUNCIL.split("\n", 1)[1])
    path.write_text(frontmatter.dumps(post), "utf-8")
    add_todo(vault, stem, "A")
    add_todo(vault, stem, "B")
    set_done(vault, stem, 1)

    out = _plain(render_pane(path))

    assert "Radar des modèles" in out and "graine" in out
    assert "À faire  1/2" in out
    assert "☑ A" in out and "☐ B" in out
    assert "Un pitch court." in out and "Un script." in out
    assert "Gardien" not in out and "created:" not in out
    assert "Gardien" in _plain(render_pane(path, full=True))


def test_cli_todo_round_trip(vault: Path, stem: str) -> None:
    assert _invoke("todo-add", stem, "Écrire le script").exit_code == 0
    assert _invoke("todo-add", stem, "Tester").exit_code == 0

    done = _invoke("todo-done", stem, "1")
    assert done.exit_code == 0, done.output
    assert "faite" in done.output
    listed = _invoke("todo", stem)
    assert "1/2" in listed.output and "☑ Écrire le script" in listed.output
    assert _invoke("todo-undo", stem, "1").exit_code == 0
    assert _invoke("todo-rm", stem, "1").exit_code == 0
    assert [t.text for t in read_todos(_note(vault, stem))] == ["Tester"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("todo-done", "{stem}", "9"), "introuvable"),
        (("todo-rm", "{stem}", "0"), "introuvable"),
        (("todo-add", "{stem}", "  "), "tâche refusée"),
        (("todo", "ghost"), "aucune idée nommée"),
        (("todo-add", "../x", "A"), "aucune idée nommée"),
    ],
)
def test_cli_errors_are_french(stem: str, args: tuple[str, ...], message: str) -> None:
    result = _invoke(*(a.format(stem=stem) for a in args))

    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


def test_cli_show_full_and_compact(vault: Path, stem: str) -> None:
    add_todo(vault, stem, "Une tâche")

    compact = _invoke("show", stem)
    full = _invoke("show", stem, "--full")

    assert compact.exit_code == 0 and full.exit_code == 0
    assert "☐ Une tâche" in compact.output and "0/1" in compact.output
    assert "Une tâche" in full.output and "Sources" in full.output


@pytest.mark.parametrize(
    "front", ["---\ntitle: [oops\n---\nbody\n", "---\n!!python/name:os.system\n---\n"]
)
def test_bad_frontmatter_is_a_french_studio_error(
    vault: Path, stem: str, front: str
) -> None:
    path = _note(vault, stem)
    path.write_text(front, encoding="utf-8")
    with pytest.raises(StudioError, match="note illisible"):
        read_todos(path)
    with pytest.raises(StudioError):
        render_pane(path)
