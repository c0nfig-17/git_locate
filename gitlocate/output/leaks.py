"""Per-leak notifications (phase 3).

Phase 2 (:mod:`gitlocate.chaining`) runs the operator's scanners and leaves
their output on disk. This module turns that output into *one Notify message per
leak*, each carrying the leak's location (repo, file, line, commit, detector),
so an operator gets an actionable alert instead of only the phase-transition
summary.

Two formats are understood out of the box, matching the default chaining
commands in ``config.yaml.example``:

* **trufflehog** ``--json`` — one JSON object per line, appended to a single
  ``trufflehog.jsonl`` for every repo. Only *verified* findings are notified by
  default (``notify.per_leak_verified_only``); the repo comes from each finding's
  own ``SourceMetadata``.
* **gitleaks** ``--report-format json`` — a JSON array written per repo to
  ``{owner}__{name}.gitleaks.json``. Every finding is notified (gitleaks does no
  live verification); the repo is recovered from the file name.

Because trufflehog *appends* across repos and runs, a persistent seen-set
(``.gitlocate_notified.json`` in the chaining workdir) records the fingerprint of
every leak already alerted on, so re-running only notifies genuinely new leaks
and a mid-run sweep never re-sends earlier repos' findings.
"""
from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import notify as notify_mod

log = logging.getLogger("gitlocate.leaks")

#: File name (inside the chaining workdir) the default trufflehog command writes.
TRUFFLEHOG_FILE = "trufflehog.jsonl"
#: Glob (inside the chaining workdir) the default gitleaks command writes to.
GITLEAKS_GLOB = "*.gitleaks.json"
#: Where the seen-set is persisted so we never alert on the same leak twice.
STATE_FILE = ".gitlocate_notified.json"


