"""The dynamics graph (FR-3.4 - FR-3.7).

Nodes are folders, forms, item groups and fields. An edge means "target is
activated by source field satisfying condition".

Edge sources, in precedence order:
  1. config dynamics.overrides / dynamics.custom_function_overrides (always win)
  2. ALS edit-check actions, when an ALS workbook has been supplied
  3. nothing else - RWS does not expose edit checks (see the acquisition report)

Targets that cannot be explained by any source are classified `unresolvable` and
listed explicitly rather than dropped (FR-3.5).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..utils.logging import get_logger
from .study_model import StudyModel

log = get_logger(__name__)

# FR-3.5 classifications
STATIC = "static"
BY_EDIT_CHECK = "dynamic_by_edit_check"
BY_DERIVATION = "dynamic_by_derivation"
BY_MATRIX_ADD = "dynamic_by_matrix_add"
UNRESOLVABLE = "unresolvable"

# Edit-check actions that activate something (the rest only raise queries etc.)
ACTIVATING_ACTIONS = {
    "AddForm": "form",
    "AddMatrix": "matrix",
    "MrgMatrix": "matrix",
    "OldMrgMatrix": "matrix",
    "SetDataPointVisible": "field",
}


@dataclass
class Condition:
    """What must hold for the edge to fire.

    `assignments` is the list of field=value pairs that together satisfy it.
    `complete` is False when the underlying expression contained an Or or an
    operator that cannot be inverted into a concrete value, in which case
    satisfying the listed assignments may not be sufficient.
    """
    assignments: list[dict] = field(default_factory=list)
    complete: bool = True
    infix: str = ""

    @property
    def field_oids(self) -> list[str]:
        return [a["field_oid"] for a in self.assignments]

    def describe(self) -> str:
        if not self.assignments:
            return "(condition not invertible)"
        joined = " AND ".join(f"{a['field_oid']}={a['value']!r}" for a in self.assignments)
        return joined if self.complete else f"{joined} (partial)"


@dataclass
class ActivationEdge:
    target_type: str           # folder | form | item_group | field | matrix
    target_oid: str
    classification: str
    condition: Condition | None = None
    source: str = ""           # als | config | config_custom_function
    action_type: str = ""
    check_name: str = ""
    note: str = ""


@dataclass
class UnresolvableTarget:
    target_type: str
    target_oid: str
    reason: str
    hint: str = ""


@dataclass
class DynamicsGraph:
    study_name: str
    crf_version_oid: str
    enabled: bool = True
    has_edit_check_source: bool = False
    edges: list[ActivationEdge] = field(default_factory=list)
    unresolvable: list[UnresolvableTarget] = field(default_factory=list)
    trigger_fields: dict[str, list[str]] = field(default_factory=dict)
    custom_functions: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def add_edge(self, edge: ActivationEdge) -> None:
        self.edges.append(edge)
        if edge.condition:
            for field_oid in edge.condition.field_oids:
                targets = self.trigger_fields.setdefault(field_oid, [])
                if edge.target_oid not in targets:
                    targets.append(edge.target_oid)

    def edges_for_trigger(self, field_oid: str) -> list[ActivationEdge]:
        return [e for e in self.edges if e.condition and field_oid in e.condition.field_oids]

    def required_values(self) -> dict[str, set[str]]:
        """field OID -> values that activate something. Drives `maximize` (FR-6.6)."""
        wanted: dict[str, set[str]] = {}
        for edge in self.edges:
            if not edge.condition:
                continue
            for assignment in edge.condition.assignments:
                wanted.setdefault(assignment["field_oid"], set()).add(assignment["value"])
        return wanted

    def targets_of_type(self, target_type: str) -> list[ActivationEdge]:
        return [e for e in self.edges if e.target_type == target_type]

    def detect_cycles(self, max_depth: int = 25) -> list[list[str]]:
        """Find activation cycles so traversal can be capped (FR-3.6)."""
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            if edge.condition:
                for field_oid in edge.condition.field_oids:
                    adjacency.setdefault(field_oid, set()).add(edge.target_oid)

        cycles: list[list[str]] = []
        seen_cycles: set[tuple[str, ...]] = set()

        def walk(node: str, path: list[str], depth: int) -> None:
            if depth > max_depth:
                return
            for nxt in adjacency.get(node, ()):
                if nxt in path:
                    cycle = path[path.index(nxt):] + [nxt]
                    key = tuple(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
                    continue
                walk(nxt, path + [nxt], depth + 1)

        for start in list(adjacency):
            walk(start, [start], 0)
        return cycles

    def stats(self) -> dict[str, Any]:
        by_class: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for edge in self.edges:
            by_class[edge.classification] = by_class.get(edge.classification, 0) + 1
            by_type[edge.target_type] = by_type.get(edge.target_type, 0) + 1
        return {
            "edges": len(self.edges),
            "trigger_fields": len(self.trigger_fields),
            "unresolvable": len(self.unresolvable),
            "by_classification": by_class,
            "by_target_type": by_type,
            "has_edit_check_source": self.has_edit_check_source,
            "custom_functions": len(self.custom_functions),
            "incomplete_conditions": sum(
                1 for e in self.edges if e.condition and not e.condition.complete),
        }

    def to_dict(self) -> dict:
        return {
            "study_name": self.study_name,
            "crf_version_oid": self.crf_version_oid,
            "enabled": self.enabled,
            "has_edit_check_source": self.has_edit_check_source,
            "stats": self.stats(),
            "cycles": self.detect_cycles(),
            "trigger_fields": self.trigger_fields,
            "required_values": {k: sorted(v) for k, v in self.required_values().items()},
            "custom_functions": self.custom_functions,
            "edges": [asdict(e) for e in self.edges],
            "unresolvable": [asdict(u) for u in self.unresolvable],
            "warnings": self.warnings,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
def build_graph(model: StudyModel, config_dynamics: dict | None = None, als=None) -> DynamicsGraph:
    """Assemble the graph from the ALS and any config overrides."""
    config_dynamics = config_dynamics or {}
    graph = DynamicsGraph(
        study_name=model.study_name,
        crf_version_oid=model.crf_version_oid,
        enabled=bool(config_dynamics.get("enabled", True)),
        has_edit_check_source=als is not None and bool(als.activations),
    )

    if als is not None:
        _add_als_activations(graph, als)
        graph.custom_functions.update(als.custom_functions or {})

    # Config overrides always win (FR-6.6).
    for override in config_dynamics.get("custom_function_overrides") or []:
        condition = Condition(
            assignments=[{
                "field_oid": override["trigger_field_oid"],
                "value": str(override["trigger_value"]),
                "operator": "IsEqualTo",
                "form_oid": "",
                "folder_oid": "",
            }],
            complete=True,
            infix=override.get("note", ""),
        )
        for key, target_type in (("activates_form_oid", "form"),
                                 ("activates_folder_oid", "folder")):
            target = override.get(key)
            if target:
                graph.add_edge(ActivationEdge(
                    target_type=target_type,
                    target_oid=target,
                    classification=BY_EDIT_CHECK,
                    condition=condition,
                    source="config_custom_function",
                    action_type="CustomFunction",
                    check_name=override.get("function_name", ""),
                    note=override.get("note", "declared in config"),
                ))

    _mark_unresolvable(graph, model, als is not None)
    return graph


def _add_als_activations(graph: DynamicsGraph, als) -> None:
    """One edge per activating action, plus the folders/forms a matrix brings in."""
    for record in als.activations:
        if not record.active:
            continue

        condition = Condition(
            assignments=[
                {
                    "field_oid": a.field_oid,
                    "value": a.value,
                    "operator": a.operator,
                    "form_oid": a.form_oid,
                    "folder_oid": a.folder_oid,
                }
                for a in record.assignments
            ],
            complete=record.condition_complete,
            infix=record.infix,
        )
        note = ("uses custom function(s): " + ", ".join(record.custom_function_names)
                if record.uses_custom_function else "")

        graph.add_edge(ActivationEdge(
            target_type=record.target_kind,
            target_oid=record.target_oid,
            classification=(BY_MATRIX_ADD if record.target_kind == "matrix" else BY_EDIT_CHECK),
            condition=condition,
            source="als",
            action_type=record.action_type,
            check_name=record.check_name,
            note=note,
        ))

        if record.target_kind != "matrix":
            continue

        # Merging a matrix activates every folder and form the matrix declares.
        for folder_oid, form_oids in (als.matrices.get(record.target_oid) or {}).items():
            graph.add_edge(ActivationEdge(
                target_type="folder",
                target_oid=folder_oid,
                classification=BY_MATRIX_ADD,
                condition=condition,
                source="als",
                action_type=record.action_type,
                check_name=record.check_name,
                note=f"via matrix {record.target_oid}",
            ))
            for form_oid in form_oids:
                graph.add_edge(ActivationEdge(
                    target_type="form",
                    target_oid=form_oid,
                    classification=BY_MATRIX_ADD,
                    condition=condition,
                    source="als",
                    action_type=record.action_type,
                    check_name=record.check_name,
                    note=f"via matrix {record.target_oid} in folder {folder_oid}",
                ))


def _mark_unresolvable(graph: DynamicsGraph, model: StudyModel, has_als: bool) -> None:
    """Record every reachable-but-unexplained target (FR-3.5)."""
    explained = {e.target_oid for e in graph.edges}
    seed = set(model.seed_folder_oids)

    for matrix in model.non_default_matrices:
        if matrix.oid not in explained:
            graph.unresolvable.append(UnresolvableTarget(
                target_type="matrix",
                target_oid=matrix.oid,
                reason="no activating edit-check action targets this matrix",
                hint=("Supply the ALS workbook to derive it." if not has_als else
                      "It may be added by a custom function or assigned manually."),
            ))

    for folder_oid in sorted({o for m in model.non_default_matrices for o in m.folder_oids} - seed):
        if folder_oid not in explained:
            graph.unresolvable.append(UnresolvableTarget(
                target_type="folder",
                target_oid=folder_oid,
                reason="reachable through a matrix with no known trigger",
                hint="Will still be detected empirically once it activates (FR-8.3).",
            ))

    for form_oid in model.unassigned_forms():
        if form_oid not in explained:
            graph.unresolvable.append(UnresolvableTarget(
                target_type="form",
                target_oid=form_oid,
                reason="assigned to no folder in any matrix, and no AddForm action targets it",
                hint="May be added by a custom function.",
            ))

    partial = [e for e in graph.edges if e.condition and not e.condition.complete]
    if partial:
        graph.warnings.append(
            f"{len(partial)} activation condition(s) contain an Or or a non-invertible "
            "operator, so the listed field values may not be sufficient on their own."
        )
    if not has_als:
        graph.warnings.append(
            "No edit-check source was available, so no activation conditions could be "
            "predicted. Dynamics will be discovered empirically by the A8 loop instead."
        )
