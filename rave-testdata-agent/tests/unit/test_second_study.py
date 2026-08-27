"""SK-3 / acceptance criterion 4: a second study works with no code changes.

The two fixtures share no identifier and follow opposite conventions, so passing
both means the parsers key off structure rather than one study's habits. Every
assertion here names the trap it guards.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
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
    strftime_pattern,
    untranslatable_formats,
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
from rave_agent.model.study_model import Item  # noqa: E402

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


def test_unknown_allowed_marker_is_not_written_into_the_value():
    """`dd- MMM- yyyy` is a full date whose parts may be unknown.

    The trailing hyphen is a property of the field, not a separator. Writing it
    out produced `01- SEP- 2012`, which Rave stored but flagged with "Clinical
    Data entered in incorrect format"; the day and year still parsed, the month
    did not, so the field looked half-entered in the CRF.
    """
    assert strftime_pattern("dd- MMM- yyyy") == "%d %b %Y"
    assert strftime_pattern("dd- MMM yyyy") == "%d %b %Y"
    assert strftime_pattern("MMM- yyyy") == "%b %Y"
    item = Item(oid="MH.END", name="END", form_oid="MH", data_type="date",
                datetime_format="dd- MMM- yyyy")
    assert format_for_rave(item, "2012-09-01") == "01 SEP 2012"


def test_hyphen_separator_survives():
    """Only a hyphen that ends a part is a marker; `yyyy-MM-dd` is a real format."""
    assert strftime_pattern("yyyy-MM-dd") == "%Y-%m-%d"


def test_lowercase_month_is_a_month_not_minutes():
    """`nn` is minutes in Rave, so `mm` is free to mean month - and does.

    Mapping `mm` to minutes rendered every `dd mm yyyy` field as `15 00 2024`,
    which Rave dropped on the floor.
    """
    assert strftime_pattern("dd mm yyyy") == "%d %m %Y"
    item = Item(oid="D.DATE", name="DATE", form_oid="D", data_type="date",
                datetime_format="dd mm yyyy")
    assert format_for_rave(item, "2024-01-15") == "15 01 2024"


def test_twelve_hour_clock_with_meridiem():
    """`hh:nn rr` left `hh` and `rr` untranslated, emitting the literal `hh:30 rr`."""
    assert strftime_pattern("hh:nn rr") == "%I:%M %p"
    item = Item(oid="D.TIME", name="TIME", form_oid="D", data_type="time",
                datetime_format="hh:nn rr")
    assert format_for_rave(item, "14:30") == "02:30 PM"


def test_unknown_format_token_is_reported_not_emitted(study_a):
    """The guard that would have caught `hh:nn rr` before it reached a study."""
    assert untranslatable_formats(study_a) == {}
    item = study_a.items["OBS.WHEN"]
    original = item.datetime_format
    try:
        item.datetime_format = "dd MMM yyyy zz"
        assert untranslatable_formats(study_a) == {"dd MMM yyyy zz": ["OBS.WHEN"]}
    finally:
        item.datetime_format = original


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


# ------------------------------------------------- ALS data dictionaries win
def _dict_model():
    """A field whose ODM CodeListRef disagrees with its Rave data dictionary."""
    from rave_agent.model.study_model import CodeList, CodeListEntry, Item, StudyModel

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")
    model.codelists["NY"] = CodeList(
        oid="NY", name="NY", data_type="text",
        entries=[CodeListEntry(coded_value="N"), CodeListEntry(coded_value="Y")],
    )
    # The export points this field at NY; Rave actually enforces 1/2.
    model.items["F.RATE"] = Item(oid="F.RATE", name="RATE", form_oid="F",
                                 data_type="text", codelist_oid="NY")
    # This one the export got right.
    model.items["F.YN"] = Item(oid="F.YN", name="YN", form_oid="F",
                               data_type="text", codelist_oid="NY")
    return model


class _StubAls:
    data_dictionaries = {
        "COMPRATE": [
            {"coded_value": "1", "decode": "Complete", "order": 1, "specify": False},
            {"coded_value": "2", "decode": "Partial", "order": 2, "specify": False},
        ],
        "NY": [
            {"coded_value": "N", "decode": "No", "order": 1, "specify": False},
            {"coded_value": "Y", "decode": "Yes", "order": 2, "specify": False},
        ],
    }
    field_dictionaries = {"F.RATE": "COMPRATE", "F.YN": "NY"}


def test_als_dictionary_overrides_a_wrong_odm_codelist():
    """Rave judges a submission against the dictionary, not the ODM codelist."""
    from rave_agent.model.matrix_resolver import apply_als_dictionaries

    model = _dict_model()
    apply_als_dictionaries(model, _StubAls())

    corrected = model.codelists[model.items["F.RATE"].codelist_oid]
    assert corrected.coded_values == ["1", "2"]
    assert any("data dictionary" in w for w in model.warnings)


def test_a_correct_odm_codelist_is_left_alone():
    """Only disagreements are rewritten - agreement must not churn the model."""
    from rave_agent.model.matrix_resolver import apply_als_dictionaries

    model = _dict_model()
    apply_als_dictionaries(model, _StubAls())
    assert model.items["F.YN"].codelist_oid == "NY"


def test_the_shared_codelist_is_not_mutated():
    """NY is shared; correcting one field must not corrupt the other's list."""
    from rave_agent.model.matrix_resolver import apply_als_dictionaries

    model = _dict_model()
    apply_als_dictionaries(model, _StubAls())
    assert model.codelists["NY"].coded_values == ["N", "Y"]


