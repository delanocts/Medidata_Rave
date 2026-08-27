"""A5 - Data Generation Agent (FR-6).

Values come from the LLM. This module does prompt construction, validation, the
repair loop, caching and persistence - none of which invents clinical data.

Ordering matters for consistency (FR-6.5): forms are generated visit by visit in
protocol order, and each prompt carries a running summary of what the subject
already has, so demographics stay stable and dates stay monotonic.
"""
from __future__ import annotations

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..config.loader import Config
from ..model.dynamics_graph import DynamicsGraph
from ..model.study_model import Item, StudyModel
from .schedule import day_offset, enrolment_date, visit_date
from ..utils.logging import get_logger
from .llm_client import LlmClient, LlmError
from .prompt_builder import (
    SYSTEM_PROMPT,
    FormRequest,
    build_form_prompt,
    build_form_schema,
    build_repair_prompt,
)
from .validators import Violation, validate_form, validate_records

log = get_logger(__name__)

# Fields carried forward as subject context, matched on the variable name.
_CONTEXT_HINTS = ("SEX", "BRTH", "DOB", "AGE", "RACE", "ETHNIC", "HEIGHT", "WEIGHT")

# How a CRF says "this is the date the visit happened". Names are Rave and CDISC
# conventions, not one study's; labels catch the studies that use neither.
def _as_date(value, fallback: date) -> date:
    """A configured ISO date, or the fallback when it is absent or unparseable."""
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


_VISIT_DATE_NAMES = ("DCMDATE", "VISDAT", "VISITDAT", "SVSTDTC", "DSSTDTC")
_VISIT_DATE_LABELS = (
    "date of visit", "visit date", "date of this visit", "date of the visit",
    "date of examination", "date of assessment", "assessment date",
)


@dataclass
class FormOutcome:
    subject_id: str
    folder_oid: str
    form_oid: str
    status: str                  # generated | cached | failed | skipped
    values: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    log_group_oid: str | None = None
    attempts: int = 0
    violations: list[str] = field(default_factory=list)
    detail: str = ""
    path: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("generated", "cached")


@dataclass
class GenerationResult:
    subject_id: str
    outcomes: list[FormOutcome] = field(default_factory=list)
    token_usage: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for outcome in self.outcomes:
            out[outcome.status] = out.get(outcome.status, 0) + 1
        return out


