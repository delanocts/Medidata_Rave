"""SK-3 / acceptance criterion 4: a second study works with no code changes.

The two fixtures share no identifier and follow opposite conventions, so passing
both means the parsers key off structure rather than one study's habits. Every
assertion here names the trap it guards.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rave_agent.generation.prompt_builder import (  # noqa: E402
    FormRequest,
    build_form_prompt,
    build_form_schema,
)
from rave_agent.generation.validators import (  # noqa: E402
    format_for_rave,
    validate_form,
    validate_value,
)
from rave_agent.model.als_parser import parse_als  # noqa: E402
from rave_agent.model.dynamics_graph import build_graph  # noqa: E402
from rave_agent.model.matrix_resolver import (  # noqa: E402
    apply_als_derivations,
    apply_als_matrices,
    apply_version_folders,
    finalise_assignments,
    resolve_primary_form_placement,
)
from rave_agent.model.odm_parser import parse_odm  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def study_a():
    model = parse_odm(FIXTURES / "study_a" / "metadata.xml", "ALPHA", "Dev")
    finalise_assignments(model)
    return model


@pytest.fixture(scope="module")
def study_b():
    base = FIXTURES / "study_b"
    model = parse_odm(base / "metadata.xml", "beta-2", "UAT")
    apply_version_folders(model, base / "version_folders.xml")
    als = parse_als(base / "als.xls")
    apply_als_matrices(model, als)
    apply_als_derivations(model, als)
    resolve_primary_form_placement(model, None)
    finalise_assignments(model)
    graph = build_graph(model, {"enabled": True, "trigger_strategy": "maximize"}, als)
    return model, als, graph


# ------------------------------------------------------------------ study A
def test_small_study_parses(study_a):
    assert study_a.crf_version_oid == "7"
    assert set(study_a.forms) == {"REG", "OBS"}
    assert study_a.primary_form_oid == "REG"
    assert study_a.seed_folder_oids == ["V1"]


def test_small_study_has_no_dynamics(study_a):
    graph = build_graph(study_a, {"enabled": True}, None)
    assert graph.edges == []
    assert graph.has_edit_check_source is False
    assert any("no edit-check source" in w.lower() for w in graph.warnings)


def test_range_checks_parsed_without_an_als(study_a):
    ranges = study_a.items["OBS.TEMP"].ranges
    assert [(r.comparator, r.values[0]) for r in ranges] == [("GE", "34"), ("LE", "43")]


# ------------------------------------------------------------------ study B
def test_lowercase_oids_and_different_version(study_b):
    model, _, _ = study_b
    assert model.crf_version_oid == "4411"
    assert model.primary_form_oid == "scr_entry"
    # Naming conventions differ completely from study A.
    assert all(oid.islower() or "_" in oid for oid in model.forms)


def test_numeric_codelists_ordered_by_mdsol_order_not_document_order(study_b):
    """`0` is declared second but ordered first - document order would invert it."""
    model, _, _ = study_b
    assert model.codelists["cl_01"].coded_values == ["0", "1"]


def test_english_chosen_over_a_language_declared_first(study_b):
    """The German TranslatedText comes first in the file."""
    model, _, _ = study_b
    assert model.items["scr_entry.eligible"].label == "Eligible for study"


def test_log_group_detected_on_a_form_that_also_has_a_fixed_section(study_b):
    model, _, _ = study_b
    assert [g.oid for g in model.log_item_groups("ae_log")] == ["ae_log_LOG_LINE"]
    # The header group is not a log group.
    assert model.item_groups["ae_log"].repeating is False


def test_hidden_field_detected(study_b):
    model, _, _ = study_b
    assert [i.oid for i in model.hidden_items()] == ["scr_entry.hidden_flag"]


def test_derived_field_named_only_by_variable_oid_is_found(study_b):
    """The Derivations row leaves FormOID/FieldOID blank - reading only FieldOID misses it."""
    model, _, _ = study_b
    assert model.items["tx_admin.calc_bmi"].derived is True


def test_matrix_grid_and_merged_folder(study_b):
    model, als, _ = study_b
    assert set(als.matrices) == {"SEED", "TREAT"}
    assert als.matrices["TREAT"]["tx1"] == ["tx_admin", "ae_log"]
    # tx1 exists only through the non-default matrix.
    assert "tx1" in model.folders
    assert "tx1" not in model.seed_folder_oids


def test_only_activating_actions_become_edges(study_b):
    """A query-only action is not an activation."""
    _, als, graph = study_b
    assert als.counts["activating_actions"] == 2
    assert all(e.action_type in ("MrgMatrix", "AddForm") for e in graph.edges)
    assert not any(e.action_type == "OpenQuery" for e in graph.edges)


def test_matrix_edge_expands_to_its_folders_and_forms(study_b):
    """Merging a matrix must name what it actually brings in."""
    _, _, graph = study_b
    kinds = {e.target_type for e in graph.edges}
    assert {"matrix", "folder", "form"} <= kinds
    assert any(e.target_oid == "tx1" and e.target_type == "folder" for e in graph.edges)


def test_trigger_values_are_invertible(study_b):
    _, _, graph = study_b
    assert graph.required_values() == {
        "scr_entry.eligible": {"1"},
        "ae_log.any_ae": {"1"},
    }


def test_nothing_left_unresolvable(study_b):
    _, _, graph = study_b
    assert graph.unresolvable == []


# ------------------------------------------- validation across both studies
def test_codelists_enforced_per_study(study_a, study_b):
    """The same validator must apply each study's own codelist."""
    model_b, _, _ = study_b
    assert validate_value(study_a, study_a.items["REG.CONSENT"], "Y") == []
    assert validate_value(study_a, study_a.items["REG.CONSENT"], "1") != []
    assert validate_value(model_b, model_b.items["scr_entry.eligible"], "1") == []
    assert validate_value(model_b, model_b.items["scr_entry.eligible"], "Y") != []


