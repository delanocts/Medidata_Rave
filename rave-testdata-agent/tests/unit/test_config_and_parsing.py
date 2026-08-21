"""Unit tests for the deterministic layers: config, XML repair, ODM parsing, graph."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rave_agent.config.loader import ConfigError, load_config  # noqa: E402
from rave_agent.config.secrets import Secrets, redact, register_secret  # noqa: E402
from rave_agent.model.als_parser import (  # noqa: E402
    _conjuncts,
    _parse_matrix_sheet,
    _parse_rpn,
)
from rave_agent.model.dynamics_graph import (  # noqa: E402
    ActivationEdge,
    Condition,
    DynamicsGraph,
)
from rave_agent.model.spreadsheetml import Workbook  # noqa: E402
from rave_agent.model.odm_parser import parse_odm  # noqa: E402
from rave_agent.utils.xml import parse_xml, to_bytes  # noqa: E402

CONFIG_DIR = REPO_ROOT / "config"

MINIMAL_ODM = """<?xml version="1.0" encoding="utf-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:mdsol="http://www.mdsol.com/ns/odm/metadata" ODMVersion="1.3">
  <Study OID="S(DEV)">
    <MetaDataVersion OID="99" Name="v1" mdsol:PrimaryFormOID="ENTRY_F"
                     mdsol:DefaultMatrixOID="DEFAULT">
      <StudyEventDef OID="VISIT1" Name="Visit One" Type="Common" Repeating="No">
        <FormRef FormOID="ENTRY_F" Mandatory="Yes" OrderNumber="1"/>
      </StudyEventDef>
      <FormDef OID="ENTRY_F" Name="Entry" Repeating="Yes">
        <ItemGroupRef ItemGroupOID="ENTRY_G" Mandatory="Yes"/>
        <ItemGroupRef ItemGroupOID="ENTRY_LOG" Mandatory="No"/>
      </FormDef>
      <ItemGroupDef OID="ENTRY_G" Name="Main" Repeating="No">
        <ItemRef ItemOID="ENTRY_F.SEX" OrderNumber="1" Mandatory="Yes"/>
        <ItemRef ItemOID="ENTRY_F.SBP" OrderNumber="2" Mandatory="No"/>
      </ItemGroupDef>
      <ItemGroupDef OID="ENTRY_LOG" Name="Log" Repeating="Yes">
        <ItemRef ItemOID="ENTRY_F.NOTE" OrderNumber="1" Mandatory="No"/>
      </ItemGroupDef>
      <ItemDef OID="ENTRY_F.SEX" Name="SEX" DataType="text" Length="1" mdsol:Visible="Yes">
        <Question><TranslatedText xml:lang="ja">SEIBETSU</TranslatedText>
                  <TranslatedText xml:lang="en">Sex</TranslatedText></Question>
        <CodeListRef CodeListOID="SEXCL"/>
      </ItemDef>
      <ItemDef OID="ENTRY_F.SBP" Name="SBP" DataType="integer" Length="3" mdsol:Visible="No">
        <Question><TranslatedText xml:lang="en">Systolic BP</TranslatedText></Question>
        <RangeCheck Comparator="GE" SoftHard="Soft"><CheckValue>60</CheckValue></RangeCheck>
        <RangeCheck Comparator="LE" SoftHard="Soft"><CheckValue>250</CheckValue></RangeCheck>
      </ItemDef>
      <ItemDef OID="ENTRY_F.NOTE" Name="NOTE" DataType="text" Length="50"/>
      <CodeList OID="SEXCL" Name="Sex" DataType="text">
        <CodeListItem CodedValue="M" mdsol:OrderNumber="1">
          <Decode><TranslatedText xml:lang="en">Male</TranslatedText></Decode>
        </CodeListItem>
        <CodeListItem CodedValue="F" mdsol:OrderNumber="2">
          <Decode><TranslatedText xml:lang="en">Female</TranslatedText></Decode>
        </CodeListItem>
      </CodeList>
    </MetaDataVersion>
  </Study>
