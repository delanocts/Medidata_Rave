#!/usr/bin/env python
"""A8 - dynamics resolution loop (FR-8, ARC-2).

Generates and submits the seed set, then repeatedly fills whatever the trigger
values unlocked, until nothing new activates.

    python scripts/run_dynamics.py --study <name> --subject TST-001
    python scripts/run_dynamics.py --study <name> --subject TST-001 --dry-run
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
from rave_agent.dynamics.resolver import (  # noqa: E402
    DynamicsResolver,
    predicted_folders,
    submitted_values,
)
from rave_agent.generation.generator import Generator  # noqa: E402
from rave_agent.generation.llm_client import LlmClient  # noqa: E402
from rave_agent.model.dynamics_graph import ActivationEdge, Condition, DynamicsGraph  # noqa: E402
from rave_agent.model.loader import load_model  # noqa: E402
from rave_agent.rave.client import RaveClient  # noqa: E402
from rave_agent.submission.submitter import Submitter  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive the dynamics loop for a subject.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def load_graph(path: Path, model) -> DynamicsGraph:
    graph = DynamicsGraph(study_name=model.study_name, crf_version_oid=model.crf_version_oid)
    if not path.is_file():
        return graph
    raw = json.loads(path.read_text(encoding="utf-8"))
    for edge in raw.get("edges") or []:
        condition = edge.get("condition")
        graph.add_edge(ActivationEdge(
            target_type=edge["target_type"], target_oid=edge["target_oid"],
            classification=edge["classification"],
            condition=Condition(**condition) if condition else None,
            source=edge.get("source", ""), action_type=edge.get("action_type", ""),
            check_name=edge.get("check_name", ""), note=edge.get("note", ""),
        ))
    return graph


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
                      log_file=config.study_output_dir / "logs" / f"dynamics_{stamp}.log")

    model_dir = config.study_output_dir / "model"
    try:
        model = load_model(model_dir / "study_model.json")
    except FileNotFoundError:
        print(f"\nNo study model. Run: python scripts/run_model.py --study {args.study}\n",
              file=sys.stderr)
        return 2

    graph = load_graph(model_dir / "dynamics_graph.json", model)

    try:
        secrets = load_secrets(Path(args.env_file), require_anthropic=True)
    except MissingSecretError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    llm = LlmClient(api_key=secrets.anthropic_api_key,
                    model=str(config.get("generation.model")),
                    max_tokens=int(config.get("generation.max_tokens") or 16000))
    generator = Generator(model, graph, config, llm, regenerate=args.regenerate)
    client = RaveClient(config, secrets)
    submitter = Submitter(client,
                          archive_root=config.study_output_dir / "submissions",
                          dry_run=dry_run)
    site_oid = str(config.get("site.number"))

    resolver = DynamicsResolver(model, graph, config, generator, submitter, site_oid)

    print(f"\nStudy      : {config.study_env}")
    print(f"Subject    : {args.subject} at site {site_oid}")
    print(f"Seed set   : {len(model.seed_folder_oids)} folder(s)")
    print(f"Max passes : {config.get('dynamics.max_iterations')}")
    print(f"Mode       : {'DRY RUN' if dry_run else 'LIVE'}\n")

    state = resolver.resolve(args.subject)

    print(f"{'PASS':<6}{'ATTEMPTED':<11}{'SUBMITTED':<11}{'REJECTED':<10}"
          f"{'DISCARDED':<11}NEW FOLDERS")
    print(f"{'-' * 5} {'-' * 10} {'-' * 10} {'-' * 9} {'-' * 10} {'-' * 30}")
    for entry in state.history:
        new = ", ".join(entry.get("newly_activated") or []) or "-"
        print(f"{entry['pass']:<6}{len(entry.get('folders_attempted') or []):<11}"
              f"{entry.get('forms_submitted', 0):<11}{entry.get('forms_rejected', 0):<10}"
              f"{entry.get('forms_discarded', 0):<11}{new[:40]}")

    discarded = sum(e.get("forms_discarded", 0) for e in state.history)
    if discarded:
        print()
        print(f"{discarded} form(s) were generated for a visit that turned "
              f"out absent - the cost of generation.lookahead_folders.")

    values = submitted_values(config.study_output_dir / "generated", args.subject)
    predicted = predicted_folders(graph, values)
    never = sorted(set(predicted) - set(state.active_folders))

    print(f"\nActive folders   : {len(state.active_folders)} / {len(model.folders)}")
    print(f"  {', '.join(state.active_folders)}")
    print(f"Predicted by ALS : {len(predicted)}")
    if never:
        print(f"Predicted but never activated ({len(never)}): {', '.join(never)}")
        print("  (FR-8.7 - likely a custom function, an unmet Or-branch, or permissions)")

    populated = sum(len(s.populated_forms) for s in state.folders.values())
    print(f"\nForms populated  : {populated}")
    if generator.unsatisfiable_triggers:
        print("\nTriggers the CRF version cannot satisfy:")
        for oid, values in sorted(generator.unsatisfiable_triggers.items()):
            print(f"  {oid} - the ALS wants {sorted(values)}, not in its codelist")
        print("  (the edit check predates this CRF version; those targets stay unreachable)")

    print(f"Tokens           : {llm.usage.to_dict()}")
    print(f"State            : {resolver._state_path(args.subject)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
