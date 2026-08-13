"""ProjectDiscovery Notify integration.

Notify (https://github.com/projectdiscovery/notify) fans a text payload out to
webhooks (Slack, Discord, Telegram, generic webhook, ...). git_locate is built
to be Notify-compatible from the start: the payload is plain, line-oriented text
(see :func:`gitlocate.output.writer.build_notify_text`), which is exactly what
``notify -bulk`` consumes on stdin.

Secrets and provider webhooks stay in Notify's own provider-config YAML — this
module only points Notify at that file (path taken from an env var or config)
and pipes the payload in. Nothing about the destinations is hardcoded here.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import List, Optional

log = logging.getLogger("gitlocate.notify")


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
        log.warning("Notify exited with %s: %s", proc.returncode,
                    (proc.stdout or b"").decode("utf-8", "replace").strip())
        return False
    return True
