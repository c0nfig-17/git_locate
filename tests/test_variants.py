"""Tests for company-name variant generation."""
from gitlocate.variants import generate_variants, generate_all

CFG = {
    "separators": ["", " ", "-", "_"],
    "suffixes": ["inc", "labs", "io"],
    "include_word_combos": True,
    "min_word_len": 2,
    "max_variants": 60,
    "max_total_variants": 120,
}


def test_basic_forms_present():
    v = generate_variants("Acme Corp", CFG)
    assert "acme" in v
    assert "acmecorp" in v
    assert "acme-corp" in v
    assert "acme_corp" in v
    assert "acme corp" in v


def test_suffix_decorations():
    v = generate_variants("Acme", CFG)
    assert "acmeinc" in v
    assert "acme-labs" in v
    assert "acme_io" in v
    # the space separator is never used to glue a suffix
    assert "acme io" not in v


def test_stopwords_are_dropped():
    v = generate_variants("The Acme Company", CFG)
    assert "acme" in v
    # "the"/"company" are stopwords and must not become standalone variants
    assert "the" not in v
    assert "company" not in v


def test_multiword_bigrams():
    v = generate_variants("Palo Alto Networks", CFG)
    assert "paloalto" in v
    assert "palo-alto" in v


def test_max_variants_truncation():
    cfg = dict(CFG, max_variants=3)
    v = generate_variants("Acme Corp", cfg)
    assert len(v) <= 3


def test_empty_name_returns_empty():
    assert generate_variants("!!!", CFG) == []


def test_generate_all_dedupes_across_names():
    out = generate_all(["Acme", "acme"], CFG)
    assert out.count("acme") == 1


def test_generate_all_respects_total_cap():
    cfg = dict(CFG, max_total_variants=5)
    out = generate_all(["Acme Corp", "Globex Inc", "Initech"], cfg)
    assert len(out) <= 5
