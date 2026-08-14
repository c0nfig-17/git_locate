"""Tests for engine repo post-processing (fork filtering)."""
from gitlocate.config import load_config
from gitlocate.engine import Engine
from gitlocate.models import Entity, Findings, KIND_REPO


def _repo(full, fork=False, domains=None):
    e = Entity(KIND_REPO, full, f"https://github.com/{full}")
    e.extra["owner_login"] = full.split("/")[0]
    e.extra["fork"] = fork
    if domains:
        e.matched_domains = set(domains)
    return e


def test_forks_dropped_unless_domain_anchored():
    cfg = load_config(None)  # include_forks defaults to False
    eng = Engine(cfg)
    findings = Findings()
    raw = [
        _repo("acme/original", fork=False),
        _repo("acme/copy", fork=True),                       # dropped
        _repo("acme/vendored", fork=True, domains={"acme.com"}),  # kept (anchored)
    ]
    # targets empty + fetch_commit_emails False -> gh is never touched
    eng._process_repos(gh=None, raw_repos=raw, targets=set(), findings=findings)
    ids = {e.identifier for e in findings.of_kind(KIND_REPO)}
    assert ids == {"acme/original", "acme/vendored"}


def test_forks_kept_when_include_forks_true():
    cfg = load_config(None)
    cfg.data["github"]["include_forks"] = True
    eng = Engine(cfg)
    findings = Findings()
    raw = [_repo("acme/original", fork=False), _repo("acme/copy", fork=True)]
    eng._process_repos(gh=None, raw_repos=raw, targets=set(), findings=findings)
    ids = {e.identifier for e in findings.of_kind(KIND_REPO)}
    assert ids == {"acme/original", "acme/copy"}


def test_duplicate_repos_are_merged():
    cfg = load_config(None)
    eng = Engine(cfg)
    findings = Findings()
    raw = [_repo("acme/x"), _repo("acme/x")]
    eng._process_repos(gh=None, raw_repos=raw, targets=set(), findings=findings)
    assert len(findings.of_kind(KIND_REPO)) == 1
