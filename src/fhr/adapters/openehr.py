"""openEHR adapter: one AQL query per record class, against archetypes."""

from __future__ import annotations

import asyncio
from typing import Any

from ..data import parse_dt
from ..sources import openehr_cdr
from ..terminology import ICD10, LOINC, SNOMED
from .base import WINDOWED, Adapter, AdapterResponse, RawStatement, SubRequest

_ARCHETYPES = {
    "condition": "openEHR-EHR-EVALUATION.problem_diagnosis.v1",
    "medication": "openEHR-EHR-INSTRUCTION.medication_order.v3",
    "allergy": "openEHR-EHR-EVALUATION.adverse_reaction_risk.v1",
    "result": "openEHR-EHR-OBSERVATION.laboratory_test_result.v1",
    "encounter": "composition context",
    "document": "openEHR-EHR-COMPOSITION.report.v1",
}

_SELECT = {
    "condition": "e/data[at0001]/items[at0002]/value AS diagnosis, e/data[at0001]/items[at0077]/value AS onset",
    "medication": "i/activities[at0001]/description[at0002]/items[at0070]/value AS medication, "
    "i/activities[at0001]/description[at0002]/items[at0009]/value AS dose_text",
    "allergy": "e/data[at0001]/items[at0002]/value AS substance, e/data[at0001]/items[at0009]/items[at0011]/value AS reaction",
    "result": "o/data[at0001]/events[at0002]/data[at0003]/items[openEHR-EHR-CLUSTER.laboratory_test_analyte.v1]/items[at0001]/value AS result, "
    "o/data[at0001]/events[at0002]/time/value AS time",
    "encounter": "c/context/start_time/value AS start, c/context/end_time/value AS end, c/context/setting/value AS setting",
    "document": "c/name/value AS title, c/content AS body",
}

_TERMINOLOGY = {"ICD10": ICD10, "SNOMED-CT": SNOMED, "LOINC": LOINC, "local::HA-FORMULARY": "urn:demo:hospital-a-formulary"}


def _aql(cls: str) -> str:
    arch = _ARCHETYPES[cls]
    contains = {
        "condition": f"CONTAINS EVALUATION e[{arch}]",
        "medication": f"CONTAINS INSTRUCTION i[{arch}]",
        "allergy": f"CONTAINS EVALUATION e[{arch}]",
        "result": f"CONTAINS OBSERVATION o[{arch}]",
        "encounter": "",
        "document": "",
    }[cls]
    where = " WHERE c/context/start_time/value >= $from" if cls in WINDOWED else ""
    return f"SELECT c/uid/value AS uid, {_SELECT[cls]}\nFROM EHR e[ehr_id/value=$ehrId] CONTAINS COMPOSITION c {contains}{where}"


def _coding(d: dict[str, Any]) -> tuple[str, str, str]:
    return _TERMINOLOGY.get(d["terminology"], d["terminology"]), d["code"], d.get("value")


def _to_statement(cls: str, c: dict[str, Any]) -> RawStatement:
    d = c["data"]
    native = {"composition_uid": c["uid"], "template": c["template"], "archetype_id": c["archetype"], "content": d}
    committed = parse_dt(c["committed"])
    if cls == "condition":
        sys, code, disp = _coding(d["diagnosis"])
        return RawStatement(cls, c["uid"], sys, code, disp, d["status"], "confirmed", asserted=committed,
                            effective_start=parse_dt(d.get("onset")), native=native)
    if cls == "medication":
        sys, code, disp = _coding(d["medication"])
        return RawStatement(cls, c["uid"], sys, code, disp, d["status"], "confirmed", asserted=committed,
                            role="clinic-list", dose_qty=d.get("dose_qty"), freq_per_day=d.get("freq_per_day"),
                            dose_text=d.get("dose_text"), native=native, extra={"order_type": d.get("order_type")})
    if cls == "allergy":
        sys, code, disp = _coding(d["substance"])
        return RawStatement(cls, c["uid"], sys, code, disp, d["status"], "confirmed", asserted=committed,
                            reaction=d.get("reaction"), severity=d.get("criticality"), native=native)
    if cls == "result":
        sys, code, disp = _coding(d["analyte"])
        return RawStatement(cls, c["uid"], sys, code, disp, "final", "confirmed", asserted=committed,
                            effective_start=parse_dt(d["time"]), value=d["magnitude"], unit=d["units"],
                            accession=d.get("accession"), native=native)
    if cls == "encounter":
        return RawStatement(cls, c["uid"], status="finished", verification="confirmed", asserted=committed,
                            effective_start=parse_dt(d["start"]), effective_end=parse_dt(d["end"]), title=d["type"],
                            text=d.get("reason"), native=native,
                            extra={"setting": d["setting"], "location": d.get("location"), "reason": d.get("reason")})
    return RawStatement(cls, c["uid"], status="final", verification="confirmed", asserted=committed,
                        effective_start=committed, title=d["title"], text=d["text"], native=native,
                        extra={"doc_type": d.get("doc_type"), "author": d.get("author")})


class OpenEhrAqlAdapter(Adapter):
    interface = "openehr-aql"

    def _params(self, sub: SubRequest, cls: str) -> dict[str, Any]:
        p: dict[str, Any] = {"ehrId": sub.local_id, "archetype": _ARCHETYPES[cls]}
        if cls in WINDOWED:
            p["from"] = sub.window_from.isoformat()
        return p

    def plan(self, sub: SubRequest) -> list[str]:
        out = []
        for cls in sub.classes:
            p = self._params(sub, cls)
            binds = "  ".join(f"${k}={v}" for k, v in p.items() if k != "archetype")
            out.append(f"POST /openehr/v1/query/aql  -- {cls}\n-- {binds}\n{_aql(cls)}")
        return out

    async def execute(self, sub: SubRequest) -> AdapterResponse:
        async def one(cls: str):
            return cls, await openehr_cdr.execute_aql(_aql(cls), self._params(sub, cls), sub.context)

        results = await asyncio.gather(*(one(c) for c in sub.classes))
        statements = [_to_statement(cls, row) for cls, resp in results for row in resp["rows"]]
        return AdapterResponse(statements, 0, "permit", self.plan(sub))
