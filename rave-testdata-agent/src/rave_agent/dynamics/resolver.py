"""A8 - the dynamics resolution loop (FR-8).

Pass 0 fills the seed folders. Each later pass asks the graph which folders the
values written so far should have unlocked, tries to fill those, and treats
acceptance as proof of activation. The loop stops when a pass activates nothing
new, or `dynamics.max_iterations` is reached.

Already-populated forms are never rewritten (FR-8.4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config.loader import Config
from ..generation.generator import Generator
from ..model.dynamics_graph import DynamicsGraph
from ..model.study_model import StudyModel
from ..submission.odm_builder import FormPayload, OdmBuildError, build_clinical_odm
from ..submission.rejections import FOLDER_INACTIVE, SHRINK_RECORDS, classify_rejection
from ..submission.submitter import Submitter
from ..utils.logging import get_logger
from .activation_state import ActivationState

log = get_logger(__name__)


@dataclass
class PassResult:
    pass_number: int
    folders_attempted: list[str] = field(default_factory=list)
    forms_submitted: int = 0
    forms_rejected: int = 0
    newly_activated: list[str] = field(default_factory=list)
    rejections: dict[str, str] = field(default_factory=dict)
    generation_failures: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "folders_attempted": self.folders_attempted,
            "forms_submitted": self.forms_submitted,
            "forms_rejected": self.forms_rejected,
            "newly_activated": self.newly_activated,
            "rejections": self.rejections,
            "generation_failures": self.generation_failures,
        }


def submitted_values(generated_root: Path, subject_id: str) -> dict[str, str]:
    """Every value written for a subject, keyed by item OID.

    Used to decide which activation conditions the subject now satisfies.
    """
    values: dict[str, str] = {}
    base = generated_root / subject_id
    if not base.is_dir():
        return values
    for path in base.glob("*/*.json"):
        if path.name.startswith("_"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for oid, value in (payload.get("values") or {}).items():
            if value not in (None, ""):
                values[oid] = str(value)
        for record in payload.get("records") or []:
            for oid, value in record.items():
                if value not in (None, ""):
                    values.setdefault(oid, str(value))
    return values


def predicted_folders(graph: DynamicsGraph, values: dict[str, str]) -> list[str]:
    """Folders the graph says should now be active, given what was written.

    An edge fires only when every assignment in its condition is satisfied. A
    condition flagged incomplete still counts - it contained an Or or a
    non-invertible operator, so satisfying the listed values may be enough.
    """
    unlocked: set[str] = set()
    for edge in graph.edges:
        if edge.target_type != "folder" or not edge.condition:
            continue
        assignments = edge.condition.assignments
        if not assignments:
            continue
        if all(values.get(a["field_oid"]) == a["value"] for a in assignments):
            unlocked.add(edge.target_oid)
    return sorted(unlocked)


class DynamicsResolver:
    def __init__(
        self,
        model: StudyModel,
        graph: DynamicsGraph,
        config: Config,
        generator: Generator,
        submitter: Submitter,
        site_oid: str,
    ):
        self.model = model
        self.graph = graph
        self.config = config
        self.generator = generator
        self.submitter = submitter
        self.site_oid = site_oid
        self.max_iterations = int(config.get("dynamics.max_iterations") or 5)
        self.generated_root = config.study_output_dir / "generated"

    # ------------------------------------------------------------------
    def _state_path(self, subject_id: str) -> Path:
        return self.config.study_output_dir / "state" / subject_id / "activation_state.json"

    def _submit_one(self, subject_id: str, folder_oid: str, outcome, pass_number: int):
        """Post a single generated form. Acceptance proves the folder is active."""
        records = {}
        if outcome.records and outcome.log_group_oid:
            records = {outcome.log_group_oid: outcome.records}
        payload = FormPayload(form_oid=outcome.form_oid,
                              values=dict(outcome.values), records=records)
        try:
            odm = build_clinical_odm(
                model=self.model, study_oid=self.config.study_env,
                subject_key=subject_id, site_oid=self.site_oid,
                folder_payloads={folder_oid: [payload]},
                subject_transaction="Update", form_transaction="Update",
            )
        except OdmBuildError as exc:
            return None, str(exc)

        result = self.submitter.post(
            odm, label=f"{subject_id}_{folder_oid}_{outcome.form_oid}",
            archive_dir=Path("subjects") / subject_id / f"pass_{pass_number}",
        )

        # Too many log lines for this form: drop one and retry. The cap is
        # published nowhere, so it is discovered by being refused. The rejection
        # is classified through the rave-submission skill's table.
        while (not result.ok
               and classify_rejection(result.error or "")[0] == SHRINK_RECORDS):
            group_oid = next((g for g, r in payload.records.items() if len(r) > 1), None)
            if group_oid is None:
                break
            payload.records[group_oid] = payload.records[group_oid][:-1]
            self._record_limit(outcome.form_oid, group_oid,
                               len(payload.records[group_oid]))
            odm = build_clinical_odm(
                model=self.model, study_oid=self.config.study_env,
                subject_key=subject_id, site_oid=self.site_oid,
                folder_payloads={folder_oid: [payload]},
                subject_transaction="Update", form_transaction="Update",
            )
            result = self.submitter.post(
                odm, label=f"{subject_id}_{folder_oid}_{outcome.form_oid}_shrunk",
                archive_dir=Path("subjects") / subject_id / f"pass_{pass_number}",
            )

        return result, (result.error or result.reason if not result.ok else "")

    def _record_limit(self, form_oid: str, group_oid: str, count: int) -> None:
        """Remember a discovered log-record cap so later runs stay inside it."""
        path = self.config.study_output_dir / "model" / "log_limits.json"
        limits = {}
        if path.is_file():
            try:
                limits = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                limits = {}
        limits[f"{form_oid}.{group_oid}"] = count
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(limits, indent=2, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------------
    def run_pass(
        self,
        subject_id: str,
        folders: list[str],
        state: ActivationState,
        pass_number: int,
    ) -> PassResult:
        result = PassResult(pass_number=pass_number, folders_attempted=list(folders))
        context: dict = {}

        for folder_oid in folders:
            folder = self.model.folders.get(folder_oid)
            if folder is None:
                continue

            for assignment in folder.forms:
                form_oid = assignment.form_oid
                if state.is_populated(folder_oid, form_oid):
                    continue  # FR-8.4: never overwrite what is already there

                outcome = self.generator.generate_form(
                    subject_id, folder_oid, form_oid, context)
                if not outcome.ok:
                    result.generation_failures[f"{folder_oid}/{form_oid}"] = (
                        outcome.detail or "; ".join(outcome.violations[:2]))
                    continue

                submission, error = self._submit_one(
                    subject_id, folder_oid, outcome, pass_number)

                # A dry run writes the payload and posts nothing, so it proves
                # nothing about activation. Recording it would leave a state file
                # claiming coverage that does not exist in Rave.
                if submission is not None and submission.status == "DRY_RUN":
                    result.forms_submitted += 1
                    self.generator._update_context(context, outcome)
                elif submission is not None and submission.ok:
                    if state.mark_active(folder_oid, form_oid, pass_number):
                        result.newly_activated.append(folder_oid)
                    result.forms_submitted += 1
                    self.generator._update_context(context, outcome)
                else:
                    result.forms_rejected += 1
                    result.rejections[f"{folder_oid}/{form_oid}"] = error
                    state.mark_refused(folder_oid, form_oid, error)
                    if classify_rejection(error)[0] == FOLDER_INACTIVE:
                        # The folder itself is absent, so its remaining forms
                        # cannot land either. A missing *form* is a different
                        # class: the folder is live and its other forms may
                        # still be writable, so that falls through to the next
                        # form rather than abandoning the folder.
                        break

        return result

    # ------------------------------------------------------------------
    def resolve(self, subject_id: str) -> ActivationState:
        """Run the fixed-point loop for one subject (FR-8.1 - FR-8.5)."""
        state = ActivationState.load(
            self._state_path(subject_id), subject_id,
            self.config.study_name, self.config.environment)

        # Pass 0: the seed set.
        seed = [oid for oid in self.model.seed_folder_oids if oid in self.model.folders]
        first = self.run_pass(subject_id, seed, state, 0)
        state.record_pass(0, first.summary())
        log.info("pass complete", extra={"subject": subject_id, "pass": 0,
                                         "submitted": first.forms_submitted})

        for pass_number in range(1, self.max_iterations + 1):
            values = submitted_values(self.generated_root, subject_id)
            predicted = predicted_folders(self.graph, values)
            state.predicted = sorted(set(state.predicted) | set(predicted))

            pending = [
                oid for oid in predicted
                if oid in self.model.folders
                and not all(state.is_populated(oid, a.form_oid)
                            for a in self.model.folders[oid].forms)
            ]
            if not pending:
                log.info("no further folders to attempt",
                         extra={"subject": subject_id, "pass": pass_number})
                break

            outcome = self.run_pass(subject_id, pending, state, pass_number)
            state.record_pass(pass_number, outcome.summary())
            log.info("pass complete", extra={
                "subject": subject_id, "pass": pass_number,
                "submitted": outcome.forms_submitted,
                "new_folders": len(outcome.newly_activated)})

            if not outcome.newly_activated and not outcome.forms_submitted:
                break  # FR-8.5: fixed point reached

        if self.submitter.dry_run:
            log.info("dry run: activation state not persisted",
                     extra={"subject": subject_id})
        else:
            state.save(self._state_path(subject_id))
        return state
