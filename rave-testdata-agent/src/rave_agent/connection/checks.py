"""A1 - Connection Agent (FR-1).

Runs an ordered PASS/FAIL matrix over reachability, auth, permissions, the
target study/site, and the LLM key. Nothing here prints a credential (FR-1.7).

The Architect-metadata check is the one that matters most: without the Architect
role, RWS returns study metadata but no edit checks, and the dynamics half of
this tool (FR-3.4, FR-8) cannot be built from metadata alone.
"""
from __future__ import annotations

import json
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from lxml import etree

from ..config.loader import Config
from ..config.secrets import Secrets
from ..rave.client import RaveClient
from ..rave.errors import AuthError, NotFoundError, RaveError
from ..utils.logging import get_logger
from ..utils.xml import parse_xml

log = get_logger(__name__)

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

MDSOL_NS = "http://www.mdsol.com/ns/odm/metadata"
ODM_NS = "http://www.cdisc.org/ns/odm/v1.3"


@dataclass
class CheckResult:
    id: str
    name: str
    status: str
    detail: str = ""
    remediation: str = ""
    mandatory: bool = True
    elapsed_seconds: float = 0.0
    data: dict = field(default_factory=dict)


class ConnectionReport:
    def __init__(self, config: Config):
        self.config = config
        self.results: list[CheckResult] = []
        self.started = datetime.now(timezone.utc)

    def add(self, result: CheckResult) -> CheckResult:
        self.results.append(result)
        icon = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}[result.status]
        log.info(f"[{icon}] {result.name}" + (f" - {result.detail}" if result.detail else ""))
        return result

    @property
    def ok(self) -> bool:
        """True only when every mandatory check passed (FR-1.6)."""
        return not any(r.status == FAIL and r.mandatory for r in self.results)

    def find(self, check_id: str) -> CheckResult | None:
        return next((r for r in self.results if r.id == check_id), None)

    def passed(self, check_id: str) -> bool:
        result = self.find(check_id)
        return result is not None and result.status == PASS

    def to_dict(self) -> dict:
        return {
            "run": {
                "started": self.started.isoformat(),
                "finished": datetime.now(timezone.utc).isoformat(),
                "config_hash": self.config.config_hash,
                "study": self.config.study_name,
                "environment": self.config.environment,
                "study_env": self.config.study_env,
                "base_url": self.config.base_url,
            },
            "overall": "PASS" if self.ok else "FAIL",
            "counts": {
                s: sum(1 for r in self.results if r.status == s) for s in (PASS, FAIL, WARN, SKIP)
            },
            "checks": [asdict(r) for r in self.results],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    def render_matrix(self) -> str:
        width = max((len(r.name) for r in self.results), default=20)
        lines = [f"{'CHECK'.ljust(width)}  STATUS  DETAIL", f"{'-' * width}  ------  {'-' * 40}"]
        for r in self.results:
            flag = "" if r.mandatory else " (optional)"
            lines.append(f"{r.name.ljust(width)}  {r.status:<6}  {r.detail}{flag}")
        return "\n".join(lines)


def _timed(fn):
    started = time.monotonic()
    try:
        result = fn()
    except Exception:
        raise
    finally:
        pass
    if isinstance(result, CheckResult):
        result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_dns_tls(config: Config) -> CheckResult:
    """FR-1.1 - network reachability and a valid TLS handshake."""
    host = urlparse(config.base_url).hostname or ""
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror as exc:
        return CheckResult(
            "dns", "DNS resolution", FAIL, f"cannot resolve {host}: {exc}",
            "Check the host name in rave.base_url, and whether you need VPN access.",
        )
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                expires = cert.get("notAfter", "unknown")
        return CheckResult("dns", "DNS + TLS handshake", PASS, f"{host}:443, cert valid to {expires}")
    except (ssl.SSLError, OSError) as exc:
        return CheckResult(
            "dns", "DNS + TLS handshake", FAIL, f"{type(exc).__name__}: {exc}",
            "TLS verification cannot be disabled (SEC-2). Fix the trust chain or proxy instead.",
        )


def check_rws_version(client: RaveClient) -> CheckResult:
    """FR-1.2 - a lightweight unauthenticated RWS endpoint returns 200."""
    try:
        response = client.get_raw("version")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "rws_version", "RWS reachable", FAIL, f"{type(exc).__name__}: {exc}",
            "Confirm rave.rws_path is correct for this instance.",
        )
    if response.status_code != 200:
        return CheckResult(
            "rws_version", "RWS reachable", FAIL,
            f"HTTP {response.status_code} from {client.base_url}/version",
            "RaveWebServices may not be enabled on this host.",
        )
    return CheckResult(
        "rws_version", "RWS reachable", PASS, f"version {response.text.strip()[:40]}",
        data={"rws_version": response.text.strip()[:40]},
    )


