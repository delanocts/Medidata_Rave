#!/usr/bin/env python
"""Orchestrator CLI - run every stage in order (ARC-3).

    python scripts/run_all.py --study <name>
    python scripts/run_all.py --study <name> --dry-run
    python scripts/run_all.py --study <name> --resume
    python scripts/run_all.py --study <name> --stop-after model
    python scripts/run_all.py --study <name> --only metadata --only model

Each stage is the same standalone script you would run by hand, so anything the
orchestrator can do is reproducible one command at a time.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import REPO_ROOT, check_dependencies  # noqa: E402

check_dependencies(include_optional=False)

from rave_agent.config.loader import ConfigError, load_config  # noqa: E402
from rave_agent.orchestrator import STAGE_KEYS, Orchestrator  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the whole pipeline for one study.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--dry-run", action="store_true",
                        help="build payloads without posting anything to Rave")
    parser.add_argument("--resume", action="store_true",
                        help="skip stages already marked ok in run_manifest.json")
    parser.add_argument("--only", action="append", choices=STAGE_KEYS,
                        help="run only these stages (repeatable)")
    parser.add_argument("--stop-after", choices=STAGE_KEYS,
                        help="stop once this stage completes")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
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
    configure_logging(level=level,
                      log_file=config.study_output_dir / "logs" / f"run_{stamp}.log")

    print(f"\nStudy   : {config.study_env}")
    print(f"Config  : {config.study_file.name}  (hash {config.config_hash})")
    print(f"Output  : {config.study_output_dir}")
    print(f"Mode    : {'DRY RUN - nothing will be posted' if args.dry_run else 'LIVE'}"
          + ("  |  RESUME" if args.resume else ""))

    orchestrator = Orchestrator(
        config=config, study_arg=args.study, env_file=Path(args.env_file),
        dry_run=args.dry_run, resume=args.resume,
        only=args.only, stop_after=args.stop_after,
    )
    manifest = orchestrator.run()

    print("\n" + "=" * 62)
    width = max(len(s.name) for s in manifest.stages) if manifest.stages else 20
    print(f"{'STAGE'.ljust(width)}  STATUS   SECONDS  DETAIL")
    print(f"{'-' * width}  -------  -------  ----------------------------")
    for stage in manifest.stages:
        print(f"{stage.name.ljust(width)}  {stage.status:<7}  "
              f"{stage.seconds:>7.1f}  {stage.detail[:40]}")

    print(f"\nManifest : {orchestrator.manifest_path}")

    failed = [s for s in manifest.stages if s.status == "failed"]
    if failed:
        print(f"Result   : FAILED at {failed[0].name}")
        print("           Re-run with --resume once fixed to continue from there.")
        return 1

    print("Result   : OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
