"""GitHub Search API client with automatic rate-limit handling.

This is the always-on discovery source. It searches organizations, repositories
and users for each name variant, and can enrich profiles (blog/email) and sample
commit author emails so that :mod:`gitlocate.sources.domain_anchor` can verify
candidates against the target domains.

Authentication is optional but strongly recommended: with a token the search
rate limit is 30 requests/minute (vs 10 unauthenticated) and the core limit is
5000/hour. The token is read from the environment variable named in config —
never hardcoded.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

from ..models import (
    Entity,
    KIND_ORG,
    KIND_REPO,
    KIND_USER,
    SOURCE_GITHUB_API,
    SOURCE_ORG_REPOS,
)

log = logging.getLogger("gitlocate.github")


class GitHubClient:
    def __init__(self, config, token: Optional[str] = None):
        if requests is None:
            raise RuntimeError("The 'requests' package is required. pip install requests")
        self.cfg = config
        self.api_url = config.get("github.api_url", "https://api.github.com").rstrip("/")
        self.per_page = int(config.get("github.per_page", 100))
        self.max_pages = int(config.get("github.max_pages", 3))
        self.token = token
        rl = config.section("rate_limit")
        self.auto_wait = bool(rl.get("auto_wait", True))
        self.max_wait = int(rl.get("max_wait_seconds", 900))
        self.retry_max = int(rl.get("retry_max", 5))
        self.backoff_base = float(rl.get("backoff_base", 2.0))

        self.session = requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "git_locate-recon",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    # -- low-level request with rate-limit awareness ----------------------
    def _request(self, method: str, url: str, **kwargs) -> Optional["requests.Response"]:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:  # network hiccup
                if attempt > self.retry_max:
                    log.warning("Request failed after %d attempts: %s", attempt, exc)
                    return None
                self._sleep(self.backoff_base ** attempt, "network error")
                continue

            # Primary rate limit exhausted.
            if resp.status_code in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining")
                retry_after = resp.headers.get("Retry-After")
                if self._maybe_wait_for_reset(resp, remaining, retry_after, attempt):
                    continue
                log.warning("Rate limited (%s) and giving up: %s", resp.status_code, url)
                return resp

            if resp.status_code >= 500:
                if attempt > self.retry_max:
                    return resp
                self._sleep(self.backoff_base ** attempt, f"server {resp.status_code}")
                continue

            return resp

    def _maybe_wait_for_reset(self, resp, remaining, retry_after, attempt) -> bool:
        """Return True if we waited and should retry, False to give up."""
        if not self.auto_wait or attempt > self.retry_max:
            return False
        wait = None
        if retry_after is not None:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = None
        if wait is None and remaining == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset is not None:
                try:
                    wait = max(0.0, float(reset) - time.time()) + 1.0
                except ValueError:
                    wait = None
        if wait is None:
            # Secondary/abuse limit without headers: exponential backoff.
            wait = self.backoff_base ** attempt
        if wait > self.max_wait:
            log.warning("Rate-limit wait %.0fs exceeds max_wait %ds; giving up",
                        wait, self.max_wait)
            return False
        self._sleep(wait, "rate limit")
        return True

    @staticmethod
    def _sleep(seconds: float, reason: str) -> None:
        seconds = max(0.5, seconds)
        log.info("Waiting %.1fs (%s)", seconds, reason)
        time.sleep(seconds)

    def _get_json(self, path_or_url: str, params: Optional[Dict] = None) -> Optional[Dict]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_url}{path_or_url}"
        resp = self._request("GET", url, params=params)
        if resp is None or resp.status_code != 200:
            if resp is not None and resp.status_code not in (200, 404):
                log.debug("GET %s -> %s", url, resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # -- search -----------------------------------------------------------
    def _search(self, endpoint: str, query: str) -> List[Dict]:
        """Paginated GitHub search returning the raw ``items`` list."""
        items: List[Dict] = []
        for page in range(1, self.max_pages + 1):
            params = {"q": query, "per_page": self.per_page, "page": page}
            data = self._get_json(f"/search/{endpoint}", params=params)
            if not data:
                break
            batch = data.get("items", [])
            items.extend(batch)
            if len(batch) < self.per_page:
                break
            if len(items) >= data.get("total_count", 0):
                break
        return items

    @staticmethod
    def _in_qualifier(fields: Iterable[str]) -> str:
        fields = [f for f in fields if f]
        return f" in:{','.join(fields)}" if fields else ""

    def search_orgs(self, variant: str) -> List[Entity]:
        fields = self.cfg.get("github.search_in.orgs", ["login", "name", "email"])
        q = f"{variant} type:org{self._in_qualifier(fields)}"
        out = []
        for item in self._search("users", q):
            out.append(self._entity_from_user_item(item, KIND_ORG, variant))
        return out

    def search_users(self, variant: str) -> List[Entity]:
        fields = self.cfg.get("github.search_in.users", ["login", "name", "email"])
        q = f"{variant} type:user{self._in_qualifier(fields)}"
        out = []
        for item in self._search("users", q):
            out.append(self._entity_from_user_item(item, KIND_USER, variant))
        return out

    def search_repos(self, variant: str) -> List[Entity]:
        fields = self.cfg.get("github.search_in.repos", ["name", "description"])
        # 'readme' is a valid search-in field but heavy; keep whatever config says.
        q = f"{variant}{self._in_qualifier(fields)}"
        out = []
        for item in self._search("repositories", q):
            out.append(self._entity_from_repo_item(item, variant))
        return out

    # -- owner repo enumeration ------------------------------------------
    def list_owner_repos(self, login: str, is_org: bool) -> List[Entity]:
        """List public repos for a confirmed org/user (strong repo signal)."""
        kind_path = "orgs" if is_org else "users"
        # /orgs/{org}/repos accepts type=public; /users/{login}/repos does not
        # (it takes all|owner|member and only ever returns public repos anyway).
        repo_type = "public" if is_org else "owner"
        out: List[Entity] = []
        for page in range(1, self.max_pages + 1):
            params = {"per_page": self.per_page, "page": page, "type": repo_type,
                      "sort": "updated"}
            data = self._get_json(f"/{kind_path}/{quote(login)}/repos", params=params)
            if not data:
                break
            for item in data:
                ent = self._entity_from_repo_item(item, variant=None)
                ent.sources = {SOURCE_ORG_REPOS}
                ent.extra["owner_login"] = login
                out.append(ent)
            if len(data) < self.per_page:
                break
        return out

    # -- profile enrichment ----------------------------------------------
    def get_account(self, login: str) -> Dict:
        """Fetch an account by login.

        ``/users/{login}`` resolves BOTH users and organizations and returns a
        ``type`` field ("User" / "Organization"), so a single call lets us both
        enrich (blog/email/name/bio) and correctly classify a bare owner login
        that came from the web-dorking source.
        """
        return self._get_json(f"/users/{quote(login)}") or {}

    def commit_author_emails(self, full_name: str, sample: int = 20) -> List[str]:
        data = self._get_json(f"/repos/{full_name}/commits",
                              params={"per_page": min(sample, 100)})
        emails: List[str] = []
        if not data:
            return emails
        for commit in data:
            for role in ("author", "committer"):
                info = (commit.get("commit") or {}).get(role) or {}
                email = info.get("email")
                if email and "noreply.github.com" not in email:
                    emails.append(email.lower())
        return sorted(set(emails))

    # -- item -> Entity ---------------------------------------------------
    @staticmethod
    def _entity_from_user_item(item: Dict, kind: str, variant: Optional[str]) -> Entity:
        login = item.get("login", "")
        ent = Entity(
            kind=kind,
            identifier=login,
            url=item.get("html_url") or f"https://github.com/{login}",
            name=item.get("name"),
            sources={SOURCE_GITHUB_API},
        )
        if variant:
            ent.matched_variants.add(variant)
        return ent

    @staticmethod
    def _entity_from_repo_item(item: Dict, variant: Optional[str]) -> Entity:
        full = item.get("full_name") or ""
        owner = (item.get("owner") or {}).get("login", "")
        ent = Entity(
            kind=KIND_REPO,
            identifier=full or item.get("name", ""),
            url=item.get("html_url") or (f"https://github.com/{full}" if full else ""),
            name=item.get("name"),
            description=item.get("description"),
            blog=item.get("homepage"),
            sources={SOURCE_GITHUB_API},
        )
        ent.extra["owner_login"] = owner
        ent.extra["clone_url"] = item.get("clone_url") or (
            f"https://github.com/{full}.git" if full else "")
        ent.extra["fork"] = bool(item.get("fork"))
        ent.extra["stars"] = item.get("stargazers_count")
        if variant:
            ent.matched_variants.add(variant)
        return ent
