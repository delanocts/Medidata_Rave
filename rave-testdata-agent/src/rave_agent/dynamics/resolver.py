"""A8 - the dynamics resolution loop (FR-8).

Pass 0 fills the seed folders. Each later pass asks the graph which folders the
values written so far should have unlocked, tries to fill those, and treats
acceptance as proof of activation. The loop stops when a pass activates nothing
new, or `dynamics.max_iterations` is reached.

Already-populated forms are never rewritten (FR-8.4).
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
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
    # Forms generated ahead of time for a folder that then turned out absent.
    # The price of the lookahead, kept visible rather than hidden.
    forms_discarded: int = 0

    def summary(self) -> dict:
        return {
            "folders_attempted": self.folders_attempted,
            "forms_submitted": self.forms_submitted,
            "forms_rejected": self.forms_rejected,
            "forms_discarded": self.forms_discarded,
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
        self.lookahead = int(config.get("generation.lookahead_folders") or 0)
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
        """Remember a discovered log-record cap so later runs stay inside it.

        This is the one file several subjects write to at once, and they are
        separate processes, so there is no lock to take. The write is made
        atomic instead: a concurrent writer can cost us an entry, which the next
        refusal rediscovers, but it can never leave a half-written file that
        every later run fails to parse.
        """
        path = self.config.study_output_dir / "model" / "log_limits.json"
        limits = {}
        if path.is_file():
            try:
                limits = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                limits = {}
        limits[f"{form_oid}.{group_oid}"] = count
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(limits, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def _record(self, result, state, folder_oid, form_oid, outcome,
                submission, error, pass_number, context) -> bool:
        """Record one submission. Returns False when the folder is absent.

        A dry run writes the payload and posts nothing, so it proves nothing
        about activation; recording it would leave a state file claiming
        coverage Rave does not have.
        """
        if submission is not None and submission.status == "DRY_RUN":
            result.forms_submitted += 1
            self.generator._update_context(context, outcome)
            return True

        if submission is not None and submission.ok:
            if state.mark_active(folder_oid, form_oid, pass_number):
                result.newly_activated.append(folder_oid)
            result.forms_submitted += 1
            self.generator._update_context(context, outcome)
            return True

        result.forms_rejected += 1
        result.rejections[f"{folder_oid}/{form_oid}"] = error
        state.mark_refused(folder_oid, form_oid, error)
        # A missing *form* leaves the folder live; a missing folder does not.
        return classify_rejection(error)[0] != FOLDER_INACTIVE

    # ------------------------------------------------------------------
    def _submit_folder(
        self,
        subject_id: str,
        folder_oid: str,
        form_oids: list[str],
        outcomes: list,
        state: ActivationState,
        result: PassResult,
        context: dict,
        pass_number: int,
    ) -> bool:
        """Post one folder's generated forms in order. False once it is absent.

        Submissions for a single subject stay serialised (C-5), so this is the
        slow half of a pass and the half the lookahead runs against.
        """
        for index, (form_oid, outcome) in enumerate(zip(form_oids, outcomes)):
            if not outcome.ok:
                result.generation_failures[f"{folder_oid}/{form_oid}"] = (
                    outcome.detail or "; ".join(outcome.violations[:2]))
                continue
            submission, error = self._submit_one(
                subject_id, folder_oid, outcome, pass_number)
            if not self._record(result, state, folder_oid, form_oid, outcome,
                                submission, error, pass_number, context):
                # The folder is not part of this subject. Whatever was already
                # generated for the rest of it is thrown away unposted.
                result.forms_discarded += len(form_oids) - index - 1
                return False
        return True

    def _pass_plan(self, folders: list[str], state: ActivationState) -> list[tuple[str, list[str]]]:
        """Folders still worth attempting, with the forms each one owes."""
        plan: list[tuple[str, list[str]]] = []
        for folder_oid in folders:
            folder = self.model.folders.get(folder_oid)
            if folder is None:
                continue
            pending = [a.form_oid for a in folder.forms
                       if not state.is_populated(folder_oid, a.form_oid)]
            if pending:
                plan.append((folder_oid, pending))
        return plan

    def _run_folder_probed(
        self,
        subject_id: str,
        folder_oid: str,
        form_oids: list[str],
        state: ActivationState,
        result: PassResult,
        context: dict,
        pass_number: int,
    ) -> None:
        """Generate a folder only as far as its answer justifies.

        The first form is generated and posted alone. A folder that is not part
        of this subject yet refuses everything, so finding that out after
        generating twenty forms wastes twenty LLM calls; one call answers it.
        This is the `lookahead_folders: 0` path - it never speculates, and it
        leaves generation and submission strictly alternating.
        """
        probe, rest = form_oids[:1], form_oids[1:]
        outcome = self.generator.generate_form(
            subject_id, folder_oid, probe[0], context)
        alive = self._submit_folder(subject_id, folder_oid, probe, [outcome],
                                    state, result, context, pass_number)
        if not alive or not rest:
            return
        outcomes = self.generator.generate_forms(subject_id, folder_oid, rest, context)
        self._submit_folder(subject_id, folder_oid, rest, outcomes,
                            state, result, context, pass_number)

    def run_pass(
        self,
        subject_id: str,
        folders: list[str],
        state: ActivationState,
        pass_number: int,
    ) -> PassResult:
        result = PassResult(pass_number=pass_number, folders_attempted=list(folders))
        context: dict = {}
        plan = self._pass_plan(folders, state)
        if not plan:
            return result

        if self.lookahead < 1:
            for folder_oid, form_oids in plan:
                self._run_folder_probed(subject_id, folder_oid, form_oids, state,
                                        result, context, pass_number)
            return result

        # Pipelined: generate the next folder while this one is being posted.
        #
        # Posting dominates a pass - Rave charges per message, not per value, so
        # a folder costs roughly ten seconds a form however small the form is,
        # while generating one costs about three. Left alternating, the two
        # never overlap and the pass takes the sum. Run one folder ahead and the
        # generation disappears behind the posting.
        #
        # The cost is that the next folder is generated before this one's probe
        # has proved it exists, so an absent folder wastes what was prepared for
        # it. That is counted in `forms_discarded`. Set `lookahead_folders: 0`
        # to trade the speed back for the probe.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="lookahead") as lane:
            ahead = lane.submit(self.generator.generate_forms,
                                subject_id, plan[0][0], plan[0][1], dict(context))
            for index, (folder_oid, form_oids) in enumerate(plan):
                outcomes = ahead.result()
                if index + 1 < len(plan):
                    # The next folder is prompted with what this one generated,
                    # so visit dates still advance in order. Only the authoritative
                    # `context` is confined to what Rave actually accepted.
                    speculative = dict(context)
                    for outcome in outcomes:
                        if outcome.ok:
                            self.generator._update_context(speculative, outcome)
                    nxt_folder, nxt_forms = plan[index + 1]
                    ahead = lane.submit(self.generator.generate_forms,
                                        subject_id, nxt_folder, nxt_forms, speculative)
                self._submit_folder(subject_id, folder_oid, form_oids, outcomes,
                                    state, result, context, pass_number)

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