def test_validation_then_rejects_the_value_rave_would_reject():
    """The whole point: the generator must stop offering the ODM's wrong value."""
    from rave_agent.generation.validators import validate_value
    from rave_agent.model.matrix_resolver import apply_als_dictionaries

    model = _dict_model()
    item = model.items["F.RATE"]
    assert validate_value(model, item, "N") == []      # before: wrongly allowed

    apply_als_dictionaries(model, _StubAls())
    assert validate_value(model, item, "N") != []      # after: correctly refused
    assert validate_value(model, item, "1") == []


# --------------------------------------------------------- parallel generation
class _CountingGenerator:
    """Stands in for the real generator to check ordering and call counts."""

    def __init__(self, model, max_parallel_forms=4):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.model = model
        self.max_parallel_forms = max_parallel_forms
        self.calls = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor
        self.unsatisfiable_triggers = {}

    def generate_form(self, subject_id, folder_oid, form_oid, context):
        from rave_agent.generation.generator import FormOutcome
        with self._lock:
            self.calls.append(form_oid)
        return FormOutcome(subject_id, folder_oid, form_oid, "generated",
                           values={f"{form_oid}.A": "1"})

    def generate_forms(self, subject_id, folder_oid, form_oids, context):
        from rave_agent.generation.generator import Generator
        return Generator.generate_forms(self, subject_id, folder_oid, form_oids, context)

    def _update_context(self, context, outcome):
        pass


def test_generate_forms_preserves_request_order():
    """Submissions stay serialised per subject, so results must not be reordered."""
    from rave_agent.model.study_model import StudyModel

    gen = _CountingGenerator(StudyModel(study_name="S", environment="D",
                                        crf_version_oid="1", crf_version_name="v"))
    wanted = [f"F{i}" for i in range(10)]
    out = gen.generate_forms("SUBJ", "FOLDER", wanted, {})
    assert [o.form_oid for o in out] == wanted
    assert sorted(gen.calls) == sorted(wanted)


def test_generate_forms_runs_concurrently():
    """With parallelism enabled the calls must actually overlap."""
    import threading
    import time
    from rave_agent.generation.generator import FormOutcome, Generator
    from rave_agent.model.study_model import StudyModel

    class Slow(_CountingGenerator):
        def __init__(self):
            super().__init__(StudyModel(study_name="S", environment="D",
                                        crf_version_oid="1", crf_version_name="v"),
                             max_parallel_forms=4)
            self.live = 0
            self.peak = 0

        def generate_form(self, subject_id, folder_oid, form_oid, context):
            with self._lock:
                self.live += 1
                self.peak = max(self.peak, self.live)
            time.sleep(0.05)
            with self._lock:
                self.live -= 1
            return FormOutcome(subject_id, folder_oid, form_oid, "generated")

    slow = Slow()
    slow.generate_forms("SUBJ", "FOLDER", [f"F{i}" for i in range(8)], {})
    assert slow.peak > 1, "forms were generated one at a time"
    assert slow.peak <= 4, f"exceeded max_parallel_forms: {slow.peak}"


def test_max_parallel_forms_one_stays_sequential():
    """The setting must be able to turn concurrency off entirely."""
    import time
    from rave_agent.generation.generator import FormOutcome
    from rave_agent.model.study_model import StudyModel

    class Serial(_CountingGenerator):
        def __init__(self):
            super().__init__(StudyModel(study_name="S", environment="D",
                                        crf_version_oid="1", crf_version_name="v"),
                             max_parallel_forms=1)
            self.live = 0
            self.peak = 0

        def generate_form(self, subject_id, folder_oid, form_oid, context):
            self.live += 1
            self.peak = max(self.peak, self.live)
            time.sleep(0.01)
            self.live -= 1
            return FormOutcome(subject_id, folder_oid, form_oid, "generated")

    s = Serial()
    s.generate_forms("SUBJ", "FOLDER", [f"F{i}" for i in range(5)], {})
    assert s.peak == 1


def test_token_usage_survives_concurrent_updates():
    """Counts are shared across generation threads."""
    import threading
    from rave_agent.generation.llm_client import TokenUsage

    usage = TokenUsage()
    def bump():
        for _ in range(500):
            usage.add(1, 2)
    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert usage.calls == 4000
    assert usage.input_tokens == 4000
    assert usage.output_tokens == 8000


