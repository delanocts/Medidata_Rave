"""Config loading and validation.

Precedence: CLI > environment > study file > defaults (5.1).
Every problem is reported at once, never one at a time (CFG-1).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

# An environment matching any of these is refused outright, whatever the
# allow-list says. A config flag alone must never unblock production (SEC-4).
_PROD_PATTERNS = (r"^prod", r"^prd$", r"^live$", r"production")

_ENV_PREFIX = "RAVE_AGENT_"


class ConfigError(RuntimeError):
    """Raised with the complete list of problems found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"Configuration is not usable ({len(problems)} problem(s)):\n{body}")


@dataclass
class Config:
    data: dict[str, Any]
    study_file: Path
    defaults_file: Path
    config_hash: str
    overrides_applied: list[str] = field(default_factory=list)

    # -- convenience accessors used across the pipeline --
    @property
    def study_name(self) -> str:
        return self.data["study"]["name"]

    @property
    def environment(self) -> str:
        return self.data["rave"]["environment"]

    @property
    def study_env(self) -> str:
        """How the study is addressed in RWS URLs, e.g. MYSTUDY(DEV)."""
        return f"{self.study_name}({self.environment})"

    @property
    def base_url(self) -> str:
        return self.data["rave"]["base_url"].rstrip("/")

    @property
    def output_root(self) -> Path:
        return Path(self.data["execution"]["output_root"]).resolve()

    @property
    def study_output_dir(self) -> Path:
        return self.output_root / self.study_name

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _coerce(text: str) -> Any:
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _set_dotted(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _load_yaml(path: Path, problems: list[str], label: str) -> dict:
    if not path.is_file():
        problems.append(f"{label} not found: {path}")
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        problems.append(f"{label} is not valid YAML ({path}): {exc}")
        return {}
    if not isinstance(loaded, dict):
        problems.append(f"{label} must be a YAML mapping ({path})")
        return {}
    return loaded


def _env_overrides() -> list[tuple[str, Any]]:
    """RAVE_AGENT_SUBJECTS__COUNT=5 -> subjects.count = 5"""
    out = []
    for key, value in os.environ.items():
        if key.startswith(_ENV_PREFIX) and key != _ENV_PREFIX:
            dotted = key[len(_ENV_PREFIX):].lower().replace("__", ".")
            out.append((dotted, _coerce(value)))
    return sorted(out)


def _check_semantics(data: dict, problems: list[str]) -> None:
    rave = data.get("rave", {})
    env = str(rave.get("environment", "")).strip()
    allowed = rave.get("allowed_environments") or []

    if env:
        normalized = env.lower()
        if any(re.search(p, normalized) for p in _PROD_PATTERNS):
            problems.append(
                f"rave.environment {env!r} looks like a production environment. "
                "This tool refuses to write synthetic data to production (C-1, SEC-4); "
                "no config flag unblocks it."
            )
        elif not any(env.lower() == str(a).lower() for a in allowed):
            problems.append(
                f"rave.environment {env!r} is not in rave.allowed_environments {allowed} (CFG-4)"
            )

    gen = data.get("generation", {})
    logs = gen.get("log_records") or {}
    lo, hi = logs.get("min"), logs.get("max")
    if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
        problems.append(f"generation.log_records.min ({lo}) is greater than .max ({hi})")

    visits = gen.get("visits") or {}
    if visits.get("mode") == "subset" and not visits.get("include"):
        problems.append("generation.visits.mode is 'subset' but generation.visits.include is empty")

    site = data.get("site", {})
    if site.get("create_if_missing") and not site.get("name"):
        problems.append("site.create_if_missing is true but site.name is not set")


def load_config(
    study: str,
    config_dir: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> Config:
    """Load defaults + the named study file, apply overrides, validate everything."""
    problems: list[str] = []
    config_dir = (config_dir or Path("config")).resolve()

    defaults_file = config_dir / "defaults.yaml"
    schema_file = config_dir / "config.schema.json"

    study_path = Path(study)
    if not study_path.is_file():
        study_path = config_dir / "studies" / f"{study}.yaml"

    defaults = _load_yaml(defaults_file, problems, "defaults.yaml")
    study_data = _load_yaml(study_path, problems, "study config")

    data = _deep_merge(defaults, study_data)

    applied: list[str] = []
    for dotted, value in _env_overrides():
        _set_dotted(data, dotted, value)
        applied.append(f"env: {dotted}={value!r}")
    for dotted, value in (cli_overrides or {}).items():
        _set_dotted(data, dotted, value)
        applied.append(f"cli: {dotted}={value!r}")

    if schema_file.is_file():
        try:
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            validator = jsonschema.Draft202012Validator(schema)
            for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
                location = ".".join(str(p) for p in err.path) or "(root)"
                problems.append(f"{location}: {err.message}")
        except json.JSONDecodeError as exc:
            problems.append(f"config.schema.json is not valid JSON: {exc}")
    else:
        problems.append(f"schema not found: {schema_file}")

    _check_semantics(data, problems)

    if problems:
        raise ConfigError(problems)

    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return Config(
        data=data,
        study_file=study_path,
        defaults_file=defaults_file,
        config_hash=hashlib.sha256(canonical.encode()).hexdigest()[:16],
        overrides_applied=applied,
    )
