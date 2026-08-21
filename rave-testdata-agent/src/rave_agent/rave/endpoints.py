"""RWS request definitions, including ones rwslib does not ship.

No study, site, form or field identifier may appear here (CFG-2) - every
request is parameterised by the caller.
"""
from __future__ import annotations

from rwslib.rws_requests import RWSAuthorizedGetRequest, make_url


class StudyDraftRequest(RWSAuthorizedGetRequest):
    """GET /metadata/studies/{project}/drafts/{oid}

    rwslib omits this ("something of an omission since you can list them").

    Note: on Rave 1.16.0 this returns "RWS URL does not exist" - drafts can be
    listed but not downloaded. Kept so the capability can be re-probed on other
    instances rather than silently assumed absent.
    """

    def __init__(self, project_name: str, oid: str | int):
        self.project_name = project_name
        self.oid = str(oid)

    def url_path(self) -> str:
        return make_url("metadata", "studies", self.project_name, "drafts", self.oid)

    def result(self, response):
        return response.text
