"""ProjectDiscovery Notify integration.

Notify (https://github.com/projectdiscovery/notify) fans a text payload out to
webhooks (Slack, Discord, Telegram, generic webhook, ...). git_locate is built
to be Notify-compatible from the start: the payload is plain, line-oriented text
(see :func:`gitlocate.output.writer.build_notify_text`), which is exactly what
``notify -bulk`` consumes on stdin.

Secrets and provider webhooks stay in Notify's own provider-config YAML — this
module only points Notify at that file (path taken from an env var or config)
and pipes the payload in. Nothing about the destinations is hardcoded here.

Because that file is hand-written, it is also the most common thing to get
wrong. The classic mistake is repeating a provider key (``custom:`` twice)
instead of adding a second entry to its list: YAML forbids duplicate keys in the
same mapping, so Notify aborts with a bare ``could not parse provider config
file``. This module checks the file before invoking Notify and reports what to
merge (see :func:`check_provider_config`).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import List, NamedTuple, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - dependency missing
    yaml = None

log = logging.getLogger("gitlocate.notify")

#: Where Notify looks when it is not given ``-provider-config``. Validating this
#: path too means the duplicate-key check still fires for the common setup where
#: nothing is configured on the git_locate side.
DEFAULT_PROVIDER_CONFIG = "~/.config/notify/provider-config.yaml"


class DuplicateKey(NamedTuple):
    """A key defined more than once inside the same YAML mapping."""

    key: str
    path: str          # dotted path of the parent mapping ("" = document root)
    line: int          # 1-based line of the repeat
    first_line: int    # 1-based line of the first definition


def _resolve_provider_config(config) -> Optional[str]:
    env_name = config.get("notify.provider_config_env")
    if env_name:
        val = os.environ.get(env_name)
        if val:
            return os.path.expanduser(val.strip())
    explicit = config.get("notify.provider_config")
    if explicit:
        return os.path.expanduser(explicit)
    return None


def resolve_provider_config(config) -> Tuple[Optional[str], bool]:
    """Return ``(path, explicit)`` for the provider config Notify will read.

    ``explicit`` is False when nothing was configured and we fell back to
    Notify's own default location — a missing file is not an error there.
    """
    path = _resolve_provider_config(config)
    if path:
        return path, True
    return os.path.expanduser(DEFAULT_PROVIDER_CONFIG), False


# ---------------------------------------------------------------------------
# Provider-config validation
# ---------------------------------------------------------------------------
def _walk(node, path: str, found: List[DuplicateKey]) -> None:
    """Collect duplicate mapping keys from a composed YAML node tree."""
    if isinstance(node, yaml.MappingNode):
        seen = {}
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", key_node))
            line = key_node.start_mark.line + 1
            if key in seen:
                found.append(DuplicateKey(key=key, path=path, line=line,
                                          first_line=seen[key]))
            else:
                seen[key] = line
            _walk(value_node, f"{path}.{key}" if path else key, found)
    elif isinstance(node, yaml.SequenceNode):
        for index, child in enumerate(node.value):
            _walk(child, f"{path}[{index}]", found)


def find_duplicate_keys(text: str) -> List[DuplicateKey]:
    """Find keys defined twice in the same mapping, anywhere in ``text``.

    PyYAML silently keeps the last value; Notify's Go parser rejects the file
    outright. Composing the node tree (rather than loading it) keeps the source
    line of every key so the report can point at what to merge.
    """
    found: List[DuplicateKey] = []
    for document in yaml.compose_all(text):
        if document is not None:
            _walk(document, "", found)
    return found


def _duplicate_key_report(path: str, duplicates: List[DuplicateKey]) -> List[str]:
    lines = [f"Notify provider config has duplicate YAML keys: {path}"]
    for dup in duplicates:
        where = f" (under '{dup.path}')" if dup.path else ""
        lines.append(f"  line {dup.line}: key '{dup.key}'{where} is already "
                     f"defined at line {dup.first_line}")
    lines.append("YAML forbids repeating a key in the same mapping, so Notify "
                 "refuses to start ('could not parse provider config file').")

    # Top-level repeats are provider blocks ('custom:' twice) — those merge into
    # one list. Repeats inside an entry are a different mistake: one field set
    # twice, which has to be removed, not merged.
    provider_keys = sorted({dup.key for dup in duplicates if not dup.path})
    if provider_keys:
        example = provider_keys[0]
        lines += [
            f"Each provider key must appear ONCE, with every destination as an "
            f"item in that key's list — do not repeat the '{example}:' block:",
            f"  {example}:",
            "    - id: first",
            "      custom_webhook_url: https://example.invalid/hook-one",
            "    - id: second",
            "      custom_webhook_url: https://example.invalid/hook-two",
            f"Merge the '{example}:' blocks into one list, keeping every entry, "
            f"then re-run. 'notify -id <id>' still selects a single destination.",
        ]
    nested = sorted({f"{dup.path}.{dup.key}" for dup in duplicates if dup.path})
    if nested:
        lines.append("Repeated inside a single entry (an entry cannot set the "
                     "same field twice) — delete one of each: "
                     + ", ".join(nested))
    return lines


def check_provider_config(path: str, require_exists: bool = True) -> List[str]:
    """Validate Notify's provider config; return human-readable problems.

    An empty list means the file parses (it says nothing about whether the
    webhooks themselves are correct — only Notify can tell you that).
    """
    if yaml is None:  # pragma: no cover - dependency missing
        log.debug("PyYAML unavailable; skipping provider-config validation.")
        return []
    if not os.path.exists(path):
        if require_exists:
            return [f"Notify provider config not found: {path}"]
        log.debug("No Notify provider config at default location %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return [f"Notify provider config could not be read: {exc}"]

    try:
        duplicates = find_duplicate_keys(text)
    except yaml.YAMLError as exc:
        return [f"Notify provider config is not valid YAML: {path}",
                f"  {str(exc).strip()}"]
    if duplicates:
        return _duplicate_key_report(path, duplicates)
    return []


def build_command(config) -> Optional[List[str]]:
    """Build the ``notify`` argv, or None if the binary is unavailable."""
    binary = config.get("notify.binary", "notify")
    resolved = shutil.which(binary) or (binary if os.path.isabs(binary) else None)
    if not resolved:
        log.warning("notify binary '%s' not found on PATH; skipping Notify send.", binary)
        return None

    cmd = [resolved]
    if config.get("notify.bulk", True):
        cmd.append("-bulk")
    provider_config = _resolve_provider_config(config)
    if provider_config:
        cmd += ["-provider-config", provider_config]
    provider_id = config.get("notify.provider_id")
    if provider_id:
        cmd += ["-id", str(provider_id)]
    return cmd


def send(config, payload: str) -> bool:
    """Pipe ``payload`` into notify. Returns True on success."""
    if not payload.strip():
        log.info("Empty Notify payload; nothing to send.")
        return False
    cmd = build_command(config)
    if cmd is None:
        return False

    # Pre-flight the provider config: Notify would only report 'could not parse
    # provider config file' and exit, so catch it here where we can say which
    # lines collide and how to merge them.
    path, explicit = resolve_provider_config(config)
    problems = check_provider_config(path, require_exists=explicit)
    if problems:
        for line in problems:
            log.error("%s", line)
        log.warning("Skipping Notify send: fix the provider config above, then re-run.")
        return False

    log.info("Sending results to Notify: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("Notify invocation failed: %s", exc)
        return False
    if proc.returncode != 0:
        output = (proc.stdout or b"").decode("utf-8", "replace").strip()
        log.warning("Notify exited with %s: %s", proc.returncode, output)
        # Notify may have read a different file than the one we checked (e.g.
        # $NOTIFY_PROVIDER_CONFIG set for the child only); translate its own
        # message rather than leaving the user with the raw parser error.
        if "already defined" in output or "could not parse provider config" in output:
            log.error("Notify could not parse its provider config. A provider key "
                      "(e.g. 'custom:') is defined more than once — merge the "
                      "blocks into a single list of entries. Run "
                      "'gitlocate --check-notify-config' for the exact lines.")
        return False
    return True