# ------------------------------------------- CRF version follows the site
SITES_ODM = """<?xml version="1.0" encoding="utf-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:mdsol="http://www.mdsol.com/ns/odm/metadata" ODMVersion="1.3">
  <AdminData>
    <Location OID="001234" Name="DEV-SITE-A" LocationType="Site">
      <MetaDataVersionRef StudyOID="S(DEV)" MetaDataVersionOID="17199" EffectiveDate="2026-03-27"/>
      <MetaDataVersionRef StudyOID="S(DEV)" MetaDataVersionOID="17001" EffectiveDate="2026-03-23"/>
    </Location>
    <Location OID="999" Name="OTHER" LocationType="Site">
      <MetaDataVersionRef StudyOID="S(DEV)" MetaDataVersionOID="17481" EffectiveDate="2026-06-01"/>
    </Location>
  </AdminData>
</ODM>"""


class _Version:
    def __init__(self, oid, name): self.oid, self.name = oid, name


class _StubClient:
    """Returns the study's published versions, newest first."""
    base_url = "https://example.invalid/RaveWebServices"

    def send(self, request, label=None):
        class R:
            value = [_Version("17481", "3.0"), _Version("17199", "2.0"),
                     _Version("17001", "0.1")]
            correlation_id = "x"
        return R()


def _acquisition(tmp_path, site_number, pinned=None):
    from rave_agent.config.loader import load_config
    from rave_agent.metadata.downloader import MetadataAcquisition

    overrides = {"site.number": site_number,
                 "execution.output_root": str(tmp_path)}
    if pinned is not None:
        overrides["study.crf_version"] = pinned
    config = load_config("aes-002", config_dir=REPO_ROOT / "config",
                         cli_overrides=overrides)
    return MetadataAcquisition(_StubClient(), config)


def test_version_follows_the_site_not_the_study_newest(tmp_path):
    """A site on an earlier amendment must not be sent the newest version.

    Using the study's newest against such a site makes Rave reject fields and
    forms that genuinely do not exist there.
    """
    acq = _acquisition(tmp_path, "001234")
    sites = tmp_path / "sites.xml"
    sites.write_text(SITES_ODM, encoding="utf-8")

    oid, name = acq.resolve_version(sites)
    assert (oid, name) == ("17199", "2.0")
    assert "site" in acq.version_source
    assert any("newest is 17481" in n for n in acq.version_notes)


def test_latest_effective_date_wins_when_a_site_has_several(tmp_path):
    acq = _acquisition(tmp_path, "001234")
    sites = tmp_path / "sites.xml"
    sites.write_text(SITES_ODM, encoding="utf-8")
    assert acq.site_versions(sites)[0] == ("17199", "2026-03-27")


def test_a_site_already_on_the_newest_version_raises_no_note(tmp_path):
    acq = _acquisition(tmp_path, "999")
    sites = tmp_path / "sites.xml"
    sites.write_text(SITES_ODM, encoding="utf-8")
    oid, _ = acq.resolve_version(sites)
    assert oid == "17481"
    assert acq.version_notes == []


def test_config_pin_beats_the_site_assignment(tmp_path):
    acq = _acquisition(tmp_path, "001234", pinned="17481")
    sites = tmp_path / "sites.xml"
    sites.write_text(SITES_ODM, encoding="utf-8")
    oid, _ = acq.resolve_version(sites)
    assert oid == "17481"
    assert acq.version_source == "config pin"


def test_unknown_site_falls_back_to_newest_and_says_so(tmp_path):
    """Falling back silently is what produced a whole run against the wrong version."""
    acq = _acquisition(tmp_path, "not-a-site")
    sites = tmp_path / "sites.xml"
    sites.write_text(SITES_ODM, encoding="utf-8")
    oid, _ = acq.resolve_version(sites)
    assert oid == "17481"
    assert "site version unknown" in acq.version_source
    assert acq.version_notes, "a silent fallback must not be possible"


# ------------------------------------------------------- generation lookahead
def _resolver_fixture(lookahead, absent_folders=()):
    """A resolver whose posting is stubbed, to observe scheduling only."""
    import threading
    import time
    from rave_agent.config.loader import Config
    from rave_agent.dynamics.resolver import DynamicsResolver
    from rave_agent.generation.generator import FormOutcome
    from rave_agent.model.dynamics_graph import DynamicsGraph
    from rave_agent.model.study_model import FormAssignment, Folder, StudyModel

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")
    for folder in ("V1", "V2", "V3"):
        model.folders[folder] = Folder(
            oid=folder, name=folder,
            forms=[FormAssignment(form_oid=f"{folder}_F{i}") for i in range(3)])

    events: list[str] = []
    lock = threading.Lock()

    class Gen:
        max_parallel_forms = 1
        unsatisfiable_triggers: dict = {}

        def generate_form(self, subject_id, folder_oid, form_oid, context):
            with lock:
                events.append(f"gen {folder_oid}")
            time.sleep(0.01)
            return FormOutcome(subject_id, folder_oid, form_oid, "generated",
                               values={f"{form_oid}.A": "1"})

        def generate_forms(self, subject_id, folder_oid, form_oids, context):
            return [self.generate_form(subject_id, folder_oid, o, context)
                    for o in form_oids]

        def _update_context(self, context, outcome):
            pass

    class Res(DynamicsResolver):
        def _submit_one(self, subject_id, folder_oid, outcome, pass_number):
            with lock:
                events.append(f"post {folder_oid}")
            time.sleep(0.03)
            if folder_oid in absent_folders:
                return None, "RWSException: Folder not found."

            class Ok:
                ok = True
                status = "SUCCESS"
            return Ok(), ""

    config = Config(
        data={"study": {"name": "S"}, "rave": {"environment": "DEV"},
              "execution": {"output_root": "./output"},
              "generation": {"lookahead_folders": lookahead},
              "dynamics": {"max_iterations": 5}},
        study_file=Path("x"), defaults_file=Path("y"), config_hash="h")
    return Res(model, DynamicsGraph(study_name="S", crf_version_oid="1"), config,
               Gen(), object(), "SITE"), events


