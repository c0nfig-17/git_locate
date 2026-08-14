"""Tool chaining.

After discovery, git_locate can invoke a configurable list of external tools on
every discovered repository. The command list lives entirely in the config file
(the ``chaining.commands`` block, the "BLOQUE PARA PEGAR HERRAMIENTAS") so the
operator wires in their own tooling — gitleaks, trufflehog, credsweeper,
github-subdomains, GitDorker, git-wild-hunt, etc. — without touching the code.

Each command is a shell string expanded per repo with these placeholders:

    {repo_url}    https clone URL of the repo
    {repo}        owner/name
    {owner}       owner login
    {name}        repo name
    {clone_dir}   local clone path (only meaningful when chaining.clone: true)
    {output_dir}  chaining.workdir

Optionally the repo is cloned once (``chaining.clone: true``) so tools that need
a working tree (gitleaks, credsweeper, git-wild-hunt) can point at ``{clone_dir}``.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from typing import Dict, List, Optional

from .models import Entity, Findings, KIND_REPO

log = logging.getLogger("gitlocate.chaining")

#: How to (re)install each tool we ship a default command for, shown when a
#: command is skipped because its tool is not on PATH.
_INSTALL_HINTS = {
    "trufflehog": "./install.sh trufflehog",
    "gitleaks": "./install.sh gitleaks",
    "credsweeper": "./install.sh credsweeper",
    "gitdorker": "./install.sh gitdorker",
    "git-wild-hunt": "./install.sh git-wild-hunt",
    "github-subdomains": "./install.sh github-subdomains",
    "notify": "./install.sh notify",
}


def _leading_executable(cmd: str) -> Optional[str]:
    """Return the program a shell command would run, or None if undeterminable.

    Skips leading ``NAME=value`` environment assignments so ``FOO=bar tool ...``
    resolves to ``tool``. Returns None when the command cannot be tokenized
    (unbalanced quotes, etc.) so we let the shell run it rather than guessing.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    for tok in tokens:
        # env assignment prefix: NAME=value (name is a valid identifier)
        eq = tok.find("=")
        if eq > 0 and tok[:eq].replace("_", "a").isalnum() and tok[0].isalpha():
            continue
        return tok
    return None


def _placeholders(entity: Entity, clone_dir: str, output_dir: str) -> Dict[str, str]:
    full = entity.identifier
    owner = entity.extra.get("owner_login") or (full.split("/")[0] if "/" in full else "")
    name = full.split("/")[-1]
    repo_url = entity.url or f"https://github.com/{full}"
    clone_url = entity.extra.get("clone_url") or (repo_url.rstrip("/") + ".git")
    return {
        "repo_url": clone_url or repo_url,
        "repo": full,
        "owner": owner,
        "name": name,
        "clone_dir": clone_dir,
        "output_dir": output_dir,
    }


def _safe_dirname(full_name: str) -> str:
    return full_name.replace("/", "__").replace("..", "_")


