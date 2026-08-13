"""Command-line interface for git_locate."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

from . import ETHICAL_NOTICE, __tool_name__, __version__
from . import logging_setup
from .config import load_config
from .chaining import Chainer
from .engine import Engine
from .output import notify as notify_mod
from .output import writer

log = logging.getLogger("gitlocate")


def _read_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        item = item.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


def _collect(values: Optional[List[str]], file_path: Optional[str]) -> List[str]:
    out: List[str] = []
    for v in values or []:
        # allow comma-separated lists too
        out.extend(part.strip() for part in v.split(",") if part.strip())
    if file_path:
        out.extend(_read_lines(file_path))
    return _dedupe(out)


def _merge(*groups: List[str]) -> List[str]:
    """De-duplicated, order-stable merge of several string lists."""
    out: List[str] = []
    for g in groups:
        out.extend(g or [])
    return _dedupe(out)


def _check_notify_config(config) -> int:
    """Validate Notify's provider config and report. 0 = usable, 1 = problems."""
    path, explicit = notify_mod.resolve_provider_config(config)
    source = "configured" if explicit else "Notify default location"
    log.info("Checking Notify provider config (%s): %s", source, path)
    problems = notify_mod.check_provider_config(path, require_exists=explicit)
    if problems:
        for line in problems:
            log.error("%s", line)
        return 1
    if not os.path.exists(path):
        log.warning("No Notify provider config at %s — set %s or "
                    "notify.provider_config before sending.", path,
                    config.get("notify.provider_config_env", "NOTIFY_PROVIDER_CONFIG"))
        return 0
    log.info("Notify provider config parses cleanly (no duplicate keys).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gitlocate",
        description=(
            "git_locate — GitHub recon *discovery*: locate organizations, "
            "repositories and users related to a target company. "
            "Authorized security testing only."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=ETHICAL_NOTICE,
    )
    p.add_argument("-c", "--company", action="append", metavar="NAME",
                   help="Company name (repeatable, or comma-separated).")
    p.add_argument("--company-file", "--companies", metavar="PATH",
                   help="File with one company name per line (the usual input).")
    p.add_argument("-d", "--domain", action="append", metavar="DOMAIN",
                   help="Associated domain (repeatable, or comma-separated).")
    p.add_argument("--domain-file", "--domains", metavar="PATH",
                   help="File with one domain per line (the usual input).")
    p.add_argument("--config", metavar="PATH",
                   help="Config YAML (default: $GITLOCATE_CONFIG or ./config.yaml).")
    p.add_argument("-o", "--output-dir", metavar="DIR",
                   help="Override output.dir from config.")
    p.add_argument("--no-web-dork", action="store_true",
                   help="Disable the web-dorking source for this run.")
    p.add_argument("--chain", action="store_true",
                   help="Run the configured chaining commands on discovered repos.")
    p.add_argument("--notify", action="store_true",
                   help="Send results to ProjectDiscovery Notify after discovery.")
    p.add_argument("--dry-run", action="store_true",
                   help="For chaining: print commands instead of executing them.")
    p.add_argument("--print-config", action="store_true",
                   help="Print the effective merged configuration and exit.")
    p.add_argument("--check-notify-config", action="store_true",
                   help="Validate the Notify provider-config YAML (duplicate "
                        "keys, syntax) and exit.")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="Increase console log verbosity (-v, -vv).")
    p.add_argument("-q", "--quiet", action="store_true", help="Only log warnings/errors.")
    p.add_argument("--debug", action="store_true",
                   help="Verbose DEBUG output on the console (the file log is "
                        "always DEBUG regardless).")
    p.add_argument("--log-file", metavar="PATH",
                   help="Debug log file path (default: <output.dir>/gitlocate.log). "
                        "Hand this file over when reporting a problem.")
    p.add_argument("--no-log-file", action="store_true",
                   help="Do not write the debug log file.")
    p.add_argument("--version", action="version",
                   version=f"{__tool_name__} {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(argv)
    logging_setup.configure_console(args.verbose, args.quiet, args.debug)

    log.warning("ETHICAL USE NOTICE: %s", ETHICAL_NOTICE)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, RuntimeError) as exc:
        log.error("Failed to load config: %s", exc)
        return 2

    # CLI overrides.
    if args.output_dir:
        config.data.setdefault("output", {})["dir"] = args.output_dir
    if args.no_web_dork:
        config.data.setdefault("web_dork", {})["enabled"] = False

    if args.print_config:
        import json
        print(json.dumps(config.data, indent=2, default=str))
        return 0

    if args.check_notify_config:
        return _check_notify_config(config)

    # -- attach the debug log file + write the diagnostic header ----------
    log_path = None
    if not args.no_log_file:
        out_dir = config.get("output.dir", "./output")
        log_path = args.log_file or os.path.join(out_dir, "gitlocate.log")
        log_path = logging_setup.attach_file_handler(log_path)
    logging_setup.log_run_header(config, raw_argv, log_path,
                                 args.config or os.environ.get("GITLOCATE_CONFIG"))
    if log_path:
        log.info("Debug log: %s", log_path)

    # Targets: CLI flags + files + a `targets:` block in the config file.
    companies = _collect(
        args.company, args.company_file or config.get("targets.companies_file"))
    companies = _merge(config.get("targets.companies") or [], companies)
    domains = _collect(
        args.domain, args.domain_file or config.get("targets.domains_file"))
    domains = _merge(config.get("targets.domains") or [], domains)

    if not companies:
        log.error("At least one company name is required "
                  "(-c/--company, --company-file, or targets.companies in config).")
        return 2
    if not domains:
        log.warning("No domains supplied: domain-anchor verification will be skipped, "
                    "which significantly weakens confidence scoring.")

    log.info("Targets: companies=%s domains=%s", companies, domains)
    args._companies = companies
    args._domains = domains

    try:
        return _run_phases(args, config)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        return 130
    except Exception:  # noqa: BLE001 - top-level guard: log full traceback
        log.exception("Unhandled error during execution. The full traceback has "
                      "been written to the debug log%s.",
                      f" ({log_path})" if log_path else "")
        return 1