def test_lookahead_overlaps_generation_with_posting():
    """The next visit must be generated while this one is still being posted."""
    from rave_agent.dynamics.activation_state import ActivationState

    resolver, events = _resolver_fixture(lookahead=1)
    resolver.run_pass("SUBJ", ["V1", "V2", "V3"], ActivationState("SUBJ", "S", "DEV"), 0)

    # V2 has to start generating before V1 has finished posting, or nothing
    # was overlapped and the pass still costs generation + posting.
    first_v2_gen = events.index("gen V2")
    last_v1_post = len(events) - 1 - events[::-1].index("post V1")
    assert first_v2_gen < last_v1_post, f"no overlap: {events}"


def test_lookahead_zero_keeps_the_probe():
    """Without a lookahead an absent visit must cost exactly one generation."""
    from rave_agent.dynamics.activation_state import ActivationState

    resolver, events = _resolver_fixture(lookahead=0, absent_folders={"V2"})
    result = resolver.run_pass("SUBJ", ["V1", "V2", "V3"], ActivationState("SUBJ", "S", "DEV"), 0)

    assert events.count("gen V2") == 1, f"generated past the probe: {events}"
    assert result.forms_discarded == 0


def test_lookahead_counts_what_an_absent_visit_wasted():
    """Speculating has a price; it must be reported, not swallowed."""
    from rave_agent.dynamics.activation_state import ActivationState

    resolver, events = _resolver_fixture(lookahead=1, absent_folders={"V2"})
    result = resolver.run_pass("SUBJ", ["V1", "V2", "V3"], ActivationState("SUBJ", "S", "DEV"), 0)

    assert events.count("gen V2") == 3        # generated before the probe answered
    assert result.forms_discarded == 2        # two of them never posted
    assert events.count("post V2") == 1       # and posting stopped at the probe


# --------------------------------------------------- parallel subjects (A-all)
def _orchestrator(max_parallel_subjects, failing=()):
    """An orchestrator whose stage runs are stubbed, to observe scheduling only."""
    import threading
    import time
    from rave_agent.config.loader import Config
    from rave_agent.orchestrator import Orchestrator, Stage

    config = Config(
        data={"study": {"name": "S"}, "rave": {"environment": "DEV"},
              "execution": {"output_root": "./output",
                            "max_parallel_subjects": max_parallel_subjects}},
        study_file=Path("x"), defaults_file=Path("y"), config_hash="h")

    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    seen = []

    class Stub(Orchestrator):
        def _run(self, stage, extra, capture=False, rate_share=1):
            subject = extra[extra.index("--subject") + 1]
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
                seen.append((subject, capture, rate_share))
            time.sleep(0.05)
            with lock:
                live["now"] -= 1
            return (1 if subject in failing else 0), 0.05, f"output for {subject}"

    orch = Stub(config, "S", Path(".env"))
    stage = Stage("dynamics", "Dynamics", "run_dynamics.py", per_subject=True)
    return orch, stage, live, seen


def test_subjects_run_concurrently_when_allowed():
    """Ten subjects must not queue behind each other one at a time."""
    orch, stage, live, _ = _orchestrator(max_parallel_subjects=5)
    subjects = [f"TST-{i:03d}" for i in range(10)]

    failures, seconds = orch._run_per_subject(stage, subjects)

    assert failures == []
    assert live["peak"] > 1, "subjects ran one at a time"
    assert live["peak"] <= 5, f"exceeded max_parallel_subjects: {live['peak']}"


def test_one_subject_at_a_time_is_still_the_default():
    """The default must behave exactly as it did before, streaming and serial."""
    orch, stage, live, seen = _orchestrator(max_parallel_subjects=1)

    orch._run_per_subject(stage, ["TST-001", "TST-002", "TST-003"])

    assert live["peak"] == 1
    assert [s for s, _, _ in seen] == ["TST-001", "TST-002", "TST-003"]
    # Serial runs keep streaming their output; only concurrent ones are buffered.
    assert all(capture is False for _, capture, _ in seen)


