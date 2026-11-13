from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from omniscient.brain import Idea, IdeaScores, write_idea
from omniscient.slack.core import (
    DEBATER_ID,
    apply_owner_verdict,
    find_idea_by_thread,
    handle_thread_reply,
    idea_blocks,
    load_env_file,
    post_digest,
)

NOW = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
COUNCIL = (
    "**Verdict:** reshape\n\n**Challenger:** Exists already.\n\n"
    "**Simplifier (smallest version):** One email from 7 runs.\n\n**Risk checker:**\n- privacy"
)


def _idea(vault: Path, title: str = "Run coach", status: str = "debated") -> Path:
    idea = Idea(
        title=title,
        pitch="Coach from FIT files, long enough.",
        status=status,  # type: ignore[arg-type]
        scores=IdeaScores(
            novelty=0.5, impact=0.5, feasibility=0.5, fit=0.5, effort=0.5
        ),
        sources=(),
        projects=("run-tracker",),
        created=NOW,
    )
    return write_idea(vault, idea, score=0.56, council=COUNCIL)


class _Web:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"17000000{len(self.posts)}.0001"}


def test_idea_blocks_show_score_verdict_smallest_version_and_buttons(
    tmp_path: Path,
) -> None:
    path = _idea(tmp_path)

    blocks = idea_blocks(path)

    text = str(blocks)
    assert "Run coach" in text and "0.56" in text
    assert "reshape" in text and "One email from 7 runs." in text
    actions = next(b for b in blocks if b["type"] == "actions")
    assert [e["action_id"] for e in actions["elements"]] == [
        "idea_build",
        "idea_pursue",
        "idea_reshape",
        "idea_drop",
    ]
    assert all(e["value"] == path.stem for e in actions["elements"])
    styles = {e["action_id"]: e.get("style") for e in actions["elements"]}
    assert styles["idea_build"] == "primary" and styles["idea_pursue"] is None


def test_idea_blocks_lead_with_title_source_link_and_poc(tmp_path: Path) -> None:
    from omniscient.brain.models import NewsItem
    from omniscient.brain.notes import write_news

    news = NewsItem(
        title="Tracker opens FIT API",
        url="https://example.com/fit-api",
        source="hn",
        published=NOW.date(),
        ingested=NOW,
        summary="A watch maker opened the FIT API for developers.",
        tags=("api",),
    )
    write_news(tmp_path, news)
    idea = Idea(
        title="Run coach",
        pitch="Coach from FIT files. Second sentence is cut.",
        status="debated",
        scores=IdeaScores(
            novelty=0.5, impact=0.5, feasibility=0.5, fit=0.5, effort=0.5
        ),
        sources=(news.id,),
        projects=("run-tracker",),
        created=NOW,
    )
    path = write_idea(tmp_path, idea, score=0.56, council=COUNCIL)

    blocks = idea_blocks(path)

    assert blocks[0]["type"] == "header" and blocks[0]["text"]["text"] == "Run coach"
    source = blocks[1]["text"]["text"]
    assert "<https://example.com/fit-api|Tracker opens FIT API>" in source
    poc = blocks[2]["text"]["text"]
    assert "Projet PoC" in poc and "One email from 7 runs." in poc
    assert "run-tracker" in poc


def test_idea_blocks_say_when_a_source_is_missing(tmp_path: Path) -> None:
    blocks = idea_blocks(_idea(tmp_path))

    assert "n/d" in blocks[1]["text"]["text"]


def test_post_digest_posts_only_new_debated_ideas_and_records_thread(
    tmp_path: Path,
) -> None:
    new = _idea(tmp_path, "Run coach")
    _idea(tmp_path, "Dropped thing", status="dropped")
    web = _Web()

    posted = post_digest(web, "C123", tmp_path)

    assert posted == (new,)
    assert frontmatter.load(new)["slack_ts"] == "170000002.0001"
    assert frontmatter.load(new)["slack_channel"] == "C123"
    assert post_digest(web, "C123", tmp_path) == ()  # nothing new the second time


def test_post_digest_is_quiet_when_nothing_is_new(tmp_path: Path) -> None:
    web = _Web()

    assert post_digest(web, "C123", tmp_path) == ()
    assert web.posts == []


@pytest.mark.parametrize(
    ("verdict", "status", "owner"),
    [
        ("drop", "dropped", "drop"),
        ("pursue", "debated", "pursue"),
        ("reshape", "debated", "reshape"),
    ],
)
def test_apply_owner_verdict(
    tmp_path: Path, verdict: str, status: str, owner: str
) -> None:
    path = _idea(tmp_path)

    apply_owner_verdict(tmp_path, path.stem, verdict)

    meta = frontmatter.load(path)
    assert meta["status"] == status
    assert meta["owner_verdict"] == owner


