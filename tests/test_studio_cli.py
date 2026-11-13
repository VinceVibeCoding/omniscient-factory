import json
from datetime import UTC, date, datetime
from pathlib import Path

import frontmatter
import pytest
from typer.testing import CliRunner, Result

from omniscient import cli
from omniscient.brain import NewsItem
from omniscient.brain.notes import write_news
from omniscient.home import packaged
from omniscient.studio import paths
from omniscient.studio.ideas import H_ATELIER, list_ideas, load_card, new_seed

REAL_CONFIG = packaged("templates") / "omniscient.toml"  # default scoring weights
SCORES = {"novelty": 0.8, "impact": 0.7, "feasibility": 0.9, "fit": 0.6, "effort": 0.3}

runner = CliRunner()


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    repos = tmp_path / "repos"
    repos.mkdir()
    monkeypatch.setattr(paths, "VAULT", vault)
    monkeypatch.setattr(paths, "REPOS", repos)
    monkeypatch.setattr(paths, "CONFIG", REAL_CONFIG)  # read-only: scoring weights
    return vault


def _invoke(*args: str, stdin: str | None = None) -> Result:
    return runner.invoke(cli.app, ["idea", *args], input=stdin)


def _seed(vault: Path, title: str = "Radar des modèles", **kw: object) -> str:
    return new_seed(vault, title=title, pitch="Un pitch.", **kw).stem  # type: ignore[arg-type]


def test_list_json_returns_the_cards(vault: Path) -> None:
    stem = _seed(vault)

    result = _invoke("list", "--json")

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows == [
        {
            "stem": stem,
            "title": "Radar des modèles",
            "status": "seed",
            "score": None,
            "kind": "new-build",
            "origin": "owner",
            "parent": None,
            "variant": 1,
            "poc_url": None,
        }
    ]


def test_list_filters_by_status_and_rejects_unknown(vault: Path) -> None:
    _seed(vault)

    assert json.loads(_invoke("list", "--status", "debated", "--json").output) == []
    bad = _invoke("list", "--status", "ghost")
    assert bad.exit_code == 1
    assert "statut inconnu" in bad.output


def test_list_table_shows_titles(vault: Path) -> None:
    _seed(vault)

    result = _invoke("list")

    assert result.exit_code == 0
    assert "Radar des modèles" in result.output


