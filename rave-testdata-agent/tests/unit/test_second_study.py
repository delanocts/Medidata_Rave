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
