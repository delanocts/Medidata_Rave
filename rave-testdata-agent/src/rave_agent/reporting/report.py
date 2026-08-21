"""A7 - run report in JSON and HTML (FR-9.2).

The report states what Rave holds, what the run deliberately traded away, and
what it could not reach. Silence about an unreached target is the failure mode:
a study can look fully populated while a third of it was never reachable.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class RunReport:
    study: str
    environment: str
    crf_version: str
    site_oid: str
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    config_hash: str = ""
    duration_seconds: float | None = None

    coverage: dict = field(default_factory=dict)
    subjects: list[dict] = field(default_factory=list)
    dynamics: dict = field(default_factory=dict)
    generation: dict = field(default_factory=dict)
    trade_offs: list[str] = field(default_factory=list)
    unreachable: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)

    synthetic: bool = True  # SEC-5

    def to_dict(self) -> dict:
        return {
            "study": self.study,
            "environment": self.environment,
            "crf_version": self.crf_version,
            "site_oid": self.site_oid,
            "generated_at": self.generated_at,
            "config_hash": self.config_hash,
            "duration_seconds": self.duration_seconds,
            "synthetic_data": self.synthetic,
            "coverage": self.coverage,
            "subjects": self.subjects,
            "dynamics": self.dynamics,
            "generation": self.generation,
            "trade_offs": self.trade_offs,
            "unreachable": self.unreachable,
            "warnings": self.warnings,
            "skills_used": self.skills_used,
        }

    def save_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    def save_html(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_html(self), encoding="utf-8")
        return path

    @property
    def ok(self) -> bool:
        """A partially covered run is a reportable outcome, not a failure.

        Only a subject that never reached Rave, or a field Rave stored
        differently from what was sent, counts against the run.
        """
        if not self.subjects:
            return False
        for subject in self.subjects:
            if not subject.get("exists"):
                return False
            if subject.get("field_checks", {}).get("mismatch"):
                return False
        return True


# ---------------------------------------------------------------------------
def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _bar(done: int, total: int) -> str:
    pct = 0 if not total else round(100 * done / total)
    return (
        f'<div class="bar"><span style="width:{pct}%"></span></div>'
        f'<span class="pct">{done}/{total} ({pct}%)</span>'
    )


def _rows(pairs: list[tuple[str, Any]]) -> str:
    return "".join(
        f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in pairs
    )


def render_html(report: RunReport) -> str:
    cov = report.coverage or {}
    denominators = cov.get("denominators", {})
    per_subject = cov.get("per_subject", [])

    subject_rows = []
    for entry in per_subject:
        checks = entry.get("field_checks", {})
        mismatch = checks.get("mismatch", 0)
        subject_rows.append(
            "<tr>"
            f"<td class=mono>{_e(entry.get('subject'))}</td>"
            f"<td>{_bar(entry.get('folders_with_data', 0), denominators.get('real_folders', 0))}</td>"
            f"<td class=num>{_e(entry.get('folder_form_pairs'))}</td>"
            f"<td class=num>{_e(entry.get('form_instances'))}</td>"
            f"<td class=num><strong>{_e(entry.get('stored_values'))}</strong></td>"
            f"<td class=num>{_e(checks.get('match', 0) + checks.get('narrowed', 0))}</td>"
            f"<td class='num {"bad" if mismatch else "good"}'>{_e(mismatch)}</td>"
            "</tr>"
        )

    unreachable_rows = "".join(
        "<tr>"
        f"<td class=mono>{_e(u.get('target'))}</td>"
        f"<td>{_e(u.get('kind'))}</td>"
        f"<td>{_e(u.get('reason'))}</td>"
        "</tr>"
        for u in report.unreachable
    ) or "<tr><td colspan=3 class=muted>Everything predicted was reached.</td></tr>"

    empty_lists = "".join(
        f"<li><span class=mono>{_e(e.get('subject'))}</span>: "
        f"{_e(', '.join(e.get('empty_folders') or []) or 'none')}</li>"
        for e in per_subject
    )

    trade_offs = "".join(f"<li>{_e(t)}</li>" for t in report.trade_offs) \
        or "<li class=muted>None recorded.</li>"
    warnings = "".join(f"<li>{_e(w)}</li>" for w in report.warnings) \
        or "<li class=muted>None.</li>"

    return f"""<!doctype html>