def test_new_prints_only_the_stem(vault: Path) -> None:
    parent = _seed(vault, "Parent")

    result = _invoke(
        "new",
        "--title", "Remix neuf",
        "--pitch", "On mélange.",
        "--project", "sec-lab",
        "--source", "abc",
        "--kind", "showcase",
        "--origin", "remix",
        "--parent", parent,
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    stem = result.output.strip()
    assert "\n" not in stem
    card = load_card(vault / "ideas" / f"{stem}.md")
    assert (card.title, card.kind, card.origin) == ("Remix neuf", "showcase", "remix")
    assert card.parents == (parent,) and card.projects == ("sec-lab",)


@pytest.mark.parametrize(
    "extra",
    [("--kind", "weird"), ("--origin", "variant"), ("--parent", "ghost")],
)
def test_new_rejects_bad_input(vault: Path, extra: tuple[str, str]) -> None:
    result = _invoke("new", "--title", "T", "--pitch", "P", *extra)

    assert result.exit_code == 1
    assert "Erreur" in result.output
    assert list_ideas(vault) == ()


def test_variant_prints_the_new_stem(vault: Path) -> None:
    parent = _seed(vault)

    result = _invoke("variant", parent, "--pitch", "Version équipe.")

    assert result.exit_code == 0, result.output
    card = load_card(vault / "ideas" / f"{result.output.strip()}.md")
    assert card.parent == parent and card.variant == 2
    assert card.title == "v2 · Radar des modèles"


def test_variant_of_unknown_idea_fails_in_french(vault: Path) -> None:
    result = _invoke("variant", "ghost", "--pitch", "x")

    assert result.exit_code == 1
    assert "aucune idée nommée" in result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.output


def test_append_reads_stdin(vault: Path) -> None:
    stem = _seed(vault)

    result = _invoke("append", stem, stdin="Plus petite version : un script.\n")

    assert result.exit_code == 0, result.output
    body = (vault / "ideas" / f"{stem}.md").read_text(encoding="utf-8")
    assert H_ATELIER in body and "Plus petite version : un script." in body


def test_append_empty_stdin_fails(vault: Path) -> None:
    stem = _seed(vault)

    result = _invoke("append", stem, stdin="  \n")

    assert result.exit_code == 1
    assert "rien à ajouter" in result.output


def test_debate_records_scores_and_prints_the_score(vault: Path) -> None:
    stem = _seed(vault)
    payload = json.dumps({"council": "**Verdict :** poursuivre", "scores": SCORES})

    result = _invoke("debate", stem, stdin=payload)

    assert result.exit_code == 0, result.output
    card = load_card(vault / "ideas" / f"{stem}.md")
    assert card.status == "debated"
    assert result.output.strip() == f"{card.score:.4f}"


@pytest.mark.parametrize(
    ("stdin", "message"),
    [
        ("{not json", "JSON invalide"),
        ("[1, 2]", "doit être un objet"),
        (json.dumps({"scores": SCORES}), "council"),
        (json.dumps({"council": "x", "scores": {"novelty": 0.5}}), "manquants"),
        (json.dumps({"council": "x", "scores": {**SCORES, "fit": "high"}}), "nombre"),
        (
            json.dumps({"council": "x", "scores": {**SCORES, "fit": 1.5}}),
            "score invalide",
        ),
    ],
)
def test_debate_bad_input_exits_1_in_french(
    vault: Path, stdin: str, message: str
) -> None:
    stem = _seed(vault)

    result = _invoke("debate", stem, stdin=stdin)

    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output
    assert load_card(vault / "ideas" / f"{stem}.md").status == "seed"


def test_context_includes_note_sources_and_projects(vault: Path) -> None:
    news = write_news(
        vault,
        NewsItem(
            source="openai",
            url="https://openai.com/index/x",
            title="Nouveau modèle X",
            published=date(2026, 11, 1),
            ingested=datetime(2026, 11, 2, tzinfo=UTC),
            summary="Un résumé de la news.",
            tags=(),
        ),
    )
    news_id = str(frontmatter.load(news)["id"])
    (paths.REPOS / "sec-lab.md").write_text("# sec-lab\nProjet de sécurité.\n", "utf-8")
    stem = _seed(vault, projects=("sec-lab", "absent"), sources=(news_id, "unknown"))

    result = _invoke("context", stem)

    assert result.exit_code == 0, result.output
    out = result.output
    assert "Radar des modèles" in out
    assert "Un résumé de la news." in out
    assert "Projet de sécurité." in out
    assert (
        out.index("Radar des modèles") < out.index("Un résumé") < out.index("Projet de")
    )


def test_verdict_applies_owner_choice(vault: Path) -> None:
    stem = _seed(vault)

    result = _invoke("verdict", stem, "drop")

    assert result.exit_code == 0, result.output
    card = load_card(vault / "ideas" / f"{stem}.md")
    assert (card.owner_verdict, card.status) == ("drop", "dropped")


def test_verdict_rejects_unknown_choice(vault: Path) -> None:
    stem = _seed(vault)

    result = _invoke("verdict", stem, "maybe")

    assert result.exit_code == 1
    assert "verdict inconnu" in result.output


def test_show_renders_the_note(vault: Path) -> None:
    stem = _seed(vault)

    result = _invoke("show", stem)

    assert result.exit_code == 0, result.output
    assert "Radar des modèles" in result.output


def test_show_refuses_path_traversal(vault: Path) -> None:
    _seed(vault)

    result = _invoke("show", "../../etc/passwd")

    assert result.exit_code == 1
    assert "aucune idée nommée" in result.output


def test_studio_command_delegates_to_run_studio(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    calls: list[tuple[Path, Path]] = []
    fake = types.ModuleType("omniscient.studio.app")
    fake.run_studio = lambda v, r: calls.append((v, r))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniscient.studio.app", fake)

    result = runner.invoke(cli.app, ["studio"])

    assert result.exit_code == 0, result.output
    assert calls == [(vault, paths.REPO)]


def test_show_follow_rerenders_on_change_and_exits_on_ctrl_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from omniscient.studio import cli as studio_cli
    from omniscient.studio import todo

    stem = _seed(vault)
    note = vault / "ideas" / f"{stem}.md"
    ticks = iter(["edit", "stop"])

    def fake_sleep(_: float) -> None:
        if next(ticks) == "stop":
            raise KeyboardInterrupt
        todo.add_todo(vault, stem, "Ajout en direct.")
        stat = note.stat()
        os.utime(note, (stat.st_atime, stat.st_mtime + 5))

    monkeypatch.setattr(studio_cli.time, "sleep", fake_sleep)

    result = _invoke("show", stem, "--follow")

    assert result.exit_code == 0, result.output
    assert result.output.count("Radar des modèles") >= 2
    assert "Ajout en direct." in result.output


@pytest.mark.parametrize("title", ["Foo [/bar] baz", "Plan [1] ok [b]x"])
def test_list_prints_markup_titles_literally(vault: Path, title: str) -> None:
    _seed(vault, title)

    result = runner.invoke(cli.app, ["idea", "list"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert title in result.output


def test_show_follow_survives_a_transient_bad_note(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from omniscient.studio import cli as studio_cli

    stem = _seed(vault)
    note = vault / "ideas" / f"{stem}.md"
    good = note.read_text(encoding="utf-8")
    ticks = iter(["break", "repair", "stop"])

    def bump() -> None:
        stat = note.stat()
        os.utime(note, (stat.st_atime, stat.st_mtime + 5))

    def fake_sleep(_: float) -> None:
        step = next(ticks)
        if step == "stop":
            raise KeyboardInterrupt
        if step == "break":
            note.write_text("---\ntitle: [oops\n---\nbody\n", encoding="utf-8")
        else:
            note.write_text(good.replace("Un pitch.", "Réparé."), encoding="utf-8")
        bump()

    monkeypatch.setattr(studio_cli.time, "sleep", fake_sleep)

    result = _invoke("show", stem, "--follow")

    assert result.exit_code == 0, result.output
    assert "note momentanément illisible" in result.output
    assert "Réparé." in result.output
