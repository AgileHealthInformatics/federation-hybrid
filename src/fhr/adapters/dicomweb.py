"""DICOMweb adapter: QIDO-RS study search plus report retrieval."""

from __future__ import annotations

from urllib.parse import urlencode

from ..data import parse_dt
from ..sources import imaging
from .base import Adapter, AdapterResponse, RawStatement, SubRequest

BASE = "https://imaging.demo/dicomweb"


class DicomWebReportsAdapter(Adapter):
    interface = "dicomweb-reports"

    def _url(self, sub: SubRequest) -> str:
        params = {"PatientID": sub.local_id, "StudyDate": f"{sub.window_from:%Y%m%d}-{sub.as_at:%Y%m%d}",
                  "since": sub.window_from.isoformat(), "includefield": "StudyDescription"}
        return f"{BASE}/studies?{urlencode(params)}"

    def plan(self, sub: SubRequest) -> list[str]:
        return ["GET " + self._url(sub), "GET {study}/reports  (per matched study)"]

    async def execute(self, sub: SubRequest) -> AdapterResponse:
        resp = await imaging.qido_with_reports(self._url(sub), sub.context)
        statements = [
            RawStatement("document", f"{BASE}/studies/{s['study_uid']}", status="final", verification="confirmed",
                         asserted=parse_dt(s["reported"]), effective_start=parse_dt(s["study_date"]),
                         title=f"{s['description']} report", text=s["report"], native=s,
                         extra={"doc_type": "Imaging report", "author": "Reporting radiologist", "modality": s["modality"]})
            for s in resp["studies"]
        ]
        return AdapterResponse(statements, 0, "permit", self.plan(sub))
