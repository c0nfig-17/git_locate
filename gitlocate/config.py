"""Configuration loading.

Configuration comes from three layers, later ones overriding earlier ones:

1. Built-in defaults (:data:`DEFAULTS`) — safe, non-secret, work out of the box.
2. A YAML config file (``--config``, ``$GITLOCATE_CONFIG`` or ``config.yaml``).
3. A handful of environment variables for the values that must never live in a
   file (see :func:`apply_env_overrides`).

Secrets (GitHub token, Serper key, Notify provider config) are NEVER stored in
the config file. The file only names the *environment variable* that holds each
secret, and the actual value is read from the environment at runtime.
"""
from __future__ import annotations

import copy
import logging
import os
from typing import Any, Dict, List, Optional

from . import yamlcheck

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - dependency missing
    yaml = None

log = logging.getLogger("gitlocate.config")


DEFAULTS: Dict[str, Any] = {
    # Targets can be supplied on the CLI, from files, OR right here in config.
    "targets": {
        "companies": [],
        "domains": [],
        "companies_file": "",
        "domains_file": "",
    },
    "github": {
        "token_env": "GITHUB_TOKEN",
        "api_url": "https://api.github.com",
        "per_page": 100,
        "max_pages": 3,
        "connect_timeout": 10,           # seconds to establish a connection
        "read_timeout": 30,              # seconds to wait for a response
        "enumerate_owner_repos": True,   # list repos of confirmed orgs/users
        "owner_repo_min_confidence": 0.4,
        "fetch_profiles": True,          # pull blog/email for domain anchoring
        "fetch_commit_emails": False,    # expensive; off by default
        "commit_sample": 20,
        "search_by_domain": True,        # search repos/accounts by the domain
        "search_email": True,            # search accounts with in:email <domain>
        "search_in": {
            "orgs": ["login", "name", "email"],
            "repos": ["name", "description", "readme"],
            "users": ["login", "name", "email"],
        },
        # Which phases run. Priority order is always orgs -> repos -> users.
        "discover_orgs": True,
        "discover_repos": True,
        "discover_users": True,
    },
    # Graph expansion: pivot from confirmed nodes to discover related ones.
    "expansion": {
        "enabled": True,
        "max_depth": 2,                  # BFS hops from the seed nodes
        "max_nodes": 400,                # global cap on pivoted nodes
        "pivot_min_confidence": 0.3,     # pivot from nodes at/above this
                                         # (0.3 = an exact name match qualifies)
        "org_members": True,             # org  -> public members (users)
        "user_orgs": True,               # user -> public orgs
        "repo_contributors": False,      # repo -> contributors (noisy; opt-in)
        "per_owner_limit": 100,          # cap members/orgs fetched per node
    },
    "web_dork": {
        # Keyless by default: DuckDuckGo needs no API key. 'serper' is optional.
        "enabled": True,
        "provider": "duckduckgo",        # duckduckgo | serper
        "duckduckgo_endpoint": "https://html.duckduckgo.com/html/",
        "api_key_env": "SERPER_API_KEY",     # used only by the serper provider
        "endpoint": "https://google.serper.dev/search",
        "num": 20,
        "use_variants": False,           # query human names only (low volume)
        "max_queries": 12,               # hard cap on web requests per run
        "delay_seconds": 1.0,            # politeness delay between web requests
        "connect_timeout": 10,
        "read_timeout": 30,
        "queries": [
            "site:github.com {name}",
            '"{name}" github',
        ],
    },
    "variants": {
        "separators": ["", " ", "-", "_"],
        "suffixes": ["inc", "labs", "io", "hq", "dev", "corp", "tech", "sec"],
        "include_word_combos": True,
        "min_word_len": 2,
        "max_variants": 60,
        "max_total_variants": 120,
    },
    "scoring": {
        "weights": {
            "domain_anchor": 0.5,
            "exact_name_match": 0.3,
            "description_match": 0.15,
            "multi_source": 0.2,
            "web_dork": 0.15,
            "owner_confirmed": 0.25,
            "commit_email_domain": 0.3,
            "domain_search": 0.15,
            "org_member": 0.2,
            "related": 0.1,
        },
        "min_confidence": 0.0,
    },
    "rate_limit": {
        "auto_wait": True,
        "proactive": True,               # wait BEFORE a request when bucket empty
        "max_wait_seconds": 900,
        "retry_max": 5,
        "backoff_base": 2.0,
    },
    "output": {
        "dir": "./output",
        "json_file": "results.json",
        "repos_flat_file": "repos.txt",
        "notify_file": "notify.txt",
        "pretty": True,
    },
    "chaining": {
        # Phase 2 runs by default: enumerate (phase 1) -> scan every discovered
        # repo with the tools below -> notify. Disable with --no-chain or by
        # setting chaining.enabled: false.
        "enabled": True,
        "workdir": "./output/chaining",
        # Clone each repo once so tools that need a working tree (gitleaks,
        # credsweeper) can point at {clone_dir}. trufflehog scans the URL and
        # does not need this.
        "clone": True,
        "timeout_seconds": 1800,
        "min_confidence": 0.0,
        # Any command whose leading tool is not installed is SKIPPED with an
        # install hint (not counted as a failure), so a partial toolbox still
        # produces results instead of a wall of errors.
        "commands": [
            # ================= BLOQUE PARA PEGAR HERRAMIENTAS =================
            # One shell command per discovered repo. Available placeholders:
            #   {repo_url}    https clone URL of the repo
            #   {repo}        owner/name
            #   {owner}       owner login
            #   {name}        repo name
            #   {clone_dir}   local clone path (only if chaining.clone: true)
            #   {output_dir}  chaining.workdir
            #
            # These defaults work out of the box after ./install.sh. Add your own
            # tools here. Use the wrappers install.sh puts on PATH (gitdorker,
            # git-wild-hunt, credsweeper) — they run each tool from its own venv.
            # Calling 'python3 /opt/GitDorker/GitDorker.py' directly uses the
            # SYSTEM python, which lacks the tool's deps ('No module named
            # termcolor'). trufflehog needs --no-update so it does not try (and
            # fail) to self-update its root-owned binary mid-run.
            "trufflehog git {repo_url} --no-update --json >> {output_dir}/trufflehog.jsonl",
            "gitleaks detect --source {clone_dir} --report-format json --report-path {output_dir}/{owner}__{name}.gitleaks.json",
            # More scanners you can enable (installed by ./install.sh):
            #   "credsweeper --path {clone_dir} --save-json {output_dir}/{owner}__{name}.credsweeper.json"
            #   "gitdorker -tf /opt/GitDorker/token.txt -q {name} -d /opt/GitDorker/Dorks/medium_dorks.txt"
            #   "github-subdomains -d {name} -t $GITHUB_TOKEN -o {output_dir}/{owner}__{name}.subdomains.txt"
            # =================================================================
        ],
    },
    "notify": {
        "enabled": False,
        "binary": "notify",
        "provider_config_env": "NOTIFY_PROVIDER_CONFIG",
        "provider_config": "",       # optional explicit path; env var wins
        "provider_id": "",           # notify -id value (e.g. "recon")
        "bulk": True,
        "payload": "phase_transition",  # phase_transition | summary | repos | json
        # Fire a Notify message when enumeration (phase 1) completes and the
        # leak-discovery stage (phase 2) is about to begin.
        "on_phase_transition": True,
    },
}


