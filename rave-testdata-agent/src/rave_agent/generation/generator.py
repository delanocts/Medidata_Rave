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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.loader import Config
from ..model.dynamics_graph import DynamicsGraph
from ..model.study_model import Item, StudyModel
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
        self._required_values = graph.required_values() if graph else {}

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
    def _update_context(self, context: dict[str, Any], outcome: FormOutcome) -> None:
        """Carry stable subject facts forward so later forms agree (FR-6.5)."""
        for oid, value in (outcome.values or {}).items():
            item = self.model.items.get(oid)
            if item is None or value in (None, ""):
                continue
            if any(hint in item.name.upper() for hint in _CONTEXT_HINTS):
                context.setdefault(f"{item.label or item.name} [{oid}]", value)
        if outcome.values:
            dates = [v for oid, v in outcome.values.items()
                     if (self.model.items.get(oid) or Item("", "", "", "")).is_date_like]
            if dates:
                context[f"{outcome.folder_oid} visit date"] = sorted(dates)[0]

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
            for assignment in self.model.folders[folder_oid].forms:
                if max_forms is not None and produced >= max_forms:
                    result.warnings.append(
                        f"stopped after {max_forms} form(s) because of --max-forms")
                    result.token_usage = self.llm.usage.to_dict()
                    return result

                outcome = self.generate_form(
                    subject_id, folder_oid, assignment.form_oid, context)
                result.outcomes.append(outcome)
                produced += 1

                if outcome.ok:
                    self._update_context(context, outcome)

        result.token_usage = self.llm.usage.to_dict()
        return result
