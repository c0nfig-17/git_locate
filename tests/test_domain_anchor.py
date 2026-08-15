"""Tests for domain normalization / registrable label / anchoring."""
from gitlocate.models import Entity, KIND_ORG
from gitlocate.sources.domain_anchor import (
    normalize_domain,
    registrable_label,
    domain_seed_labels,
    prepare_targets,
    match_value,
    anchor_entity,
)


def test_normalize_domain_strips_scheme_www_and_path():
    assert normalize_domain("https://www.Acme.com/some/path") == "acme.com"
    assert normalize_domain("ACME.com") == "acme.com"
    assert normalize_domain("") is None
    assert normalize_domain(None) is None


def test_registrable_label():
    assert registrable_label("acme.com") == "acme"
    assert registrable_label("shop.acme.co.uk") == "acme"     # two-level suffix
    assert registrable_label("sub.acme.io") == "acme"
    assert registrable_label("acme") == "acme"                # bare label


def test_domain_seed_labels_dedupes_order_stable():
    labels = domain_seed_labels(["acme.com", "www.acme.com", "other.io", "acme.io"])
    assert labels == ["acme", "other"]


def test_prepare_targets():
    assert prepare_targets(["Acme.com", "https://x.io/"]) == {"acme.com", "x.io"}


def test_match_value_url_and_email():
    targets = {"acme.com"}
    assert match_value("https://blog.acme.com", targets) == "acme.com"
    assert match_value("ceo@acme.com", targets) == "acme.com"
    assert match_value("ceo@evil.com", targets) is None
    assert match_value("https://notacme.com", targets) is None  # not a subdomain
    assert match_value(None, targets) is None


def test_subdomain_matches_but_lookalike_does_not():
    targets = {"acme.com"}
    assert match_value("https://mail.acme.com", targets) == "acme.com"
    assert match_value("https://acme.com.evil.net", targets) is None


def test_anchor_entity_records_matches():
    ent = Entity(KIND_ORG, "acme", "https://github.com/acme",
                 blog="https://acme.com", email="dev@acme.com")
    matched = anchor_entity(ent, {"acme.com"})
    assert matched == {"acme.com"}
    assert ent.matched_domains == {"acme.com"}


def test_anchor_entity_with_extra_commit_emails():
    ent = Entity(KIND_ORG, "acme", "https://github.com/acme")
    matched = anchor_entity(ent, {"acme.com"}, extra_values=["x@acme.com"])
    assert "acme.com" in matched
