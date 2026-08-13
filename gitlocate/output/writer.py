"""Serialize findings to disk.

Three artifacts are produced, all designed to be consumed by later tools with
zero manual editing:

- ``results.json``  the full structured record (orgs / repos / users + summary)
- ``repos.txt``     a flat, one-URL-per-line list of discovered repos, ready to
                    pipe into cloners, scanners, or ``notify``
- ``notify.txt``    a plain-text, line-oriented payload compatible with
                    ProjectDiscovery Notify (``notify -bulk``)

The JSON shape is intentionally flat and stable so downstream automation can
rely on it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List

from .. import __tool_name__, __version__
from ..models import Findings, KIND_ORG, KIND_REPO, KIND_USER


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_document(findings: Findings, companies: List[str], domains: List[str]) -> Dict:
    orgs = findings.of_kind(KIND_ORG)
    repos = findings.of_kind(KIND_REPO)
    users = findings.of_kind(KIND_USER)
    return {
        "tool": __tool_name__,
        "version": __version__,
        "generated_at": _now_iso(),
        "targets": {"companies": companies, "domains": domains},
        "summary": {
            "organizations": len(orgs),
            "repositories": len(repos),
            "users": len(users),
        },
        "organizations": [e.to_dict() for e in orgs],
        "repositories": [e.to_dict() for e in repos],
        "users": [e.to_dict() for e in users],
        # Flat list ready to chain into other tooling.
        "repo_urls": [e.url for e in repos if e.url],
    }


def repo_urls(findings: Findings) -> List[str]:
    return [e.url for e in findings.of_kind(KIND_REPO) if e.url]


def write_json(document: Dict, path: str, pretty: bool = True) -> None:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as fh:
        if pretty:
            json.dump(document, fh, indent=2, ensure_ascii=False)
        else:
            json.dump(document, fh, ensure_ascii=False)
        fh.write("\n")


def write_repos_flat(findings: Findings, path: str) -> None:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as fh:
        for url in repo_urls(findings):
            fh.write(url + "\n")


def build_notify_text(document: Dict, payload: str = "summary") -> str:
    """Build a line-oriented, Notify-compatible message.

    Notify sends each stdin line (or bulk block) to the configured webhooks, so
    we keep it compact and human-readable.
    """
    if payload == "repos":
        return "\n".join(document.get("repo_urls", []))

    if payload == "json":
        return json.dumps(document, ensure_ascii=False)

    # default: summary
    tgt = document.get("targets", {})
    summary = document.get("summary", {})
    lines: List[str] = []
    lines.append(f"[{document['tool']}] GitHub recon discovery")
    companies = ", ".join(tgt.get("companies", [])) or "-"
    domains = ", ".join(tgt.get("domains", [])) or "-"
    lines.append(f"targets: {companies} | domains: {domains}")
    lines.append(
        "found: {o} orgs, {r} repos, {u} users".format(
            o=summary.get("organizations", 0),
            r=summary.get("repositories", 0),
            u=summary.get("users", 0),
        )
    )

    def _top(kind_list, label, n=5):
        top = kind_list[:n]
        if not top:
            return
        lines.append(f"top {label}:")
        for e in top:
            lines.append(f"  - {e['id']} ({e['confidence']:.2f}) {e['url']}")

    _top(document.get("organizations", []), "orgs")
    _top(document.get("repositories", []), "repos")
    _top(document.get("users", []), "users")
    return "\n".join(lines)


def write_notify_text(text: str, path: str) -> None:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip("\n") + "\n")


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
