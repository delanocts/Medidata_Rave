#!/usr/bin/env python
"""A1 - standalone connection test (FR-1, ARC-2).

    python scripts/test_connection.py --study <study-config-name>

Exit code 0 only if every mandatory check passed (FR-1.6).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import REPO_ROOT, check_dependencies  # noqa: E402

check_dependencies(include_optional=True)

from rave_agent.config.loader import ConfigError, load_config  # noqa: E402
from rave_agent.config.secrets import MissingSecretError, load_secrets  # noqa: E402
from rave_agent.connection.checks import run_all_checks  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate connectivity, credentials and permissions.")
    parser.add_argument("--study", required=True, help="study config name or path (e.g. the basename of a file in config/studies)")
    parser.add_argument("--config", default=str(REPO_ROOT / "config"), help="config directory")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"), help="path to .env")
    parser.add_argument("--skip-llm", action="store_true", help="skip the Anthropic key check")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--dry-run", action="store_true", help="validate config only, make no calls")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(args.study, config_dir=Path(args.config))
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    level = args.log_level or config.get("execution.log_level", "INFO")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_file = config.study_output_dir / "logs" / f"connection_{stamp}.log"
    configure_logging(level=level, log_file=log_file)

    print(f"\nStudy      : {config.study_env}")
    print(f"Host       : {config.base_url}{config.get('rave.rws_path')}")
    print(f"Config     : {config.study_file.name}  (hash {config.config_hash})")
    if config.overrides_applied:
        print(f"Overrides  : {', '.join(config.overrides_applied)}")
    print()

    if args.dry_run:
        print("--dry-run: configuration is valid; no calls made.")
        return 0

    try:
        secrets = load_secrets(Path(args.env_file), require_anthropic=not args.skip_llm)
    except MissingSecretError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    report = run_all_checks(config, secrets, skip_llm=args.skip_llm)

    print("\n" + report.render_matrix())

    report_path = report.write(config.study_output_dir / "connection_report.json")
    counts = report.to_dict()["counts"]
    print(f"\nResult : {'PASS' if report.ok else 'FAIL'}  "
          f"({counts['PASS']} pass, {counts['FAIL']} fail, {counts['WARN']} warn, {counts['SKIP']} skip)")
    print(f"Report : {report_path}")
    print(f"Log    : {log_file}")

    failures = [r for r in report.results if r.status in ("FAIL", "WARN")]
    if failures:
        print("\nWhat to do next:")
        for r in failures:
            if r.remediation:
                print(f"  [{r.status}] {r.name}: {r.remediation}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
