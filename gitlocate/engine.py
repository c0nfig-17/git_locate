"""Discovery orchestration.

Ties the sources together in the required priority order — organizations first,
then repositories, then users — while keeping the web-dorking source optional and
the domains as the verification anchor throughout.

Pipeline
--------
1. Generate name variants.
2. Gather candidates: GitHub Search API (orgs/repos/users) + web dork (optional).
3. Resolve & enrich owners (type + blog/email) and anchor them to the domains.
4. Score owners, then enumerate public repos of the confirmed ones.
5. Enrich & anchor repos (homepage + optional commit-email sampling).
6. Final scoring + confidence filtering.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from .models import (
    Entity,
    Findings,
    KIND_ORG,
    KIND_REPO,
    KIND_USER,
)
from .scoring import Scorer
from .sources.domain_anchor import anchor_entity, prepare_targets
from .sources.github_api import GitHubClient
from .sources.web_dork import WebDork
from .variants import generate_all

log = logging.getLogger("gitlocate.engine")


class Engine:
    def __init__(self, config):
        self.cfg = config
        self.scorer = Scorer(config)

    def run(self, companies: List[str], domains: List[str]) -> Findings:
        variants = generate_all(companies, self.cfg.section("variants"))
        variant_set: Set[str] = set(variants)
        targets = prepare_targets(domains)
        log.info("Generated %d name variants", len(variants))

        gh = self._build_github(len(variants))
        web = self._build_webdork()

        raw_owners: List[Entity] = []   # kind org/user (org may be tentative)
        raw_repos: List[Entity] = []

        # --- Phase 1+2+3: search candidates -----------------------------
        if gh is not None:
            self._github_search(gh, variants, raw_owners, raw_repos)

        if web is not None and web.available:
            log.info("Web-dorking source active (%s)", self.cfg.get("web_dork.provider"))
            for ent in web.discover(companies, variants):
                (raw_repos if ent.kind == KIND_REPO else raw_owners).append(ent)
        elif self.cfg.get("web_dork.enabled", True):
            log.info("Web-dorking disabled or no API key present; using GitHub API only.")

        if gh is None:
            log.warning("No GitHub client available (missing 'requests' or fatal init).")
            return Findings()

        # --- Phase: resolve & enrich owners -----------------------------
        findings = Findings()
        self._resolve_owners(gh, raw_owners, targets, findings)

        # Score owners now so we know which are 'confirmed' before enumerating.
        for kind in (KIND_ORG, KIND_USER):
            for ent in findings.of_kind(kind):
                self.scorer.score_entity(ent, variant_set, confirmed_owners=set())

        # --- Phase: enumerate repos of confirmed owners -----------------
        if self.cfg.get("github.enumerate_owner_repos", True):
            self._enumerate_owner_repos(gh, findings, raw_repos)

        # --- Phase: enrich & anchor repos, then aggregate ---------------
        self._process_repos(gh, raw_repos, targets, findings)

        # --- Final scoring + filter -------------------------------------
        self.scorer.score_all(findings, variant_set)
        return self._filter(findings)

    # -- builders ---------------------------------------------------------
    def _build_github(self, n_variants: int) -> Optional[GitHubClient]:
        token = self.cfg.github_token
        if not token:
            log.warning("No GitHub token in env var '%s'. Running UNAUTHENTICATED "
                        "(much lower rate limits).", self.cfg.get("github.token_env"))
        try:
            return GitHubClient(self.cfg, token=token)
        except RuntimeError as exc:
            log.error("%s", exc)
            return None

    def _build_webdork(self) -> Optional[WebDork]:
        if not self.cfg.get("web_dork.enabled", True):
            return None
        key = self.cfg.serper_api_key
        if not key:
            return None
        try:
            return WebDork(self.cfg, key)
        except RuntimeError as exc:
            log.warning("Web dorking unavailable: %s", exc)
            return None

    # -- phases -----------------------------------------------------------
    def _github_search(self, gh: GitHubClient, variants: List[str],
                       raw_owners: List[Entity], raw_repos: List[Entity]) -> None:
        if self.cfg.get("github.discover_orgs", True):
            log.info("Searching organizations (%d variants)...", len(variants))
            for v in variants:
                raw_owners.extend(gh.search_orgs(v))
        if self.cfg.get("github.discover_repos", True):
            log.info("Searching repositories (%d variants)...", len(variants))
            for v in variants:
                raw_repos.extend(gh.search_repos(v))
        if self.cfg.get("github.discover_users", True):
            log.info("Searching users (%d variants)...", len(variants))
            for v in variants:
                raw_owners.extend(gh.search_users(v))

    def _resolve_owners(self, gh: GitHubClient, raw_owners: List[Entity],
                        targets: Set[str], findings: Findings) -> None:
        fetch = self.cfg.get("github.fetch_profiles", True)
        # Cache account lookups by login to avoid duplicate API calls.
        account_cache: Dict[str, Dict] = {}
        for ent in raw_owners:
            login = ent.identifier
            acct = None
            if fetch:
                key = login.lower()
                if key not in account_cache:
                    account_cache[key] = gh.get_account(login)
                acct = account_cache[key]
            if acct:
                acc_type = (acct.get("type") or "").lower()
                if acc_type == "organization":
                    ent.kind = KIND_ORG
                elif acc_type == "user":
                    ent.kind = KIND_USER
                ent.name = ent.name or acct.get("name")
                ent.blog = ent.blog or acct.get("blog")
                ent.email = ent.email or acct.get("email")
                ent.description = ent.description or acct.get("bio")
                if acct.get("html_url"):
                    ent.url = acct["html_url"]
                if targets:
                    anchor_entity(ent, targets)
            findings.add(ent)

    def _enumerate_owner_repos(self, gh: GitHubClient, findings: Findings,
                               raw_repos: List[Entity]) -> None:
        threshold = float(self.cfg.get("github.owner_repo_min_confidence", 0.4))
        owners = [e for k in (KIND_ORG, KIND_USER) for e in findings.of_kind(k)]
        selected = [e for e in owners if e.confidence >= threshold]
        if not selected:
            return
        log.info("Enumerating public repos for %d confirmed owners...", len(selected))
        for owner in selected:
            repos = gh.list_owner_repos(owner.identifier, is_org=(owner.kind == KIND_ORG))
            raw_repos.extend(repos)

    def _process_repos(self, gh: GitHubClient, raw_repos: List[Entity],
                       targets: Set[str], findings: Findings) -> None:
        fetch_commits = self.cfg.get("github.fetch_commit_emails", False)
        sample = int(self.cfg.get("github.commit_sample", 20))
        # De-duplicate repos by identifier before the (expensive) commit sampling.
        deduped: Dict[str, Entity] = {}
        for ent in raw_repos:
            key = ent.identifier.lower()
            if key in deduped:
                deduped[key].merge(ent)
            else:
                deduped[key] = ent

        for ent in deduped.values():
            # repo homepage/blog anchoring
            if targets:
                anchor_entity(ent, targets)
            if fetch_commits and "/" in ent.identifier:
                emails = gh.commit_author_emails(ent.identifier, sample=sample)
                if emails and targets:
                    matched = anchor_entity(ent, targets, extra_values=emails)
                    if matched:
                        ent.extra["commit_email_match"] = True
            findings.add(ent)

    def _filter(self, findings: Findings) -> Findings:
        min_conf = float(self.cfg.get("scoring.min_confidence", 0.0))
        if min_conf <= 0:
            return findings
        filtered = Findings()
        for ent in findings.all():
            if ent.confidence >= min_conf:
                filtered.add(ent)
        return filtered