class Config:
    """Thin wrapper over the merged config dict with dotted-path access."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> Dict[str, Any]:
        val = self.data.get(name, {})
        return val if isinstance(val, dict) else {}

    # -- secret resolution ------------------------------------------------
    def resolve_secret(self, env_var_name: Optional[str]) -> Optional[str]:
        """Read a secret from the environment variable named in config."""
        if not env_var_name:
            return None
        val = os.environ.get(env_var_name)
        return val.strip() if val else None

    @property
    def github_token(self) -> Optional[str]:
        return self.resolve_secret(self.get("github.token_env"))

    @property
    def serper_api_key(self) -> Optional[str]:
        return self.resolve_secret(self.get("web_dork.api_key_env"))


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _default_config_path() -> Optional[str]:
    env_path = os.environ.get("GITLOCATE_CONFIG")
    if env_path:
        return env_path
    for candidate in ("config.yaml", "config.yml"):
        if os.path.isfile(candidate):
            return candidate
    return None


def apply_env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the small set of non-secret env overrides.

    Secrets are handled by :meth:`Config.resolve_secret` at read time, not here.
    These overrides let CI / one-off runs tweak behaviour without a config file.
    """
    def _boolenv(name: str) -> Optional[bool]:
        raw = os.environ.get(name)
        if raw is None:
            return None
        return raw.strip().lower() in ("1", "true", "yes", "on")

    out = copy.deepcopy(data)

    out_dir = os.environ.get("GITLOCATE_OUTPUT_DIR")
    if out_dir:
        out.setdefault("output", {})["dir"] = out_dir

    web_enabled = _boolenv("GITLOCATE_WEB_DORK")
    if web_enabled is not None:
        out.setdefault("web_dork", {})["enabled"] = web_enabled

    notify_enabled = _boolenv("GITLOCATE_NOTIFY")
    if notify_enabled is not None:
        out.setdefault("notify", {})["enabled"] = notify_enabled

    chaining_enabled = _boolenv("GITLOCATE_CHAINING")
    if chaining_enabled is not None:
        out.setdefault("chaining", {})["enabled"] = chaining_enabled

    return out


def load_config(path: Optional[str] = None) -> Config:
    """Load config: defaults <- file <- env overrides."""
    data = copy.deepcopy(DEFAULTS)

    cfg_path = path or _default_config_path()
    if cfg_path:
        if yaml is None:
            raise RuntimeError(
                "A config file was provided but PyYAML is not installed. "
                "Install it with `pip install pyyaml`."
            )
        with open(cfg_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        try:
            file_data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            # ValueError so the CLI reports it as a config problem (exit 2)
            # instead of dumping a PyYAML traceback at the operator.
            raise ValueError(yamlcheck.format_yaml_error(cfg_path, exc)) from exc
        if not isinstance(file_data, dict):
            raise ValueError(f"Config file {cfg_path} must contain a YAML mapping")
        # PyYAML keeps the last of two identical keys without a word, which
        # silently discards a whole block (e.g. two 'chaining:' sections).
        for dup in yamlcheck.find_duplicate_keys(text):
            where = f"{dup.path}.{dup.key}" if dup.path else dup.key
            log.warning("%s line %d: '%s' is already defined at line %d — the "
                        "earlier block is ignored; merge them.",
                        cfg_path, dup.line, where, dup.first_line)
        data = _deep_merge(data, file_data)

    data = apply_env_overrides(data)
    return Config(data)
