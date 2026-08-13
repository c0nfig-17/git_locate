"""Logging configuration.

Two sinks are configured:

- **Console** — concise, at the verbosity chosen on the CLI (``-q``/``-v``/``-vv``
  / ``--debug``).
- **File** — ALWAYS at DEBUG level, capturing every action the tool takes: each
  HTTP request (method, URL, params, status, timing, rate-limit headers), every
  rate-limit wait, each discovery phase and its counts, web queries, the Notify
  invocation, and every chaining command with its exit code.

The file log is what you hand back when something breaks: it is verbose and
self-contained enough to diagnose failures across the API, rate-limit,
web-search, notification and chaining stages without re-running anything.

Secrets are never written to the log — only whether each expected environment
variable is *present*, never its value.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime, timezone
from typing import List, Optional

from . import __tool_name__, __version__

ROOT = "gitlocate"

_CONSOLE_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_FILE_FMT = ("%(asctime)s %(levelname)-7s %(name)s [%(filename)s:%(lineno)d] "
             "%(message)s")


def configure_console(verbose: int, quiet: bool, debug: bool) -> None:
    """Set up the root gitlocate logger + a console handler."""
    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG)   # handlers do the filtering
    root.propagate = False
    # Clear any pre-existing handlers (e.g. from a prior run in the same proc).
    for h in list(root.handlers):
        root.removeHandler(h)

    if quiet:
        level = logging.WARNING
    elif debug or verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.INFO

    ch = logging.StreamHandler(stream=sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
    root.addHandler(ch)


def attach_file_handler(path: str) -> Optional[str]:
    """Attach a DEBUG file handler. Returns the path, or None on failure."""
    root = logging.getLogger(ROOT)
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    except OSError as exc:
        root.warning("Could not open debug log file %s: %s", path, exc)
        return None
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    return path


def _env_present(name: Optional[str]) -> str:
    if not name:
        return "unset(name)"
    return "present" if os.environ.get(name) else "absent"


def log_run_header(config, argv: List[str], log_path: Optional[str],
                   config_path: Optional[str]) -> None:
    """Write a diagnostic header describing the environment and settings."""
    log = logging.getLogger(ROOT)
    log.info("==================== %s %s ====================",
             __tool_name__, __version__)
    log.debug("started_at=%s", datetime.now(timezone.utc).isoformat())
    log.debug("python=%s", sys.version.replace("\n", " "))
    log.debug("platform=%s", platform.platform())
    log.debug("cwd=%s", os.getcwd())
    log.debug("argv=%s", argv)
    log.debug("config_file=%s", config_path or "(defaults only)")
    log.debug("debug_log=%s", log_path or "(none)")
    # Secret presence — names from config, values NEVER logged.
    log.debug("env %s=%s", config.get("github.token_env"),
              _env_present(config.get("github.token_env")))
    log.debug("env %s=%s (web_dork provider=%s)",
              config.get("web_dork.api_key_env"),
              _env_present(config.get("web_dork.api_key_env")),
              config.get("web_dork.provider"))
    log.debug("env %s=%s", config.get("notify.provider_config_env"),
              _env_present(config.get("notify.provider_config_env")))
