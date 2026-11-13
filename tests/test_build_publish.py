from __future__ import annotations

import subprocess
from pathlib import Path

import frontmatter
import pytest

from omniscient.build.publish import (
    PublishError,
    check_web_demo,
    find_secrets,
    publish_web_demo,
    record_url,
    render_index,
)


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    return out.stdout.decode().strip()


def _poc(tmp_path: Path, page: str = "<title>Démo pluie</title><p>ok</p>") -> Path:
    poc = tmp_path / "poc"
    (poc / "web").mkdir(parents=True)
    (poc / "web" / "index.html").write_text(page, encoding="utf-8")
    (poc / "proto.py").write_text("print('not published')", encoding="utf-8")
    return poc


def _site(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    site = tmp_path / "site"
    _git(tmp_path, "clone", "-q", str(remote), str(site))
    (site / ".nojekyll").write_text("", encoding="utf-8")
    _git(site, "add", "-A")
    _git(site, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    _git(site, "push", "-q", "-u", "origin", "main")
    return site


@pytest.mark.parametrize(
    "leak",
    [
        "xo" + "xb-1234567890-abcdef",
        "sk-" + "ant-api03-abcdefghijkl",
        "AK" + "IAABCDEFGHIJKLMNOP",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "/ho" + "me/someone/projects",
    ],
)
def test_find_secrets_flags_keys_and_local_paths(tmp_path: Path, leak: str) -> None:
    (tmp_path / "a.js").write_text(f"const x = '{leak}';", encoding="utf-8")

    assert find_secrets(tmp_path)


def test_check_web_demo_needs_an_index_page(tmp_path: Path) -> None:
    poc = tmp_path / "poc"
    poc.mkdir()

    with pytest.raises(PublishError, match="index.html"):
        check_web_demo(poc)


def test_check_web_demo_refuses_secret_content(tmp_path: Path) -> None:
    poc = _poc(
        tmp_path, "<script>const k='" + "sk-" + "ant-api03-abcdefghijkl'</script>"
    )

    with pytest.raises(PublishError, match="secret"):
        check_web_demo(poc)


def test_check_web_demo_refuses_oversized_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omniscient.build.publish.MAX_SITE_BYTES", 5)

    with pytest.raises(PublishError, match="over"):
        check_web_demo(_poc(tmp_path))


def test_render_index_lists_each_published_demo_by_title(tmp_path: Path) -> None:
    (tmp_path / "rain").mkdir()
    (tmp_path / "rain" / "index.html").write_text("<title>Pluie &amp; vent</title>")
    (tmp_path / ".git").mkdir()

    page = render_index(tmp_path)

    assert '<a href="rain/">Pluie &amp; vent</a>' in page
    assert ".git" not in page


def test_publish_copies_only_the_web_folder_and_pushes(tmp_path: Path) -> None:
    site = _site(tmp_path)

    url = publish_web_demo(_poc(tmp_path), "rain", site, "https://x.github.io/pocs/")

    assert url == "https://x.github.io/pocs/rain/"
    assert (site / "rain" / "index.html").is_file()
    assert not (site / "rain" / "proto.py").exists()
    assert "Démo pluie" in (site / "index.html").read_text(encoding="utf-8")
    assert _git(tmp_path / "remote.git", "log", "-1", "--format=%s") == "publish: rain"


def test_publish_needs_a_site_clone(tmp_path: Path) -> None:
    with pytest.raises(PublishError, match="site clone"):
        publish_web_demo(_poc(tmp_path), "rain", tmp_path / "nope", "https://x/")


def test_record_url_adds_link_once(tmp_path: Path) -> None:
    note = tmp_path / "idea.md"
    note.write_text(
        "---\ntitle: t\n---\n\n## Prototype\n**Verdict :** ok\n", encoding="utf-8"
    )

    record_url(note, "https://x/rain/")
    record_url(note, "https://x/rain/")

    post = frontmatter.load(note)
    assert post["poc_url"] == "https://x/rain/"
    assert post.content.count("**Essayer :** https://x/rain/") == 1
