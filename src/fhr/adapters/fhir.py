"""FHIR R4 search adapter."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

from ..data import parse_dt
from ..sources import fhir_server
from .base import Adapter, AdapterResponse, RawStatement, SubRequest

_SEARCH = {
    "condition": ("Condition", None, None),
    "medication": ("MedicationRequest", None, None),
    "allergy": ("AllergyIntolerance", None, None),
    "result": ("Observation", "laboratory", "date"),
    "vital-sign": ("Observation", "vital-signs", "date"),
    "encounter": ("Encounter", None, "date"),
    "document": ("DocumentReference", None, "date"),
}


def _code(cc: dict[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    if not cc or not cc.get("coding"):
        return None, None, (cc or {}).get("text")
    c = cc["coding"][0]
    return c.get("system"), c.get("code"), c.get("display")


def _first(cc: dict[str, Any] | None) -> str | None:
    return _code(cc)[1]


def _to_statement(cls: str, full_url: str, r: dict[str, Any]) -> RawStatement:
    if cls == "condition":
        sys, code, disp = _code(r["code"])
        return RawStatement(cls, full_url, sys, code, disp, _first(r.get("clinicalStatus")), _first(r.get("verificationStatus")),
                            asserted=parse_dt(r.get("recordedDate")), effective_start=parse_dt(r.get("onsetDateTime")),
                            effective_end=parse_dt(r.get("abatementDateTime")), native=r)
    if cls == "medication":
        sys, code, disp = _code(r["medicationCodeableConcept"])
        dosage = (r.get("dosageInstruction") or [{}])[0]
        rep = dosage.get("timing", {}).get("repeat", {})
        qty = (dosage.get("doseAndRate") or [{}])[0].get("doseQuantity", {}).get("value")
        bounds = rep.get("boundsPeriod", {})
        status = r["status"]
        supply = r.get("dispenseRequest", {}).get("expectedSupplyDuration", {}).get("value")
        return RawStatement(cls, full_url, sys, code, disp, status,
                            "entered-in-error" if status == "entered-in-error" else "confirmed",
                            asserted=parse_dt(r.get("authoredOn")), effective_start=parse_dt(bounds.get("start") or r.get("authoredOn")),
                            effective_end=parse_dt(bounds.get("end")), role=_first((r.get("category") or [None])[0]),
                            dose_qty=qty, freq_per_day=rep.get("frequency") if rep.get("periodUnit") == "d" else None,
                            dose_text=dosage.get("text"), native=r, extra={"supply_days": supply} if supply else {})
    if cls == "allergy":
        sys, code, disp = _code(r["code"])
        return RawStatement(cls, full_url, sys, code, disp, _first(r.get("clinicalStatus")), _first(r.get("verificationStatus")),
                            asserted=parse_dt(r.get("recordedDate")), native=r,
                            extra={"note": r["note"][0]["text"]} if r.get("note") else {})
    if cls in ("result", "vital-sign"):
        sys, code, disp = _code(r["code"])
        acc = next((i["value"] for i in r.get("identifier", []) if "accession" in i.get("system", "")), None)
        status = r["status"]
        return RawStatement(cls, full_url, sys, code, disp, status,
                            "entered-in-error" if status == "entered-in-error" else "confirmed",
                            asserted=parse_dt(r.get("issued") or r.get("effectiveDateTime")),
                            effective_start=parse_dt(r.get("effectiveDateTime")),
                            value=r["valueQuantity"]["value"], unit=r["valueQuantity"]["unit"], accession=acc,
                            version=r.get("meta", {}).get("versionId"), amended=status in ("amended", "corrected"),
                            native=r, extra={"note": r["note"][0]["text"]} if r.get("note") else {})
    if cls == "encounter":
        p = r.get("period", {})
        return RawStatement(cls, full_url, status=r["status"], verification="confirmed", asserted=parse_dt(p.get("end") or p.get("start")),
                            effective_start=parse_dt(p.get("start")), effective_end=parse_dt(p.get("end")),
                            title=(r.get("type") or [{}])[0].get("text"),
                            text=(r.get("reasonCode") or [{}])[0].get("text"), native=r,
                            extra={"setting": r["class"]["display"].capitalize(), "location": r.get("serviceProvider", {}).get("display"),
                                   "reason": (r.get("reasonCode") or [{}])[0].get("text")})
    att = r["content"][0]["attachment"]
    return RawStatement(cls, full_url, status=r["status"], verification="confirmed", asserted=parse_dt(r.get("date")),
                        effective_start=parse_dt(r.get("date")), title=att.get("title") or _code(r.get("type"))[2],
                        text=att.get("data_text"), native=r,
                        extra={"doc_type": _code(r.get("type"))[2], "author": (r.get("author") or [{}])[0].get("display")})


class FhirSearchAdapter(Adapter):
    interface = "fhir-r4-search"

    def _url(self, sub: SubRequest, cls: str) -> str:
        rtype, category, date_param = _SEARCH[cls]
        params = [("patient", sub.local_id)]
        if category:
            params.append(("category", category))
        if date_param:
            params.append((date_param, "ge" + sub.window_from.isoformat()))
        params.append(("_count", "200"))
        return f"{fhir_server.BASE}/{rtype}?{urlencode(params)}"

    def plan(self, sub: SubRequest) -> list[str]:
        return [f"GET {self._url(sub, c)}" for c in sub.classes]

    async def execute(self, sub: SubRequest) -> AdapterResponse:
        async def one(cls: str):
            return cls, await fhir_server.search(self._url(sub, cls), sub.context)

        results = await asyncio.gather(*(one(c) for c in sub.classes))
        statements = [_to_statement(cls, e["fullUrl"], e["resource"]) for cls, b in results for e in b.get("entry", [])]
        return AdapterResponse(statements, 0, "permit", self.plan(sub))
