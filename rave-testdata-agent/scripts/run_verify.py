#!/usr/bin/env python
"""A7 - verification and reporting (FR-9, ARC-2).

Reads each subject back from Rave, reconciles field by field against what was
generated, and writes the run report.

    python scripts/run_verify.py --study <name>
    python scripts/run_verify.py --study <name> --subject TST-001
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
from rave_agent.generation.skill_rules import available_skills  # noqa: E402
from rave_agent.model.loader import load_model  # noqa: E402
from rave_agent.rave.client import RaveClient  # noqa: E402
from rave_agent.reporting.reconciler import coverage, reconcile_subject  # noqa: E402
from rave_agent.reporting.report import RunReport  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile Rave against what was submitted.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--subject", action="append",
                        help="limit to these subjects (repeatable); default is all generated")
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def _discover_subjects(config, requested: list[str] | None) -> list[str]:
    if requested:
        return requested
    root = config.study_output_dir / "generated"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _dynamics_summary(config, subjects: list[str]) -> tuple[dict, list[dict]]:
    """Aggregate activation state, and list what the graph predicted but never got."""
    summary = {"passes_run": 0, "active_folders": 0, "predicted": 0}
    unreachable: list[dict] = []
    seen: set[str] = set()

    for subject in subjects:
        state = _load_json(
            config.study_output_dir / "state" / subject / "activation_state.json")
        if not state:
            continue
        summary["passes_run"] = max(summary["passes_run"], state.get("passes_run", 0))
        summary["active_folders"] = max(
            summary["active_folders"], len(state.get("active_folders") or []))
        summary["predicted"] = max(
            summary["predicted"], len(state.get("predicted_folders") or []))
        for folder in state.get("never_activated") or []:
            if folder in seen:
                continue
            seen.add(folder)
            unreachable.append({
                "target": folder, "kind": "folder",
                "reason": "predicted by the ALS but never accepted data - an unmet "
                          "Or-branch, a custom function, or a workflow state this run "
                          "does not reach",
            })
    return summary, unreachable


def _generation_summary(config, subjects: list[str]) -> tuple[dict, list[str]]:
    calls = tokens_in = tokens_out = failed = 0
    trade_offs: list[str] = []

    for subject in subjects:
        summary = _load_json(
            config.study_output_dir / "generated" / subject / "_summary.json")
        usage = summary.get("token_usage") or {}
        calls += usage.get("calls", 0)
        tokens_in += usage.get("input_tokens", 0)
        tokens_out += usage.get("output_tokens", 0)
        failed += (summary.get("counts") or {}).get("failed", 0)

    strategy = config.get("dynamics.trigger_strategy")
    if strategy == "maximize":
        trade_offs.append(
            "dynamics.trigger_strategy is 'maximize': trigger fields were set to the "
            "value unlocking the most targets, which deliberately favours uncommon "
            "answers over typical ones.")
    if config.get("generation.require_all_fields"):
        trade_offs.append(
            "generation.require_all_fields is on: every writable field was populated, "
            "including conditional fields a real site would leave blank.")

    limits = _load_json(config.study_output_dir / "model" / "log_limits.json")
    if limits:
        trade_offs.append(
            f"{len(limits)} per-form log-record cap(s) were discovered by being refused "
            "and are capped accordingly; Rave publishes them nowhere.")

    return {
        "model": config.get("generation.model"),
        "llm_calls": calls,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "forms_failed_generation": failed,
    }, trade_offs


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = datetime.now(timezone.utc)

    try:
        config = load_config(args.study, config_dir=Path(args.config))
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    level = args.log_level or config.get("execution.log_level", "INFO")
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    configure_logging(level=level,
                      log_file=config.study_output_dir / "logs" / f"verify_{stamp}.log")

    try:
        model = load_model(config.study_output_dir / "model" / "study_model.json")
    except FileNotFoundError:
        print(f"\nNo study model. Run: python scripts/run_model.py --study {args.study}\n",
              file=sys.stderr)
        return 2

    subjects = _discover_subjects(config, args.subject)
    if not subjects:
        print("\nNothing to verify: no generated data found.\n", file=sys.stderr)
        return 2

    try:
        secrets = load_secrets(Path(args.env_file), require_anthropic=False)
    except MissingSecretError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    client = RaveClient(config, secrets)
    generated_root = config.study_output_dir / "generated"

    print(f"\nStudy    : {config.study_env}")
    print(f"Subjects : {', '.join(subjects)}\n")

    reconciliations = [
        reconcile_subject(client, config, model, subject, generated_root)
        for subject in subjects
    ]

    cov = coverage(model, reconciliations)
    dynamics, unreachable = _dynamics_summary(config, subjects)
    generation, trade_offs = _generation_summary(config, subjects)

    report = RunReport(
        study=config.study_name,
        environment=config.environment,
        crf_version=f"{model.crf_version_oid} ({model.crf_version_name})",
        site_oid=str(config.get("site.number")),
        config_hash=config.config_hash,
        duration_seconds=round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        coverage=cov,
        dynamics=dynamics,
        generation=generation,
        trade_offs=trade_offs,
        unreachable=unreachable,
        warnings=list(model.warnings)[:20],
        skills_used=available_skills(),
        subjects=[
            {
                "subject": r.subject_id, "exists": r.exists,
                "stored_values": r.stored_values,
                "folder_form_pairs": r.folder_form_pairs,
                "field_checks": r.counts(),
                "error": r.error,
                "mismatches": [
                    {"item": c.item_oid, "folder": c.folder_oid,
                     "submitted": c.submitted, "stored": c.stored}
                    for c in r.mismatches[:25]
                ],
                "absent": [
                    {"item": c.item_oid, "folder": c.folder_oid, "form": c.form_oid,
                     "submitted": c.submitted}
                    for c in r.absent[:25]
                ],
            }
            for r in reconciliations
        ],
    )

    width = max(len(s) for s in subjects)
    denominator = cov["denominators"]["real_folders"]
    print(f"{'SUBJECT'.ljust(width)}  FOLDERS  PAIRS  INSTANCES  VALUES  "
          f"VERIFIED  MISMATCH  ABSENT")
    print(f"{'-' * width}  -------  -----  ---------  ------  --------  --------  ------")
    for entry, rec in zip(cov["per_subject"], reconciliations):
        checks = entry["field_checks"]
        verified = (checks.get("match", 0) + checks.get("normalised", 0)
                    + checks.get("narrowed", 0))
        print(f"{entry['subject'].ljust(width)}  "
              f"{entry['folders_with_data']:>3}/{denominator:<3}  "
              f"{entry['folder_form_pairs']:>5}  {entry['form_instances']:>9}  "
              f"{entry['stored_values']:>6}  {verified:>8}  "
              f"{checks.get('mismatch', 0):>8}  {checks.get('absent', 0):>7}")
        if rec.error:
            print(f"{' ' * width}  ERROR: {rec.error}")

    print(f"\nWritable fields defined : {cov['denominators']['fields_writable']} "
          f"(of {cov['denominators']['fields_defined']}; "
          f"{cov['denominators']['fields_excluded_derived']} derived, "
          f"{cov['denominators']['fields_excluded_not_visible']} not visible)")

    if unreachable:
        print(f"\nPredicted but never activated ({len(unreachable)}): "
              f"{', '.join(u['target'] for u in unreachable)}")

    if trade_offs:
        print("\nTrade-offs this run made:")
        for note in trade_offs:
            print(f"  - {note}")

    absent = [c for r in reconciliations for c in r.absent]
    if absent:
        print(f"\nSent but not held by Rave ({len(absent)}) - these submissions "
              "did not land:")
        for c in absent[:8]:
            print(f"  {c.item_oid} in {c.folder_oid}/{c.form_oid}")

    mismatches = [c for r in reconciliations for c in r.mismatches]
    if mismatches:
        print(f"\nFields Rave stored differently ({len(mismatches)}):")
        for c in mismatches[:10]:
            print(f"  {c.item_oid} in {c.folder_oid}: sent {c.submitted!r}, "
                  f"stored {c.stored!r}")

    reports_dir = config.study_output_dir / "reports"
    json_path = report.save_json(reports_dir / f"run_{stamp}.json")
    html_path = report.save_html(reports_dir / f"run_{stamp}.html")
    print(f"\nReport : {html_path}")
    print(f"         {json_path}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