def _run_phases(args, config) -> int:
    """Execute phase 1 (enumeration) and optionally phase 2 (chaining)."""
    companies = args._companies
    domains = args._domains

    # ============================ PHASE 1: enumeration =====================
    log.info("=== Phase 1: enumeration (discovery) ===")
    engine = Engine(config)
    findings = engine.run(companies, domains)

    document = writer.build_document(findings, companies, domains)

    # -- write artifacts --------------------------------------------------
    out_dir = config.get("output.dir", "./output")
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, config.get("output.json_file", "results.json"))
    repos_path = os.path.join(out_dir, config.get("output.repos_flat_file", "repos.txt"))
    notify_path = os.path.join(out_dir, config.get("output.notify_file", "notify.txt"))
    pretty = bool(config.get("output.pretty", True))

    writer.write_json(document, json_path, pretty=pretty)
    writer.write_repos_flat(findings, repos_path)

    notify_payload = config.get("notify.payload", "phase_transition")
    notify_text = writer.build_notify_text(document, payload=notify_payload)
    writer.write_notify_text(notify_text, notify_path)

    summary = document["summary"]
    log.info("Phase 1 complete: %d orgs, %d repos, %d users",
             summary["organizations"], summary["repositories"], summary["users"])
    log.info("Wrote %s, %s, %s", json_path, repos_path, notify_path)

    # ---- Phase transition notification (1 -> 2) -------------------------
    # Notify fires here, at the boundary between enumeration and leak
    # discovery, so you know phase 2 is about to run over the discovered repos.
    want_notify = args.notify or config.get("notify.enabled", False) \
        or config.get("notify.on_phase_transition", False)
    if want_notify:
        if notify_mod.send(config, notify_text):
            log.info("Phase-transition notification sent to Notify.")
        else:
            log.warning("Notify send did not complete (see warnings above).")

    # ======================= PHASE 2: leak discovery =======================
    # Runs the configured external tools (gitleaks, trufflehog, ...) per repo.
    # Phase 3 (per-leak notification) is handled by those tools' own configs
    # (e.g. piping their findings into `notify`).
    if args.chain or config.get("chaining.enabled", False):
        log.info("=== Phase 2: leak discovery (chaining) ===")
        chainer = Chainer(config, dry_run=args.dry_run)
        stats = chainer.run(findings)
        verb = "would run" if args.dry_run else "run"
        suffix = " (dry-run: nothing was executed)" if args.dry_run else ""
        log.info("Chaining: %d repos, %d commands %s, %d failures, %d skipped%s",
                 stats["repos"], stats["commands_run"], verb, stats["failures"],
                 stats["skipped"], suffix)
    else:
        log.info("Phase 2 (leak discovery) not run. Enable with --chain or "
                 "chaining.enabled, after pasting your tools into config.yaml.")

    # Emit the JSON path on stdout for easy piping in shell pipelines.
    print(json_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
