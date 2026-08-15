"""Tests for GitHub client helpers that need no network."""
from gitlocate.sources.github_api import GitHubClient
from gitlocate.models import KIND_ORG, KIND_REPO


class FakeResp:
    def __init__(self, status_code, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_in_qualifier():
    assert GitHubClient._in_qualifier(["login", "name"]) == " in:login,name"
    assert GitHubClient._in_qualifier([]) == ""
    assert GitHubClient._in_qualifier(["", None, "email"]) == " in:email"


def test_user_entity_from_item():
    ent = GitHubClient._entity_from_user_item(
        {"login": "acme", "html_url": "https://github.com/acme", "name": "Acme"},
        KIND_ORG, "acme")
    assert ent is not None
    assert ent.identifier == "acme"
    assert ent.kind == KIND_ORG
    assert "acme" in ent.matched_variants


def test_user_entity_empty_login_returns_none():
    assert GitHubClient._entity_from_user_item({"login": ""}, KIND_ORG, None) is None
    assert GitHubClient._entity_from_user_item({}, KIND_ORG, None) is None


def test_repo_entity_captures_fork_and_topics():
    ent = GitHubClient._entity_from_repo_item({
        "full_name": "acme/web",
        "owner": {"login": "acme"},
        "clone_url": "https://github.com/acme/web.git",
        "fork": True,
        "archived": True,
        "topics": ["Acme", "Security"],
    }, "acme")
    assert ent is not None
    assert ent.identifier == "acme/web"
    assert ent.extra["fork"] is True
    assert ent.extra["archived"] is True
    assert ent.extra["topics"] == ["acme", "security"]
    assert ent.extra["clone_url"].endswith(".git")


def test_repo_entity_empty_returns_none():
    assert GitHubClient._entity_from_repo_item({}, None) is None


def test_is_rate_limited():
    assert GitHubClient._is_rate_limited(FakeResp(429)) is True
    assert GitHubClient._is_rate_limited(
        FakeResp(403, {"X-RateLimit-Remaining": "0"})) is True
    assert GitHubClient._is_rate_limited(
        FakeResp(403, {"Retry-After": "60"})) is True
    assert GitHubClient._is_rate_limited(
        FakeResp(403, body={"message": "API rate limit exceeded"})) is True
    # a bare 403 (permissions/proxy) is NOT rate limiting
    assert GitHubClient._is_rate_limited(
        FakeResp(403, body={"message": "Must have push access"})) is False
