"""The console is a viewer, so what is tested is that it cannot lie or overwrite.

No HTTP here - the handler is a thin shell over these functions, and the parts
worth guarding are the ones that touch config files on disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "web"))

import server  # noqa: E402
from fields import GROUPS, all_fields  # noqa: E402


def test_every_form_field_is_a_real_config_key():
    """A field the schema does not know is a control that silently does nothing."""
    import json

    schema = json.loads((REPO_ROOT / "config" / "config.schema.json").read_text(encoding="utf-8"))

    def known(dotted: str) -> bool:
        node = schema
        for part in dotted.split("."):
            props = node.get("properties") or {}
            if part not in props:
                return False
            node = props[part]
        return True

    unknown = [f["key"] for f in all_fields() if not known(f["key"])]
    assert unknown == [], f"fields absent from config.schema.json: {unknown}"


def test_fields_are_declared_once():
    keys = [f["key"] for f in all_fields()]
    assert len(keys) == len(set(keys))
    assert all(g["fields"] for g in GROUPS)


def test_dotted_helpers_round_trip():
    data = {}
    server.plant(data, "generation.log_records.min", 5)
    server.plant(data, "generation.log_records.max", 10)
    assert data == {"generation": {"log_records": {"min": 5, "max": 10}}}
    assert server.dig(data, "generation.log_records.max") == 10
    assert server.dig(data, "generation.nope.deeper") is None


def test_prune_drops_hollow_branches():
    assert server.prune({"a": {"b": None}, "c": {"d": 1}}) == {"c": {"d": 1}}


def test_a_production_environment_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    """The guard has to hold through the console, not only through the CLI.

    The hooks under `.claude/` only fire inside a Claude Code session, so for a
    browser the loader is the *only* line of defence - and the console must not
    quietly write the file before asking it.
    """
    studies = tmp_path / "studies"
    studies.mkdir()
    target = studies / "demo.yaml"
    target.write_text("study:\n  name: KEEP-ME\n", encoding="utf-8")
    monkeypatch.setattr(server, "STUDIES_DIR", studies)

    result = server.save_study("demo", {
        "rave.base_url": "https://example.mdsol.com",
        "rave.environment": "PROD",
        "study.name": "DEMO",
        "site.number": "001", "site.name": "S", "site.country": "US",
        "subjects.count": 1, "subjects.id_prefix": "TST-", "subjects.id_start_number": 1,
    })

    assert result["ok"] is False
    assert any("production" in p.lower() for p in result["problems"]), result["problems"]
    assert target.read_text(encoding="utf-8") == "study:\n  name: KEEP-ME\n"
    assert not list(studies.glob(".*.candidate.yaml")), "the candidate file was left behind"


def test_a_valid_study_is_written_without_the_defaults(tmp_path, monkeypatch):
    """A study file states what makes this study different, not the whole config."""
    studies = tmp_path / "studies"
    studies.mkdir()
    monkeypatch.setattr(server, "STUDIES_DIR", studies)

    defaults = server.defaults_data()
    result = server.save_study("demo", {
        "rave.base_url": "https://example.mdsol.com",
        "rave.environment": "DEV",
        "study.name": "DEMO",
        "site.number": "001", "site.name": "Site One", "site.country": "US",
        "subjects.count": 2, "subjects.id_prefix": "TST-", "subjects.id_start_number": 1,
        # left at the default on purpose - it must not be written back out
        "dynamics.trigger_strategy": defaults["dynamics"]["trigger_strategy"],
        "generation.max_parallel_forms": defaults["generation"]["max_parallel_forms"],
    })

    assert result["ok"] is True, result
    written = (studies / "demo.yaml").read_text(encoding="utf-8")
    assert "DEMO" in written
    assert "trigger_strategy" not in written
    assert "max_parallel_forms" not in written


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "x" * 65, "semi;colon"])
def test_a_study_name_cannot_wander_out_of_the_directory(name):
    assert not server.SAFE_NAME.match(name)


def test_the_console_binds_to_localhost_by_default():
    """This process can write to Rave; it must not be reachable from the network."""
    import inspect

    assert inspect.signature(server.serve).parameters["host"].default == "127.0.0.1"