def check_auth_and_study(client: RaveClient, config: Config) -> tuple[CheckResult, CheckResult]:
    """FR-1.3 - authenticate, then confirm the study+environment is visible."""
    from rwslib.rws_requests import ClinicalStudiesRequest

    try:
        result = client.send(ClinicalStudiesRequest())
        studies = result.value
    except AuthError as exc:
        auth = CheckResult(
            "auth", "Authentication (Basic)", FAIL, str(exc),
            "Check RAVE_USERNAME/RAVE_PASSWORD in .env. If MFA is enforced on this "
            "account, Basic auth will not work - use a dedicated service account.",
        )
        return auth, CheckResult("study", "Study visible", SKIP, "authentication failed")
    except RaveError as exc:
        auth = CheckResult("auth", "Authentication (Basic)", FAIL, str(exc),
                           "Confirm the account has RWS module access.")
        return auth, CheckResult("study", "Study visible", SKIP, "authentication failed")

    auth = CheckResult(
        "auth", "Authentication (Basic)", PASS,
        f"{len(studies)} clinical study/environment(s) visible",
        data={"study_count": len(studies)},
    )

    wanted = config.study_env.lower()
    match = next((s for s in studies if (s.oid or "").lower() == wanted), None)
    if match is None:
        near = [s.oid for s in studies if config.study_name.lower() in (s.oid or "").lower()]
        hint = f" Same study in other environments: {near}." if near else ""
        study = CheckResult(
            "study", "Study visible", FAIL,
            f"{config.study_env} not in the {len(studies)} study/environment(s) this account can see.{hint}",
            "Confirm the study name and environment, and that the user is assigned "
            "to this study in this specific environment.",
        )
    else:
        study = CheckResult(
            "study", "Study visible", PASS, f"{match.oid}",
            data={"study_oid": match.oid, "studyname": match.studyname,
                  "environment": match.environment, "protocolname": match.protocolname},
        )
    return auth, study


def check_architect_access(client: RaveClient, config: Config) -> CheckResult:
    """The Architect role gates /metadata/studies, which is where edit checks live."""
    from rwslib.rws_requests import MetadataStudiesRequest

    try:
        result = client.send(MetadataStudiesRequest())
        studies = result.value
    except AuthError as exc:
        return CheckResult(
            "architect", "Architect metadata access", FAIL, str(exc),
            "The account needs the Architect role to read /metadata/studies. Without it "
            "edit checks and derivations are unavailable and the dynamics graph (FR-3.4) "
            "cannot be built from metadata. Fallback: export the ALS manually (FR-2.2).",
        )
    except RaveError as exc:
        return CheckResult("architect", "Architect metadata access", FAIL, str(exc),
                           "Confirm the account holds the Architect role.")

    names = [(s.studyname or s.oid or "") for s in studies]
    match = next((n for n in names if n.lower() == config.study_name.lower()), None)
    if match is None:
        return CheckResult(
            "architect", "Architect metadata access", FAIL,
            f"{config.study_name} not among {len(names)} Architect study/studies: {names[:10]}",
            "The account can reach Architect but is not assigned to this study's design. "
            "Ask for Architect access to this project.",
        )
    return CheckResult("architect", "Architect metadata access", PASS,
                       f"{match} visible among {len(names)} Architect study/studies",
                       data={"architect_studies": len(names)})


