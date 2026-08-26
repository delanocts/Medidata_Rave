#!/usr/bin/env python
"""A4 - site and subject provisioning (FR-4, FR-5, ARC-2).

    python scripts/run_provision.py --study <name> --dry-run
    python scripts/run_provision.py --study <name>
    python scripts/run_provision.py --study <name> --site-only
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import REPO_ROOT, check_dependencies  # noqa: E402

check_dependencies(include_optional=False)

from rave_agent.config.loader import ConfigError, load_config  # noqa: E402
from rave_agent.config.secrets import MissingSecretError, load_secrets  # noqa: E402
from rave_agent.model.loader import load_model  # noqa: E402
from rave_agent.provisioning.sites import (  # noqa: E402
    assigned_version,
    ensure_site,
    list_sites,
)
from rave_agent.provisioning.subjects import SubjectPolicyError, enrol_subjects  # noqa: E402
from rave_agent.rave.client import RaveClient  # noqa: E402
from rave_agent.submission.odm_builder import OdmBuildError  # noqa: E402
from rave_agent.submission.submitter import Submitter  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify/create the site and enrol subjects.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--dry-run", action="store_true",
                        help="build payloads and write them to disk without posting")
    parser.add_argument("--site-only", action="store_true", help="stop after the site check")
    parser.add_argument("--ignore-version-drift", action="store_true",
                        help="provision even when the model was built against a "
                             "different CRF version than the site is on")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(args.study, config_dir=Path(args.config))
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    dry_run = args.dry_run or bool(config.get("execution.dry_run"))
    level = args.log_level or config.get("execution.log_level", "INFO")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    configure_logging(level=level,
                      log_file=config.study_output_dir / "logs" / f"provision_{stamp}.log")

    try:
        model = load_model(config.study_output_dir / "model" / "study_model.json")
    except FileNotFoundError:
        print(f"\nNo study model. Run: python scripts/run_model.py --study {args.study}\n",
              file=sys.stderr)
        return 2

    try:
        secrets = load_secrets(Path(args.env_file), require_anthropic=False)
    except MissingSecretError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    client = RaveClient(config, secrets)
    submitter = Submitter(client,
                          archive_root=config.study_output_dir / "submissions",
                          dry_run=dry_run)

    number = str(config.get("site.number"))
    name = str(config.get("site.name"))

    # Ask the site itself, now, rather than trusting the version frozen into the
    # study model. A2 does resolve it from the site, but A2 is skipped on
    # --resume and on --only, so the model can be older than the site it
    # describes. This is a read, and it happens before anything is written.
    sites = list_sites(client, config)
    site_version, effective = assigned_version(sites, number, name)

    if not site_version:
        reason = "no version assigned to the site; using the model's"
    elif site_version == model.crf_version_oid:
        reason = f"assigned to site {number}"
        if effective:
            reason += f", effective {effective}"
    else:
        reason = f"DISAGREES WITH SITE {number}"

    print()
    print(f"Study       : {config.study_env}")
    print(f"CRF version : {model.crf_version_oid} ({model.crf_version_name})  - {reason}")
    print(f"Site        : {number} ({name})")
    print(f"Mode        : {'DRY RUN - nothing will be posted' if dry_run else 'LIVE - will write to Rave'}")
    print()

    if site_version and site_version != model.crf_version_oid:
        where = f" (effective {effective})" if effective else ""
        print(f"  WARNING: site {number} is on CRF version {site_version}{where}, but the")
        print(f"           study model was built against {model.crf_version_oid}. Posting now would")
        print("           use the wrong version: fields that do not exist there are")
        print("           rejected, and forms land in the wrong folders.")
        print()
        print("           Rebuild against the site's version first:")
        print(f"             python scripts/run_metadata.py --study {args.study} --force-download")
        print(f"             python scripts/run_model.py --study {args.study}")
        print()
        if not args.ignore_version_drift:
            print("Refusing to provision against a stale CRF version.",
                  file=sys.stderr)
            print("Pass --ignore-version-drift to override.", file=sys.stderr)
            return 1

    site = ensure_site(client, config, model.crf_version_oid, submitter, sites=sites)
    print(f"SITE  [{site.action.upper()}] {site.number}: {site.detail}")
    if site.known_sites:
        print(f"      existing sites: {site.known_sites[:8]}")

    if site.action in ("create_failed", "missing"):
        print("\nCannot continue without a site.", file=sys.stderr)
        return 1

    if args.site_only:
        print("\n--site-only: stopping before subject creation.")
        return 0

    try:
        result = enrol_subjects(client, config, model, site.number, submitter)
    except (SubjectPolicyError, OdmBuildError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    print(f"\nEntry point : {result.entry_form_oid} in {result.entry_folder_oid}"
          f"  (subject ID -> {result.entry_item_oid})")
    print(f"Existing    : {len(result.existing_ids)} subject(s) already in the study\n")

    width = max((len(s.subject_id) for s in result.subjects), default=12)
    print(f"{'SUBJECT'.ljust(width)}  STATUS    DETAIL")
    print(f"{'-' * width}  --------  ----------------------------------------")
    for record in result.subjects:
        print(f"{record.subject_id.ljust(width)}  {record.status:<8}  {record.detail[:60]}")

    path = result.save(config.study_output_dir / "subjects.json")
    counts = result.counts()
    print(f"\nCounts : {counts}")
    print(f"Wrote  : {path}")

    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