class Generator:
    def __init__(
        self,
        model: StudyModel,
        graph: DynamicsGraph,
        config: Config,
        llm: LlmClient,
        regenerate: bool = False,
    ):
        self.model = model
        self.graph = graph
        self.config = config
        self.llm = llm
        self.regenerate = regenerate
        self.root = config.study_output_dir / "generated"
        self.max_retries = int(config.get("generation.max_validation_retries") or 3)
        self.therapeutic_area = config.get("generation.therapeutic_area")
        self.realistic = bool(config.get("generation.therapeutic_realism", True))
        self.require_all = bool(config.get("generation.require_all_fields", False))
        self.max_values_per_form = int(
            config.get("generation.max_values_per_form") or 150)
        self.strategy = str(config.get("dynamics.trigger_strategy") or "maximize")
        self._overrides = self._collect_overrides()
        self.discovered_limits = self._load_discovered_limits()
        # trigger field -> values the ALS wants but the codelist will not accept
        self.unsatisfiable_triggers: dict[str, set[str]] = {}
        # Forms within a folder are generated concurrently, so the shared
        # bookkeeping below needs guarding.
        self._lock = threading.Lock()
        self.max_parallel_forms = max(
            1, int(config.get("generation.max_parallel_forms") or 1))
        self._required_values = graph.required_values() if graph else {}
        # When the first subject screens, and how wide a window the cohort
        # enrols across. Both deliberately fixed rather than relative to today,
        # so a subject regenerated next month keeps the dates Rave already has.
        self._enrol_first = _as_date(config.get("generation.enrolment.first_date"),
                                     date(2024, 1, 8))
        self._enrol_window = max(0, int(
            config.get("generation.enrolment.window_days") or 0))

    def _load_discovered_limits(self) -> dict:
        """Per-form log-record caps learned from Rave rejections (A6 writes these).

        Rave enforces a maximum number of log lines per form that neither the
        ODM metadata nor the ALS exposes. The submitter discovers it by being
        refused, and records it here so later generations stay within it.
        """
        path = self.config.study_output_dir / "model" / "log_limits.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    # ------------------------------------------------------------------
    def _collect_overrides(self) -> dict[str, str]:
        """Config overrides always win over anything the graph suggests (FR-6.6)."""
        out: dict[str, str] = {}
        for entry in self.config.get("dynamics.overrides") or []:
            out[str(entry["field_oid"])] = str(entry["value"])
        return out

    def _scheduled_visit(self, subject_id: str, folder) -> tuple[str, str, int | None]:
        """This subject's Day 1 and this visit's date, both as ISO strings.

        The visit date is ("", "", None) when the folder names no protocol day -
        an unscheduled visit has no computable date and the model is left to
        choose one from context.
        """
        anchor = enrolment_date(subject_id, self._enrol_first, self._enrol_window)
        offset = day_offset(folder.name)
        if offset is None:
            return anchor.isoformat(), "", None
        return anchor.isoformat(), visit_date(anchor, offset).isoformat(), offset

    def _visit_date_items(self, items: list[Item]) -> list[Item]:
        """Fields on this form that name themselves as the date of the visit."""
        out = []
        for item in items:
            if item.data_type != "date":
                continue
            name = (item.name or "").upper()
            label = (item.label or "").lower()
            if (any(hint in name for hint in _VISIT_DATE_NAMES)
                    or any(hint in label for hint in _VISIT_DATE_LABELS)):
                out.append(item)
        return out

    def _forced_values(self, items: list[Item]) -> tuple[dict[str, str], list[str]]:
        """Decide which trigger fields to pin, and explain why in the prompt."""
        forced: dict[str, str] = {}
        notes: list[str] = []

        for item in items:
            if item.oid in self._overrides:
                forced[item.oid] = self._overrides[item.oid]
                notes.append(f'"{item.oid}" is pinned by configuration.')
                continue

            if self.strategy == "as_configured":
                continue

            candidates = sorted(self._required_values.get(item.oid) or [])
            candidates = [v for v in candidates if self._satisfiable(item, v)]
            if not candidates:
                continue

            if self.strategy == "maximize":
                # Pick the value unlocking the most targets.
                best = max(
                    candidates,
                    key=lambda value: len(self._targets_for(item.oid, value)),
                )
            else:  # random
                best = random.choice(candidates)

            unlocked = self._targets_for(item.oid, best)
            forced[item.oid] = best
            notes.append(
                f'"{item.oid}" = "{best}" activates {len(unlocked)} downstream '
                f"target(s) such as {sorted(unlocked)[:4]}."
            )

        return forced, notes

    def _satisfiable(self, item: Item, value: str) -> bool:
        """Can this field actually hold this value?

        An edit check can name a value the field's codelist does not offer -
        typically because the check predates the CRF version. Pinning such a
        value guarantees the form fails validation and burns every repair
        attempt, so those triggers are skipped and reported instead.
        """
        if not item.codelist_oid:
            return True
        codelist = self.model.codelists.get(item.codelist_oid)
        if codelist is None:
            return True
        if value in codelist.coded_values:
            return True
        with self._lock:
            self.unsatisfiable_triggers.setdefault(item.oid, set()).add(value)
        return False

    def _targets_for(self, field_oid: str, value: str) -> set[str]:
        targets = set()
        for edge in self.graph.edges_for_trigger(field_oid):
            for assignment in edge.condition.assignments:
                if assignment["field_oid"] == field_oid and assignment["value"] == value:
                    targets.add(edge.target_oid)
        return targets

    # ------------------------------------------------------------------
    def _cache_path(self, subject_id: str, folder_oid: str, form_oid: str) -> Path:
        return self.root / subject_id / folder_oid / f"{form_oid}.json"

    def _load_cached(self, path: Path) -> dict | None:
        if self.regenerate or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _record_count(self, form_oid: str, group_oid: str | None = None) -> int:
        """How many log lines to generate, capped by what the form allows.

        Rave enforces a per-form maximum that neither the ODM metadata nor the
        ALS exposes - exceeding it is rejected with "Record restricted by max
        limit". The highest count seen on a real subject is a safe lower bound
        for that cap, so it is used as a ceiling when available.
        """
        low = int(self.config.get("generation.log_records.min") or 1)
        high = int(self.config.get("generation.log_records.max") or low)
        high = max(low, high)

        # A discovered limit is authoritative; an observed count is only a lower
        # bound on the real cap (sampled subjects often hold a single row), so it
        # is never used to reduce the configured count.
        cap = (self.discovered_limits or {}).get(f"{form_oid}.{group_oid}")
        if cap:
            high = min(high, int(cap))
            low = min(low, high)

        # A wide log form multiplies out fast: fields x records is what the model
        # has to write, and a long reply gets truncated at max_tokens. Keep the
        # product under a budget rather than letting a 68-field form ask for ten
        # records.
        if group_oid and group_oid in self.model.item_groups:
            width = len(self.model.item_groups[group_oid].item_oids)
            if width > 0:
                affordable = max(1, self.max_values_per_form // width)
                high = min(high, affordable)
                low = min(low, high)

        return random.randint(low, max(low, high))

    # ------------------------------------------------------------------
    def generate_form(
        self,
        subject_id: str,
        folder_oid: str,
        form_oid: str,
        subject_context: dict[str, Any],
    ) -> FormOutcome:
        """Generate (or reuse) one form, repairing until it validates (FR-6.4)."""
        path = self._cache_path(subject_id, folder_oid, form_oid)

        cached = self._load_cached(path)
        if cached is not None:
            return FormOutcome(
                subject_id, folder_oid, form_oid, "cached",
                values=cached.get("values") or {},
                records=cached.get("records") or [],
                log_group_oid=cached.get("log_group_oid"),
                path=str(path), detail="reused from disk",
            )

        folder = self.model.folders.get(folder_oid)
        form = self.model.forms.get(form_oid)
        if folder is None or form is None:
            return FormOutcome(subject_id, folder_oid, form_oid, "skipped",
                               detail="folder or form is not in the study model")

        # A form may carry a fixed section, a repeating log section, or both.
        log_groups = self.model.log_item_groups(form_oid)
        log_group = log_groups[0] if log_groups else None
        log_oids = set(log_group.item_oids) if log_group else set()

        def usable(item) -> bool:
            # Derived fields are computed by Rave and reject any posted value.
            return item.visible and not item.derived

        fixed_items = [i for i in self.model.items_for_form(form_oid)
                       if usable(i) and i.oid not in log_oids]
        log_items = [self.model.items[o] for o in sorted(log_oids)
                     if o in self.model.items and usable(self.model.items[o])]

        if not fixed_items and not log_items:
            return FormOutcome(subject_id, folder_oid, form_oid, "skipped",
                               detail="form has no visible fields to populate")

        forced, notes = self._forced_values(fixed_items + log_items)

        # When the visit happened is arithmetic, so it is pinned rather than
        # asked for. Left to the model, every subject screened on the same day
        # and visits three days apart shared one date - the prompt carried no
        # per-subject anchor and no scale to place a protocol day against.
        # Pinning goes through the same path as a trigger value: stated in the
        # prompt, checked afterwards, repaired if the model deviates. Nothing is
        # substituted silently (FR-6.4).
        anchor, scheduled, offset = self._scheduled_visit(subject_id, folder)
        if scheduled:
            for item in self._visit_date_items(fixed_items):
                forced.setdefault(item.oid, scheduled)
            notes.append(
                f"Day 1 for this subject is {anchor}. This visit is protocol day "
                f"{offset}, so it takes place on {scheduled}. Every other date on "
                f"this form must be consistent with that."
            )
        elif anchor:
            notes.append(
                f"Day 1 for this subject is {anchor}. This visit has no protocol "
                f"day; date it plausibly relative to the visits already recorded."
            )

        request = FormRequest(
            subject_id=subject_id,
            folder_oid=folder_oid, folder_name=folder.name,
            form_oid=form_oid, form_name=form.name,
            items=fixed_items,
            log_items=log_items,
            log_group_oid=log_group.oid if log_group else None,
            record_count=(self._record_count(form_oid, log_group.oid)
                          if log_items else 1),
            forced_values=forced,
            trigger_notes=notes,
            require_all_fields=self.require_all,
        )

        schema = build_form_schema(self.model, request)
        prompt = build_form_prompt(
            self.model, request, subject_context,
            self.therapeutic_area, self.realistic,
        )
        messages = [{"role": "user", "content": prompt}]

        outcome = FormOutcome(subject_id, folder_oid, form_oid, "failed",
                              log_group_oid=request.log_group_oid)

        for attempt in range(1, self.max_retries + 2):
            outcome.attempts = attempt
            try:
                response = self.llm.generate(
                    SYSTEM_PROMPT, messages, schema,
                    label=f"{subject_id}/{folder_oid}/{form_oid}",
                )
            except LlmError as exc:
                outcome.detail = str(exc)
                return outcome

            if request.is_log:
                records = response.data.get("records") or []
                candidate_values = dict(response.data.get("values") or {})
                # Each section is judged only against its own fields.
                violations = validate_records(
                    self.model, form_oid, records, item_scope=request.log_items,
                    require_all=self.require_all)
                if request.items:
                    violations.extend(validate_form(
                        self.model, form_oid, candidate_values,
                        item_scope=request.items, require_all=self.require_all))
                if len(records) != request.record_count:
                    violations.append(Violation(
                        f"{form_oid} records", len(records),
                        "wrong number of log records",
                        expected=f"exactly {request.record_count}"))
                candidate_records = records
            else:
                values = dict(response.data)
                violations = validate_form(self.model, form_oid, values,
                                           item_scope=request.items,
                                           require_all=self.require_all)
                candidate_values, candidate_records = values, []

            violations.extend(self._check_forced(forced, response.data, request.is_log))

            if not violations:
                outcome.status = "generated"
                outcome.values = candidate_values
                outcome.records = candidate_records
                self._persist(path, outcome)
                outcome.path = str(path)
                return outcome

            outcome.violations = [v.describe() for v in violations]
            log.warning("validation failed", extra={
                "subject": subject_id, "form": form_oid,
                "attempt": attempt, "violations": len(violations),
            })

            if attempt > self.max_retries:
                break

            messages = messages + [
                {"role": "assistant", "content": response.raw_text},
                {"role": "user", "content": build_repair_prompt(violations, response.data)},
            ]

        # Never substitute a value silently (FR-6.4).
        outcome.detail = (
            f"still invalid after {self.max_retries} repair attempt(s); "
            f"{len(outcome.violations)} violation(s) remain"
        )
        return outcome

    def _check_forced(self, forced: dict[str, str], data: dict, is_log: bool) -> list[Violation]:
        """A pinned trigger value is not negotiable."""
        if not forced:
            return []
        payloads = data.get("records") or [] if is_log else [data]
        problems = []
        for payload in payloads:
            for oid, expected in forced.items():
                if oid in payload and str(payload[oid]) != expected:
                    problems.append(Violation(
                        oid, payload[oid],
                        "must take the pinned trigger value",
                        expected=f'"{expected}"'))
        return problems

    def _persist(self, path: Path, outcome: FormOutcome) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "subject_id": outcome.subject_id,
            "folder_oid": outcome.folder_oid,
            "form_oid": outcome.form_oid,
            "log_group_oid": outcome.log_group_oid,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "synthetic": True,
            "values": outcome.values,
            "records": outcome.records,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    def generate_forms(
        self,
        subject_id: str,
        folder_oid: str,
        form_oids: list[str],
        subject_context: dict[str, Any],
    ) -> list[FormOutcome]:
        """Generate several forms of one folder at once.

        Concurrency is deliberately scoped to a single folder. Cross-visit
        consistency (demographics carried forward, visit dates advancing) is
        preserved by keeping folders sequential; within one visit the forms
        share a date and a patient, so generating them against the same context
        snapshot loses nothing.

        Results come back in the order requested, so submission order - which
        must stay serialised per subject - is unaffected.
        """
        if not form_oids:
            return []
        if self.max_parallel_forms == 1 or len(form_oids) == 1:
            return [
                self.generate_form(subject_id, folder_oid, oid, subject_context)
                for oid in form_oids
            ]

        snapshot = dict(subject_context)
        workers = min(self.max_parallel_forms, len(form_oids))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="generate") as pool:
            return list(pool.map(
                lambda oid: self.generate_form(subject_id, folder_oid, oid, snapshot),
                form_oids,
            ))

    # ------------------------------------------------------------------
    def _visit_date_candidate(self, outcome: FormOutcome) -> tuple[str, bool]:
        """The date this visit happened, and whether the field said it was.

        A CRF collects many dates and only one of them is the visit: a
        demographics form carries a birth date, a history form an onset years
        back, a substance-use form a quit date. Picking any of those as "the
        visit date" tells the next visit's prompt the subject was seen decades
        ago, and it dutifully agrees.

        Fields are matched on name and label rather than OID, because the
        convention is Rave's and the study's identifiers are its own. Time-only
        fields are excluded: they are date-like to the metadata but a clock time
        sorts before every real date as a string.
        """
        named: list[str] = []
        other: list[str] = []
        for oid, value in (outcome.values or {}).items():
            item = self.model.items.get(oid)
            if item is None or not value or item.data_type not in ("date", "datetime"):
                continue
            name = (item.name or "").upper()
            label = (item.label or "").lower()
            if (any(hint in name for hint in _VISIT_DATE_NAMES)
                    or any(hint in label for hint in _VISIT_DATE_LABELS)):
                named.append(str(value))
            else:
                other.append(str(value))
        if named:
            return sorted(named)[0], True
        if other:
            return sorted(other)[0], False
        return "", False

    def _update_context(self, context: dict[str, Any], outcome: FormOutcome) -> None:
        """Carry stable subject facts forward so later forms agree (FR-6.5)."""
        for oid, value in (outcome.values or {}).items():
            item = self.model.items.get(oid)
            if item is None or value in (None, ""):
                continue
            if any(hint in item.name.upper() for hint in _CONTEXT_HINTS):
                context.setdefault(f"{item.label or item.name} [{oid}]", value)

        # One visit, one date. A field that names itself as the visit date wins
        # and then holds; anything else only fills an empty slot, so the first
        # form in the visit sets it and later forms cannot overwrite it. The old
        # rule was the opposite - last form wins, earliest date on it - which
        # let a smoking-cessation date become the date of a screening visit.
        value, is_named = self._visit_date_candidate(outcome)
        if not value:
            return
        # Keyed by the visit's *name*, because the name is where the protocol
        # day lives - "Screening (Day -30) visit date" tells the next visit how
        # far away it is, where "SCREEN visit date" told it nothing and it
        # copied the date verbatim. The bookkeeping key stays on the OID, which
        # cannot collide.
        folder = self.model.folders.get(outcome.folder_oid)
        label = (folder.name if folder and folder.name else outcome.folder_oid)
        key = f"{label} visit date"
        claimed = f"_{outcome.folder_oid} visit date claimed"
        if is_named and not context.get(claimed):
            context[key] = value
            context[claimed] = True
        else:
            context.setdefault(key, value)

    def generate_subject(
        self,
        subject_id: str,
        folders: list[str],
        max_forms: int | None = None,
    ) -> GenerationResult:
        """Generate every assigned form for a subject, visit by visit."""
        result = GenerationResult(subject_id=subject_id)
        context: dict[str, Any] = {}
        produced = 0

        ordered = sorted(
            (oid for oid in folders if oid in self.model.folders),
            key=lambda oid: (self.model.folders[oid].order is None,
                             self.model.folders[oid].order, oid),
        )

        for folder_oid in ordered:
            pending = [a.form_oid for a in self.model.folders[folder_oid].forms]
            if max_forms is not None:
                room = max_forms - produced
                if room <= 0:
                    result.warnings.append(
                        f"stopped after {max_forms} form(s) because of --max-forms")
                    result.token_usage = self.llm.usage.to_dict()
                    return result
                pending = pending[:room]

            for outcome in self.generate_forms(
                    subject_id, folder_oid, pending, context):
                result.outcomes.append(outcome)
                produced += 1
                if outcome.ok:
                    self._update_context(context, outcome)

        result.token_usage = self.llm.usage.to_dict()
        return result