</ODM>
"""


@pytest.fixture
def model(tmp_path):
    path = tmp_path / "metadata.xml"
    path.write_text(MINIMAL_ODM, encoding="utf-8")
    return parse_odm(path, "S", "DEV")


# --------------------------------------------------------------------- config
def test_valid_study_config_loads():
    config = load_config("aes-002", config_dir=CONFIG_DIR)
    assert config.study_env == "AES-002(DEV)"
    assert config.base_url.startswith("https://")


@pytest.mark.parametrize("environment", ["PROD", "Prod", "production", "LIVE", "prd"])
def test_production_is_always_refused(environment):
    """SEC-4: no config value may unblock a production target."""
    with pytest.raises(ConfigError) as excinfo:
        load_config("aes-002", config_dir=CONFIG_DIR,
                    cli_overrides={"rave.environment": environment})
    assert any("production" in p.lower() for p in excinfo.value.problems)


def test_tls_verification_cannot_be_disabled():
    with pytest.raises(ConfigError):
        load_config("aes-002", config_dir=CONFIG_DIR,
                    cli_overrides={"rave.verify_tls": False})


def test_all_problems_reported_at_once():
    """CFG-1: fail fast with every problem, not one at a time."""
    with pytest.raises(ConfigError) as excinfo:
        load_config("aes-002", config_dir=CONFIG_DIR, cli_overrides={
            "subjects.count": 0,
            "generation.temperature": 9,
            "generation.batch_scope": "nonsense",
        })
    assert len(excinfo.value.problems) >= 3


def test_unknown_environment_rejected():
    with pytest.raises(ConfigError):
        load_config("aes-002", config_dir=CONFIG_DIR,
                    cli_overrides={"rave.environment": "Staging"})


# -------------------------------------------------------------------- secrets
def test_secrets_are_redacted_and_never_repr():
    register_secret("p@ssw0rd-value")
    assert "p@ssw0rd-value" not in redact("auth=p@ssw0rd-value")
    rendered = repr(Secrets("user", "p@ssw0rd-value", "sk-ant-key-value"))
    assert "p@ssw0rd-value" not in rendered
    assert "sk-ant-key-value" not in rendered


# ------------------------------------------------------------------ xml repair
def test_plain_utf8_bom_is_stripped():
    assert parse_xml("﻿<?xml version='1.0'?><a/>").tag == "a"


def test_latin1_misdecoded_bom_is_repaired():
    """rwslib decodes dataset responses as latin-1, mangling the BOM."""
    original = '<?xml version="1.0" encoding="utf-8"?><a>café</a>'
    wire = ("﻿" + original).encode("utf-8").decode("latin-1")
    element = parse_xml(wire)
    assert element.tag == "a"
    assert element.text == "café"  # the mis-decode is fully reversed


def test_leading_whitespace_tolerated():
    assert parse_xml("\r\n\t<?xml version='1.0'?><a/>").tag == "a"


def test_to_bytes_accepts_bytes():
    assert to_bytes(b"\xef\xbb\xbf<a/>") == b"<a/>"


# --------------------------------------------------------------- odm parsing
def test_structure_parsed(model):
    assert model.crf_version_oid == "99"
    assert model.primary_form_oid == "ENTRY_F"
    assert model.default_matrix_oid == "DEFAULT"
    assert set(model.forms) == {"ENTRY_F"}
    assert set(model.folders) == {"VISIT1"}


def test_english_question_preferred_over_other_languages(model):
    assert model.items["ENTRY_F.SEX"].label == "Sex"


def test_codelist_entries_ordered_with_decodes(model):
    codelist = model.codelists["SEXCL"]
    assert codelist.coded_values == ["M", "F"]
    assert codelist.entries[0].decode == "Male"


def test_mandatory_comes_from_itemref(model):
    assert model.items["ENTRY_F.SEX"].mandatory is True
    assert model.items["ENTRY_F.SBP"].mandatory is False


def test_range_checks_captured(model):
    ranges = model.items["ENTRY_F.SBP"].ranges
    assert [r.comparator for r in ranges] == ["GE", "LE"]
    assert ranges[0].values == ["60"]


def test_hidden_items_detected(model):
    assert [i.oid for i in model.hidden_items()] == ["ENTRY_F.SBP"]


def test_log_detection_uses_item_groups_not_form_flag(model):
    """FormDef@Repeating is unreliable in Rave; repeating item groups are the signal."""
    assert model.forms["ENTRY_F"].repeating is True
    assert [g.oid for g in model.log_item_groups("ENTRY_F")] == ["ENTRY_LOG"]


def test_item_form_association(model):
    assert model.items["ENTRY_F.SBP"].form_oid == "ENTRY_F"
    assert len(model.items_for_form("ENTRY_F")) == 3


def test_no_spurious_warnings(model):
    assert model.warnings == []


def test_dangling_reference_is_warned_not_dropped(tmp_path):
    broken = MINIMAL_ODM.replace('CodeListOID="SEXCL"', 'CodeListOID="MISSING"')
    path = tmp_path / "broken.xml"
    path.write_text(broken, encoding="utf-8")
    parsed = parse_odm(path, "S", "DEV")
    assert any("MISSING" in w for w in parsed.warnings)
    assert "ENTRY_F.SEX" in parsed.items  # recorded, not discarded


# ------------------------------------------------------------- dynamics graph
def _cond(field_oid, value, complete=True):
    return Condition(assignments=[{"field_oid": field_oid, "value": value,
                                   "operator": "IsEqualTo", "form_oid": "", "folder_oid": ""}],
                     complete=complete)


def test_cycle_detection():
    graph = DynamicsGraph(study_name="S", crf_version_oid="99")
    graph.add_edge(ActivationEdge("field", "B", "dynamic_by_edit_check", _cond("A", "Y")))
    graph.add_edge(ActivationEdge("field", "A", "dynamic_by_edit_check", _cond("B", "Y")))
    assert graph.detect_cycles(), "A->B->A should be reported"


def test_acyclic_graph_reports_no_cycles():
    graph = DynamicsGraph(study_name="S", crf_version_oid="99")
    graph.add_edge(ActivationEdge("form", "F2", "dynamic_by_edit_check", _cond("F1.YN", "Y")))
    assert graph.detect_cycles() == []


def test_trigger_index_built():
    graph = DynamicsGraph(study_name="S", crf_version_oid="99")
    graph.add_edge(ActivationEdge("form", "AE_F", "dynamic_by_edit_check", _cond("F1.AEYN", "Y")))
    assert graph.edges_for_trigger("F1.AEYN")
    assert graph.trigger_fields["F1.AEYN"] == ["AE_F"]


def test_multi_assignment_condition_indexes_every_field():
    """A conjunction registers under each field it needs."""
    graph = DynamicsGraph(study_name="S", crf_version_oid="99")
    condition = Condition(assignments=[
        {"field_oid": "A.X", "value": "N", "operator": "IsEqualTo", "form_oid": "", "folder_oid": ""},
        {"field_oid": "B.Y", "value": "1", "operator": "IsEqualTo", "form_oid": "", "folder_oid": ""},
    ])
    graph.add_edge(ActivationEdge("matrix", "PRIMARY", "dynamic_by_matrix_add", condition))
    assert set(graph.trigger_fields) == {"A.X", "B.Y"}
    assert graph.required_values() == {"A.X": {"N"}, "B.Y": {"1"}}


def test_incomplete_condition_is_flagged_not_hidden():
    condition = Condition(assignments=[{"field_oid": "A.X", "value": "N",
                                        "operator": "IsEqualTo", "form_oid": "", "folder_oid": ""}],
                          complete=False)
    assert "(partial)" in condition.describe()


def test_uninvertible_condition_describes_itself():
    assert Condition().describe() == "(condition not invertible)"


# ------------------------------------------------------------------ ALS (RPN)
def test_rpn_conjunction_inverted_to_assignments():
    """Postfix: fieldA, 'N', IsEqualTo, fieldB, '120', IsEqualTo, And"""
    steps = [
        {"stepordinal": "1", "formoid": "ENR_F", "fieldoid": "YN", "dataformat": "CodedValue"},
        {"stepordinal": "2", "staticvalue": "N", "dataformat": "$200"},
        {"stepordinal": "3", "checkfunction": "IsEqualTo"},
        {"stepordinal": "4", "formoid": "DS_F", "fieldoid": "CAT", "dataformat": "CodedValue"},
        {"stepordinal": "5", "staticvalue": "120", "dataformat": "$200"},
        {"stepordinal": "6", "checkfunction": "IsEqualTo"},
        {"stepordinal": "7", "checkfunction": "And"},
    ]
    tree, parsed = _parse_rpn(steps)
    assert parsed
    assignments, complete = _conjuncts(tree)
    assert complete
    assert [(a.field_oid, a.value) for a in assignments] == [("ENR_F.YN", "N"), ("DS_F.CAT", "120")]


def test_rpn_or_marks_condition_incomplete():
    steps = [
        {"stepordinal": "1", "formoid": "F", "fieldoid": "A", "dataformat": "CodedValue"},
        {"stepordinal": "2", "staticvalue": "1", "dataformat": "$200"},
        {"stepordinal": "3", "checkfunction": "IsEqualTo"},
        {"stepordinal": "4", "formoid": "F", "fieldoid": "A", "dataformat": "CodedValue"},
        {"stepordinal": "5", "staticvalue": "2", "dataformat": "$200"},
        {"stepordinal": "6", "checkfunction": "IsEqualTo"},
        {"stepordinal": "7", "checkfunction": "Or"},
    ]
    tree, parsed = _parse_rpn(steps)
    assert parsed
    _, complete = _conjuncts(tree)
    assert complete is False, "an Or means the listed values are not the only way"


def test_rpn_unary_operator_is_not_invertible():
    steps = [
        {"stepordinal": "1", "formoid": "F", "fieldoid": "A", "dataformat": "StandardValue"},
        {"stepordinal": "2", "checkfunction": "IsNotEmpty"},
    ]
    tree, parsed = _parse_rpn(steps)
    assert parsed
    assignments, complete = _conjuncts(tree)
    assert assignments == [] and complete is False


def test_malformed_rpn_reports_incomplete_rather_than_guessing():
    steps = [{"stepordinal": "1", "checkfunction": "IsEqualTo"}]  # operator, no operands
    _, parsed = _parse_rpn(steps)
    assert parsed is False


def test_matrix_grid_parsed_from_real_als_shape(tmp_path):
    """Matrix tabs are grids: rows are forms, columns folders, X = assigned."""
    sheet = """<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="Matrix1#PRIMARY"><Table>
  <Row><Cell><Data>Matrix: PRIMARY</Data></Cell><Cell><Data>Subject</Data></Cell>
       <Cell><Data>SCREEN</Data></Cell><Cell><Data>RAND</Data></Cell></Row>
  <Row><Cell><Data>DOV_F</Data></Cell><Cell ss:Index="3"><Data>X</Data></Cell>
       <Cell><Data>X</Data></Cell></Row>
  <Row><Cell><Data>DS_RN_F</Data></Cell><Cell ss:Index="4"><Data>X</Data></Cell></Row>
 </Table></Worksheet>
</Workbook>"""
    path = tmp_path / "als.xls"
    path.write_text(sheet, encoding="utf-8")
    grid = _parse_matrix_sheet(Workbook(path), "Matrix1#PRIMARY")
    assert grid == {"SCREEN": ["DOV_F"], "RAND": ["DOV_F", "DS_RN_F"]}


def test_sparse_row_index_respected(tmp_path):
    """ss:Index is 1-based and skips empty cells; misreading it shifts every column."""
    sheet = """<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="S"><Table>
  <Row><Cell><Data>a</Data></Cell><Cell ss:Index="4"><Data>d</Data></Cell></Row>
 </Table></Worksheet>
</Workbook>"""
    path = tmp_path / "s.xls"
    path.write_text(sheet, encoding="utf-8")
    assert list(Workbook(path).rows("S"))[0] == ["a", "", "", "d"]
