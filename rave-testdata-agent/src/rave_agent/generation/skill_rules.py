"""Load skill reference content into the runtime.

The skills under `.claude/skills/` are the single source of truth for the rules
this tool follows. Some of them are meant for a human or a Claude Code subagent
to read; a few are meant for the *runtime* to read, so that editing a skill file
changes behaviour with no code change.

`clinical-data-generation/reference/data-rules.md` is loaded into every
generation prompt. `rave-submission/reference/error-codes.md`, when present,
supplies the rejection-to-classification table used when interpreting a Rave
response.

Nothing here is required for the tool to work: a missing skill file degrades to
a built-in fallback rather than an error, so the pipeline still runs from a
checkout with no `.claude/` directory.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..utils.logging import get_logger

log = get_logger(__name__)

# src/rave_agent/generation/skill_rules.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_DIR = _REPO_ROOT / ".claude" / "skills"

# Used when the skill file is absent, so behaviour never depends on it existing.
_FALLBACK_DATA_RULES = """\
- Return the coded value for any field with a codelist, never the label.
- Dates are YYYY-MM-DD.
- Respect max lengths and numeric ranges exactly.
- Keep values clinically plausible and internally consistent; a start date is on
  or before its end date, and demographics never change between visits.
- Use no real personal data: no real names, medical record numbers, national
  identifiers, addresses, phone numbers or emails.
"""


def skills_dir() -> Path:
    return _SKILLS_DIR


@lru_cache(maxsize=None)
def load_reference(skill: str, filename: str) -> str | None:
    """Read one reference file from a skill, or None if it is not there."""
    path = _SKILLS_DIR / skill / "reference" / filename
    if not path.is_file():
        log.debug("skill reference absent", extra={"skill": skill, "ref": filename})
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("skill reference unreadable",
                    extra={"skill": skill, "ref": filename, "error": str(exc)})
        return None


def _body_after_frontmatter(text: str) -> str:
    """Strip a leading YAML frontmatter block, and the H1 that usually follows."""
    stripped = text.lstrip()
    if stripped.startswith("---"):
        parts = stripped.split("---", 2)
        if len(parts) == 3:
            stripped = parts[2]
    lines = [
        line for line in stripped.splitlines()
        # Drop the file's own title and the note about where it is injected.
        if not line.startswith("# ")
    ]
    return "\n".join(lines).strip()


def data_generation_rules() -> str:
    """The clinical rules block injected into every generation prompt.

    Sourced from `clinical-data-generation/reference/data-rules.md` so the rules
    can be tuned per deployment without touching the prompt builder.
    """
    text = load_reference("clinical-data-generation", "data-rules.md")
    if not text:
        return _FALLBACK_DATA_RULES.strip()

    body = _body_after_frontmatter(text)
    # Drop the file's own explanatory preamble - everything before the first
    # section heading is about maintaining the file, not about the data.
    marker = body.find("## ")
    return (body[marker:] if marker != -1 else body).strip()


def available_skills() -> list[str]:
    """Skill names present in the checkout, for the run report."""
    if not _SKILLS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in _SKILLS_DIR.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    )