class Chainer:
    def __init__(self, config, dry_run: bool = False):
        self.cfg = config
        self.dry_run = dry_run
        self.commands: List[str] = list(config.get("chaining.commands", []) or [])
        self.workdir = os.path.abspath(config.get("chaining.workdir", "./output/chaining"))
        self.clone = bool(config.get("chaining.clone", False))
        self.timeout = int(config.get("chaining.timeout_seconds", 1800))
        self.min_conf = float(config.get("chaining.min_confidence", 0.0))
        # Cache which(binary) results and remember which tools we had to skip so
        # we can print one actionable summary at the end instead of a wall of
        # "command not found" failures.
        self._avail_cache: Dict[str, bool] = {}
        self._missing_tools: Dict[str, int] = {}

    def _tool_available(self, binary: Optional[str]) -> bool:
        """True if the command's program can be run. Unknown/unparseable -> True
        (let the shell try) so we never wrongly block a valid command."""
        if not binary:
            return True
        if binary in self._avail_cache:
            return self._avail_cache[binary]
        if os.sep in binary or (os.altsep and os.altsep in binary):
            ok = os.path.isfile(binary) and os.access(binary, os.X_OK)
        else:
            ok = shutil.which(binary) is not None
        self._avail_cache[binary] = ok
        return ok

    def run(self, findings: Findings) -> Dict[str, int]:
        stats = {"repos": 0, "commands_run": 0, "failures": 0, "skipped": 0}
        if not self.commands:
            log.info("Chaining enabled but no commands configured; nothing to run.")
            return stats

        # Commands that interpolate {clone_dir} need a working tree. Warn once,
        # up front, if clone is off — otherwise each would run with an empty
        # path (e.g. 'gitleaks detect --source ') and fail cryptically.
        needs_clone = [c for c in self.commands if "{clone_dir}" in c]
        if needs_clone and not self.clone:
            log.warning("%d chaining command(s) use {clone_dir} but "
                        "chaining.clone is false — they will be SKIPPED. Set "
                        "chaining.clone: true to clone each repo first.",
                        len(needs_clone))

        os.makedirs(self.workdir, exist_ok=True)
        all_repos = findings.of_kind(KIND_REPO)
        repos = [e for e in all_repos if e.confidence >= self.min_conf]
        if not repos:
            if not all_repos:
                log.info("No repositories discovered in phase 1; nothing to "
                         "chain over. Check the target (a real company name, "
                         "ideally with -d <domain>).")
            else:
                log.info("None of the %d discovered repo(s) scored >= %.2f "
                         "(chaining.min_confidence); nothing to chain. Lower the "
                         "threshold to include them.", len(all_repos), self.min_conf)
            return stats
        for entity in repos:
            stats["repos"] += 1
            clone_dir = ""
            if self.clone:
                clone_dir = self._clone_repo(entity)
                if clone_dir is None:
                    stats["failures"] += 1
                    clone_dir = ""
            ph = _placeholders(entity, clone_dir, self.workdir)
            for template in self.commands:
                # Never run a {clone_dir} command without a clone: an empty
                # --source/--path either errors or silently scans the cwd.
                if "{clone_dir}" in template and not clone_dir:
                    stats["skipped"] += 1
                    continue
                cmd = template
                for key, val in ph.items():
                    cmd = cmd.replace("{" + key + "}", val)
                # Skip (don't fail) a command whose tool isn't installed — a
                # missing scanner shouldn't look like the run is broken. In
                # dry-run we still print it, so the preview shows every command.
                if not self.dry_run:
                    binary = _leading_executable(cmd)
                    if not self._tool_available(binary):
                        self._missing_tools[binary] = \
                            self._missing_tools.get(binary, 0) + 1
                        stats["skipped"] += 1
                        continue
                ok = self._run_command(cmd)
                if ok is None:
                    stats["skipped"] += 1
                elif ok:
                    stats["commands_run"] += 1
                else:
                    stats["failures"] += 1
        self._report_missing_tools()
        return stats

    def _report_missing_tools(self) -> None:
        """One actionable summary for tools that were never on PATH."""
        for tool, count in self._missing_tools.items():
            hint = _INSTALL_HINTS.get(tool, "install it and re-run "
                                      "(see ./install.sh --help)")
            log.warning("Skipped %d command(s): '%s' is not installed. "
                        "Install it with: %s", count, tool, hint)

    def _clone_repo(self, entity: Entity) -> Optional[str]:
        target = os.path.join(self.workdir, _safe_dirname(entity.identifier))
        clone_url = entity.extra.get("clone_url") or (entity.url.rstrip("/") + ".git")
        if os.path.isdir(os.path.join(target, ".git")):
            return target
        cmd = ["git", "clone", "--depth", "1", clone_url, target]
        if self.dry_run:
            log.info("[dry-run] %s", " ".join(cmd))
            return target
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=self.timeout)
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("Clone failed for %s: %s", entity.identifier, exc)
            return None
        if proc.returncode != 0:
            log.warning("Clone failed for %s: %s", entity.identifier,
                        (proc.stdout or b"").decode("utf-8", "replace").strip())
            return None
        return target

    def _run_command(self, cmd: str) -> Optional[bool]:
        cmd = cmd.strip()
        if not cmd or cmd.startswith("#"):
            return None
        if self.dry_run:
            log.info("[dry-run] %s", cmd)
            return True
        log.info("chaining: %s", cmd)
        started = time.monotonic()
        try:
            # shell=True so operators can use pipes/redirects in their commands.
            proc = subprocess.run(cmd, shell=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            log.warning("Command timed out after %ss: %s", self.timeout, cmd)
            return False
        except OSError as exc:
            log.warning("Command failed to start: %s (%s)", cmd, exc)
            return False
        elapsed = time.monotonic() - started
        log.debug("chaining command exited %s in %.1fs: %s",
                  proc.returncode, elapsed, cmd)
        if proc.returncode != 0:
            # The command RAN (it wasn't a failure to launch); a non-zero exit
            # is frequently how a scanner signals it found something — gitleaks
            # exits 1 on leaks, trufflehog 183 with --fail — so we surface the
            # code but do not treat the whole run as failed.
            log.info("Command finished with exit %s in %.1fs (non-zero often "
                     "means findings, not an error): %s",
                     proc.returncode, elapsed, cmd)
        return True


def validate_shell(cmd: str) -> List[str]:
    """Best-effort tokenization to surface obviously malformed commands."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return []