def test_each_study_renders_dates_in_its_own_format(study_a, study_b):
    model_b, _, _ = study_b
    assert format_for_rave(study_a.items["OBS.WHEN"], "2026-03-14") == "14 MAR 2026"
    assert format_for_rave(model_b.items["scr_entry.scr_dt"], "2026-03-14") == "2026-03-14"


def test_time_field_is_not_treated_as_a_date(study_b):
    """A date in a HH:nn field stores garbage; the validator must refuse it."""
    model, _, _ = study_b
    item = model.items["tx_admin.tx_time"]
    assert validate_value(model, item, "09:30") == []
    assert validate_value(model, item, "2026-03-14") != []
    assert format_for_rave(item, "09:30") == "09:30"


def test_hard_range_enforced(study_b):
    model, _, _ = study_b
    assert validate_value(model, model.items["tx_admin.dose"], "50") == []
    assert validate_value(model, model.items["tx_admin.dose"], "250") != []


def test_log_records_validate_against_the_log_group_only(study_b):
    """Validating a record against the whole form reports the header as missing."""
    model, _, _ = study_b
    log_items = [model.items[o] for o in model.item_groups["ae_log_LOG_LINE"].item_oids]
    record = {"ae_log.term": "Headache", "ae_log.sev": "1", "ae_log.onset": "2026-03-14"}
    assert validate_form(model, "ae_log", record, item_scope=log_items) == []
    # Without the scope, the fixed section's mandatory field is wrongly flagged.
    assert validate_form(model, "ae_log", record) != []


# --------------------------------------------------- prompts, both studies
@pytest.mark.parametrize("which", ["a", "b"])
def test_prompt_and_schema_build_for_either_study(which, study_a, study_b):
    model = study_a if which == "a" else study_b[0]
    form_oid = "REG" if which == "a" else "scr_entry"
    items = [i for i in model.items_for_form(form_oid) if i.visible and not i.derived]

    request = FormRequest("SUBJ-1", "F", "Folder", form_oid, "Form", items,
                          require_all_fields=True)
    prompt = build_form_prompt(model, request, therapeutic_area="cardiology")
    schema = build_form_schema(model, request)

    for item in items:
        assert item.oid in prompt
        assert item.oid in schema["properties"]
    # require_all_fields means every field is required, not just the mandatory ones.
    assert set(schema["required"]) == {i.oid for i in items}


def test_pinned_trigger_value_becomes_a_single_member_enum(study_b):
    """Asking in prose is not enough - the model sometimes picks another code."""
    model, _, _ = study_b
    item = model.items["scr_entry.eligible"]
    request = FormRequest("S", "scr", "Screening", "scr_entry", "Entry", [item],
                          forced_values={item.oid: "1"})
    schema = build_form_schema(model, request)
    assert schema["properties"][item.oid]["enum"] == ["1"]


# --------------------------------------------------- reconciliation spellings
def test_rave_respelling_a_datetime_is_not_a_mismatch():
    """Rave returns `2025-01-14T09:35:00` for `2025-01-14 09:35` - same instant."""
    from rave_agent.model.study_model import Item, StudyModel
    from rave_agent.reporting.reconciler import _compare

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")
    item = Item(oid="F.DT", name="DT", form_oid="F", data_type="datetime",
                datetime_format="dd MMM yyyy HH:nn")
    model.items[item.oid] = item

    assert _compare(model, item.oid, "2025-01-14 09:35", "2025-01-14T09:35:00") == "normalised"
    assert _compare(model, item.oid, "2025-01-14 09:35", "2025-01-14T11:00:00") == "mismatch"


def test_rave_upper_casing_text_is_not_a_mismatch():
    from rave_agent.model.study_model import Item, StudyModel
    from rave_agent.reporting.reconciler import _compare

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")
    item = Item(oid="F.T", name="T", form_oid="F", data_type="text")
    model.items[item.oid] = item
    assert _compare(model, item.oid, "Headache", "HEADACHE") == "normalised"


def test_absent_is_not_reported_as_a_mismatch():
    """A value Rave never received is a different problem from one it reshaped."""
    from rave_agent.model.study_model import Item, StudyModel
    from rave_agent.reporting.reconciler import _compare

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")
    item = Item(oid="F.T", name="T", form_oid="F", data_type="text")
    model.items[item.oid] = item
    assert _compare(model, item.oid, "anything", None) == "absent"


def test_rave_padding_a_decimal_is_not_a_mismatch():
    """Rave pads to the field's significant digits: 0.2 comes back as 0.20."""
    from rave_agent.model.study_model import Item, StudyModel
    from rave_agent.reporting.reconciler import _compare

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")
    item = Item(oid="F.N", name="N", form_oid="F", data_type="float",
                significant_digits=2)
    model.items[item.oid] = item

    assert _compare(model, item.oid, "0.2", "0.20") == "normalised"
    assert _compare(model, item.oid, "10.0", "10.00") == "normalised"
    # A genuinely different number is still a mismatch.
    assert _compare(model, item.oid, "2", "1") == "mismatch"
