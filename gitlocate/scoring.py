"""Confidence scoring.

Each entity is scored in ``[0, 1]`` from a small set of weighted, explainable
signals. The individual contributions are stored on ``entity.signals`` so the
JSON output shows *why* something scored the way it did — useful when triaging
a large recon surface by hand.

Signals
-------
- ``domain_anchor``       one of the target domains appears in the profile
                          blog/email/homepage or commit emails (dominant signal)
- ``exact_name_match``    a generated variant equals the login or name exactly
- ``description_match``   a variant appears inside the description
- ``multi_source``        the entity was seen through more than one source
- ``web_dork``            the entity surfaced via the web-dorking source
- ``owner_confirmed``     (repos) the owner is a confirmed org/user above thresh
- ``commit_email_domain`` a sampled commit email matches a target domain
"""
from __future__ import annotations

from typing import Dict, Iterable, Set

from .models import (
    Entity,
    Findings,
    KIND_REPO,
    SOURCE_GITHUB_API,
    SOURCE_WEB_DORK,
)


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _exact_variant_hit(entity: Entity, variants: Set[str]) -> bool:
    login = entity.identifier.split("/")[-1]
    login_n = _norm(login)
    name_n = _norm(entity.name or "")
    norm_variants = {_norm(v) for v in variants if v}
    return login_n in norm_variants or (bool(name_n) and name_n in norm_variants)


def _description_variant_hit(entity: Entity, variants: Iterable[str]) -> bool:
    desc = (entity.description or "").lower()
    if not desc:
        return False
    return any(v and v.lower() in desc for v in variants)


class Scorer:
    def __init__(self, config):
        w = config.get("scoring.weights", {}) or {}
        self.w = {
            "domain_anchor": float(w.get("domain_anchor", 0.5)),
            "exact_name_match": float(w.get("exact_name_match", 0.3)),
            "description_match": float(w.get("description_match", 0.15)),
            "multi_source": float(w.get("multi_source", 0.2)),
            "web_dork": float(w.get("web_dork", 0.15)),
            "owner_confirmed": float(w.get("owner_confirmed", 0.25)),
            "commit_email_domain": float(w.get("commit_email_domain", 0.3)),
            "domain_search": float(w.get("domain_search", 0.15)),
            "org_member": float(w.get("org_member", 0.2)),
            "related": float(w.get("related", 0.1)),
        }
        self.min_confidence = float(config.get("scoring.min_confidence", 0.0))
        self.owner_min = float(config.get("github.owner_repo_min_confidence", 0.4))

    def score_entity(self, entity: Entity, all_variants: Set[str],
                     confirmed_owners: Set[str]) -> None:
        signals: Dict[str, float] = {}

        if entity.matched_domains:
            # commit-email matches are tracked separately via extra flag
            if entity.extra.get("commit_email_match"):
                signals["commit_email_domain"] = self.w["commit_email_domain"]
            signals["domain_anchor"] = self.w["domain_anchor"]

        if _exact_variant_hit(entity, all_variants):
            signals["exact_name_match"] = self.w["exact_name_match"]

        if _description_variant_hit(entity, entity.matched_variants or all_variants):
            signals["description_match"] = self.w["description_match"]

        if len(entity.sources) > 1:
            signals["multi_source"] = self.w["multi_source"]

        if SOURCE_WEB_DORK in entity.sources:
            signals["web_dork"] = self.w["web_dork"]

        # Found by searching the target domain string itself.
        if entity.extra.get("domain_search"):
            signals["domain_search"] = self.w["domain_search"]

        # Pivot signals: being a public member of a confirmed org is meaningful;
        # other graph relations (a confirmed user's org, a contributor) less so.
        if entity.extra.get("org_member"):
            signals["org_member"] = self.w["org_member"]
        elif entity.extra.get("related"):
            signals["related"] = self.w["related"]

        if entity.kind == KIND_REPO:
            owner = (entity.extra.get("owner_login") or "").lower()
            if owner and owner in confirmed_owners:
                signals["owner_confirmed"] = self.w["owner_confirmed"]

        entity.signals = signals
        entity.confidence = min(1.0, round(sum(signals.values()), 6))

    def score_all(self, findings: Findings, all_variants: Set[str]) -> None:
        # First pass: score orgs/users so we know which owners are "confirmed".
        for kind in ("org", "user"):
            for ent in findings.of_kind(kind):
                self.score_entity(ent, all_variants, confirmed_owners=set())

        confirmed_owners = {
            e.identifier.lower()
            for k in ("org", "user")
            for e in findings.of_kind(k)
            if e.confidence >= self.owner_min
        }

        # Second pass: repos (need confirmed-owner set) and re-score owners is
        # unnecessary since their inputs did not change.
        for ent in findings.of_kind(KIND_REPO):
            self.score_entity(ent, all_variants, confirmed_owners)
