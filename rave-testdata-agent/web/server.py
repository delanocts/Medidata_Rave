"""A local console for the pipeline (stdlib only - nothing to install).

Deliberately a *viewer*, not a second way to run things. It shells out to
`scripts/run_all.py`, exactly as you would by hand, and reads the artifacts the
stages already write: `run_manifest.json` after every stage, and the per-subject
JSONL logs. That keeps one execution path to reason about, and `--resume` still
works if you close the browser.

Bound to 127.0.0.1 on purpose: this process holds a session that can write to
your Rave. Credentials are never sent to the browser - the page is told whether
`.env` loads, and nothing more.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "web"))

import yaml  # noqa: E402

from fields import GROUPS, all_fields  # noqa: E402
from rave_agent.config.loader import ConfigError, load_config  # noqa: E402
from rave_agent.config.secrets import MissingSecretError, load_secrets  # noqa: E402
from rave_agent.orchestrator import STAGES  # noqa: E402

CONFIG_DIR = REPO_ROOT / "config"
STUDIES_DIR = CONFIG_DIR / "studies"
STATIC_DIR = Path(__file__).resolve().parent / "static"
ENV_FILE = REPO_ROOT / ".env"

# A study name becomes a file name, so it may not wander out of the directory.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# --------------------------------------------------------------------------
# dotted-key helpers - the form speaks "generation.log_records.min"
def dig(data: dict, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def plant(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def prune(node):
    """Drop empty branches so a saved study file carries no hollow sections."""
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        cleaned = prune(value)
        if cleaned not in (None, {}, ""):
            out[key] = cleaned
    return out


# --------------------------------------------------------------------------
class Run:
    """The one run this server will supervise at a time.

    One at a time is not laziness: two runs against the same study would race
    `run_manifest.json` and `activation_state.json`, which were written on the
    assumption that a study has a single owner.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.study = ""
        self.run_id = ""
        self.dry_run = True
        self.started = ""
        self.out_path: Path | None = None
        self.argv: list[str] = []
        self.started_ts = 0.0

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, study: str, dry_run: bool, resume: bool,
              stop_after: str = "") -> dict:
        with self.lock:
            if self.active:
                return {"ok": False, "error": f"a run is already in progress for {self.study}"}

            config = load_config(study, config_dir=CONFIG_DIR)   # raises ConfigError
            logs = config.study_output_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)

            self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.study = study
            self.dry_run = dry_run
            self.started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.started_ts = time.time()
            self.out_path = logs / f"web_{self.run_id}.out"

            argv = [sys.executable, str(REPO_ROOT / "scripts" / "run_all.py"),
                    "--study", study]
            if dry_run:
                argv.append("--dry-run")
            if resume:
                argv.append("--resume")
            if stop_after:
                argv += ["--stop-after", stop_after]
            self.argv = argv

            handle = open(self.out_path, "w", encoding="utf-8", errors="replace")
            self.proc = subprocess.Popen(
                argv, cwd=str(REPO_ROOT), stdout=handle,
                stderr=subprocess.STDOUT, text=True,
                # Detached enough that a browser refresh cannot take it down.
                env=dict(os.environ, PYTHONUNBUFFERED="1"),
            )
            return {"ok": True, "run_id": self.run_id, "argv": argv[1:]}

    def stop(self) -> dict:
        with self.lock:
            if not self.active:
                return {"ok": False, "error": "nothing is running"}
            self.proc.terminate()
            return {"ok": True}

    def snapshot(self, tail: int = 400) -> dict:
        study = self.study
        manifest = {}
        if study:
            try:
                config = load_config(study, config_dir=CONFIG_DIR)
                path = config.study_output_dir / "run_manifest.json"
                if path.is_file():
                    # The manifest on disk still describes the *previous* run
                    # until the first stage of this one completes - the
                    # connection test alone takes half a minute. Showing it
                    # would report six green stages for a run that has not done
                    # anything yet.
                    fresh = path.stat().st_mtime >= self.started_ts - 1
                    if fresh or not self.active:
                        manifest = json.loads(path.read_text(encoding="utf-8"))
            except (ConfigError, json.JSONDecodeError, OSError):
                manifest = {}

        lines: list[str] = []
        if self.out_path and self.out_path.is_file():
            try:
                lines = self.out_path.read_text(
                    encoding="utf-8", errors="replace").splitlines()[-tail:]
            except OSError:
                lines = []

        code = None if self.proc is None else self.proc.poll()
        return {
            "study": study,
            "run_id": self.run_id,
            "active": self.active,
            "exit_code": code,
            "dry_run": self.dry_run,
            "started": self.started,
            "manifest": manifest,
            "lines": lines,
        }


RUN = Run()


# --------------------------------------------------------------------------
def study_names() -> list[str]:
    if not STUDIES_DIR.is_dir():
        return []
    return sorted(p.stem for p in STUDIES_DIR.glob("*.yaml"))


def defaults_data() -> dict:
    path = CONFIG_DIR / "defaults.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.is_file() else {}


def effective(study: str) -> dict:
    """Every field's current value, defaults merged under the study file."""
    try:
        config = load_config(study, config_dir=CONFIG_DIR)
        data = config.data
    except ConfigError:
        # An invalid file must still be editable, or it can never be repaired.
        raw = STUDIES_DIR / f"{study}.yaml"
        data = defaults_data()
        if raw.is_file():
            overlay = yaml.safe_load(raw.read_text(encoding="utf-8")) or {}
            for field in all_fields():
                value = dig(overlay, field["key"])
                if value is not None:
                    plant(data, field["key"], value)
    return {f["key"]: dig(data, f["key"]) for f in all_fields()}


