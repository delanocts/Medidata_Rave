"""Sequence the stages, record a run manifest, support --resume (ARC-3, ERR-5).

Each stage is the same standalone entry point a user would run by hand; the
orchestrator only decides order, records outcomes, and knows what may be skipped
on a resume. Nothing here reimplements a stage.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config.loader import Config
from .utils.logging import get_logger

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


@dataclass
class Stage:
    key: str
    name: str
    script: str
    per_subject: bool = False
    optional: bool = False       # a failure is reported, not fatal
    writes_to_rave: bool = False
    needs_env: bool = True       # False for stages that only read local artifacts


# The spec's build order is also the runtime order: prove access, acquire
# metadata, model it, provision, then generate/submit, then report.
STAGES: list[Stage] = [
    Stage("connection", "Connection test", "test_connection.py"),
    Stage("metadata", "Metadata acquisition", "run_metadata.py"),
    # A3 reads only on-disk artifacts, so it takes no credentials.
    Stage("model", "Study model + dynamics graph", "run_model.py", needs_env=False),
    Stage("provision", "Site and subjects", "run_provision.py", writes_to_rave=True),
    Stage("dynamics", "Generate, submit, resolve dynamics", "run_dynamics.py",
          per_subject=True, writes_to_rave=True),
    Stage("verify", "Verification and reporting", "run_verify.py", optional=True),
]

STAGE_KEYS = [s.key for s in STAGES]


@dataclass
class StageOutcome:
    key: str
    name: str
    status: str                  # ok | failed | skipped
    exit_code: int | None = None
    seconds: float = 0.0
    detail: str = ""
    subjects: list[str] = field(default_factory=list)


@dataclass
class RunManifest:
    run_id: str
    study: str
    environment: str
    config_hash: str
    started: str
    finished: str = ""
    dry_run: bool = False
    stages: list[StageOutcome] = field(default_factory=list)

    def record(self, outcome: StageOutcome) -> None:
        self.stages = [s for s in self.stages if s.key != outcome.key]
        self.stages.append(outcome)

    def completed_keys(self) -> list[str]:
        return [s.key for s in self.stages if s.status == "ok"]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "study": self.study,
            "environment": self.environment,
            "config_hash": self.config_hash,
            "started": self.started,
            "finished": self.finished,
            "dry_run": self.dry_run,
            "stages": [asdict(s) for s in self.stages],
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "RunManifest | None":
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        manifest = cls(
            run_id=raw.get("run_id", ""), study=raw.get("study", ""),
            environment=raw.get("environment", ""),
            config_hash=raw.get("config_hash", ""),
            started=raw.get("started", ""), finished=raw.get("finished", ""),
            dry_run=bool(raw.get("dry_run")),
        )
        for entry in raw.get("stages") or []:
            manifest.stages.append(StageOutcome(**{
                k: v for k, v in entry.items()
                if k in StageOutcome.__dataclass_fields__
            }))
        return manifest


class Orchestrator:
    def __init__(
        self,
        config: Config,
        study_arg: str,
        env_file: Path,
        dry_run: bool = False,
        resume: bool = False,
        only: list[str] | None = None,
        stop_after: str | None = None,
    ):
        self.config = config
        self.study_arg = study_arg
        self.env_file = env_file
        self.dry_run = dry_run
        self.resume = resume
        self.only = set(only or [])
        self.stop_after = stop_after
        self.manifest_path = config.study_output_dir / "run_manifest.json"

    # ------------------------------------------------------------------
    def _subjects(self) -> list[str]:
        """Subject IDs to drive per-subject stages, from A4's output."""
        path = self.config.study_output_dir / "subjects.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [
            entry["subject_id"] for entry in payload.get("subjects") or []
            if entry.get("status") in ("created", "exists")
        ]

    def _run(self, stage: Stage, extra: list[str]) -> tuple[int, float]:
        command = [sys.executable, str(SCRIPTS / stage.script),
                   "--study", self.study_arg]
        if stage.needs_env:
            command += ["--env-file", str(self.env_file)]
        if self.dry_run and stage.script != "run_verify.py":
            command.append("--dry-run")
        command += extra

        log.info("stage start", extra={"stage": stage.key})
        started = time.monotonic()
        completed = subprocess.run(command, cwd=REPO_ROOT)
        return completed.returncode, round(time.monotonic() - started, 1)

    # ------------------------------------------------------------------
    def run(self) -> RunManifest:
        previous = RunManifest.load(self.manifest_path) if self.resume else None

        if previous and previous.config_hash != self.config.config_hash:
            log.warning("config changed since the previous run; resuming anyway",
                        extra={"was": previous.config_hash,
                               "now": self.config.config_hash})

        manifest = previous or RunManifest(
            run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            study=self.config.study_name,
            environment=self.config.environment,
            config_hash=self.config.config_hash,
            started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            dry_run=self.dry_run,
        )
        done = set(manifest.completed_keys()) if self.resume else set()

        for stage in STAGES:
            if self.only and stage.key not in self.only:
                continue

            if stage.key in done:
                manifest.record(StageOutcome(
                    stage.key, stage.name, "skipped",
                    detail="already completed in this run manifest"))
                print(f"[skip] {stage.name} - completed previously")
                continue

            print(f"\n=== {stage.name} ===")

            if stage.per_subject:
                subjects = self._subjects()
                if not subjects:
                    manifest.record(StageOutcome(
                        stage.key, stage.name, "failed",
                        detail="no subjects available; provisioning must run first"))
                    manifest.save(self.manifest_path)
                    return manifest

                failures, seconds = [], 0.0
                for subject in subjects:
                    code, elapsed = self._run(stage, ["--subject", subject])
                    seconds += elapsed
                    if code != 0:
                        failures.append(subject)
                outcome = StageOutcome(
                    stage.key, stage.name,
                    "failed" if failures else "ok",
                    exit_code=1 if failures else 0, seconds=round(seconds, 1),
                    subjects=subjects,
                    detail=(f"failed for {', '.join(failures)}" if failures else ""),
                )
            else:
                code, seconds = self._run(stage, [])
                outcome = StageOutcome(
                    stage.key, stage.name, "ok" if code == 0 else "failed",
                    exit_code=code, seconds=seconds,
                )

            manifest.record(outcome)
            manifest.save(self.manifest_path)

            if outcome.status == "failed" and not stage.optional:
                # ERR-1: stop before generating or posting anything else.
                print(f"\n{stage.name} failed (exit {outcome.exit_code}). "
                      f"Fix it and re-run with --resume to continue from here.")
                manifest.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
                manifest.save(self.manifest_path)
                return manifest

            if self.stop_after and stage.key == self.stop_after:
                print(f"\n--stop-after {stage.key}: stopping here.")
                break

        manifest.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest.save(self.manifest_path)
        return manifest
