"""Company-name variant generation.

From a human company name we derive the many spellings an org/repo/user login
might realistically use: joined, spaced, hyphenated, underscored, individual
words, word combinations and common suffixes (inc, labs, io, hq, dev, ...).

The generated variants drive the GitHub Search API queries and the web dorks.
All behaviour is data-driven from the ``variants`` config block — nothing here
is hardcoded to a particular company.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Very common company words that are noise as standalone GitHub search terms.
_DEFAULT_STOPWORDS = {
    "the", "and", "of", "for", "group", "holdings", "company", "co",
    "ltd", "limited", "llc", "gmbh", "sa", "srl", "bv", "plc",
}


def _tokens(name: str) -> List[str]:
    """Split a company name into lowercase alphanumeric tokens."""
    return [t.lower() for t in _TOKEN_RE.findall(name)]


def _meaningful_tokens(tokens: Iterable[str], stopwords: set, min_len: int) -> List[str]:
    out = [t for t in tokens if len(t) >= min_len and t not in stopwords]
    return out or [t for t in tokens]  # never return empty


def generate_variants(name: str, cfg: Dict) -> List[str]:
    """Return a de-duplicated, order-stable list of search variants for ``name``."""
    separators = cfg.get("separators", ["", " ", "-", "_"])
    suffixes = cfg.get("suffixes", [])
    include_combos = cfg.get("include_word_combos", True)
    min_word_len = int(cfg.get("min_word_len", 2))
    stopwords = set(w.lower() for w in cfg.get("stopwords", _DEFAULT_STOPWORDS))
    max_variants = int(cfg.get("max_variants", 60))

    raw = _tokens(name)
    if not raw:
        return []
    tokens = _meaningful_tokens(raw, stopwords, min_word_len)

    variants: List[str] = []

    def _add(v: str) -> None:
        v = v.strip().lower()
        if v and v not in variants:
            variants.append(v)

    # Separators to use when appending suffixes (default to the joining set,
    # minus the space form which is never used in a login+suffix like "acme io").
    suffix_seps = [s for s in separators if s != " "] or [""]

    # Variants are emitted in *priority* order so that, after truncation to
    # max_variants, the highest-value forms always survive. The most valuable
    # forms are the short ones a real org/user login actually uses (e.g. "acme",
    # "acme-labs", "acmedev") — NOT long compound forms ("acmecorp-inc").

    # 1. The full name across every separator style.
    for sep in separators:
        _add(sep.join(tokens))

    # 2. Each individual meaningful word.
    for t in tokens:
        _add(t)

    # 3. Adjacent word combinations (bi-grams). For a 2-token name these dedupe
    #    against the full-name forms; they matter for 3+ token names
    #    (e.g. "Palo Alto Networks" -> "paloalto", "palo-alto").
    if include_combos and len(tokens) > 1:
        for i in range(len(tokens) - 1):
            pair = tokens[i:i + 2]
            for sep in separators:
                _add(sep.join(pair))

    # 4. Suffix-decorated bases, PRIMARY base (leading token) first so its
    #    high-value forms are not starved by the longer compound bases.
    base_forms: List[str] = [tokens[0]]
    for sep in ("", "-", "_"):
        base_forms.append(sep.join(tokens))
    seen_bases: List[str] = []
    for b in base_forms:
        if b and b not in seen_bases:
            seen_bases.append(b)
    for base in seen_bases:
        for suf in suffixes:
            for sep in suffix_seps:
                _add(f"{base}{sep}{suf}")

    return variants[:max_variants]


def generate_all(names: Iterable[str], cfg: Dict) -> List[str]:
    """Variants for several company names, de-duplicated across all of them."""
    out: List[str] = []
    for name in names:
        for v in generate_variants(name, cfg):
            if v not in out:
                out.append(v)
    max_total = int(cfg.get("max_total_variants", cfg.get("max_variants", 60) * 3))
    return out[:max_total]
