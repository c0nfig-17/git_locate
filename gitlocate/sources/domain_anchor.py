"""Domain-anchor verification.

Domains are the strongest signal that a discovered org/repo/user really belongs
to the target. This module checks a candidate's profile ``blog``/``email`` (and,
for repos, the ``homepage`` and sampled commit author emails) against the target
domains, recording every match on ``entity.matched_domains``.

A match here dominates the confidence score — it is hard to fake a company's own
domain on a profile or in commit metadata.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set
from urllib.parse import urlparse

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


# Common two-level public suffixes so "acme.co.uk" -> label "acme", not "co".
_TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "org.nz",
    "co.jp", "or.jp", "ne.jp", "com.br", "net.br", "com.mx", "com.ar",
    "co.in", "net.in", "org.in", "co.za", "com.sg", "com.hk", "com.tr",
    "com.cn", "net.cn", "org.cn", "co.kr", "com.es", "com.pl",
}


def normalize_domain(value: Optional[str]) -> Optional[str]:
    """Reduce an input to a bare, lowercased registrable-ish hostname."""
    if not value:
        return None
    value = value.strip().lower()
    if not value:
        return None
    if not _SCHEME_RE.match(value):
        value = "http://" + value
    host = urlparse(value).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host or None


def host_from_url(value: Optional[str]) -> Optional[str]:
    return normalize_domain(value)


def domain_from_email(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    dom = email.rsplit("@", 1)[1].strip().lower()
    return dom or None


def _host_matches_target(host: Optional[str], targets: Set[str]) -> Optional[str]:
    """Return the target domain matched by ``host`` (exact or subdomain)."""
    if not host:
        return None
    for tgt in targets:
        if host == tgt or host.endswith("." + tgt):
            return tgt
    return None


def prepare_targets(domains: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for d in domains:
        nd = normalize_domain(d)
        if nd:
            out.add(nd)
    return out


def registrable_label(domain: Optional[str]) -> Optional[str]:
    """Return the registrable label of a domain WITHOUT its TLD.

    ``acme.com`` -> ``acme``; ``shop.acme.co.uk`` -> ``acme``; a bare label
    (``acme``) is returned as-is. This is the "domain without TLD" form used as
    a name/search seed.
    """
    host = normalize_domain(domain)
    if not host:
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in _TWO_LEVEL_SUFFIXES and len(parts) >= 3:
        return parts[-3]
    return parts[-2]


def domain_seed_labels(domains: Iterable[str]) -> List[str]:
    """De-duplicated registrable labels for a set of domains (order-stable)."""
    out: List[str] = []
    for d in domains:
        lbl = registrable_label(d)
        if lbl and lbl not in out:
            out.append(lbl)
    return out


def match_value(value: Optional[str], targets: Set[str]) -> Optional[str]:
    """Match a URL-ish or email-ish value against target domains."""
    if not value:
        return None
    if "@" in value and "://" not in value:
        return _host_matches_target(domain_from_email(value), targets)
    return _host_matches_target(host_from_url(value), targets)


def anchor_entity(entity, targets: Set[str], extra_values: Optional[List[str]] = None) -> Set[str]:
    """Record every target domain matched by the entity's fields.

    Returns the set of newly matched domains (also merged into
    ``entity.matched_domains``).
    """
    matched: Set[str] = set()
    candidates: List[Optional[str]] = [entity.blog, entity.email]
    candidates.extend(extra_values or [])
    for val in candidates:
        m = match_value(val, targets)
        if m:
            matched.add(m)
    if matched:
        entity.matched_domains |= matched
    return matched