def check_study_versions(client: RaveClient, config: Config) -> CheckResult:
    """FR-2.3 / OQ-5 - list CRF versions so A2 can pick the active one."""
    from rwslib.rws_requests import StudyVersionsRequest

    try:
        result = client.send(StudyVersionsRequest(config.study_name))
        versions = result.value
    except RaveError as exc:
        return CheckResult("versions", "CRF versions listed", FAIL, str(exc),
                           "Requires Architect access to this study.")

    listed = [{"oid": v.oid, "name": v.name} for v in versions]
    if not listed:
        return CheckResult("versions", "CRF versions listed", FAIL,
                           "no MetaDataVersion entries returned",
                           "The study draft may never have been published to a version.")
    return CheckResult("versions", "CRF versions listed", PASS,
                       f"{len(listed)} version(s), newest OID {listed[0]['oid']} ({listed[0]['name']})",
                       data={"versions": listed[:20]})


def check_edit_checks_present(client: RaveClient, config: Config, version_oid: str) -> CheckResult:
    """Confirm the metadata actually carries mdsol edit checks / derivations.

    This is what makes FR-3.4 possible without the ALS.
    """
    from rwslib.rws_requests import StudyVersionRequest

    try:
        result = client.send(StudyVersionRequest(config.study_name, version_oid))
        xml_text = result.value
    except RaveError as exc:
        return CheckResult("editchecks", "Edit checks in metadata", FAIL, str(exc),
                           "Could not download the study version metadata.")

    try:
        root = parse_xml(xml_text)
    except etree.XMLSyntaxError as exc:
        return CheckResult("editchecks", "Edit checks in metadata", FAIL,
                           f"metadata is not parseable XML: {exc}", "")

    counts = {
        tag: len(root.findall(f".//{{{MDSOL_NS}}}{tag}"))
        for tag in ("EditCheckDef", "DerivationDef", "CheckAction", "CheckStep")
    }
    forms = len(root.findall(f".//{{{ODM_NS}}}FormDef"))
    fields = len(root.findall(f".//{{{ODM_NS}}}ItemDef"))
    events = len(root.findall(f".//{{{ODM_NS}}}StudyEventDef"))
    structure = {"forms": forms, "fields": fields, "folders": events}

    dynamic_actions = {"AddForm", "AddMatrix", "MrgMatrix", "OldMrgMatrix", "SetDataPointVisible"}
    action_types: dict[str, int] = {}
    custom_functions = 0
    for action in root.findall(f".//{{{MDSOL_NS}}}CheckAction"):
        kind = action.get("Type") or action.get("ActionType") or "unknown"
        action_types[kind] = action_types.get(kind, 0) + 1
        if kind == "CustomFunction":
            custom_functions += 1

    activating = sum(v for k, v in action_types.items() if k in dynamic_actions)
    data = {**structure, **counts, "action_types": action_types,
            "activating_actions": activating, "custom_function_actions": custom_functions,
            "version_oid": version_oid, "metadata_bytes": len(xml_text)}

    if counts["EditCheckDef"] == 0 and counts["DerivationDef"] == 0:
        return CheckResult(
            "editchecks", "Edit checks in metadata", WARN,
            f"metadata parsed ({forms} forms, {fields} fields, {events} folders) but contains "
            "no mdsol:EditCheckDef or mdsol:DerivationDef",
            "Dynamics cannot be derived. Either this study genuinely has no edit checks, "
            "or this endpoint is not returning Architect extensions for your role.",
            mandatory=False, data=data,
        )

    detail = (f"{counts['EditCheckDef']} edit checks, {counts['DerivationDef']} derivations, "
              f"{activating} form/folder-activating actions")
    if custom_functions:
        detail += f", {custom_functions} custom-function actions (unresolvable, see FR-3.5)"
    return CheckResult("editchecks", "Edit checks in metadata", PASS, detail,
                       mandatory=False, data=data)