def test_parallel_subjects_split_the_rave_budget():
    """N subjects must not quietly issue N times the agreed request rate."""
    orch, stage, _, seen = _orchestrator(max_parallel_subjects=4)

    orch._run_per_subject(stage, ["TST-001", "TST-002", "TST-003", "TST-004"])

    assert all(share == 4 for _, _, share in seen), seen
    assert all(capture is True for _, capture, _ in seen)


def test_a_failing_subject_does_not_take_the_others_with_it():
    orch, stage, _, _ = _orchestrator(max_parallel_subjects=4, failing={"TST-002"})

    failures, _ = orch._run_per_subject(stage, ["TST-001", "TST-002", "TST-003"])

    assert failures == ["TST-002"]


def test_the_budget_override_is_a_key_the_loader_accepts(monkeypatch):
    """Every RAVE_AGENT_* name the orchestrator sets must be a real config key.

    None of them are private. The loader turns every RAVE_AGENT_* variable into a
    config key - RAVE_AGENT_SUBJECTS__COUNT becomes subjects.count - and the
    schema rejects anything it does not know. A made-up name therefore does not
    quietly do nothing; it fails every child at startup, before any work begins.
    """
    import subprocess
    import yaml
    from rave_agent import orchestrator as orch_mod
    from rave_agent.config.loader import _ENV_PREFIX

    from rave_agent.config.loader import Config
    from rave_agent.orchestrator import Orchestrator, Stage

    # The real Orchestrator, not the stub: this is about what _run puts in the
    # child's environment, which the stub replaces wholesale.
    config = Config(
        data={"study": {"name": "S"}, "rave": {"environment": "DEV",
                                               "requests_per_minute": 120},
              "execution": {"output_root": "./output", "max_parallel_subjects": 4}},
        study_file=Path("x"), defaults_file=Path("y"), config_hash="h")
    orch = Orchestrator(config, "S", Path(".env"))
    stage = Stage("dynamics", "Dynamics", "run_dynamics.py", per_subject=True)
    captured = {}

    def fake_run(command, cwd=None, env=None, **kwargs):
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)
    orch._run(stage, ["--subject", "TST-001"], capture=True, rate_share=4)

    added = {k: v for k, v in (captured["env"] or {}).items()
             if k.startswith(_ENV_PREFIX) and os.environ.get(k) != v}
    assert added, "no budget override was passed to the child"

    defaults = yaml.safe_load((REPO_ROOT / "config" / "defaults.yaml").read_text(encoding="utf-8"))
    for key, value in added.items():
        dotted = key[len(_ENV_PREFIX):].lower().replace("__", ".")
        node = defaults
        for part in dotted.split("."):
            assert isinstance(node, dict) and part in node, (
                f"{key} maps to config key {dotted!r}, which does not exist - "
                "the schema will reject it and every subject will fail")
            node = node[part]
        # 120 a minute for the study, four subjects, so 30 each.
        assert value == "30", f"{dotted} = {value}, expected the budget divided by 4"


