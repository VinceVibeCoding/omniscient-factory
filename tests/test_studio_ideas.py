from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import pytest

from omniscient import i18n
from omniscient.brain import IdeaScores
from omniscient.studio.ideas import (
    H_ATELIER,
    H_ORIGIN,
    StudioError,
    append_atelier,
    fork_variant,
    list_ideas,
    load_card,
    new_seed,
    record_debate,
    resolve,
    scan_ideas,
    tree_order,
)

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
OK_COUNCIL = "**Verdict :** poursuivre\n\nLe conseil dit oui."
WEIGHTS = {
    "novelty": 0.25,
    "impact": 0.25,
    "feasibility": 0.2,
    "fit": 0.15,
    "effort": 0.15,
}


def _seed(vault: Path, title: str = "Tableau des idées", **kw: object) -> Path:
    return new_seed(vault, title=title, pitch="Un pitch court.", now=NOW, **kw)  # type: ignore[arg-type]


def _scores(value: float = 0.8, effort: float = 0.2) -> IdeaScores:
    return IdeaScores(value, value, value, value, effort)


def test_new_seed_writes_an_undebated_owner_seed(tmp_path: Path) -> None:
    path = _seed(tmp_path, projects=("sec-lab",), sources=("abc123",))

    card = load_card(path)
    assert path.parent == tmp_path / "ideas"
    assert card.status == "seed"
    assert card.score is None
    assert card.origin == "owner"
    assert card.projects == ("sec-lab",)
    assert card.sources == ("abc123",)
    assert card.variant == 1
    assert "scores" not in frontmatter.load(path).metadata


def test_new_seed_rejects_unknown_origin_and_parent(tmp_path: Path) -> None:
    with pytest.raises(StudioError):
        _seed(tmp_path, origin="martian")
    with pytest.raises(StudioError):
        _seed(tmp_path, origin="remix", parents=("nope",))


def test_new_seed_remix_links_its_parents(tmp_path: Path) -> None:
    a = _seed(tmp_path, "Idée A").stem
    b = _seed(tmp_path, "Idée B").stem

    remix = load_card(_seed(tmp_path, "Mélange", origin="remix", parents=(a, b)))

    assert remix.origin == "remix"
    assert remix.parents == (a, b)
    body = remix.path.read_text(encoding="utf-8")
    assert H_ORIGIN in body
    assert f"[[{a}]]" in body and f"[[{b}]]" in body


def test_fork_variant_numbers_and_names_variants(tmp_path: Path) -> None:
    parent = _seed(tmp_path, "Radar", projects=("sec-lab",), kind="upgrade").stem

    first = load_card(
        fork_variant(tmp_path, parent, pitch="Pour les équipes.", now=NOW)
    )
    second = load_card(
        fork_variant(tmp_path, parent, pitch="En CLI.", title="Radar CLI", now=NOW)
    )

    assert (first.variant, second.variant) == (2, 3)
    assert first.title == "v2 · Radar"
    assert second.title == "Radar CLI"
    assert first.parent == parent and second.parent == parent
    assert first.origin == "variant"
    assert first.projects == ("sec-lab",) and first.kind == "upgrade"
    assert f"Variante de [[{parent}]]" in first.path.read_text(encoding="utf-8")


def test_fork_variant_of_unknown_idea_fails(tmp_path: Path) -> None:
    with pytest.raises(StudioError):
        fork_variant(tmp_path, "ghost", pitch="x")


def test_append_atelier_twice_keeps_both_entries_in_one_section(tmp_path: Path) -> None:
    stem = _seed(tmp_path).stem

    append_atelier(tmp_path, stem, "Première note.", now=NOW)
    path = append_atelier(tmp_path, stem, "Deuxième note.", now=NOW)

    body = path.read_text(encoding="utf-8")
    assert body.count(H_ATELIER) == 1
    assert body.index("Première note.") < body.index("Deuxième note.")


def test_append_atelier_refuses_blank_text(tmp_path: Path) -> None:
    stem = _seed(tmp_path).stem
    with pytest.raises(StudioError):
        append_atelier(tmp_path, stem, "   \n")


def test_record_debate_scores_a_seed_and_marks_it_debated(tmp_path: Path) -> None:
    stem = _seed(tmp_path).stem

    path = record_debate(
        tmp_path, stem, council=OK_COUNCIL, scores=_scores(), weights=WEIGHTS
    )

    card = load_card(path)
    assert card.status == "debated"
    assert card.score == pytest.approx(round(_scores().weighted(WEIGHTS), 4))
    body = path.read_text(encoding="utf-8")
    assert i18n.H_COUNCIL in body and "Le conseil dit oui." in body


