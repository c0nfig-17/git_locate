"""Tests for the web-dork GitHub URL parsing (no network)."""
from gitlocate.sources.web_dork import _parse_github_urls, DuckDuckGoProvider


def test_parse_owner_and_repo():
    out = _parse_github_urls([
        "https://github.com/acme",
        "https://github.com/acme/website",
    ])
    assert ("owner", "acme", None) in out
    assert ("repo", "acme", "website") in out


def test_reserved_owner_paths_skipped():
    out = _parse_github_urls([
        "https://github.com/features",
        "https://github.com/login",
        "https://github.com/orgs/acme",  # 'orgs' is reserved as an owner
    ])
    assert out == []


def test_repo_subpaths_collapse_to_repo():
    out = _parse_github_urls([
        "https://github.com/acme/website/blob/main/README.md",
    ])
    assert out == [("repo", "acme", "website")]


def test_dedupes_owner_repo_pairs():
    out = _parse_github_urls([
        "https://github.com/acme/website",
        "https://github.com/acme/website/issues",
    ])
    assert out == [("repo", "acme", "website")]


def test_ddg_extract_urls_decodes_redirect():
    html = (
        '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Facme">x</a>'
        '<a href="https://github.com/acme/site">y</a>'
        '<a href="https://example.com/unrelated">z</a>'
    )
    urls = DuckDuckGoProvider.extract_urls(html)
    assert "https://github.com/acme" in urls
    assert "https://github.com/acme/site" in urls
    assert all("example.com" not in u for u in urls)