def save_study(study: str, values: dict) -> dict:
    """Write the study file, but only after the real loader accepts it.

    Only values that differ from `defaults.yaml` are written, so a study file
    stays the short statement of what makes this study different - which is the
    whole reason adding a study is one file and no code.
    """
    defaults = defaults_data()
    body: dict = {}
    for field in all_fields():
        key = field["key"]
        value = values.get(key)
        if value in (None, ""):
            continue
        if field.get("always") or value != dig(defaults, key):
            plant(body, key, value)
    body = prune(body)

    header = (f"# Study: {body.get('study', {}).get('name', study)}\n"
              f"# Written by the local console on "
              f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}Z.\n"
              f"# The only file that changes when targeting a different study.\n\n")
    text = header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)

    # Validate before overwriting: load_config accepts a path as the study, so
    # the candidate is checked against the real defaults and the real schema.
    scratch = STUDIES_DIR / f".{study}.candidate.yaml"
    try:
        scratch.write_text(text, encoding="utf-8")
        load_config(str(scratch), config_dir=CONFIG_DIR)
    except ConfigError as exc:
        return {"ok": False, "problems": exc.problems}
    finally:
        scratch.unlink(missing_ok=True)

    (STUDIES_DIR / f"{study}.yaml").write_text(text, encoding="utf-8")
    return {"ok": True, "yaml": text}


def env_state() -> dict:
    if not ENV_FILE.is_file():
        return {"ok": False, "detail": f"no .env at {ENV_FILE}"}
    try:
        load_secrets(ENV_FILE, require_anthropic=True)
        return {"ok": True, "detail": "RAVE_USERNAME, RAVE_PASSWORD and ANTHROPIC_API_KEY loaded"}
    except MissingSecretError as exc:
        return {"ok": False, "detail": str(exc).splitlines()[0]}


def latest_report(study: str) -> dict:
    try:
        config = load_config(study, config_dir=CONFIG_DIR)
    except ConfigError:
        return {}
    reports = sorted((config.study_output_dir / "reports").glob("run_*.html"))
    if not reports:
        return {}
    return {"name": reports[-1].name, "path": str(reports[-1])}


# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "rave-testdata-console"

    def log_message(self, fmt, *args):        # quiet; the page is the log
        pass

    # -- plumbing ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload, code: int = 200):
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routes ------------------------------------------------------------
    def do_GET(self):                                     # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        study = (query.get("study") or [""])[0]

        if route in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

        if route == "/api/bootstrap":
            return self._json({
                "groups": GROUPS,
                "studies": study_names(),
                "stages": [{"key": st.key, "name": st.name} for st in STAGES],
                "env": env_state(),
                "repo": str(REPO_ROOT),
            })

        if route == "/api/study":
            if not SAFE_NAME.match(study):
                return self._json({"error": "bad study name"}, 400)
            problems = []
            try:
                load_config(study, config_dir=CONFIG_DIR)
            except ConfigError as exc:
                problems = exc.problems
            return self._json({"values": effective(study), "problems": problems,
                               "report": latest_report(study)})

        if route == "/api/status":
            return self._json(RUN.snapshot())

        if route == "/api/events":
            return self._events()

        if route == "/api/report":
            if not SAFE_NAME.match(study):
                return self._json({"error": "bad study name"}, 400)
            found = latest_report(study)
            if not found:
                return self._send(404, b"no report yet", "text/plain; charset=utf-8")
            return self._file(Path(found["path"]), "text/html; charset=utf-8")

        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):                                    # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        payload = self._body()
        study = str(payload.get("study") or "")

        if route == "/api/study":
            if not SAFE_NAME.match(study):
                return self._json({"ok": False, "problems": ["study name must be letters, digits, dot, dash or underscore"]}, 400)
            result = save_study(study, payload.get("values") or {})
            return self._json(result, 200 if result["ok"] else 400)

        if route == "/api/run":
            if not SAFE_NAME.match(study):
                return self._json({"ok": False, "error": "bad study name"}, 400)
            try:
                result = RUN.start(study, bool(payload.get("dry_run", True)),
                                   bool(payload.get("resume", False)),
                                   str(payload.get("stop_after") or ""))
            except ConfigError as exc:
                return self._json({"ok": False, "error": str(exc), "problems": exc.problems}, 400)
            return self._json(result, 200 if result["ok"] else 409)

        if route == "/api/stop":
            return self._json(RUN.stop())

        return self._send(404, b"not found", "text/plain; charset=utf-8")

    # -- helpers -----------------------------------------------------------
    def _file(self, path: Path, ctype: str):
        if not path.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        return self._send(200, path.read_bytes(), ctype)

    def _events(self):
        """Server-sent events: a whole snapshot each tick.

        Snapshots rather than deltas - the payload is small, and it means a
        browser that reconnects mid-run is immediately correct instead of
        replaying a diff it missed.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        idle = 0
        try:
            while True:
                snap = RUN.snapshot()
                self.wfile.write(b"data: " + json.dumps(snap).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                if not snap["active"]:
                    idle += 1
                    if idle > 4:           # let the last output settle, then rest
                        time.sleep(2)
                        idle = 0
                else:
                    idle = 0
                time.sleep(1)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print()
    print("  rave-testdata-agent console")
    print(f"  open  http://{host}:{port}")
    print(f"  repo  {REPO_ROOT}")
    print()
    print("  Bound to localhost only. Credentials stay in this process; the")
    print("  browser is told whether .env loads and nothing more.")
    print("  Ctrl-C to stop.")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Local web console for the pipeline.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Left at localhost on purpose; this process can write to Rave.")
    args = ap.parse_args()
    serve(args.host, args.port)
