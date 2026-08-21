"""ALS workbook -> edit checks, derivations, matrices and custom functions (FR-3.2).

Rave Architect exports the ALS as SpreadsheetML (see `spreadsheetml.py`). The
tabs that matter here:

  Checks / CheckSteps / CheckActions  the edit-check logic and what it fires
  Derivations / DerivationSteps       derived fields
  CustomFunctions                     C# / SQL source, keyed by name
  Matrices + Matrix<n>#<OID>          folder x form assignment grids
  Folders / Forms / Fields            structural detail

CheckSteps encode the condition as a **postfix (RPN) expression**: operand steps
carry a field reference or a literal, operator steps carry a `checkfunction`.
`_parse_rpn` turns that back into a tree, and `_conjuncts` flattens the common
"all of these must hold" case into concrete field=value assignments the
generator can actually target.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..utils.logging import get_logger
from .spreadsheetml import Workbook

log = get_logger(__name__)

# Operators and how many operands they consume.
_UNARY = {
    "IsEmpty", "IsNotEmpty", "IsPresent", "IsNotPresent", "Not",
    "IsMissingValue", "IsNotMissingValue", "IsBlank", "IsNotBlank",
}
_BINARY = {
    "IsEqualTo", "IsNotEqualTo", "IsGreaterThan", "IsLessThan",
    "IsGreaterThanOrEqualTo", "IsLessThanOrEqualTo", "And", "Or",
    "Contains", "DoesNotContain", "StartsWith", "IsInDataDictionary",
    "IsNotInDataDictionary", "Plus", "Minus", "Multiply", "Divide",
}
# Operators that assert a value, so they can be inverted into an assignment.
_ASSIGNING = {"IsEqualTo"}

ACTIVATING_ACTIONS = {
    "AddForm": "form",
    "AddMatrix": "matrix",
    "MrgMatrix": "matrix",
    "OldMrgMatrix": "matrix",
    "SetDataPointVisible": "field",
}


@dataclass
class FieldRef:
    field_oid: str = ""
    form_oid: str = ""
    folder_oid: str = ""
    variable_oid: str = ""
    data_format: str = ""
    record_position: str = ""

    @property
    def qualified(self) -> str:
        """FORM.FIELD, matching the ItemDef OID convention in the ODM."""
        if self.form_oid and self.field_oid:
            return f"{self.form_oid}.{self.field_oid}"
        return self.field_oid or self.variable_oid


@dataclass
class Assignment:
    """A concrete field=value the generator can set to satisfy a condition."""
    field_oid: str          # qualified FORM.FIELD
    value: str
    operator: str = "IsEqualTo"
    form_oid: str = ""
    folder_oid: str = ""


@dataclass
class ActivationRecord:
    """One activating edit-check action, with the condition that fires it."""
    check_name: str
    action_type: str
    target_kind: str                     # form | matrix | field
    target_oid: str                      # form OID, matrix OID, or field OID
    active: bool = True
    infix: str = ""
    assignments: list[Assignment] = field(default_factory=list)
    condition_complete: bool = True      # False when Or/negation made it partial
    uses_custom_function: bool = False
    custom_function_names: list[str] = field(default_factory=list)
    source_field_oid: str = ""           # field the action hangs off
    source_form_oid: str = ""
    source_folder_oid: str = ""


@dataclass
class AlsModel:
    path: str = ""
    draft_name: str = ""
    activations: list[ActivationRecord] = field(default_factory=list)
    # matrix OID -> folder OID -> [form OIDs]
    matrices: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    matrix_names: dict[str, str] = field(default_factory=dict)
    folder_names: dict[str, str] = field(default_factory=dict)
    custom_functions: dict[str, str] = field(default_factory=dict)
    derivations: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "draft_name": self.draft_name,
            "counts": self.counts,
            "matrix_names": self.matrix_names,
            "folder_names": self.folder_names,
            "matrices": self.matrices,
            "activations": [asdict(a) for a in self.activations],
            "derivations": self.derivations,
            "custom_functions": {
                # source can be long; keep it but note the size
                name: source for name, source in self.custom_functions.items()
            },
            "warnings": self.warnings,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# RPN handling
# ---------------------------------------------------------------------------

def _classify_step(step: dict[str, str]) -> tuple[str, Any]:
    """Return ('op'|'field'|'literal'|'custom', payload) for one CheckStep."""
    function = (step.get("checkfunction") or "").strip()
    if function:
        return "op", function

    custom = (step.get("customfunction") or "").strip()
    if custom:
        return "custom", custom

    field_oid = (step.get("fieldoid") or "").strip()
    form_oid = (step.get("formoid") or "").strip()
    if field_oid or form_oid:
        return "field", FieldRef(
            field_oid=field_oid,
            form_oid=form_oid,
            folder_oid=(step.get("folderoid") or "").strip(),
            variable_oid=(step.get("variableoid") or "").strip(),
            data_format=(step.get("dataformat") or "").strip(),
            record_position=(step.get("recordposition") or "").strip(),
        )

    return "literal", (step.get("staticvalue") or "").strip()


def _parse_rpn(steps: list[dict[str, str]]) -> tuple[Any, bool]:
    """Fold postfix CheckSteps into a nested tree.

    Returns (tree, complete). `complete` is False if the stack did not resolve
    cleanly - the condition is then treated as only partially understood rather
    than being silently misread.
    """
    stack: list[Any] = []
    custom_used: list[str] = []
    ordered = sorted(steps, key=lambda s: int(s.get("stepordinal") or 0))

    for step in ordered:
        kind, payload = _classify_step(step)
        if kind in ("field", "literal"):
            stack.append(payload)
        elif kind == "custom":
            custom_used.append(payload)
            stack.append({"custom_function": payload})
        else:  # operator
            if payload in _UNARY:
                if not stack:
                    return {"unresolved": ordered}, False
                stack.append({"op": payload, "args": [stack.pop()]})
            elif payload in _BINARY:
                if len(stack) < 2:
                    return {"unresolved": ordered}, False
                right = stack.pop()
                left = stack.pop()
                stack.append({"op": payload, "args": [left, right]})
            else:
                # Unknown function: consume one operand conservatively.
                arg = stack.pop() if stack else None
                stack.append({"op": payload, "args": [arg]})

    if len(stack) != 1:
        return {"unresolved": ordered, "stack_depth": len(stack)}, False
    return stack[0], True


def _conjuncts(tree: Any) -> tuple[list[Assignment], bool]:
    """Extract field=value assignments from a condition tree.

    Only pure conjunctions of equalities are fully invertible. An `Or` means
    several ways to satisfy the condition, so the first branch is taken and the
    result is flagged incomplete - the caller records that rather than pretending
    the condition is fully known.
    """
    assignments: list[Assignment] = []
    complete = True

    def walk(node: Any) -> None:
        nonlocal complete
        if not isinstance(node, dict) or "op" not in node:
            return
        operator = node["op"]
        args = node.get("args") or []

        if operator == "And":
            for arg in args:
                walk(arg)
            return

        if operator == "Or":
            # Satisfying either branch works; take the first and flag partial.
            complete = False
            if args:
                walk(args[0])
            return

        if operator in _ASSIGNING and len(args) == 2:
            left, right = args
            if isinstance(left, FieldRef) and isinstance(right, str):
                assignments.append(Assignment(
                    field_oid=left.qualified, value=right, operator=operator,
                    form_oid=left.form_oid, folder_oid=left.folder_oid,
                ))
                return

        # Any other operator (IsNotEmpty, ranges, custom functions...) cannot be
        # turned into a single concrete value.
        complete = False

    walk(tree)
    return assignments, complete


def _collect_custom_functions(tree: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "custom_function" in node:
                found.append(node["custom_function"])
            for arg in node.get("args") or []:
                walk(arg)

    walk(tree)
    return found


# ---------------------------------------------------------------------------
# Matrix grids
# ---------------------------------------------------------------------------

def _parse_matrix_sheet(workbook: Workbook, sheet: str) -> dict[str, list[str]]:
    """A matrix tab is a grid: rows are forms, columns are folders, X = assigned."""
    rows = list(workbook.rows(sheet))
    if not rows:
        return {}
    header = rows[0]
    # Column 0 is the matrix label, column 1 is "Subject"; folders start after.
    folders = {index: name.strip() for index, name in enumerate(header)
               if index >= 2 and name.strip()}
    assignment: dict[str, list[str]] = {oid: [] for oid in folders.values()}

    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        form_oid = row[0].strip()
        for index, folder_oid in folders.items():
            if index < len(row) and row[index].strip().upper() == "X":
                assignment[folder_oid].append(form_oid)

    return {oid: forms for oid, forms in assignment.items() if forms}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_als(path: Path) -> AlsModel:
    """Parse an ALS workbook into the activation and structure model."""
    workbook = Workbook(path)
    model = AlsModel(path=str(path))

    required = ("Checks", "CheckSteps", "CheckActions")
    missing = [sheet for sheet in required if not workbook.has_sheet(sheet)]
    if missing:
        model.warnings.append(
            f"ALS is missing expected sheet(s): {missing}. Dynamics cannot be derived from it."
        )
        return model

    if workbook.has_sheet("CRFDraft"):
        drafts = workbook.records("CRFDraft")
        if drafts:
            model.draft_name = drafts[0].get("draftname") or drafts[0].get("projectname") or ""

    # -- custom functions -------------------------------------------------
    for record in workbook.records("CustomFunctions") if workbook.has_sheet("CustomFunctions") else []:
        name = (record.get("functionname") or "").strip()
        if name:
            model.custom_functions[name] = record.get("sourcecode") or ""

    # -- matrices ---------------------------------------------------------
    for record in workbook.records("Matrices") if workbook.has_sheet("Matrices") else []:
        oid = (record.get("oid") or "").strip()
        if oid:
            model.matrix_names[oid] = (record.get("matrixname") or "").strip()

    for sheet in workbook.sheet_names:
        if not sheet.startswith("Matrix") or "#" not in sheet:
            continue
        matrix_oid = sheet.split("#", 1)[1]
        # Sheet names strip punctuation from the OID; map back via the OID list.
        resolved = next(
            (oid for oid in model.matrix_names
             if "".join(ch for ch in oid if ch.isalnum()).upper() == matrix_oid.upper()),
            matrix_oid,
        )
        grid = _parse_matrix_sheet(workbook, sheet)
        if grid:
            model.matrices[resolved] = grid

    # -- folders ----------------------------------------------------------
    for record in workbook.records("Folders") if workbook.has_sheet("Folders") else []:
        oid = (record.get("oid") or "").strip()
        if oid:
            model.folder_names[oid] = (record.get("foldername") or "").strip()

    # -- derivations ------------------------------------------------------
    if workbook.has_sheet("Derivations"):
        model.derivations = [
            {
                "name": r.get("derivationname", ""),
                "active": (r.get("active") or "").upper() == "TRUE",
                "form_oid": r.get("formoid", ""),
                "field_oid": r.get("fieldoid", ""),
                # When FormOID/FieldOID are blank the target is named only by
                # VariableOID, so it has to be resolved by variable name.
                "variable_oid": r.get("variableoid", ""),
                "folder_oid": r.get("folderoid", ""),
            }
            for r in workbook.records("Derivations")
        ]

    # -- checks -----------------------------------------------------------
    checks = {r.get("checkname", ""): r for r in workbook.records("Checks")}

    steps_by_check: dict[str, list[dict[str, str]]] = {}
    for record in workbook.records("CheckSteps"):
        steps_by_check.setdefault(record.get("checkname", ""), []).append(record)

    activating = 0
    for action in workbook.records("CheckActions"):
        action_type = (action.get("actiontype") or "").strip()
        target_kind = ACTIVATING_ACTIONS.get(action_type)
        if target_kind is None:
            continue

        check_name = action.get("checkname", "")
        check = checks.get(check_name, {})

        if target_kind == "matrix":
            target_oid = (action.get("actionoptions") or "").strip()
        elif target_kind == "form":
            target_oid = (action.get("formoid") or "").strip()
        else:  # field
            form_oid = (action.get("formoid") or "").strip()
            field_oid = (action.get("fieldoid") or "").strip()
            target_oid = f"{form_oid}.{field_oid}" if form_oid and field_oid else field_oid

        if not target_oid:
            model.warnings.append(
                f"check {check_name}: {action_type} action has no resolvable target")
            continue

        tree, parsed = _parse_rpn(steps_by_check.get(check_name, []))
        assignments, complete = _conjuncts(tree) if parsed else ([], False)
        custom_names = _collect_custom_functions(tree) if parsed else []

        model.activations.append(ActivationRecord(
            check_name=check_name,
            action_type=action_type,
            target_kind=target_kind,
            target_oid=target_oid,
            active=(check.get("checkactive") or "TRUE").upper() == "TRUE",
            infix=(check.get("infix") or "")[:500],
            assignments=assignments,
            condition_complete=parsed and complete,
            uses_custom_function=bool(custom_names),
            custom_function_names=custom_names,
            source_field_oid=(action.get("fieldoid") or "").strip(),
            source_form_oid=(action.get("formoid") or "").strip(),
            source_folder_oid=(action.get("folderoid") or "").strip(),
        ))
        activating += 1

    model.counts = {
        "checks": len(checks),
        "check_steps": sum(len(v) for v in steps_by_check.values()),
        "activating_actions": activating,
        "matrices": len(model.matrices),
        "custom_functions": len(model.custom_functions),
        "derivations": len(model.derivations),
        "fully_resolved_conditions": sum(
            1 for a in model.activations if a.condition_complete and a.assignments),
        "partial_conditions": sum(1 for a in model.activations if not a.condition_complete),
    }
    log.info("ALS parsed", extra=model.counts)
    return model
