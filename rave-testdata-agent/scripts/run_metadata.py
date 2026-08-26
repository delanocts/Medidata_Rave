#!/usr/bin/env python
"""A2 - standalone metadata acquisition (FR-2, ARC-2).

    python scripts/run_metadata.py --study <study-config-name>
    python scripts/run_metadata.py --study <study-config-name> --force-download
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import REPO_ROOT, check_dependencies  # noqa: E402

check_dependencies(include_optional=False)

from rave_agent.config.loader import ConfigError, load_config  # noqa: E402
from rave_agent.config.secrets import MissingSecretError, load_secrets  # noqa: E402
from rave_agent.metadata.downloader import MetadataAcquisition  # noqa: E402
from rave_agent.rave.client import RaveClient  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download study design metadata from Rave.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--force-download", action="store_true",
                        help="ignore the cache and re-download everything (FR-2.5)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
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
    configure_logging(level=level, log_file=config.study_output_dir / "logs" / f"metadata_{stamp}.log")

    print(f"\nStudy   : {config.study_env}")
    print(f"Target  : {config.study_output_dir / 'metadata'}\n")

    if args.dry_run:
        print("--dry-run: configuration is valid; no downloads performed.")
        return 0

    try:
        secrets = load_secrets(Path(args.env_file), require_anthropic=False)
    except MissingSecretError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    client = RaveClient(config, secrets)
    agent = MetadataAcquisition(client, config)
    summary = agent.run(force=args.force_download)

    version = summary["crf_version"]
    print(f"CRF version : {version['oid']} ({version['name']})")
    print(f"              chosen because it is {version.get('source') or 'unknown'}")
    print()
    for note in summary.get("version_notes") or []:
        print(f"  NOTE: {note}")
        print()

    width = max(len(a["name"]) for a in summary["artifacts"])
    print(f"{'ARTIFACT'.ljust(width)}  STATUS      SIZE       FILE")
    print(f"{'-' * width}  ----------  ---------  ----------------------------")
    for a in summary["artifacts"]:
        size = f"{a['bytes']:,}" if a["bytes"] else "-"
        print(f"{a['name'].ljust(width)}  {a['status']:<10}  {size:>9}  {a['filename'] or '-'}")
        if a["status"] in ("failed", "missing") and a["detail"]:
            print(f"{' ' * width}              {a['detail']}")

    stats = summary["structure"]
    if stats:
        print("\nStudy structure:")
        for key in ("forms", "fields", "item_groups", "codelists", "seed_folders",
                    "matrices", "total_folders", "edit_checks", "derivations"):
            if key in stats:
                print(f"  {key.replace('_', ' '):<16} {stats[key]}")
        for key in ("primary_form_oid", "default_matrix_oid"):
            if stats.get(key):
                print(f"  {key.replace('_', ' '):<16} {stats[key]}")

    summary_path = config.study_output_dir / "metadata" / "acquisition_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nManifest : {config.study_output_dir / 'metadata' / 'metadata_manifest.json'}")

    failed = [a for a in summary["artifacts"] if a["status"] == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
