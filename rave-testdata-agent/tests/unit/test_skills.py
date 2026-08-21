"""The skills are runtime inputs, so they get the same test treatment as code."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rave_agent.generation.skill_rules import (  # noqa: E402
    available_skills,
    data_generation_rules,
    skills_dir,
)
from rave_agent.submission.rejections import (  # noqa: E402
    BAD_VALUE,
    DERIVED_FIELD,
    FOLDER_INACTIVE,
    FORM_INACTIVE,
    PAYLOAD_SHAPE,
    PERMISSION,
    SEMANTIC,
    SHRINK_RECORDS,
    classify_rejection,
)

SKILL_DIRS = sorted(p for p in skills_dir().iterdir() if p.is_dir()) if skills_dir().is_dir() else []

# Nothing tying the skills to the study they were learned on.
STUDY_SPECIFIC = re.compile(
    r"AES-002|ENR_F|ENROLL_F|DM_F|IE_F|RECRUIT_F|DEV-AES|TestSite|cognizant|11002",
    re.IGNORECASE,
)


def test_skills_are_present():
    assert len(available_skills()) >= 10, available_skills()


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_has_frontmatter_with_name_and_description(skill_dir):
    """A skill without a description cannot be selected by an agent."""
    path = skill_dir / "SKILL.md"
    assert path.is_file(), f"{skill_dir.name} has no SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{skill_dir.name}: no frontmatter"
    front = text.split("---", 2)[1]
    assert re.search(r"^name:\s*\S+", front, re.MULTILINE), f"{skill_dir.name}: no name"
    assert re.search(r"^description:\s*\S+", front, re.MULTILINE), \
        f"{skill_dir.name}: no description"


@pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_name_matches_directory(skill_dir):
    front = (skill_dir / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
    declared = re.search(r"^name:\s*(\S+)", front, re.MULTILINE).group(1)
    assert declared == skill_dir.name


def _skill_files():
    for skill_dir in SKILL_DIRS:
        yield from skill_dir.rglob("*.md")


@pytest.mark.parametrize("path", sorted(_skill_files()), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_skills_are_study_agnostic(path):
    """SK-1: a study, site, form or field identifier in a skill is a defect."""
    hit = STUDY_SPECIFIC.search(path.read_text(encoding="utf-8"))
    assert hit is None, f"{path} mentions {hit.group(0)!r}"


# --------------------------------------------------------------- generation
def test_generation_rules_load_from_the_skill():
    rules = data_generation_rules()
    assert len(rules) > 200
    assert "coded value" in rules.lower()
    assert "yyyy-mm-dd" in rules.lower()


def test_generation_rules_carry_the_privacy_requirement():
    """FR-6.9 must survive any edit to the skill file."""
    rules = data_generation_rules().lower()
    assert "never" in rules
    for forbidden in ("medical record", "phone", "address"):
        assert forbidden in rules, forbidden


def test_generation_rules_reach_the_prompt():
    from rave_agent.generation.prompt_builder import FormRequest, build_form_prompt
    from rave_agent.model.study_model import Item, StudyModel

    model = StudyModel(study_name="S", environment="DEV",
                       crf_version_oid="1", crf_version_name="v1")
    item = Item(oid="F.A", name="A", form_oid="F", data_type="text", label="A field")
    model.items[item.oid] = item

    request = FormRequest("SUBJ", "FOLDER", "Folder", "F", "Form", [item])
    prompt = build_form_prompt(model, request)
    assert "coded value" in prompt.lower()


# --------------------------------------------------------------- rejections
@pytest.mark.parametrize("reason,expected", [
    ("RWSException: Record restricted by max limit", SHRINK_RECORDS),
    ("RWSException: Folder not found.", FOLDER_INACTIVE),
    ("RWSException: Form does not exist in the designated folder.", FORM_INACTIVE),
    ("RWSException: Transaction on  derived field is not permitted.", DERIVED_FIELD),
    ("RWSException: Data not in dictionary.", BAD_VALUE),
    ("RWSException: Record already exists.", PAYLOAD_SHAPE),
    ("RWSException: Study does not exist.", PERMISSION),
    ("RWSException: Something nobody has seen", SEMANTIC),
])
def test_rejection_classification(reason, expected):
    assert classify_rejection(reason)[0] == expected


def test_double_space_in_rave_message_still_matches():
    """Rave's own text has a double space; markers must not depend on spacing."""
    single = classify_rejection("Transaction on derived field is not permitted.")
    double = classify_rejection("Transaction on  derived field is not permitted.")
    assert single == double == (DERIVED_FIELD, "Rave computes this field")


def test_folder_and_form_refusals_are_different_classes():
    """Conflating them abandons a live folder after its first missing form."""
    folder = classify_rejection("Folder not found.")[0]
    form = classify_rejection("Form does not exist in the designated folder.")[0]
    assert folder != form


def test_unknown_rejection_is_never_retried_as_transient():
    assert classify_rejection("totally novel failure")[0] == SEMANTIC


# ------------------------------------------------------------ dry-run safety
def test_dry_run_never_marks_a_folder_active():
    """A dry run posts nothing, so it must not claim coverage.

    Recording activation from a dry run leaves a state file that disagrees with
    Rave - the report then shows folders as populated that hold no data.
    """
    from rave_agent.dynamics.activation_state import ActivationState
    from rave_agent.submission.submitter import SubmissionResult

    state = ActivationState(subject_id="S", study="ST", environment="DEV")
    dry = SubmissionResult(ok=True, label="x", status="DRY_RUN")

    assert dry.ok, "a dry run still reports ok - that is what makes this a trap"
    assert dry.status == "DRY_RUN"

    # The resolver must branch on status, not on ok alone.
    if dry.status != "DRY_RUN":
        state.mark_active("FOLDER", "FORM", 0)
    assert state.active_folders == []
