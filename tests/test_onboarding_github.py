import base64
from pathlib import Path

import frontmatter
import pytest

from omniscient.onboarding.github import (
    GitHubError,
    describe_github_repo,
    list_user_repos,
    parse_full_name,
)


def _repo(name: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "full_name": f"octocat/{name}",
        "description": f"{name} desc",
        **extra,
    }


class FakeFetch:
    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, object]:
        self.calls.append((url, headers))
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response
        return 404, {"message": "Not Found"}


API = "https://api.github.com"


def test_list_user_repos_sorted_and_token_only_in_header() -> None:
    fetch = FakeFetch(
        {
            f"{API}/users/octocat/repos": (
                200,
                [_repo("zeta"), _repo("Alpha", fork=True)],
            )
        }
    )

    repos = list_user_repos("octocat", "tok123", fetch)

    assert [r.name for r in repos] == ["Alpha", "zeta"]
    assert repos[0].fork is True
    url, headers = fetch.calls[0]
    assert "tok123" not in url
    assert headers["Authorization"] == "Bearer tok123"


def test_list_user_repos_without_token_sends_no_auth() -> None:
    fetch = FakeFetch({f"{API}/users/octocat/repos": (200, [])})
    list_user_repos("octocat", None, fetch)
    assert "Authorization" not in fetch.calls[0][1]


def test_list_user_repos_paginates() -> None:
    page1 = [_repo(f"r{i:03}") for i in range(100)]
    fetch = FakeFetch(
        {
            f"{API}/users/octocat/repos?type=owner&sort=updated&per_page=100&page=1": (
                200,
                page1,
            ),
            f"{API}/users/octocat/repos?type=owner&sort=updated&per_page=100&page=2": (
                200,
                [_repo("last")],
            ),
        }
    )
    assert len(list_user_repos("octocat", None, fetch)) == 101


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(404, "introuvable"), (403, "limite"), (429, "limite"), (500, "erreur 500")],
)
def test_list_user_repos_errors_in_french(status: int, fragment: str) -> None:
    fetch = FakeFetch({f"{API}/users/": (status, {"message": "x"})})
    with pytest.raises(GitHubError, match=fragment):
        list_user_repos("octocat", None, fetch)


def test_list_user_repos_rejects_invalid_user() -> None:
    with pytest.raises(GitHubError, match="invalide"):
        list_user_repos("bad/user", None, FakeFetch({}))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("octocat/Hello-World", "octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World.git", "octocat/Hello-World"),
        ("github.com/octocat/x/", "octocat/x"),
        ("octocat", None),
        ("https://gitlab.com/a/b", None),
    ],
)
def test_parse_full_name(source: str, expected: str | None) -> None:
    assert parse_full_name(source) == expected


def test_describe_github_repo_builds_registry_markdown(tmp_path: Path) -> None:
    # Arrange
    readme = base64.b64encode(b"# Hello\n\nA tiny demo repo.\n").decode()
    fetch = FakeFetch(
        {
            f"{API}/repos/octocat/Hello-World/languages": (
                200,
                {"Python": 10, "Shell": 2},
            ),
            f"{API}/repos/octocat/Hello-World/readme": (
                200,
                {"encoding": "base64", "content": readme},
            ),
            f"{API}/repos/octocat/Hello-World": (
                200,
                {
                    "description": "d",
                    "language": "TypeScript",
                    "topics": ["ai", "demo"],
                },
            ),
        }
    )

    # Act
    meta = frontmatter.loads(
        describe_github_repo("octocat/Hello-World", tmp_path / "hello-world", fetch)
    ).metadata

    # Assert
    assert meta["name"] == "hello-world"
    assert meta["path"] == str(tmp_path / "hello-world")
    assert meta["purpose"] == "A tiny demo repo."
    assert meta["stack"] == ["typescript", "python", "shell"]
    assert meta["remote"] == "github.com/octocat/Hello-World"
    assert meta["git"] is False
    assert "ai, demo" in meta["notes"]


def test_describe_github_repo_without_readme_uses_description(tmp_path: Path) -> None:
    fetch = FakeFetch(
        {
            f"{API}/repos/o/r/languages": (200, {}),
            f"{API}/repos/o/r/readme": (404, None),
            f"{API}/repos/o/r": (200, {"description": "Short.", "topics": []}),
        }
    )
    meta = frontmatter.loads(describe_github_repo("o/r", tmp_path, fetch)).metadata
    assert meta["purpose"] == "Short."
    assert meta["agent_docs"] == []


def test_describe_github_repo_404(tmp_path: Path) -> None:
    with pytest.raises(GitHubError, match="introuvable"):
        describe_github_repo("o/missing", tmp_path, FakeFetch({}))


def test_describe_github_repo_masks_secrets_in_readme_and_description(
    tmp_path: Path,
) -> None:
    # Arrange: a README leaking a token, a description leaking an AWS key id.
    token = "ghp_" + "a" * 36
    readme = base64.b64encode(f"# R\n\nDeploy with {token} please.\n".encode()).decode()
    fetch = FakeFetch(
        {
            f"{API}/repos/o/r/languages": (200, {}),
            f"{API}/repos/o/r/readme": (
                200,
                {"encoding": "base64", "content": readme},
            ),
            f"{API}/repos/o/r": (200, {"description": "x", "topics": []}),
        }
    )

    # Act
    markdown = describe_github_repo("o/r", tmp_path / "r", fetch)

    # Assert
    assert token not in markdown
    assert "Deploy with [masqué] please." in markdown