def test_apply_owner_verdict_rejects_unknown_idea_and_path_tricks(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unknown idea"):
        apply_owner_verdict(tmp_path, "../../AGENTS", "drop")


class _Runs:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.started: list[dict[str, Any]] = []

    def start(self, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.started.append({"agent_id": agent_id, **body})
        return {"run_id": "r1"}

    def wait(self, run_id: str, timeout: float) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "completed",
            "cost": {"usd": 0.05},
            "result": {"output": None, "final_answer": self.answer},
        }


class _Client:
    def __init__(self, answer: str) -> None:
        self.runs = _Runs(answer)


def test_handle_thread_reply_records_discussion_and_answers(tmp_path: Path) -> None:
    path = _idea(tmp_path)
    post = frontmatter.load(path)
    post["slack_ts"] = "111.222"
    path.write_text(frontmatter.dumps(post))
    client = _Client("Fair point: start with your last 7 runs only.")

    reply = handle_thread_reply(client, tmp_path, "111.222", "Isn't Strava doing this?")

    assert reply is not None
    assert reply.text == "Fair point: start with your last 7 runs only."
    assert reply.cost_usd == pytest.approx(0.05)
    content = path.read_text()
    assert "## Discussion" in content
    assert "**Propriétaire :** Isn't Strava doing this?" in content
    assert "**Débatteur :** Fair point" in content
    prompt = client.runs.started[0]["input"]["content"]
    assert client.runs.started[0]["agent_id"] == DEBATER_ID
    assert "Isn't Strava doing this?" in prompt and "Exists already." in prompt


def test_handle_thread_reply_ignores_threads_without_an_idea(tmp_path: Path) -> None:
    assert handle_thread_reply(_Client("x"), tmp_path, "999.000", "hello") is None


def test_find_idea_by_thread(tmp_path: Path) -> None:
    path = _idea(tmp_path)
    post = frontmatter.load(path)
    post["slack_ts"] = "5.5"
    path.write_text(frontmatter.dumps(post))

    assert find_idea_by_thread(tmp_path, "5.5") == path
    assert find_idea_by_thread(tmp_path, "6.6") is None


def test_load_env_file_parses_without_overriding_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("# c\nSLACK_BOT_TOKEN=xoxb-1   # bot\nEMPTY=\nKEEP=file\n")
    monkeypatch.setenv("KEEP", "process")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    loaded = load_env_file(env)

    assert loaded["SLACK_BOT_TOKEN"] == "xoxb-1"
    assert loaded["KEEP"] == "process"
    assert "EMPTY" not in loaded


def test_idea_blocks_read_french_council_labels(tmp_path: Path) -> None:
    from omniscient import i18n

    council_fr = (
        f"{i18n.L_VERDICT} remodeler\n\n{i18n.L_CHALLENGER} Existe déjà.\n\n"
        f"{i18n.L_SIMPLIFIER} Un courriel à partir de 7 sorties.\n\n{i18n.L_RISKS}\n- vie privée"
    )
    idea = Idea(
        title="Coach course",
        pitch="Coach à partir des fichiers FIT.",
        status="debated",
        scores=IdeaScores(
            novelty=0.5, impact=0.5, feasibility=0.5, fit=0.5, effort=0.5
        ),
        sources=(),
        projects=(),
        created=NOW,
    )
    path = write_idea(tmp_path, idea, score=0.5, council=council_fr)

    text = str(idea_blocks(path))

    assert "remodeler" in text
    assert "Un courriel à partir de 7 sorties." in text
    assert "Poursuivre" in text and "nouveau projet" in text
    assert "Nouveau projet" in text  # the idea kind, shown first


class _WebWithUpdate(_Web):
    def __init__(self) -> None:
        super().__init__()
        self.updates: list[dict[str, Any]] = []

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {"ok": True}


def test_refresh_posts_updates_every_posted_idea_in_place(tmp_path: Path) -> None:
    from omniscient.slack.core import refresh_posts

    posted = _idea(tmp_path, "Coach posté")
    _idea(tmp_path, "Jamais posté")
    post = frontmatter.load(posted)
    post["slack_ts"], post["slack_channel"] = "9.9", "C9"
    posted.write_text(frontmatter.dumps(post))
    web = _WebWithUpdate()

    refreshed = refresh_posts(web, tmp_path)

    assert refreshed == (posted,)
    assert web.updates[0]["channel"] == "C9" and web.updates[0]["ts"] == "9.9"
    assert web.updates[0]["text"] == "Coach posté"
    assert any(b["type"] == "actions" for b in web.updates[0]["blocks"])


class _GoneError(Exception):
    def __init__(self) -> None:
        super().__init__("message_not_found")
        self.response = {"ok": False, "error": "message_not_found"}


class _WebWithDeletedPost(_WebWithUpdate):
    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        raise _GoneError()


def test_refresh_posts_unlinks_deleted_messages_so_digest_reposts(
    tmp_path: Path,
) -> None:
    from omniscient.slack.core import refresh_posts

    path = _idea(tmp_path, "Supprimé de Slack")
    post = frontmatter.load(path)
    post["slack_ts"], post["slack_channel"] = "9.9", "C9"
    path.write_text(frontmatter.dumps(post))
    web = _WebWithDeletedPost()

    assert refresh_posts(web, tmp_path) == ()
    assert "slack_ts" not in frontmatter.load(path).metadata
    assert post_digest(web, "C9", tmp_path) == (path,)  # reposted
