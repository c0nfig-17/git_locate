"""Data models for discovered entities.

Everything the tool discovers is normalized into an :class:`Entity`. Entities
are aggregated by ``(kind, identifier)`` so that the same org/repo/user seen
through several sources is merged into a single record whose signals and
confidence accumulate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# Entity kinds, ordered by discovery/reporting priority.
KIND_ORG = "org"
KIND_REPO = "repo"
KIND_USER = "user"
KIND_ORDER = [KIND_ORG, KIND_REPO, KIND_USER]

# Discovery source identifiers.
SOURCE_GITHUB_API = "github_api"
SOURCE_WEB_DORK = "web_dork"
SOURCE_ORG_REPOS = "org_repos"       # repo enumerated from a confirmed owner
SOURCE_DOMAIN_ANCHOR = "domain_anchor"


@dataclass
class Entity:
    """A single discovered GitHub asset (organization, repository or user)."""

    kind: str
    identifier: str                       # login (org/user) or "owner/name" (repo)
    url: str
    name: Optional[str] = None
    description: Optional[str] = None
    blog: Optional[str] = None            # profile blog / repo homepage
    email: Optional[str] = None
    sources: Set[str] = field(default_factory=set)
    matched_variants: Set[str] = field(default_factory=set)
    matched_domains: Set[str] = field(default_factory=set)
    # signal name -> contribution already weighted (filled by scoring)
    signals: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    extra: Dict[str, object] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.identifier.lower()}"

    def merge(self, other: "Entity") -> None:
        """Fold another observation of the same entity into this one."""
        self.sources |= other.sources
        self.matched_variants |= other.matched_variants
        self.matched_domains |= other.matched_domains
        # Prefer non-empty metadata; first non-null wins, later fills gaps.
        for attr in ("name", "description", "blog", "email", "url"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        self.extra.update(other.extra)

    def to_dict(self) -> dict:
        """Serialize to the clean, machine-consumable output shape."""
        return {
            "id": self.identifier,
            "url": self.url,
            "confidence": round(self.confidence, 3),
            "name": self.name,
            "description": self.description,
            "blog": self.blog,
            "email": self.email,
            "matched_domains": sorted(self.matched_domains),
            "matched_variants": sorted(self.matched_variants),
            "sources": sorted(self.sources),
            "signals": {k: round(v, 3) for k, v in sorted(self.signals.items())},
        }


class Findings:
    """Aggregates entities keyed by ``(kind, identifier)``."""

    def __init__(self) -> None:
        self._by_key: Dict[str, Entity] = {}

    def add(self, entity: Entity) -> Entity:
        existing = self._by_key.get(entity.key)
        if existing is None:
            self._by_key[entity.key] = entity
            return entity
        existing.merge(entity)
        return existing

    def get(self, kind: str, identifier: str) -> Optional[Entity]:
        return self._by_key.get(f"{kind}:{identifier.lower()}")

    def of_kind(self, kind: str) -> List[Entity]:
        items = [e for e in self._by_key.values() if e.kind == kind]
        # Highest confidence first, then identifier for stable ordering.
        items.sort(key=lambda e: (-e.confidence, e.identifier.lower()))
        return items

    def all(self) -> List[Entity]:
        out: List[Entity] = []
        for kind in KIND_ORDER:
            out.extend(self.of_kind(kind))
        return out

    def __len__(self) -> int:
        return len(self._by_key)
