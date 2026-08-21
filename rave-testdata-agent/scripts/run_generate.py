#!/usr/bin/env python
"""A5 - standalone data generation (FR-6, ARC-2).

Reads the study model and dynamics graph from disk; calls the LLM; writes
validated values to output/<study>/generated/. Makes no Rave calls.

    python scripts/run_generate.py --study <name> --subject TST-001
    python scripts/run_generate.py --study <name> --subject TST-001 --folder SCREEN
    python scripts/run_generate.py --study <name> --subject TST-001 --regenerate
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import REPO_ROOT, check_dependencies  # noqa: E402

check_dependencies(include_optional=True)

from rave_agent.config.loader import ConfigError, load_config  # noqa: E402
from rave_agent.config.secrets import MissingSecretError, load_secrets  # noqa: E402
from rave_agent.generation.generator import Generator  # noqa: E402
from rave_agent.generation.llm_client import LlmClient  # noqa: E402
from rave_agent.model.dynamics_graph import DynamicsGraph  # noqa: E402
from rave_agent.model.loader import load_model  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate clinical data with the LLM.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--folder", action="append",
                        help="limit to these folder OIDs (repeatable)")
    parser.add_argument("--form", action="append",
                        help="limit to these form OIDs (repeatable)")
    parser.add_argument("--max-forms", type=int, help="stop after N forms")
    parser.add_argument("--regenerate", action="store_true",
                        help="ignore cached values and call the LLM again (FR-6.8)")
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def _resolve_folders(config, model, requested: list[str] | None) -> list[str]:
    """Which visits to populate (OQ-6): all assigned, or a configured subset."""
    if requested:
        return requested

    visits = config.get("generation.visits") or {}
    if visits.get("mode") == "subset" and visits.get("include"):
        chosen = list(visits["include"])
    else:
        chosen = list(model.seed_folder_oids)

    excluded = set(visits.get("exclude") or [])
    return [oid for oid in chosen if oid not in excluded]


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
                      log_file=config.study_output_dir / "logs" / f"generate_{stamp}.log")

    model_dir = config.study_output_dir / "model"
    try:
        model = load_model(model_dir / "study_model.json")
    except FileNotFoundError:
        print(f"\nNo study model. Run: python scripts/run_model.py --study {args.study}\n",
              file=sys.stderr)
        return 2

    graph_path = model_dir / "dynamics_graph.json"
    graph = DynamicsGraph(study_name=model.study_name, crf_version_oid=model.crf_version_oid)
    if graph_path.is_file():
        from rave_agent.model.dynamics_graph import ActivationEdge, Condition
        raw = json.loads(graph_path.read_text(encoding="utf-8"))
        for edge in raw.get("edges") or []:
            condition = edge.get("condition")
            graph.add_edge(ActivationEdge(
                target_type=edge["target_type"], target_oid=edge["target_oid"],
                classification=edge["classification"],
                condition=Condition(**condition) if condition else None,
                source=edge.get("source", ""), action_type=edge.get("action_type", ""),
                check_name=edge.get("check_name", ""), note=edge.get("note", ""),
            ))

    try:
        secrets = load_secrets(Path(args.env_file), require_anthropic=True)
    except MissingSecretError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    llm = LlmClient(
        api_key=secrets.anthropic_api_key,
        model=str(config.get("generation.model")),
        max_tokens=int(config.get("generation.max_tokens") or 16000),
    )

    generator = Generator(model, graph, config, llm, regenerate=args.regenerate)
    folders = _resolve_folders(config, model, args.folder)

    print(f"\nStudy    : {config.study_env}")
    print(f"Subject  : {args.subject}")
    print(f"Model    : {config.get('generation.model')}")
    print(f"Area     : {config.get('generation.therapeutic_area')}")
    print(f"Strategy : {config.get('dynamics.trigger_strategy')}")
    print(f"Folders  : {', '.join(folders)}")
    print(f"Cache    : {'BYPASSED (--regenerate)' if args.regenerate else 'enabled'}\n")

    if args.form:
        wanted = set(args.form)
        for folder_oid in folders:
            folder = model.folders.get(folder_oid)
            if folder:
                folder.forms = [a for a in folder.forms if a.form_oid in wanted]

    result = generator.generate_subject(args.subject, folders, max_forms=args.max_forms)

    width = max((len(o.form_oid) for o in result.outcomes), default=12)
    print(f"{'FOLDER'.ljust(14)}  {'FORM'.ljust(width)}  STATUS     N  DETAIL")
    print(f"{'-' * 14}  {'-' * width}  ---------  -  ------------------------------")
    for outcome in result.outcomes:
        count = len(outcome.records) if outcome.records else len(outcome.values)
        print(f"{outcome.folder_oid.ljust(14)}  {outcome.form_oid.ljust(width)}  "
              f"{outcome.status:<9}  {count:<1}  {outcome.detail[:40]}")
        for violation in outcome.violations[:3]:
            print(f"{' ' * (16 + width)}   ! {violation[:70]}")

    print(f"\nCounts : {result.counts()}")
    print(f"Tokens : {result.token_usage}")
    if generator.unsatisfiable_triggers:
        print("Note   : triggers the CRF version cannot satisfy - "
              + "; ".join(f"{k}={sorted(v)}" for k, v in
                          sorted(generator.unsatisfiable_triggers.items())))
    for warning in result.warnings:
        print(f"Note   : {warning}")

    summary = config.study_output_dir / "generated" / args.subject / "_summary.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({
        "subject": args.subject,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": result.counts(),
        "token_usage": result.token_usage,
        "forms": [
            {"folder": o.folder_oid, "form": o.form_oid, "status": o.status,
             "attempts": o.attempts, "violations": o.violations, "detail": o.detail}
            for o in result.outcomes
        ],
    }, indent=2), encoding="utf-8")
    print(f"Wrote  : {summary}")

    return 1 if result.counts().get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
