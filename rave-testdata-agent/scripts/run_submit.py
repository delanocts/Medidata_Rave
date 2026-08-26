#!/usr/bin/env python
"""A6 - assemble and post ODM clinical data (FR-7, ARC-2).

Reads generated data from `output/<study>/generated/<subject>/<folder>/<form>.json`,
or a single hand-written payload file, and posts it.

    # prove the write path with hand-written data (spec section 16, step 4)
    python scripts/run_submit.py --study <name> --subject TST-001 --payload payload.json

    # post everything generated for one subject
    python scripts/run_submit.py --study <name> --subject TST-001

    # build the ODM but post nothing
    python scripts/run_submit.py --study <name> --subject TST-001 --dry-run

Payload file shape - folder OID -> form OID -> {item OID: value}:

    {
      "SCREEN": {
        "ENR_F": {"ENR_F._R_ENROLLFAILYN": "N", "ENR_F._R_SAEYN": "N"}
      }
    }

A repeating item group is given as a list of records instead:

    {"AE_F": {"__records__": {"AE_F_LOG_LINE": [{"AE_F.AETERM": "Headache"}]}}}
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
from rave_agent.model.loader import load_model  # noqa: E402
from rave_agent.rave.client import RaveClient  # noqa: E402
from rave_agent.submission.odm_builder import (  # noqa: E402
    FormPayload,
    OdmBuildError,
    build_clinical_odm,
)
from rave_agent.submission.submitter import Submitter  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402

RECORDS_KEY = "__records__"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post ODM clinical data to Rave.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--subject", required=True, help="subject key, e.g. TST-001")
    parser.add_argument("--payload", help="JSON file of hand-written values")
    parser.add_argument("--pass-number", type=int, default=1,
                        help="dynamics pass, used for the archive path")
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def _load_payload_file(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"payload file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("payload must be an object of folder OID -> form OID -> values")
    return data


def _load_generated(root: Path, subject: str) -> dict:
    """Collect generated/<subject>/<folder>/<form>.json into the payload shape.

    A5 writes {"values": {...}, "records": [...], "log_group_oid": "..."}; this
    flattens that into the folder -> form -> values mapping the builder wants,
    moving repeating rows under the __records__ key.
    """
    base = root / subject
    if not base.is_dir():
        return {}

    out: dict = {}
    for form_file in sorted(base.glob("*/*.json")):
        if form_file.name.startswith("_"):
            continue
        folder_oid = form_file.parent.name
        form_oid = form_file.stem
        payload = json.loads(form_file.read_text(encoding="utf-8"))

        values = dict(payload.get("values") or {})
        records = payload.get("records") or []
        group_oid = payload.get("log_group_oid")
        if records and group_oid:
            values[RECORDS_KEY] = {group_oid: records}
        elif records and not group_oid:
            print(f"  warning: {form_file.name} has records but no log_group_oid; skipped",
                  file=sys.stderr)

        if values:
            out.setdefault(folder_oid, {})[form_oid] = values
    return out


def _to_payloads(data: dict) -> dict[str, list[FormPayload]]:
    payloads: dict[str, list[FormPayload]] = {}
    for folder_oid, forms in data.items():
        for form_oid, values in (forms or {}).items():
            records = {}
            plain = {}
            for key, value in (values or {}).items():
                if key == RECORDS_KEY and isinstance(value, dict):
                    records = value
                else:
                    plain[key] = value
            payloads.setdefault(folder_oid, []).append(
                FormPayload(form_oid=form_oid, values=plain, records=records))
    return payloads


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
                      log_file=config.study_output_dir / "logs" / f"submit_{args.subject}_{stamp}.log")

    try:
        model = load_model(config.study_output_dir / "model" / "study_model.json")
    except FileNotFoundError:
        print(f"\nNo study model. Run: python scripts/run_model.py --study {args.study}\n",
              file=sys.stderr)
        return 2

    if args.payload:
        try:
            data = _load_payload_file(Path(args.payload))
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 2
        source = args.payload
    else:
        data = _load_generated(config.study_output_dir / "generated", args.subject)
        source = str(config.study_output_dir / "generated" / args.subject)

    if not data:
        print(f"\nNothing to submit for {args.subject} (looked in {source}).\n", file=sys.stderr)
        return 2

    site_oid = str(config.get("site.number"))
    print(f"\nStudy   : {config.study_env}")
    print(f"Subject : {args.subject} at site {site_oid}")
    print(f"Source  : {source}")
    print(f"Mode    : {'DRY RUN' if dry_run else 'LIVE'}\n")

    try:
        payloads = _to_payloads(data)
    except OdmBuildError as exc:
        print(f"\nODM assembly failed: {exc}\n", file=sys.stderr)
        return 1

    for folder_oid, forms in payloads.items():
        for payload in forms:
            count = len(payload.values) + sum(len(r) for r in payload.records.values())
            print(f"  {folder_oid:<12} {payload.form_oid:<16} {count} value(s)")

    try:
        secrets = load_secrets(Path(args.env_file), require_anthropic=False)
    except MissingSecretError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    client = RaveClient(config, secrets)
    submitter = Submitter(client,
                          archive_root=config.study_output_dir / "submissions",
                          dry_run=dry_run)

    limits_path = config.study_output_dir / "model" / "log_limits.json"
    limits = json.loads(limits_path.read_text(encoding="utf-8")) if limits_path.is_file() else {}

    # One post per form. Rave reports a single reason per transaction, so
    # submitting forms separately attributes each rejection to the form that
    # caused it (FR-7.4) instead of failing the whole visit.
    results, discovered = [], False
    print()
    for folder_oid, forms in payloads.items():
        for payload in forms:
            outcome, learned = _submit_form(
                model, config, submitter, args.subject, site_oid,
                folder_oid, payload, args.pass_number, limits)
            results.append((folder_oid, payload.form_oid, outcome))
            discovered = discovered or learned

    if discovered and not dry_run:
        limits_path.parent.mkdir(parents=True, exist_ok=True)
        limits_path.write_text(json.dumps(limits, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nLearned log-record limits -> {limits_path}")

    width = max(len(f) for _, f, _ in results)
    print(f"\n{'FOLDER'.ljust(12)}  {'FORM'.ljust(width)}  RESULT")
    print(f"{'-' * 12}  {'-' * width}  {'-' * 46}")
    failed = 0
    for folder_oid, form_oid, outcome in results:
        if outcome.ok:
            detail = outcome.status
        else:
            detail = f"REJECTED - {outcome.error or outcome.reason}"
            failed += 1
        print(f"{folder_oid.ljust(12)}  {form_oid.ljust(width)}  {detail[:60]}")

    print(f"\n{len(results) - failed}/{len(results)} form(s) accepted")
    return 1 if failed else 0


def _submit_form(model, config, submitter, subject, site_oid, folder_oid,
                 payload, pass_number, limits) -> tuple:
    """Post one form, shrinking its log records if Rave says there are too many.

    The per-form maximum is not published anywhere, so it is discovered here and
    remembered in `limits` for later runs.
    """
    archive = Path("subjects") / subject / f"pass_{pass_number}"
    learned = False

    while True:
        odm = build_clinical_odm(
            model=model, study_oid=config.study_env, subject_key=subject,
            site_oid=site_oid, folder_payloads={folder_oid: [payload]},
            subject_transaction="Update", form_transaction="Update",
        )
        outcome = submitter.post(
            odm, label=f"{subject}_{folder_oid}_{payload.form_oid}", archive_dir=archive)

        if outcome.ok or "max limit" not in (outcome.error or "").lower():
            return outcome, learned

        # Too many log lines: drop one and try again, down to a single record.
        group_oid = next((g for g, r in payload.records.items() if len(r) > 1), None)
        if group_oid is None:
            return outcome, learned

        new_count = len(payload.records[group_oid]) - 1
        payload.records[group_oid] = payload.records[group_oid][:new_count]
        limits[f"{payload.form_oid}.{group_oid}"] = new_count
        learned = True
        print(f"  {payload.form_oid}: over the log-record limit, retrying with {new_count}")


    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
