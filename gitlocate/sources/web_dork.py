"""Pluggable web-dorking source.

This module locates the *official* GitHub presence of a target the way a human
would: by Googling ``site:github.com <company>``. It is intentionally optional
and pluggable — it only runs when its API key is present in the environment.
If the key is missing, discovery silently continues with the GitHub API alone.

The default provider is Serper (google.serper.dev). Additional providers can be
added by registering another :class:`WebDorkProvider` implementation; the
provider name is selected in the ``web_dork.provider`` config key.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

from ..models import Entity, KIND_ORG, KIND_REPO, KIND_USER, SOURCE_WEB_DORK

log = logging.getLogger("gitlocate.webdork")

# github.com/<owner>            -> org/user
# github.com/<owner>/<repo>     -> repo
_GH_PATH_RE = re.compile(
    r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)(?:/([A-Za-z0-9._-]+))?",
    re.IGNORECASE,
)
# Reserved GitHub paths that are not orgs/users.
_RESERVED = {
    "about", "features", "topics", "collections", "trending", "marketplace",
    "sponsors", "settings", "notifications", "explore", "search", "login",
    "join", "pricing", "enterprise", "team", "customer-stories", "readme",
    "orgs", "users", "apps", "blog", "site", "contact", "security",
}


def _parse_github_urls(urls: List[str]) -> List[Tuple[str, str, Optional[str]]]:
    """Return (kind_hint, owner, repo) tuples from a list of URLs.

    kind_hint is 'repo' when a repo path is present, else 'owner'.
    """
    out: List[Tuple[str, str, Optional[str]]] = []
    seen = set()
    for url in urls:
        m = _GH_PATH_RE.search(url or "")
        if not m:
            continue
        owner = m.group(1)
        repo = m.group(2)
        if not owner or owner.lower() in _RESERVED:
            continue
        if repo:
            repo = repo.rstrip("/.").split("?")[0].split("#")[0]
            if repo.lower() in {"blob", "tree", "releases", "issues", "pulls",
                                 "actions", "wiki", "commits", "branches", ".git"}:
                repo = None
        key = (owner.lower(), (repo or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(("repo" if repo else "owner", owner, repo))
    return out


class WebDorkProvider:
    name = "base"

    def __init__(self, config, api_key: str):
        self.cfg = config
        self.api_key = api_key

    def search(self, query: str) -> List[str]:  # pragma: no cover - interface
        raise NotImplementedError


class SerperProvider(WebDorkProvider):
    name = "serper"

    def __init__(self, config, api_key: str):
        super().__init__(config, api_key)
        if requests is None:
            raise RuntimeError("The 'requests' package is required for web dorking.")
        self.endpoint = config.get("web_dork.endpoint", "https://google.serper.dev/search")
        self.num = int(config.get("web_dork.num", 20))
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "User-Agent": "git_locate-recon",
        })

    def search(self, query: str) -> List[str]:
        try:
            resp = self.session.post(self.endpoint,
                                     json={"q": query, "num": self.num},
                                     timeout=30)
        except requests.RequestException as exc:
            log.warning("Serper request failed: %s", exc)
            return []
        if resp.status_code != 200:
            log.warning("Serper returned HTTP %s", resp.status_code)
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        urls: List[str] = []
        for key in ("organic", "topStories", "peopleAlsoAsk"):
            for entry in data.get(key, []) or []:
                link = entry.get("link") or entry.get("url")
                if link:
                    urls.append(link)
        return urls


_PROVIDERS = {SerperProvider.name: SerperProvider}


def build_provider(config, api_key: str) -> Optional[WebDorkProvider]:
    name = config.get("web_dork.provider", "serper")
    cls = _PROVIDERS.get(name)
    if cls is None:
        log.warning("Unknown web_dork provider '%s'; skipping web dorking.", name)
        return None
    return cls(config, api_key)


class WebDork:
    """High-level web-dorking discovery over configured queries."""

    def __init__(self, config, api_key: Optional[str]):
        self.cfg = config
        self.provider = build_provider(config, api_key) if api_key else None

    @property
    def available(self) -> bool:
        return self.provider is not None

    def discover(self, names: List[str], variants: List[str]) -> List[Entity]:
        if not self.provider:
            return []
        templates = self.cfg.get("web_dork.queries", ["site:github.com {name}"])
        # Query with the human names (best signal) and the compact variants.
        terms: List[str] = []
        for n in names:
            if n not in terms:
                terms.append(n)
        for v in variants:
            if v not in terms:
                terms.append(v)

        entities: List[Entity] = []
        for term in terms:
            for tmpl in templates:
                query = tmpl.replace("{name}", term)
                urls = self.provider.search(query)
                for kind_hint, owner, repo in _parse_github_urls(urls):
                    entities.append(self._make_entity(kind_hint, owner, repo, term))
        return entities

    @staticmethod
    def _make_entity(kind_hint: str, owner: str, repo: Optional[str], term: str) -> Entity:
        if kind_hint == "repo" and repo:
            full = f"{owner}/{repo}"
            ent = Entity(
                kind=KIND_REPO,
                identifier=full,
                url=f"https://github.com/{full}",
                name=repo,
                sources={SOURCE_WEB_DORK},
            )
            ent.extra["owner_login"] = owner
            ent.extra["clone_url"] = f"https://github.com/{full}.git"
        else:
            # A bare owner path: could be an org or a user; default to org since
            # web dorks for a company usually surface the org. The GitHub-API
            # cross-check / enrichment later refines this.
            ent = Entity(
                kind=KIND_ORG,
                identifier=owner,
                url=f"https://github.com/{owner}",
                sources={SOURCE_WEB_DORK},
            )
        ent.matched_variants.add(term.lower())
        return ent
