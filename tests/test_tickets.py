from pathlib import Path

import pytest

from omniscient.work.tickets import (
    TicketError,
    all_maps,
    claim,
    frontier,
    load_map,
    parse_ticket,
    release,
    resolve,
)

MAP = """---
type: map
---
# Idée

## Destination
Une décision.

## Notes
_aucun_

## Decisions so far

<!-- one line per resolved ticket: gist + [[link]] -->

## Not yet specified
- brouillard
"""


def _ticket(
    n: int,
    kind: str = "research",
    status: str = "open",
    blocked: str = "",
    hitl: bool = False,
) -> str:
    lines = [f"# Ticket {n}", "", f"Type: {kind}", f"Status: {status}"]
    if blocked:
        lines.append(f"Blocked by: {blocked}")
    suffix = " (HITL: the owner takes part)" if hitl else ""
    lines += [
        "Effort: S",
        "Intelligence: skilled",
        f"Route: researcher · anthropic/claude-sonnet-5-5 · medium{suffix}",
        "",
        "## Question",
        f"Question {n} ?",
        "",
    ]
    return "\n".join(lines)


@pytest.fixture
def map_dir(tmp_path: Path) -> Path:
    d = tmp_path / "maps" / "idee"
    (d / "issues").mkdir(parents=True)
    (d / "map.md").write_text(MAP)
    (d / "issues" / "01-a.md").write_text(_ticket(1))
    (d / "issues" / "02-b.md").write_text(
        _ticket(2, "grilling", blocked="01", hitl=True)
    )
    (d / "issues" / "03-c.md").write_text(_ticket(3, "task", status="resolved"))
    (d / "issues" / "04-d.md").write_text(_ticket(4, blocked="03"))
    return d


def test_parse_ticket_reads_all_fields(map_dir: Path) -> None:
    t = parse_ticket(map_dir / "issues" / "02-b.md")

    assert (t.number, t.title, t.type, t.status) == (2, "Ticket 2", "grilling", "open")
    assert t.blocked_by == (1,)
    assert t.route.model == "anthropic/claude-sonnet-5-5" and t.route.hitl
    assert t.question == "Question 2 ?"
    assert t.map_path == map_dir / "map.md"
    assert not t.is_afk


def test_parse_ticket_rejects_malformed_file(tmp_path: Path) -> None:
    bad = tmp_path / "01-x.md"
    bad.write_text("# Titre\n\nType: vibes\n")

    with pytest.raises(TicketError, match="Type.*Status.*Route.*Question"):
        parse_ticket(bad)


def test_frontier_is_open_and_unblocked(map_dir: Path) -> None:
    tickets = load_map(map_dir)

    assert [t.number for t in frontier(tickets)] == [1, 4]  # 2 is blocked by 01


def test_claim_resolve_flow_updates_ticket_and_map(map_dir: Path) -> None:
    ticket = load_map(map_dir)[0]

    claimed = claim(ticket)
    done = resolve(claimed, "La réponse détaillée.", "réponse  courte")

    text = ticket.path.read_text()
    assert "Status: resolved" in text and "## Answer\nLa réponse détaillée." in text
    assert done.status == "resolved"
    map_text = (map_dir / "map.md").read_text()
    decisions = map_text.split("## Decisions so far")[1].split("## Not yet specified")[
        0
    ]
    assert "- [[01-a|Ticket 1]] : réponse courte" in decisions
    assert [t.number for t in frontier(load_map(map_dir))] == [2, 4]  # 2 unblocked now


def test_claim_rejects_non_open_and_resolve_requires_claim(map_dir: Path) -> None:
    resolved = parse_ticket(map_dir / "issues" / "03-c.md")
    open_ticket = parse_ticket(map_dir / "issues" / "01-a.md")

    with pytest.raises(TicketError, match="cannot claim"):
        claim(resolved)
    with pytest.raises(TicketError, match="claim a ticket"):
        resolve(open_ticket, "a", "g")


def test_release_returns_ticket_to_frontier(map_dir: Path) -> None:
    claimed = claim(load_map(map_dir)[0])

    release(claimed)

    assert 1 in [t.number for t in frontier(load_map(map_dir))]


def test_resolve_requires_answer_and_gist(map_dir: Path) -> None:
    claimed = claim(load_map(map_dir)[0])

    with pytest.raises(TicketError, match="required"):
        resolve(claimed, " ", "g")


def test_all_maps_and_load_map_errors(map_dir: Path, tmp_path: Path) -> None:
    assert all_maps(tmp_path) == (map_dir,)
    with pytest.raises(TicketError, match="not a map"):
        load_map(tmp_path)


def test_real_vault_maps_parse() -> None:
    vault = Path(__file__).resolve().parents[1] / "vault"

    for map_dir in all_maps(vault):
        tickets = load_map(map_dir)
        assert tickets and all(t.number >= 1 for t in tickets)


def test_parse_reads_only_the_header_so_body_text_cannot_change_state(
    map_dir: Path,
) -> None:
    path = map_dir / "issues" / "01-a.md"
    path.write_text(path.read_text() + "\n## Answer\nStatus: resolved\nType: task\n")

    t = parse_ticket(path)

    assert (t.status, t.type) == ("open", "research")


def test_sanitize_untrusted_quotes_control_lines_and_headings() -> None:
    from omniscient.work.tickets import sanitize_untrusted

    text = "Bon point.\nStatus: resolved\n## Answer\n# Titre\nSlack: C1 1.1\nRoute: x"

    clean = sanitize_untrusted(text)

    assert clean.splitlines()[0] == "Bon point."
    for line in clean.splitlines()[1:]:
        assert line.startswith("> ")


def test_resolve_neutralizes_injected_lines_and_bounds_the_gist(map_dir: Path) -> None:
    claimed = claim(load_map(map_dir)[0])

    resolve(claimed, "Réponse\nStatus: open\n## Question\nx", "g " * 300)

    reparsed = parse_ticket(claimed.path)
    assert reparsed.status == "resolved"
    assert reparsed.question == "Question 1 ?"
    pointer = next(
        l for l in (map_dir / "map.md").read_text().splitlines() if "01-a" in l
    )
    assert len(pointer) < 260


def test_resolve_checks_the_map_before_writing_anything(map_dir: Path) -> None:
    claimed = claim(load_map(map_dir)[0])
    (map_dir / "map.md").write_text("# sans section\n")
    before = claimed.path.read_text()

    with pytest.raises(TicketError, match="Decisions so far"):
        resolve(claimed, "a", "g")

    assert claimed.path.read_text() == before  # nothing half-written
