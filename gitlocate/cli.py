"""Command-line interface for git_locate."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

from . import ETHICAL_NOTICE, __tool_name__, __version__
from .config import load_config
from .chaining import Chainer
from .engine import Engine
from .output import notify as notify_mod
from .output import writer

log = logging.getLogger("gitlocate")


def _read_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]


def _collect(values: Optional[List[str]], file_path: Optional[str]) -> List[str]:
    out: List[str] = []
    for v in values or []:
        # allow comma-separated lists too
        out.extend(part.strip() for part in v.split(",") if part.strip())
    if file_path:
        out.extend(_read_lines(file_path))
    # de-duplicate, order-stable
    seen = set()
    result = []
    for item in out:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


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
    p.add_argument("--company-file", metavar="PATH",
                   help="File with one company name per line.")
    p.add_argument("-d", "--domain", action="append", metavar="DOMAIN",
                   help="Associated domain (repeatable, or comma-separated).")
    p.add_argument("--domain-file", metavar="PATH",
                   help="File with one domain per line.")
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
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="Increase log verbosity (-v, -vv).")
    p.add_argument("-q", "--quiet", action="store_true", help="Only log warnings/errors.")
    p.add_argument("--version", action="version",
                   version=f"{__tool_name__} {__version__}")
    return p


def _setup_logging(verbose: int, quiet: bool) -> None:
    level = logging.INFO
    if quiet:
        level = logging.WARNING
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.quiet)

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

    companies = _collect(args.company, args.company_file)
    domains = _collect(args.domain, args.domain_file)

    if not companies:
        log.error("At least one company name is required (-c/--company or --company-file).")
        return 2
    if not domains:
        log.warning("No domains supplied: domain-anchor verification will be skipped, "
                    "which significantly weakens confidence scoring.")

    log.info("Targets: companies=%s domains=%s", companies, domains)

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

    notify_payload = config.get("notify.payload", "summary")
    notify_text = writer.build_notify_text(document, payload=notify_payload)
    writer.write_notify_text(notify_text, notify_path)

    summary = document["summary"]
    log.info("Discovery complete: %d orgs, %d repos, %d users",
             summary["organizations"], summary["repositories"], summary["users"])
    log.info("Wrote %s, %s, %s", json_path, repos_path, notify_path)

    # -- optional: Notify -------------------------------------------------
    if args.notify or config.get("notify.enabled", False):
        if notify_mod.send(config, notify_text):
            log.info("Results sent to Notify.")
        else:
            log.warning("Notify send did not complete (see warnings above).")

    # -- optional: chaining -----------------------------------------------
    if args.chain or config.get("chaining.enabled", False):
        chainer = Chainer(config, dry_run=args.dry_run)
        stats = chainer.run(findings)
        log.info("Chaining: %d repos, %d commands run, %d failures, %d skipped",
                 stats["repos"], stats["commands_run"], stats["failures"],
                 stats["skipped"])

    # Emit the JSON path on stdout for easy piping in shell pipelines.
    print(json_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