@dataclass
class Leak:
    """A single normalized secret finding with its location."""

    tool: str                       # "trufflehog" | "gitleaks"
    rule: str                       # detector / rule name
    repo: str = ""                  # owner/name (best effort)
    file: str = ""
    line: Optional[int] = None
    commit: str = ""
    verified: bool = False
    redacted: str = ""              # a safe-to-show snippet, never the raw secret
    extra: Dict[str, str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Stable identity used to de-duplicate across sweeps and re-runs."""
        native = self.extra.get("fingerprint")
        if native:
            return f"{self.tool}|{native}"
        line = "" if self.line is None else str(self.line)
        return "|".join([self.tool, self.repo, self.file, line, self.rule,
                         self.redacted])

    def format_message(self) -> str:
        """Plain-text, Discord/Slack-friendly single-leak alert."""
        status = " (VERIFIED)" if self.verified else ""
        header = f"\U0001F511 {self.tool} — {self.rule or 'secret'}{status}"
        loc = self.file + (f":{self.line}" if self.line is not None else "")
        lines = [header]
        if self.repo:
            lines.append(f"repo:   {self.repo}")
        if loc:
            lines.append(f"file:   {loc}")
        count = self.extra.get("locations")
        if isinstance(count, int) and count > 1:
            lines.append(f"seen:   {count} locations (first shown)")
        if self.commit:
            lines.append(f"commit: {self.commit}")
        if self.redacted:
            lines.append(f"match:  {self.redacted}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def _git_meta(finding: dict) -> dict:
    """Pull the location block out of a trufflehog SourceMetadata, whatever the
    source type is named (Git, Github, Filesystem, ...)."""
    data = (((finding.get("SourceMetadata") or {}).get("Data")) or {})
    if not isinstance(data, dict):
        return {}
    if "Git" in data and isinstance(data["Git"], dict):
        return data["Git"]
    # Fall back to the first mapping value (Github/Gitlab/Filesystem/...).
    for val in data.values():
        if isinstance(val, dict):
            return val
    return {}


def _repo_from_url(url: str) -> str:
    """https://github.com/owner/name(.git) -> owner/name."""
    if not url:
        return ""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    parts = [p for p in u.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return u


def parse_trufflehog_jsonl(path: str, verified_only: bool = True) -> List[Leak]:
    """Parse a trufflehog ``--json`` output file into :class:`Leak` records."""
    leaks: List[Leak] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or raw[0] != "{":
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # A finding always carries SourceMetadata + a detector; skip any
                # stray log/status line that lacks them.
                if not isinstance(obj, dict) or "SourceMetadata" not in obj:
                    continue
                verified = bool(obj.get("Verified"))
                if verified_only and not verified:
                    continue
                meta = _git_meta(obj)
                line_no = meta.get("line")
                leaks.append(Leak(
                    tool="trufflehog",
                    rule=str(obj.get("DetectorName") or "secret"),
                    repo=_repo_from_url(str(meta.get("repository") or "")),
                    file=str(meta.get("file") or ""),
                    line=int(line_no) if isinstance(line_no, int) else None,
                    commit=str(meta.get("commit") or "")[:12],
                    verified=verified,
                    redacted=str(obj.get("Redacted") or "")[:200],
                ))
    except OSError:
        return []
    return leaks


def parse_gitleaks_json(path: str, repo_hint: str = "") -> List[Leak]:
    """Parse a gitleaks ``--report-format json`` file into :class:`Leak`
    records. gitleaks reports carry no repo, so ``repo_hint`` (derived from the
    file name) supplies it."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    leaks: List[Leak] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        start = item.get("StartLine")
        leaks.append(Leak(
            tool="gitleaks",
            rule=str(item.get("RuleID") or item.get("Description") or "secret"),
            repo=repo_hint,
            file=str(item.get("File") or ""),
            line=int(start) if isinstance(start, int) else None,
            commit=str(item.get("Commit") or "")[:12],
            verified=False,
            redacted=str(item.get("Match") or item.get("Description") or "")[:200],
            extra={"fingerprint": str(item.get("Fingerprint") or "")}
            if item.get("Fingerprint") else {},
        ))
    return leaks


def _repo_from_gitleaks_filename(path: str) -> str:
    """``owner__name.gitleaks.json`` -> ``owner/name`` (matches the default
    chaining command's ``{owner}__{name}.gitleaks.json``)."""
    base = os.path.basename(path)
    if base.endswith(".gitleaks.json"):
        base = base[: -len(".gitleaks.json")]
    return base.replace("__", "/", 1)


def collect_leaks(workdir: str, verified_only: bool = True,
                  include_gitleaks: bool = True) -> List[Leak]:
    """Parse every known scanner output under ``workdir`` into leaks."""
    leaks: List[Leak] = []
    th = os.path.join(workdir, TRUFFLEHOG_FILE)
    if os.path.isfile(th):
        leaks.extend(parse_trufflehog_jsonl(th, verified_only=verified_only))
    if include_gitleaks:
        for gl in sorted(glob.glob(os.path.join(workdir, GITLEAKS_GLOB))):
            leaks.extend(parse_gitleaks_json(gl, _repo_from_gitleaks_filename(gl)))
    return leaks


def collapse_duplicates(leaks: List[Leak]) -> List[Leak]:
    """Merge leaks that are the same secret seen in several places.

    gitleaks (and trufflehog on copied files/fixtures) reports the identical
    match once per file — e.g. the same placeholder across ``fixtures/v1..v4``.
    Grouping by ``(tool, repo, rule, redacted)`` collapses those into one alert
    that keeps the first location and a count of how many were found. The
    representative's fingerprint is made content-based so the seen-set de-dupes
    the whole group across runs regardless of which file it first appeared in.
    """
    groups: Dict[tuple, Leak] = {}
    order: List[tuple] = []
    for leak in leaks:
        key = (leak.tool, leak.repo, leak.rule, leak.redacted)
        rep = groups.get(key)
        if rep is None:
            leak.extra = dict(leak.extra)
            leak.extra["locations"] = 1
            leak.extra["fingerprint"] = "collapsed|" + "|".join(
                [leak.tool, leak.repo, leak.rule, leak.redacted])
            groups[key] = leak
            order.append(key)
        else:
            rep.extra["locations"] += 1
    return [groups[k] for k in order]


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------
class LeakNotifier:
    """Sends one Notify message per *new* leak, remembering what it already sent.

    ``sweep()`` re-reads the workdir and notifies only fingerprints not seen
    before, so it is safe to call after every repo (streaming alerts) and again
    at the end of the run.
    """

    def __init__(self, config, workdir: str, dry_run: bool = False):
        self.cfg = config
        self.workdir = workdir
        self.dry_run = dry_run
        self.verified_only = bool(config.get("notify.per_leak_verified_only", True))
        self.include_gitleaks = bool(
            config.get("notify.per_leak_include_gitleaks", True))
        self.collapse = bool(
            config.get("notify.per_leak_collapse_duplicates", True))
        self.max_messages = int(config.get("notify.per_leak_max_messages", 200))
        self._state_path = os.path.join(workdir, STATE_FILE)
        self._seen = self._load_seen()
        self._sent = 0
        self._capped = False

    def _load_seen(self) -> set:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return set(str(x) for x in data)
        except (OSError, json.JSONDecodeError):
            pass
        return set()

    def _save_seen(self) -> None:
        if self.dry_run:
            # A preview must not mark leaks as sent, or the real run would skip
            # them. Keep the in-memory set (so one dry-run doesn't repeat itself)
            # but never touch the persisted state.
            return
        try:
            os.makedirs(self.workdir, exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as fh:
                json.dump(sorted(self._seen), fh)
        except OSError as exc:
            log.debug("Could not persist notified-leak state: %s", exc)

    def sweep(self) -> int:
        """Notify any leaks not seen before. Returns how many were sent."""
        if self.max_messages and self._sent >= self.max_messages:
            return 0
        leaks = collect_leaks(self.workdir, verified_only=self.verified_only,
                              include_gitleaks=self.include_gitleaks)
        if self.collapse:
            leaks = collapse_duplicates(leaks)
        new = 0
        for leak in leaks:
            fp = leak.fingerprint()
            if fp in self._seen:
                continue
            self._seen.add(fp)
            if self.max_messages and self._sent >= self.max_messages:
                if not self._capped:
                    log.warning("per-leak Notify cap reached (%d messages); "
                                "remaining leaks are on disk but not sent. Raise "
                                "notify.per_leak_max_messages (0 = unlimited).",
                                self.max_messages)
                    self._capped = True
                continue
            if self._send(leak):
                self._sent += 1
                new += 1
        if new:
            self._save_seen()
        return new

    def _send(self, leak: Leak) -> bool:
        msg = leak.format_message()
        if self.dry_run:
            log.info("[dry-run] would Notify leak:\n%s", msg)
            return True
        if notify_mod.send(self.cfg, msg, quiet=True):
            log.info("Notified leak: %s %s %s", leak.tool, leak.repo,
                     leak.file + (f":{leak.line}" if leak.line is not None else ""))
            return True
        log.warning("Notify send failed for a leak (%s %s); see warnings above.",
                    leak.tool, leak.repo)
        return False


def enabled(config) -> bool:
    """True if per-leak notification is turned on in config."""
    return bool(config.get("notify.per_leak", False))