def check_site(client: RaveClient, config: Config) -> CheckResult:
    """FR-1.4 / FR-4.1 - does the configured site exist for this study?"""
    from rwslib.rws_requests.odm_adapter import SitesMetadataRequest

    site_number = str(config.get("site.number"))
    create_if_missing = bool(config.get("site.create_if_missing"))

    try:
        result = client.send(SitesMetadataRequest(config.study_name, config.environment))
        xml_text = result.value
    except RaveError as exc:
        return CheckResult("site", "Site available", FAIL, str(exc),
                           "Could not list sites for this study.")

    try:
        root = parse_xml(xml_text)
    except etree.XMLSyntaxError as exc:
        return CheckResult("site", "Site available", FAIL, f"Sites.odm not parseable: {exc}", "")

    sites = []
    for loc in root.findall(f".//{{{ODM_NS}}}Location"):
        sites.append({"oid": loc.get("OID"), "name": loc.get("Name"),
                      "type": loc.get("LocationType")})

    match = next((s for s in sites if s["oid"] == site_number or s["name"] == site_number), None)
    if match:
        return CheckResult("site", "Site available", PASS,
                           f"{match['oid']} ({match['name']}) already exists",
                           data={"site": match, "existing_sites": len(sites), "action": "use_existing"})

    known = [s["oid"] for s in sites][:10]
    if create_if_missing:
        return CheckResult(
            "site", "Site available", WARN,
            f"{site_number} does not exist; will be created at stage A4. "
            f"{len(sites)} existing site(s): {known}",
            "Site creation posts an AdminData/Location payload. If the account lacks "
            "site-creation rights this will fail at A4 with a clear message.",
            mandatory=False,
            data={"existing_sites": len(sites), "known": known, "action": "create"},
        )
    return CheckResult(
        "site", "Site available", FAIL,
        f"{site_number} not found and site.create_if_missing is false. Existing: {known}",
        "Either create the site manually in Rave, or set site.create_if_missing: true.",
        data={"existing_sites": len(sites), "known": known},
    )


def check_subjects(client: RaveClient, config: Config) -> CheckResult:
    """FR-5.2 - see which subject IDs already exist, so A4 can apply its policy."""
    from rwslib.rws_requests import StudySubjectsRequest

    try:
        result = client.send(
            StudySubjectsRequest(config.study_name, config.environment, status=True)
        )
        subjects = result.value
    except NotFoundError:
        return CheckResult("subjects", "Existing subjects listed", PASS,
                           "no subjects yet (study is empty)",
                           mandatory=False, data={"existing": 0, "names": []})
    except RaveError as exc:
        return CheckResult("subjects", "Existing subjects listed", WARN, str(exc),
                           "A4 will re-check before creating subjects.", mandatory=False)

    names = [getattr(s, "subjectkey", None) or getattr(s, "subject_name", None) for s in subjects]
    names = [n for n in names if n]
    prefix = str(config.get("subjects.id_prefix") or "")
    collisions = [n for n in names if prefix and n.startswith(prefix)]
    detail = f"{len(names)} existing subject(s)"
    if collisions:
        detail += f"; {len(collisions)} already use prefix {prefix!r}: {collisions[:5]}"
    return CheckResult("subjects", "Existing subjects listed", PASS, detail, mandatory=False,
                       data={"existing": len(names), "names": names[:50],
                             "prefix_collisions": collisions[:50]})


