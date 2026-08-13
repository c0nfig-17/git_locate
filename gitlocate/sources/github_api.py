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

            # 429 is always throttling; a 403 is throttling ONLY when it carries
            # rate-limit evidence. A bare 403 (permissions, blocked by a proxy,
            # SSO) must return immediately instead of looping with backoff.
            if resp.status_code in (403, 429):
                if self._is_rate_limited(resp) and \
                        self._maybe_wait_for_reset(resp, attempt):
                    continue
                if resp.status_code == 403:
                    log.warning("HTTP 403 (not rate-limit; permissions/proxy?): %s", url)
                else:
                    log.warning("Rate limited (429) and giving up: %s", url)
                return resp

            if resp.status_code >= 500:
                if attempt > self.retry_max:
                    return resp
                self._sleep(self.backoff_base ** attempt, f"server {resp.status_code}")
                continue

            return resp

    @staticmethod
    def _is_rate_limited(resp) -> bool:
        """True only when the response carries genuine rate-limit evidence."""
        if resp.status_code == 429:
            return True
        # 403: require a Retry-After, exhausted remaining, or an explicit message.
        if resp.headers.get("Retry-After") is not None:
            return True
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        body = ""
        try:
            body = (resp.json().get("message") or "").lower()
        except (ValueError, AttributeError):
            pass
        return "rate limit" in body or "abuse" in body

    def _maybe_wait_for_reset(self, resp, attempt) -> bool:
        """Wait for the rate limit to reset. Return True if we waited."""
        if not self.auto_wait or attempt > self.retry_max:
            return False
        wait = None
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = None
        if wait is None and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset is not None:
                try:
                    wait = max(0.0, float(reset) - time.time()) + 1.0
                except ValueError:
                    wait = None
        if wait is None:
            # Secondary limit signalled by message but no timing headers.
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

    # -- domain-anchored search ------------------------------------------
    # Searching by the raw domain finds assets that *reference* the company's
    # domain (in a profile email, repo homepage, description or README) even when
    # the login/name doesn't resemble the company name at all.
    def search_accounts_by_email(self, domain: str, kind: str) -> List[Entity]:
        """Find accounts whose public email is on ``domain`` (``in:email``)."""
        gh_type = "org" if kind == KIND_ORG else "user"
        q = f"{domain} in:email type:{gh_type}"
        out = []
        for item in self._search("users", q):
            ent = self._entity_from_user_item(item, kind, variant=None)
            ent.extra["domain_search"] = True
            out.append(ent)
        return out

    def search_repos_by_domain(self, domain: str) -> List[Entity]:
        """Find repos that mention ``domain`` in name/description/readme."""
        q = f'"{domain}" in:name,description,readme'
        out = []
        for item in self._search("repositories", q):
            ent = self._entity_from_repo_item(item, variant=None)
            ent.extra["domain_search"] = True
            out.append(ent)
        return out

    # -- graph pivots (cheap core-API calls, not search) -----------------
    def _paginate_logins(self, path: str, limit: int) -> List[str]:
        logins: List[str] = []
        for page in range(1, self.max_pages + 1):
            data = self._get_json(path, params={"per_page": self.per_page, "page": page})
            if not data:
                break
            for item in data:
                login = item.get("login")
                if login:
                    logins.append(login)
            if len(data) < self.per_page or len(logins) >= limit:
                break
        return logins[:limit]

    def org_public_members(self, login: str, limit: int = 100) -> List[str]:
        """Public members of an organization (org -> users pivot)."""
        return self._paginate_logins(f"/orgs/{quote(login)}/public_members", limit)

    def user_orgs(self, login: str, limit: int = 100) -> List[str]:
        """Public organization memberships of a user (user -> orgs pivot)."""
        return self._paginate_logins(f"/users/{quote(login)}/orgs", limit)

    def repo_contributors(self, full_name: str, limit: int = 50) -> List[str]:
        """Contributor logins of a repo (repo -> users pivot)."""
        return self._paginate_logins(f"/repos/{full_name}/contributors", limit)

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