def test_record_debate_keeps_a_later_status(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    post = frontmatter.load(path)
    post["status"] = "building"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")

    record_debate(
        tmp_path, path.stem, council=OK_COUNCIL, scores=_scores(), weights=WEIGHTS
    )

    assert load_card(path).status == "building"


def test_record_debate_rejects_empty_council_and_bad_weights(tmp_path: Path) -> None:
    stem = _seed(tmp_path).stem
    with pytest.raises(StudioError):
        record_debate(tmp_path, stem, council=" ", scores=_scores(), weights=WEIGHTS)
    with pytest.raises(StudioError):
        record_debate(
            tmp_path, stem, council=OK_COUNCIL, scores=_scores(), weights={"fit": 1.0}
        )


def test_tree_order_puts_variants_under_their_parent(tmp_path: Path) -> None:
    root = _seed(tmp_path, "Racine").stem
    other = _seed(tmp_path, "Autre").stem
    record_debate(
        tmp_path, other, council=OK_COUNCIL, scores=_scores(0.9), weights=WEIGHTS
    )
    v2 = fork_variant(tmp_path, root, pitch="p", now=NOW).stem
    v3 = fork_variant(tmp_path, root, pitch="q", now=NOW).stem

    order = [(c.stem, d) for c, d in tree_order(list_ideas(tmp_path))]

    assert order[0] == (other, 0)
    assert order[1:] == [(root, 0), (v2, 1), (v3, 1)]


def test_resolve_refuses_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    stem = _seed(tmp_path).stem

    assert resolve(tmp_path, stem) == tmp_path / "ideas" / f"{stem}.md"
    for bad in ("../secret", "../../etc/passwd", "missing"):
        with pytest.raises(StudioError):
            resolve(tmp_path, bad)


def test_list_ideas_filters_by_status_and_sorts_best_first(tmp_path: Path) -> None:
    low = _seed(tmp_path, "Basse").stem
    high = _seed(tmp_path, "Haute").stem
    seed = _seed(tmp_path, "Graine").stem
    record_debate(
        tmp_path, low, council=OK_COUNCIL, scores=_scores(0.3), weights=WEIGHTS
    )
    record_debate(
        tmp_path, high, council=OK_COUNCIL, scores=_scores(0.9), weights=WEIGHTS
    )

    assert [c.stem for c in list_ideas(tmp_path)] == [high, low, seed]
    assert [c.stem for c in list_ideas(tmp_path, frozenset({"seed"}))] == [seed]
    assert list_ideas(tmp_path / "empty") == ()


def test_record_debate_abandonner_drops_the_seed(tmp_path: Path) -> None:
    stem = new_seed(tmp_path, title="Idée faible", pitch="Pitch.").stem
    council = "**Verdict :** abandonner\n\n**Challengeur :** Déjà fait."

    record_debate(tmp_path, stem, council=council, scores=_scores(), weights=WEIGHTS)

    assert load_card(resolve(tmp_path, stem)).status == "dropped"


def test_record_debate_rejects_a_council_without_a_known_verdict(
    tmp_path: Path,
) -> None:
    stem = new_seed(tmp_path, title="Idée", pitch="Pitch.").stem

    with pytest.raises(StudioError, match="Verdict"):
        record_debate(
            tmp_path,
            stem,
            council="**Verdict :** peut-être",
            scores=_scores(),
            weights=WEIGHTS,
        )


def test_agent_headings_cannot_escape_their_section(tmp_path: Path) -> None:
    stem = new_seed(tmp_path, title="Idée", pitch="Pitch.").stem
    append_atelier(tmp_path, stem, "Riff\n## Evil\ntexte")
    append_atelier(tmp_path, stem, "Deuxième")
    council = "**Verdict :** remodeler\n## Sous\nxx"
    record_debate(tmp_path, stem, council=council, scores=_scores(), weights=WEIGHTS)
    record_debate(tmp_path, stem, council=OK_COUNCIL, scores=_scores(), weights=WEIGHTS)

    content = resolve(tmp_path, stem).read_text(encoding="utf-8")
    assert "\n## Evil" not in content and "### Evil" in content
    assert content.index("### Evil") < content.index("Deuxième")
    assert "Sous" not in content


def test_unreadable_notes_are_skipped_not_fatal(tmp_path: Path) -> None:
    new_seed(tmp_path, title="Bonne", pitch="Pitch.")
    (tmp_path / "ideas" / "2026-01-01 cassee.md").write_text("---\ntitle: [oops\n---\n")

    cards, bad = scan_ideas(tmp_path)

    assert [c.title for c in cards] == ["Bonne"] and bad == ("2026-01-01 cassee.md",)
    assert len(list_ideas(tmp_path)) == 1
    with pytest.raises(StudioError):
        resolve(tmp_path, "a\x00b")
