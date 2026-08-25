#!/usr/bin/env python
"""A3 - standalone study model build (FR-3, ARC-2).

Reads only on-disk artifacts written by A2; makes no network calls.

    python scripts/run_model.py --study <study-config-name>
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
from rave_agent.metadata.manifest import MetadataManifest  # noqa: E402
from rave_agent.model.dynamics_graph import build_graph  # noqa: E402
from rave_agent.metadata.observed_structure import ObservedStructure  # noqa: E402
from rave_agent.model.matrix_resolver import (  # noqa: E402
    apply_als_derivations,
    apply_als_dictionaries,
    apply_als_matrices,
    apply_observed_structure,
    apply_version_folders,
    finalise_assignments,
    resolve_primary_form_placement,
    summarise,
)
from rave_agent.model.odm_parser import parse_odm  # noqa: E402
from rave_agent.utils.logging import configure_logging  # noqa: E402


def model_dir_for(config) -> Path:
    return config.study_output_dir / "model"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build study_model.json and dynamics_graph.json.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--config", default=str(REPO_ROOT / "config"))
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
    configure_logging(level=level, log_file=config.study_output_dir / "logs" / f"model_{stamp}.log")

    metadata_dir = config.study_output_dir / "metadata"
    manifest = MetadataManifest.load(
        metadata_dir / "metadata_manifest.json", config.study_name, config.environment
    )

    odm_record = manifest.get("odm_metadata")
    if odm_record is None:
        print(f"\nNo ODM metadata found in {metadata_dir}.\n"
              f"Run: python scripts/run_metadata.py --study {args.study}\n", file=sys.stderr)
        return 2

    odm_path = metadata_dir / odm_record.filename
    print(f"\nStudy    : {config.study_env}")
    print(f"Metadata : {odm_path.name}")

    if args.dry_run:
        print("--dry-run: inputs resolved; model not built.")
        return 0

    model = parse_odm(odm_path, config.study_name, config.environment)

    folders_record = manifest.get("version_folders")
    if folders_record:
        apply_version_folders(model, metadata_dir / folders_record.filename)
    else:
        model.warnings.append("version_folders.xml absent; only the default matrix is known")

    observed = ObservedStructure.load(metadata_dir / "observed_structure.json")
    if observed and observed.folder_forms:
        apply_observed_structure(model, observed)
        print(f"Observed : {len(observed.folder_forms)} folder(s) learned from "
              f"{len(observed.subjects_sampled)} existing subject(s)")
    else:
        print("Observed : no subject observations available")

    als_record = manifest.get("als")
    als = None
    if als_record:
        from rave_agent.model.als_parser import parse_als
        als = parse_als(metadata_dir / als_record.filename)
        apply_als_matrices(model, als)
        apply_als_derivations(model, als)
        apply_als_dictionaries(model, als)
        print(f"ALS      : {als_record.filename} - "
              f"{als.counts.get('activating_actions', 0)} activating action(s), "
              f"{als.counts.get('matrices', 0)} matrix grid(s), "
              f"{als.counts.get('custom_functions', 0)} custom function(s)")
        als.save(model_dir_for(config) / "als_model.json")
    else:
        print("ALS      : not supplied - dynamics will be discovered empirically")

    resolve_primary_form_placement(model, observed)
    finalise_assignments(model)

    graph = build_graph(model, config.get("dynamics") or {}, als)

    model_dir = model_dir_for(config)
    model_path = model.save(model_dir / "study_model.json")
    graph_path = graph.save(model_dir / "dynamics_graph.json")
    matrix_path = model_dir / "matrix_summary.json"
    matrix_path.write_text(json.dumps(summarise(model), indent=2), encoding="utf-8")

    print(f"\nCRF version : {model.crf_version_oid} ({model.crf_version_name})")
    print(f"Entry point : {model.primary_form_oid} in folder {model.primary_form_folder_oid}"
          "   (mdsol:PrimaryFormOID)")
    print(f"Seed matrix : {model.default_matrix_oid}")

    print("\nStudy model:")
    for key, value in model.stats().items():
        print(f"  {key.replace('_', ' '):<22} {value}")

    matrix = summarise(model)
    print(f"\nSeed folders ({matrix['seed_folder_count']}): {', '.join(matrix['seed_folders'])}")
    print(f"Reachable folders       : {matrix['reachable_folder_count']}")
    print(f"Beyond the seed set     : {len(matrix['beyond_seed'])}")

    print("\nDynamics graph:")
    for key, value in graph.stats().items():
        print(f"  {key.replace('_', ' '):<22} {value}")

    top = sorted(graph.trigger_fields.items(), key=lambda kv: len(kv[1]), reverse=True)[:8]
    if top:
        print("\nMost powerful trigger fields (activations each unlocks):")
        required = graph.required_values()
        for field_oid, targets in top:
            values = ", ".join(sorted(required.get(field_oid, [])))
            print(f"  {field_oid:<34} {len(targets):>4}  set to: {values}")

    if model.warnings or graph.warnings:
        print("\nWarnings:")
        for warning in (model.warnings + graph.warnings)[:12]:
            print(f"  - {warning}")
        total = len(model.warnings) + len(graph.warnings)
        if total > 12:
            print(f"  ... and {total - 12} more (see {model_path.name})")

    print(f"\nWrote : {model_path}")
    print(f"        {graph_path}")
    print(f"        {matrix_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