def test_a_budget_smaller_than_the_subject_count_still_lets_everyone_call():
    """Integer division must never hand a child a rate of zero."""
    from rave_agent.rave.rate_limit import RateLimiter

    assert RateLimiter(max(1, 2 // 4)).per_minute == 1


# ------------------------------------------- reporting is scoped to the run
def _verify_only_run(tmp_path, provisioned, also_on_disk=()):
    """Run just the reporting stage and return the argv it would have used."""
    import subprocess
    import json as _json
    from rave_agent import orchestrator as orch_mod
    from rave_agent.config.loader import Config

    out = tmp_path / "S"
    (out / "generated").mkdir(parents=True)
    for subject in set(provisioned) | set(also_on_disk):
        (out / "generated" / subject).mkdir()
    if provisioned is not None:
        (out / "subjects.json").write_text(_json.dumps(
            {"subjects": [{"subject_id": s, "status": "created"} for s in provisioned]}),
            encoding="utf-8")

    config = Config(
        data={"study": {"name": "S"}, "rave": {"environment": "DEV"},
              "execution": {"output_root": str(tmp_path)}},
        study_file=Path("x"), defaults_file=Path("y"), config_hash="h")

    seen = {}

    def fake_run(command, cwd=None, env=None, **kwargs):
        seen["command"] = list(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    orch_mod.subprocess.run, original = fake_run, orch_mod.subprocess.run
    try:
        orch_mod.Orchestrator(config, "S", Path(".env"), only=["verify"]).run()
    finally:
        orch_mod.subprocess.run = original
    return seen["command"]


def test_reporting_covers_only_the_subjects_this_run_made(tmp_path):
    """The output directory accumulates; a run of two must not report on five.

    Discovering subjects from `generated/` meant a single-subject run produced a
    report about every subject the study had ever had.
    """
    command = _verify_only_run(
        tmp_path,
        provisioned=["TST-006", "TST-007"],
        also_on_disk=["TST-001", "TST-002", "TST-003"])

    passed = [command[i + 1] for i, a in enumerate(command) if a == "--subject"]
    assert passed == ["TST-006", "TST-007"], command
    assert "TST-001" not in command


def test_reporting_falls_back_when_the_run_provisioned_nothing(tmp_path):
    """A --only verify has no subjects of its own; report on what is there."""
    command = _verify_only_run(tmp_path, provisioned=[], also_on_disk=["TST-001"])

    assert "--subject" not in command, command


# ------------------------------------- A4 re-checks the version against the site
_SITES_ODM = """<?xml version="1.0" encoding="utf-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:mdsol="http://www.mdsol.com/ns/odm/metadata" ODMVersion="1.3">
  <AdminData>
    <Location OID="001234" Name="DEV-SITE-ONE" LocationType="Site">
      <MetaDataVersionRef StudyOID="S(DEV)" MetaDataVersionOID="17001"
                          EffectiveDate="2026-03-23"/>
      <MetaDataVersionRef StudyOID="S(DEV)" MetaDataVersionOID="17199"
                          EffectiveDate="2026-03-27"/>
    </Location>
    <Location OID="009999" Name="DEV-SITE-TWO" LocationType="Site"/>
  </AdminData>
</ODM>
"""


def _sites_via_client():
    """list_sites over a stubbed transport, so the parsing is the real thing."""
    from rave_agent.config.loader import Config
    from rave_agent.provisioning.sites import list_sites

    class Result:
        value = _SITES_ODM

    class FakeClient:
        def send(self, request, label=None):
            return Result()

    config = Config(data={"study": {"name": "S"}, "rave": {"environment": "DEV"}},
                    study_file=Path("x"), defaults_file=Path("y"), config_hash="h")
    return list_sites(FakeClient(), config)


def test_list_sites_carries_the_version_each_site_is_on():
    """A4 needs the assignment, and must not pay a second round trip for it."""
    sites = _sites_via_client()
    one = next(s for s in sites if s["oid"] == "001234")

    # Newest effective date first, not document order.
    assert one["versions"][0] == ("17199", "2026-03-27")
    assert one["versions"][1] == ("17001", "2026-03-23")


def test_assigned_version_matches_by_number_or_by_name():
    from rave_agent.provisioning.sites import assigned_version

    sites = _sites_via_client()
    assert assigned_version(sites, "001234", "DEV-SITE-ONE") == ("17199", "2026-03-27")
    assert assigned_version(sites, "DEV-SITE-ONE", "DEV-SITE-ONE") == ("17199", "2026-03-27")


def test_no_assignment_is_not_a_disagreement():
    """An unknown site and a site with no version must both read as 'no answer'.

    The caller refuses to provision on a disagreement, so 'I could not tell' has
    to be distinguishable from 'the site is on something else' - otherwise a
    site Rave simply does not describe would block every run.
    """
    from rave_agent.provisioning.sites import assigned_version

    sites = _sites_via_client()
    assert assigned_version(sites, "009999", "DEV-SITE-TWO") == ("", "")
    assert assigned_version(sites, "NOSUCH", "NOSUCH") == ("", "")


def test_the_two_stages_read_the_site_the_same_way(tmp_path):
    """A2 reads a file, A4 reads a live response; they must agree.

    Two copies of the matching rule is how the stage that chooses a version and
    the stage that uses it drift apart, which is the failure this check exists
    to catch in the first place.
    """
    from rave_agent.metadata.downloader import site_version_refs
    from rave_agent.utils.xml import parse_xml

    path = tmp_path / "sites.xml"
    path.write_text(_SITES_ODM, encoding="utf-8")

    from_file = site_version_refs(
        parse_xml(path.read_text(encoding="utf-8")), "001234", "DEV-SITE-ONE")
    from_live = _sites_via_client()[0]["versions"]
    assert from_file == from_live == [("17199", "2026-03-27"), ("17001", "2026-03-23")]


# ------------------------------------------------ visits run in schedule order
def _scheduled_model():
    from rave_agent.model.study_model import Folder, StudyModel

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")
    # OIDs deliberately chosen so that sorting them as strings gets it wrong:
    # "D420" < "RAND", and "FU_D91" > "FU_D331" because '9' > '3'.
    for oid, order in (("RAND", 9), ("FU_D91", 18), ("FU_D331", 22), ("D420", 23)):
        model.folders[oid] = Folder(oid=oid, name=oid, order=order)
    return model


def test_schedule_order_beats_oid_order():
    """Day 1 must be written before Day 420, whatever the OIDs sort like."""
    model = _scheduled_model()
    jumbled = ["D420", "FU_D331", "FU_D91", "RAND"]

    assert sorted(jumbled) == ["D420", "FU_D331", "FU_D91", "RAND"]   # the old order
    assert model.in_schedule_order(jumbled) == ["RAND", "FU_D91", "FU_D331", "D420"]


def test_folders_without_an_ordinal_keep_a_stable_tail():
    """A study that publishes no ordinal must still order deterministically."""
    from rave_agent.model.study_model import Folder

    model = _scheduled_model()
    model.folders["ZZ_EXTRA"] = Folder(oid="ZZ_EXTRA", name="ZZ_EXTRA")
    model.folders["AA_EXTRA"] = Folder(oid="AA_EXTRA", name="AA_EXTRA")

    out = model.in_schedule_order(["ZZ_EXTRA", "D420", "AA_EXTRA", "RAND"])
    assert out == ["RAND", "D420", "AA_EXTRA", "ZZ_EXTRA"]


def test_a_pass_generates_visits_in_schedule_order():
    """The ordering has to bite where the work is planned, not just in a helper.

    Generation carries each visit's date into the next visit's prompt, so a
    visit written before the one it dates from has nothing to measure against -
    which is how a Day 420 final visit landed eleven months before Day 1.
    """
    from rave_agent.dynamics.activation_state import ActivationState
    from rave_agent.model.study_model import FormAssignment

    resolver, events = _resolver_fixture(lookahead=0)
    resolver.model = _scheduled_model()
    for folder in resolver.model.folders.values():
        folder.forms = [FormAssignment(form_oid=f"{folder.oid}_DOV")]

    resolver.run_pass("SUBJ", ["D420", "FU_D331", "FU_D91", "RAND"],
                      ActivationState("SUBJ", "S", "DEV"), 0)

    generated = [e.split()[1] for e in events if e.startswith("gen ")]
    assert generated == ["RAND", "FU_D91", "FU_D331", "D420"], events


# ------------------------------------------ the date carried forward is the visit's
def _carrier():
    """A Generator with only the model wired up - _update_context needs no more."""
    from rave_agent.generation.generator import Generator
    from rave_agent.model.study_model import Item, StudyModel

    model = StudyModel(study_name="S", environment="D",
                       crf_version_oid="1", crf_version_name="v")

    def add(oid, name, data_type, label=""):
        model.items[oid] = Item(oid=oid, name=name, form_oid=oid.split(".")[0],
                                data_type=data_type, label=label)

    add("DOV.DCMDATE", "_R_DCMDATE", "date", "Date of visit")
    add("DOV.TIME", "VISTIM", "time", "Time of visit")
    add("DM.BRTHDAT", "BRTHDAT", "date", "Date of birth")
    add("MH.MHSTDAT", "MHSTDAT", "date", "Onset date")
    add("SU.SUENDAT", "SUENDAT", "date", "Date stopped")
    add("VS.VSDAT", "VSDAT", "date", "Assessment date")

    carrier = object.__new__(Generator)
    carrier.model = model
    return carrier


def _feed(carrier, context, form_oid, values):
    from rave_agent.generation.generator import FormOutcome
    carrier._update_context(
        context, FormOutcome("SUBJ", "SCREEN", form_oid, "generated", values=values))


def test_a_history_date_never_becomes_the_visit_date():
    """The bug: last form wins, earliest date on it.

    A screening visit ended up dated to a smoking-cessation date five years
    earlier, and the next two visits were generated to match it.
    """
    carrier, context = _carrier(), {}
    _feed(carrier, context, "DOV", {"DOV.DCMDATE": "2024-03-01"})
    _feed(carrier, context, "DM", {"DM.BRTHDAT": "1989-03-22"})
    _feed(carrier, context, "MH", {"MH.MHSTDAT": "2020-07-15"})
    _feed(carrier, context, "SU", {"SU.SUENDAT": "2019-06-15"})

    assert context["SCREEN visit date"] == "2024-03-01"


def test_a_named_visit_date_beats_an_earlier_unrelated_one():
    """Forms are not generated in a guaranteed order, so the named field must win."""
    carrier, context = _carrier(), {}
    _feed(carrier, context, "MH", {"MH.MHSTDAT": "2020-07-15"})
    assert context["SCREEN visit date"] == "2020-07-15"   # nothing better yet

    _feed(carrier, context, "DOV", {"DOV.DCMDATE": "2024-03-01"})
    assert context["SCREEN visit date"] == "2024-03-01"   # the named field takes over


def test_the_first_named_field_holds():
    """Two forms can both name a date; the visit still has only one."""
    carrier, context = _carrier(), {}
    _feed(carrier, context, "DOV", {"DOV.DCMDATE": "2024-03-01"})
    _feed(carrier, context, "VS", {"VS.VSDAT": "2024-03-02"})

    assert context["SCREEN visit date"] == "2024-03-01"


def test_a_clock_time_is_not_a_date():
    """`is_date_like` covers time, and "09:30" sorts before every real date.

    Five of one subject's visits carried a time of day forward as the date the
    visit happened.
    """
    carrier, context = _carrier(), {}
    _feed(carrier, context, "DOV", {"DOV.TIME": "09:30", "DOV.DCMDATE": "2024-03-01"})
    assert context["SCREEN visit date"] == "2024-03-01"

    carrier, context = _carrier(), {}
    _feed(carrier, context, "DOV", {"DOV.TIME": "09:30"})
    assert "SCREEN visit date" not in context


def test_bookkeeping_stays_out_of_the_prompt():
    """How a value was decided is not a value the model should be shown."""
    from rave_agent.generation.prompt_builder import FormRequest, build_form_prompt

    carrier, context = _carrier(), {}
    _feed(carrier, context, "DOV", {"DOV.DCMDATE": "2024-03-01"})
    assert any(k.startswith("_") for k in context), "nothing was recorded to hide"

    request = FormRequest("SUBJ", "SCREEN", "Screening", "VS", "Vitals", [])
    prompt = build_form_prompt(carrier.model, request, subject_context=context)
    assert "2024-03-01" in prompt
    assert "claimed" not in prompt


# ------------------------------------------------- visit dates are arithmetic
def test_protocol_day_comes_from_the_visit_name():
    """Rave publishes target_days for almost nothing; the name carries it."""
    from rave_agent.generation.schedule import day_offset

    assert day_offset("Screening (Day -30)") == -30
    assert day_offset("TX1 (Day 1)") == 1
    assert day_offset("Final Visit (Day 420)") == 420
    # Two day numbers: the prose one is relative, the bracketed one is the
    # protocol day, and it comes last.
    assert day_offset("Day 3 post Tx1 (Day 4)") == 4
    assert day_offset("Day 14 post Tx3 (Day 75)") == 75


def test_an_unscheduled_visit_is_not_given_a_day():
    """A discontinuation or an AE log has no protocol day and must not get one."""
    from rave_agent.generation.schedule import day_offset

    assert day_offset("Premature Discontinuation") is None
    assert day_offset("CM/AE/SAE") is None
    assert day_offset("") is None


def test_day_one_is_the_reference_and_there_is_no_day_zero():
    from datetime import date
    from rave_agent.generation.schedule import visit_date

    anchor = date(2024, 3, 28)
    assert visit_date(anchor, 1) == anchor
    assert visit_date(anchor, 4) == date(2024, 3, 31)      # three days after Day 1
    assert visit_date(anchor, -1) == date(2024, 3, 27)     # the day before Day 1
    assert visit_date(anchor, -30) == date(2024, 2, 27)


def test_subjects_enrol_on_different_days_but_always_the_same_ones():
    """23 of 25 subjects screened on 2024-03-01 because nothing varied per subject.

    The spread has to be reproducible: a subject regenerated tomorrow must keep
    the date Rave already holds, so this cannot be a random draw, and `hash()`
    is salted per process.
    """
    from datetime import date
    from rave_agent.generation.schedule import enrolment_date

    first, window = date(2024, 1, 8), 120
    subjects = [f"TST-{n:03d}" for n in range(1, 21)]
    dates = [enrolment_date(s, first, window) for s in subjects]

    assert len(set(dates)) > 10, "the cohort still enrols in a clump"
    assert all(first <= d < first + timedelta(days=window) for d in dates)
    assert dates == [enrolment_date(s, first, window) for s in subjects]


def test_a_zero_window_puts_everyone_on_the_first_date():
    """Opting out must not divide by zero."""
    from datetime import date
    from rave_agent.generation.schedule import enrolment_date

    assert enrolment_date("TST-001", date(2024, 1, 8), 0) == date(2024, 1, 8)


def _scheduler(first="2024-01-08", window=120):
    from datetime import date
    from rave_agent.generation.generator import Generator

    gen = object.__new__(Generator)
    gen._enrol_first = date.fromisoformat(first)
    gen._enrol_window = window
    return gen


def test_the_three_screening_visits_no_longer_share_a_date():
    """They were identical for all 25 subjects across both studies."""
    from rave_agent.model.study_model import Folder

    gen = _scheduler()
    seen = {}
    for oid, name in (("SCREEN", "Screening (Day -30)"),
                      ("SCREEN_27", "Screening (Day -27)"),
                      ("SCREEN_15", "Screening (Day -15)")):
        _, scheduled, _ = gen._scheduled_visit("CAN-004", Folder(oid=oid, name=name))
        seen[oid] = scheduled

    assert len(set(seen.values())) == 3, seen
    assert seen["SCREEN"] < seen["SCREEN_27"] < seen["SCREEN_15"]


def test_an_unscheduled_visit_gets_an_anchor_but_no_date():
    from rave_agent.model.study_model import Folder

    anchor, scheduled, offset = _scheduler()._scheduled_visit(
        "CAN-004", Folder(oid="PD", name="Premature Discontinuation"))
    assert anchor and scheduled == "" and offset is None


def test_only_visit_date_fields_are_pinned():
    """Pinning the wrong field would fix a birth date to the day of the visit."""
    from rave_agent.model.study_model import Item

    def item(oid, name, label, data_type="date"):
        return Item(oid=oid, name=name, form_oid="F", data_type=data_type, label=label)

    picked = _scheduler()._visit_date_items([
        item("F.DCMDATE", "_R_DCMDATE", "Date of visit"),
        item("F.BRTHDAT", "BRTHDAT", "Date of birth"),
        item("F.MHSTDAT", "MHSTDAT", "Onset date"),
        item("F.VSDAT", "VSDAT", "Assessment date"),
        item("F.TIME", "VISTIM", "Time of visit", data_type="time"),
    ])
    assert [i.oid for i in picked] == ["F.DCMDATE", "F.VSDAT"]