<meta charset="utf-8">
<title>{_e(report.study)} run report</title>
<style>
  :root {{
    --bg:#fff; --fg:#1a1d21; --muted:#6b7280; --line:#e5e7eb;
    --accent:#2563eb; --good:#059669; --bad:#dc2626; --panel:#f9fafb;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0f1216; --fg:#e6e8eb; --muted:#9aa3ad; --line:#252a31;
             --accent:#60a5fa; --good:#34d399; --bad:#f87171; --panel:#161a1f; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2rem 1.25rem; background:var(--bg); color:var(--fg);
    font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  main {{ max-width:60rem; margin:0 auto; }}
  h1 {{ font-size:1.5rem; margin:0 0 .25rem; }}
  h2 {{ font-size:1.05rem; margin:2rem 0 .6rem; padding-bottom:.3rem;
        border-bottom:1px solid var(--line); }}
  .sub {{ color:var(--muted); margin:0 0 1.5rem; }}
  .tag {{ display:inline-block; padding:.15rem .5rem; border-radius:999px;
    font-size:.75rem; background:var(--panel); border:1px solid var(--line); }}
  table {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
  th,td {{ text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line);
           vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em; }}
  .muted {{ color:var(--muted); }}
  .good {{ color:var(--good); }}
  .bad {{ color:var(--bad); font-weight:600; }}
  .bar {{ display:inline-block; width:6rem; height:.5rem; background:var(--line);
    border-radius:999px; overflow:hidden; vertical-align:middle; margin-right:.4rem; }}
  .bar span {{ display:block; height:100%; background:var(--accent); }}
  .pct {{ font-size:.8rem; color:var(--muted); font-variant-numeric:tabular-nums; }}
  .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px;
    padding:.85rem 1rem; }}
  .wrap {{ overflow-x:auto; }}
  ul {{ margin:.4rem 0; padding-left:1.15rem; }}
  li {{ margin:.2rem 0; }}
</style>
<main>
  <h1>{_e(report.study)} ({_e(report.environment)}) run report</h1>
  <p class="sub">
    CRF version {_e(report.crf_version)} &middot; site {_e(report.site_oid)} &middot;
    {_e(report.generated_at)}
    &nbsp;<span class="tag">synthetic data</span>
  </p>

  <h2>Coverage in Rave</h2>
  <p class="muted" style="margin-top:0">
    Read back from Rave, not taken from submission acknowledgements.
  </p>
  <div class="wrap"><table>
    <tr><th>Subject</th><th>Folders</th><th class=num>Folder&ndash;form pairs</th>
        <th class=num>Form instances</th><th class=num>Stored values</th>
        <th class=num>Verified</th><th class=num>Mismatched</th></tr>
    {''.join(subject_rows) or '<tr><td colspan=7 class=muted>No subjects.</td></tr>'}
  </table></div>

  <h2>Denominators</h2>
  <div class="panel"><table>
    {_rows(list(denominators.items()))}
  </table></div>

  <h2>Folders with no data</h2>
  <ul>{empty_lists or '<li class=muted>None.</li>'}</ul>

  <h2>Predicted but never activated</h2>
  <div class="wrap"><table>
    <tr><th>Target</th><th>Kind</th><th>Reason</th></tr>
    {unreachable_rows}
  </table></div>

  <h2>Trade-offs this run made</h2>
  <ul>{trade_offs}</ul>

  <h2>Generation</h2>
  <div class="panel"><table>{_rows(list((report.generation or {}).items()))}</table></div>

  <h2>Dynamics</h2>
  <div class="panel"><table>{_rows(list((report.dynamics or {}).items()))}</table></div>

  <h2>Warnings</h2>
  <ul>{warnings}</ul>

  <p class="muted" style="margin-top:2rem;font-size:.85rem">
    All data in this run is synthetic and contains no real patient information.
    Config hash {_e(report.config_hash)}.
  </p>
</main>
"""
