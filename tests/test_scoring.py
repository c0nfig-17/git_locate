"""Tests for confidence scoring, including topic-aware description matching."""
from gitlocate.config import load_config
from gitlocate.models import Entity, KIND_ORG, KIND_REPO, SOURCE_GITHUB_API, SOURCE_WEB_DORK
from gitlocate.scoring import Scorer


def _scorer():
    return Scorer(load_config(None))


def test_domain_anchor_plus_exact_name():
    ent = Entity(KIND_ORG, "acme", "https://github.com/acme")
    ent.matched_domains = {"acme.com"}
    _scorer().score_entity(ent, {"acme"}, confirmed_owners=set())
    assert ent.signals["domain_anchor"] == 0.5
    assert ent.signals["exact_name_match"] == 0.3
    assert abs(ent.confidence - 0.8) < 1e-6


def test_multi_source_and_web_dork():
    ent = Entity(KIND_ORG, "acme", "https://github.com/acme",
                 sources={SOURCE_GITHUB_API, SOURCE_WEB_DORK})
    _scorer().score_entity(ent, set(), confirmed_owners=set())
    assert "multi_source" in ent.signals
    assert "web_dork" in ent.signals


def test_confidence_clamped_to_one():
    ent = Entity(KIND_ORG, "acme", "https://github.com/acme",
                 sources={SOURCE_GITHUB_API, SOURCE_WEB_DORK})
    ent.matched_domains = {"acme.com"}
    ent.extra["commit_email_match"] = True
    ent.extra["domain_search"] = True
    _scorer().score_entity(ent, {"acme"}, confirmed_owners=set())
    assert ent.confidence == 1.0


def test_repo_owner_confirmed_signal():
    repo = Entity(KIND_REPO, "acme/site", "https://github.com/acme/site")
    repo.extra["owner_login"] = "acme"
    _scorer().score_entity(repo, set(), confirmed_owners={"acme"})
    assert repo.signals.get("owner_confirmed") == 0.25


def test_topic_matches_count_as_description_match():
    repo = Entity(KIND_REPO, "acme/x", "https://github.com/acme/x")
    repo.extra["topics"] = ["acme", "security"]  # no description at all
    _scorer().score_entity(repo, {"acme"}, confirmed_owners=set())
    assert "description_match" in repo.signals


def test_no_signals_means_zero_confidence():
    ent = Entity(KIND_ORG, "unrelated", "https://github.com/unrelated",
                 sources={SOURCE_GITHUB_API})
    _scorer().score_entity(ent, {"acme"}, confirmed_owners=set())
    assert ent.confidence == 0.0
