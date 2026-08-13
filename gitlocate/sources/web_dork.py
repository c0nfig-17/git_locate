"""Pluggable web-search ("dorking") source.

Locates the *official* GitHub presence of a target the way a human would: by
searching ``site:github.com <company>`` on a web search engine and keeping the
``github.com`` results.

Providers
---------
- **duckduckgo** (default, *keyless*): scrapes the DuckDuckGo HTML endpoint. No
  API key required — this is the default so the tool needs no third-party
  account to benefit from web dorking.
- **serper** (optional): Google results via google.serper.dev. Higher quality /
  volume but needs an API key in the env var named by ``web_dork.api_key_env``.

The source is intentionally low-volume: by default it queries only the human
company names (not every generated variant) with a couple of templates, so it
issues a handful of requests, not hundreds. If a provider that needs a key has
none, the tool silently continues with the GitHub API alone.
"""
from __future__ import annotations

import html as _html
import logging
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

from ..models import Entity, KIND_ORG, KIND_REPO, SOURCE_WEB_DORK

log = logging.getLogger("gitlocate.webdork")

# github.com/<owner>            -> org/user
# github.com/<owner>/<repo>     -> repo
_GH_PATH_RE = re.compile(
    r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)(?:/([A-Za-z0-9._-]+))?",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
# Reserved GitHub paths that are not orgs/users.
_RESERVED = {
    "about", "features", "topics", "collections", "trending", "marketplace",
    "sponsors", "settings", "notifications", "explore", "search", "login",
    "join", "pricing", "enterprise", "team", "customer-stories", "readme",
    "orgs", "users", "apps", "blog", "site", "contact", "security", "new",
    "pulls", "issues", "codespaces", "dashboard", "watching", "stars",
}
_REPO_RESERVED = {
    "blob", "tree", "releases", "issues", "pulls", "actions", "wiki",
    "commits", "branches", "tags", "graphs", "network", "settings", ".git",
}


def _parse_github_urls(urls: List[str]) -> List[Tuple[str, str, Optional[str]]]:
    """Return (kind_hint, owner, repo) tuples from a list of URLs."""
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
            if not repo or repo.lower() in _REPO_RESERVED:
                repo = None
        key = (owner.lower(), (repo or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(("repo" if repo else "owner", owner, repo))
    return out


class WebDorkProvider:
    name = "base"
    requires_key = False

    def __init__(self, config, api_key: Optional[str] = None):
        self.cfg = config
        self.api_key = api_key

    def search(self, query: str) -> List[str]:  # pragma: no cover - interface
        raise NotImplementedError


class DuckDuckGoProvider(WebDorkProvider):
    """Keyless web search via the DuckDuckGo HTML endpoint."""

    name = "duckduckgo"
    requires_key = False

    def __init__(self, config, api_key: Optional[str] = None):
        super().__init__(config, api_key)
        if requests is None:
            raise RuntimeError("The 'requests' package is required for web dorking.")
        self.endpoint = config.get("web_dork.duckduckgo_endpoint",
                                   "https://html.duckduckgo.com/html/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:123.0) "
                           "Gecko/20100101 Firefox/123.0"),
            "Accept": "text/html",
        })

    @staticmethod
    def extract_urls(html_text: str) -> List[str]:
        """Extract target URLs from a DuckDuckGo HTML results page.

        DDG wraps outbound links as ``/l/?uddg=<url-encoded-target>``; we decode
        those and also keep any direct links.
        """
        urls: List[str] = []
        for href in _HREF_RE.findall(html_text or ""):
            href = _html.unescape(href)
            if href.startswith("//"):
                href = "https:" + href
            parsed = urlparse(href)
            if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
                target = parse_qs(parsed.query).get("uddg", [None])[0]
                if target:
                    urls.append(unquote(target))
            elif "github.com" in href:
                urls.append(href)
        return urls

    def search(self, query: str) -> List[str]:
        try:
            resp = self.session.post(self.endpoint, data={"q": query, "kl": "us-en"},
                                     timeout=30)
        except requests.RequestException as exc:
            log.warning("DuckDuckGo request failed: %s", exc)
            return []
        if resp.status_code != 200:
            log.warning("DuckDuckGo returned HTTP %s", resp.status_code)
            return []
        return self.extract_urls(resp.text)


class SerperProvider(WebDorkProvider):
    """Optional Google results via google.serper.dev (needs an API key)."""

    name = "serper"
    requires_key = True

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
                                     json={"q": query, "num": self.num}, timeout=30)
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


_PROVIDERS = {
    DuckDuckGoProvider.name: DuckDuckGoProvider,
    SerperProvider.name: SerperProvider,
}


def build_provider(config) -> Optional[WebDorkProvider]:
    """Build the configured provider, resolving its API key from the env.

    Returns None when the provider needs a key that isn't set (graceful
    degradation to GitHub-API-only discovery).
    """
    name = config.get("web_dork.provider", "duckduckgo")
    cls = _PROVIDERS.get(name)
    if cls is None:
        log.warning("Unknown web_dork provider '%s'; web dorking disabled.", name)
        return None
    api_key = None
    if cls.requires_key:
        api_key = config.resolve_secret(config.get("web_dork.api_key_env"))
        if not api_key:
            log.info("web_dork provider '%s' needs an API key ($%s) which is not "
                     "set; continuing with the GitHub API only.",
                     name, config.get("web_dork.api_key_env"))
            return None
    try:
        return cls(config, api_key)
    except RuntimeError as exc:
        log.warning("Web dorking unavailable: %s", exc)
        return None


class WebDork:
    """High-level web-search discovery over a small set of queries."""

    def __init__(self, config):
        self.cfg = config
        self.provider = build_provider(config)
        self.max_queries = int(config.get("web_dork.max_queries", 12))
        self.delay = float(config.get("web_dork.delay_seconds", 1.0))
        self.use_variants = bool(config.get("web_dork.use_variants", False))

    @property
    def available(self) -> bool:
        return self.provider is not None

    def discover(self, names: List[str], variants: List[str]) -> List[Entity]:
        if not self.provider:
            return []
        templates = self.cfg.get("web_dork.queries", ["site:github.com {name}"])
        terms: List[str] = list(names)
        if self.use_variants:
            for v in variants:
                if v not in terms:
                    terms.append(v)

        entities: List[Entity] = []
        issued = 0
        for term in terms:
            for tmpl in templates:
                if issued >= self.max_queries:
                    log.info("web_dork: reached max_queries=%d", self.max_queries)
                    return entities
                query = tmpl.replace("{name}", term)
                urls = self.provider.search(query)
                issued += 1
                for kind_hint, owner, repo in _parse_github_urls(urls):
                    entities.append(self._make_entity(kind_hint, owner, repo, term))
                if self.delay > 0:
                    time.sleep(self.delay)
        return entities

    @staticmethod
    def _make_entity(kind_hint: str, owner: str, repo: Optional[str], term: str) -> Entity:
        if kind_hint == "repo" and repo:
            full = f"{owner}/{repo}"
            ent = Entity(kind=KIND_REPO, identifier=full,
                         url=f"https://github.com/{full}", name=repo,
                         sources={SOURCE_WEB_DORK})
            ent.extra["owner_login"] = owner
            ent.extra["clone_url"] = f"https://github.com/{full}.git"
        else:
            # A bare owner path from a company web search is almost always the
            # org; get_account() later refines org-vs-user.
            ent = Entity(kind=KIND_ORG, identifier=owner,
                         url=f"https://github.com/{owner}", sources={SOURCE_WEB_DORK})
        ent.matched_variants.add(term.lower())
        return ent
