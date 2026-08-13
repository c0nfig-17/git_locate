"""Discovery orchestration (phase 1: enumeration).

Ties the sources together in the required priority order — organizations first,
then repositories, then users — while keeping the web-search source optional and
the domains as the verification anchor throughout.

Discovery pipeline
------------------
1. **Seeds** — name variants (from company names *and* the domains' registrable
   labels, i.e. the domain "without TLD") plus the full domains as anchor/search
   terms.
2. **Search** — GitHub Search API by variant (orgs/repos/users) and by domain
   (repos mentioning the domain, accounts with an ``in:email`` on the domain),
   plus optional keyless web dorking.
3. **Resolve & enrich** owners (type + blog/email) and anchor them to the
   domains.
4. **Graph expansion** — bounded BFS pivots from confirmed nodes:
   org → public members, user → public orgs, (optional) repo → contributors.
5. **Owner repos** — enumerate public repos of every confirmed owner.
6. **Repo enrichment** — homepage + optional commit-email anchoring.
7. **Score** everything and filter by confidence.
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
    SOURCE_PIVOT,
)
from .scoring import Scorer
from .sources.domain_anchor import anchor_entity, domain_seed_labels, prepare_targets
from .sources.github_api import GitHubClient
from .sources.web_dork import WebDork
from .variants import generate_all

log = logging.getLogger("gitlocate.engine")


class Engine:
    def __init__(self, config):
        self.cfg = config
        self.scorer = Scorer(config)
        self._accounts: Dict[str, Dict] = {}   # login -> /users/{login} payload

    def run(self, companies: List[str], domains: List[str]) -> Findings:
        # --- Phase: seeds ------------------------------------------------
        # Domains are useful with AND without the TLD: the registrable label
        # (acme.com -> "acme") becomes an extra name seed for variant/search.
        labels = domain_seed_labels(domains)
        name_seeds = list(companies)
        for lbl in labels:
            if lbl.lower() not in {c.lower() for c in name_seeds}:
                name_seeds.append(lbl)
        variants = generate_all(name_seeds, self.cfg.section("variants"))
        variant_set: Set[str] = set(variants)
        targets = prepare_targets(domains)
        log.info("Seeds: %d name variants (incl. domain labels %s), %d domains",
                 len(variants), labels, len(targets))

        gh = self._build_github()
        if gh is None:
            log.error("No GitHub client available (missing 'requests'). Aborting.")
            return Findings()
        web = self._build_webdork()

        raw_owners: List[Entity] = []
        raw_repos: List[Entity] = []

        # --- Phase: search candidates -----------------------------------
        self._github_search(gh, variants, raw_owners, raw_repos)
        if self.cfg.get("github.search_by_domain", True) and domains:
            self._github_domain_search(gh, domains, raw_owners, raw_repos)

        if web is not None and web.available:
            log.info("Web search source active (provider=%s)",
                     self.cfg.get("web_dork.provider"))
            for ent in web.discover(companies, variants):
                (raw_repos if ent.kind == KIND_REPO else raw_owners).append(ent)
        elif self.cfg.get("web_dork.enabled", True):
            log.info("Web search source inactive; using GitHub API only.")

        # --- Phase: resolve & enrich owners -----------------------------
        findings = Findings()
        for ent in raw_owners:
            self._enrich_owner(gh, ent, targets)
            findings.add(ent)

        # Score owners so we know which are 'confirmed' before pivoting.
        self._score_owners(findings, variant_set)

        # --- Phase: graph expansion -------------------------------------
        self._expand(gh, findings, targets, variant_set)

        # --- Phase: enumerate repos of confirmed owners -----------------
        if self.cfg.get("github.enumerate_owner_repos", True):
            self._enumerate_owner_repos(gh, findings, raw_repos)

        # --- Phase: enrich & anchor repos, then aggregate ---------------
        self._process_repos(gh, raw_repos, targets, findings)

        # --- Phase: optional repo -> contributor pivot ------------------
        if self.cfg.get("expansion.repo_contributors", False):
            self._expand_contributors(gh, findings, targets, variant_set)

        # --- Final scoring + filter -------------------------------------
        self.scorer.score_all(findings, variant_set)
        result = self._filter(findings)
        log.info("Enumeration totals: %d orgs, %d repos, %d users (pre-filter %d)",
                 len(result.of_kind(KIND_ORG)), len(result.of_kind(KIND_REPO)),
                 len(result.of_kind(KIND_USER)), len(findings))
        gh.log_stats()
        return result

    # -- builders ---------------------------------------------------------
    def _build_github(self) -> Optional[GitHubClient]:
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
        try:
            return WebDork(self.cfg)
        except RuntimeError as exc:
            log.warning("Web search unavailable: %s", exc)
            return None

    # -- search phases ----------------------------------------------------
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

    def _github_domain_search(self, gh: GitHubClient, domains: List[str],
                              raw_owners: List[Entity], raw_repos: List[Entity]) -> None:
        log.info("Searching by domain (%d domains)...", len(domains))
        search_email = self.cfg.get("github.search_email", True)
        for dom in domains:
            raw_repos.extend(gh.search_repos_by_domain(dom))
            if search_email:
                raw_owners.extend(gh.search_accounts_by_email(dom, KIND_ORG))
                raw_owners.extend(gh.search_accounts_by_email(dom, KIND_USER))

    # -- enrichment -------------------------------------------------------
    def _enrich_owner(self, gh: GitHubClient, ent: Entity, targets: Set[str]) -> None:
        """Resolve an owner's true type + profile fields and anchor on domains."""
        if not self.cfg.get("github.fetch_profiles", True):
            return
        key = ent.identifier.lower()
        if key not in self._accounts:
            self._accounts[key] = gh.get_account(ent.identifier)
        acct = self._accounts[key]
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

    def _score_owners(self, findings: Findings, variant_set: Set[str]) -> None:
        for kind in (KIND_ORG, KIND_USER):
            for ent in findings.of_kind(kind):
                self.scorer.score_entity(ent, variant_set, confirmed_owners=set())

    # -- graph expansion --------------------------------------------------
    def _expand(self, gh: GitHubClient, findings: Findings, targets: Set[str],
                variant_set: Set[str]) -> None:
        exp = self.cfg.section("expansion")
        if not exp.get("enabled", True):
            return
        max_depth = int(exp.get("max_depth", 2))
        max_nodes = int(exp.get("max_nodes", 400))
        pivot_min = float(exp.get("pivot_min_confidence", 0.5))
        per_owner = int(exp.get("per_owner_limit", 100))
        do_members = exp.get("org_members", True)
        do_user_orgs = exp.get("user_orgs", True)
        if not (do_members or do_user_orgs):
            return

        visited: Set[str] = set()
        added = 0
        frontier = [e for k in (KIND_ORG, KIND_USER) for e in findings.of_kind(k)
                    if e.confidence >= pivot_min]
        log.info("Graph expansion: %d seed node(s), max_depth=%d, max_nodes=%d",
                 len(frontier), max_depth, max_nodes)

        depth = 0
        while frontier and depth < max_depth and added < max_nodes:
            newly: List[Entity] = []
            for node in frontier:
                if added >= max_nodes:
                    break
                if node.key in visited:
                    continue
                visited.add(node.key)

                if node.kind == KIND_ORG and do_members:
                    for login in gh.org_public_members(node.identifier, per_owner):
                        e = Entity(KIND_USER, login, f"https://github.com/{login}",
                                   sources={SOURCE_PIVOT})
                        e.extra["org_member"] = True
                        e.extra["via"] = f"member_of:{node.identifier}"
                        if self._register_pivot(findings, e, newly):
                            added += 1
                        if added >= max_nodes:
                            break

                elif node.kind == KIND_USER and do_user_orgs:
                    for org_login in gh.user_orgs(node.identifier, per_owner):
                        e = Entity(KIND_ORG, org_login, f"https://github.com/{org_login}",
                                   sources={SOURCE_PIVOT})
                        e.extra["related"] = True
                        e.extra["via"] = f"org_of:{node.identifier}"
                        if self._register_pivot(findings, e, newly):
                            added += 1
                        if added >= max_nodes:
                            break

            # Enrich + anchor + score the freshly discovered nodes.
            for e in newly:
                self._enrich_owner(gh, e, targets)
                self.scorer.score_entity(e, variant_set, confirmed_owners=set())
            frontier = [e for e in newly if e.confidence >= pivot_min]
            depth += 1
        if added:
            log.info("Graph expansion added %d node(s) over %d hop(s)", added, depth)

    @staticmethod
    def _register_pivot(findings: Findings, e: Entity, newly: List[Entity]) -> bool:
        """Add a pivoted entity; return True only if it was newly created."""
        result = findings.add(e)
        if result is e:
            newly.append(e)
            return True
        return False

    def _expand_contributors(self, gh: GitHubClient, findings: Findings,
                             targets: Set[str], variant_set: Set[str]) -> None:
        confirmed = self._confirmed_owner_set(findings)
        budget = int(self.cfg.get("expansion.max_nodes", 400))
        per = int(self.cfg.get("expansion.per_owner_limit", 100))
        added = 0
        newly: List[Entity] = []
        for repo in findings.of_kind(KIND_REPO):
            if added >= budget:
                break
            owner = (repo.extra.get("owner_login") or "").lower()
            if owner not in confirmed and not repo.matched_domains:
                continue
            if "/" not in repo.identifier:
                continue
            for login in gh.repo_contributors(repo.identifier, per):
                e = Entity(KIND_USER, login, f"https://github.com/{login}",
                           sources={SOURCE_PIVOT})
                e.extra["related"] = True
                e.extra["via"] = f"contributor_of:{repo.identifier}"
                if self._register_pivot(findings, e, newly):
                    added += 1
                if added >= budget:
                    break
        for e in newly:
            self._enrich_owner(gh, e, targets)
        if added:
            log.info("Contributor expansion added %d user(s)", added)

    # -- repos ------------------------------------------------------------
    def _confirmed_owner_set(self, findings: Findings) -> Set[str]:
        threshold = float(self.cfg.get("github.owner_repo_min_confidence", 0.4))
        return {e.identifier.lower()
                for k in (KIND_ORG, KIND_USER)
                for e in findings.of_kind(k)
                if e.confidence >= threshold}

    def _enumerate_owner_repos(self, gh: GitHubClient, findings: Findings,
                               raw_repos: List[Entity]) -> None:
        threshold = float(self.cfg.get("github.owner_repo_min_confidence", 0.4))
        owners = [e for k in (KIND_ORG, KIND_USER) for e in findings.of_kind(k)]
        selected = [e for e in owners if e.confidence >= threshold]
        if not selected:
            return
        log.info("Enumerating public repos for %d confirmed owner(s)...", len(selected))
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
            if targets:
                anchor_entity(ent, targets)   # repo homepage/blog anchoring
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