def check_anthropic(secrets: Secrets, config: Config) -> CheckResult:
    """FR-1.5 - the LLM key works, with a minimal call."""
    model = str(config.get("generation.model"))
    if not secrets.has_anthropic:
        return CheckResult("llm", "Anthropic API key", FAIL, "ANTHROPIC_API_KEY is empty",
                           "Add it to .env. A5 cannot generate data without it.")
    try:
        import anthropic
    except ImportError:
        return CheckResult("llm", "Anthropic API key", FAIL, "anthropic package not installed",
                           "pip install -r requirements.txt")

    try:
        client = anthropic.Anthropic(api_key=secrets.anthropic_api_key)
        response = client.messages.create(
            model=model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
        return CheckResult("llm", "Anthropic API key", PASS,
                           f"{model} responded ({response.usage.input_tokens} in / "
                           f"{response.usage.output_tokens} out): {text[:20]!r}",
                           data={"model": model,
                                 "input_tokens": response.usage.input_tokens,
                                 "output_tokens": response.usage.output_tokens})
    except Exception as exc:  # noqa: BLE001 - surface the provider's own message
        name = type(exc).__name__
        remediation = "Check the key at console.anthropic.com."
        if "authentication" in name.lower() or "401" in str(exc):
            remediation = "The key is invalid or revoked. Regenerate it at console.anthropic.com."
        elif "credit" in str(exc).lower() or "billing" in str(exc).lower():
            remediation = "The key is valid but the account has no credit. Add billing."
        elif "not_found" in str(exc).lower() or "model" in str(exc).lower():
            remediation = f"Model {model!r} may not be available to this account."
        return CheckResult("llm", "Anthropic API key", FAIL, f"{name}: {str(exc)[:200]}", remediation)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all_checks(config: Config, secrets: Secrets, skip_llm: bool = False) -> ConnectionReport:
    report = ConnectionReport(config)

    dns = report.add(_timed(lambda: check_dns_tls(config)))
    if dns.status == FAIL:
        for cid, name in (("rws_version", "RWS reachable"), ("auth", "Authentication (Basic)"),
                          ("study", "Study visible"), ("architect", "Architect metadata access"),
                          ("versions", "CRF versions listed"), ("editchecks", "Edit checks in metadata"),
                          ("site", "Site available"), ("subjects", "Existing subjects listed")):
            report.add(CheckResult(cid, name, SKIP, "host unreachable"))
        if not skip_llm:
            report.add(_timed(lambda: check_anthropic(secrets, config)))
        return report

    client = RaveClient(config, secrets)

    version_check = report.add(_timed(lambda: check_rws_version(client)))
    if version_check.status == FAIL:
        for cid, name in (("auth", "Authentication (Basic)"), ("study", "Study visible"),
                          ("architect", "Architect metadata access"),
                          ("versions", "CRF versions listed"), ("editchecks", "Edit checks in metadata"),
                          ("site", "Site available"), ("subjects", "Existing subjects listed")):
            report.add(CheckResult(cid, name, SKIP, "RWS not reachable"))
        if not skip_llm:
            report.add(_timed(lambda: check_anthropic(secrets, config)))
        return report

    auth, study = check_auth_and_study(client, config)
    report.add(auth)
    report.add(study)

    if auth.status == PASS:
        architect = report.add(_timed(lambda: check_architect_access(client, config)))

        if architect.status == PASS:
            versions = report.add(_timed(lambda: check_study_versions(client, config)))
            if versions.status == PASS and versions.data.get("versions"):
                newest = versions.data["versions"][0]["oid"]
                report.add(_timed(lambda: check_edit_checks_present(client, config, newest)))
            else:
                report.add(CheckResult("editchecks", "Edit checks in metadata", SKIP,
                                       "no CRF version to inspect", mandatory=False))
        else:
            report.add(CheckResult("versions", "CRF versions listed", SKIP, "no Architect access"))
            report.add(CheckResult("editchecks", "Edit checks in metadata", SKIP,
                                   "no Architect access", mandatory=False))

        if study.status == PASS:
            report.add(_timed(lambda: check_site(client, config)))
            report.add(_timed(lambda: check_subjects(client, config)))
        else:
            report.add(CheckResult("site", "Site available", SKIP, "study not visible"))
            report.add(CheckResult("subjects", "Existing subjects listed", SKIP,
                                   "study not visible", mandatory=False))
    else:
        for cid, name, mand in (("architect", "Architect metadata access", True),
                                ("versions", "CRF versions listed", True),
                                ("editchecks", "Edit checks in metadata", False),
                                ("site", "Site available", True),
                                ("subjects", "Existing subjects listed", False)):
            report.add(CheckResult(cid, name, SKIP, "authentication failed", mandatory=mand))

    if skip_llm:
        report.add(CheckResult("llm", "Anthropic API key", SKIP, "--skip-llm", mandatory=False))
    else:
        report.add(_timed(lambda: check_anthropic(secrets, config)))

    return report
