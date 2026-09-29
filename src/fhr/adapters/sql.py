"""Governed SQL service adapter.

The pattern is a published view contract: any primary-care system that
exposes the governed views (PROBLEMS, MEDICATIONS, ALLERGIES, IMMUNISATIONS,
OBSERVATIONS, CONSULTATIONS) with the contract columns can join by catalogue
configuration alone. The code system of each view comes from the catalogue
(SNOMED CT for the current GP, Read v2 for the archived practice system), and
the endpoint names the service to call.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..data import parse_dt
from ..sources import gp_legacy_sql, gp_sql
from .base import Adapter, AdapterResponse, RawStatement, SubRequest

SERVICES = {"gp": gp_sql.query_service, "gp-legacy": gp_legacy_sql.query_service}

_QUERIES: dict[str, tuple[str, str | None]] = {
    "condition": (
        "SELECT PROBLEM_ID, CODE, TERM, STATUS, SIGNIFICANCE, ONSET, RECORDED, END_DATE, ENTERED_IN_ERROR "
        "FROM PROBLEMS WHERE PATIENT_ID = :pid",
        None,
    ),
    "medication": (
        "SELECT MED_ID, DRUG_CODE, DRUG_NAME, DOSE_TEXT, DOSE_QTY, FREQ_PER_DAY, COURSE_TYPE, STATUS, START_DATE, "
        "STOP_DATE, STOP_REASON, LAST_ISSUE, LAST_REVIEW, ENTERED_IN_ERROR "
        "FROM MEDICATIONS WHERE PATIENT_ID = :pid AND (STATUS = 'active' OR STOP_DATE >= :from)",
        None,
    ),
    "allergy": (
        "SELECT ALLERGY_ID, CODE, TERM, REACTION, SEVERITY, STATUS, RECORDED, ENTERED_IN_ERROR "
        "FROM ALLERGIES WHERE PATIENT_ID = :pid",
        None,
    ),
    "immunization": (
        "SELECT IMM_ID, CODE, TERM, GIVEN_DATE, RECORDED, GIVEN_BY, BATCH, ENTERED_IN_ERROR "
        "FROM IMMUNISATIONS WHERE PATIENT_ID = :pid",
        None,
    ),
    "result": (
        "SELECT OBS_ID, TEST_CODE, TEST_NAME, VALUE, UNITS, EFFECTIVE, RECORDED, NOTE, ENTERED_IN_ERROR "
        "FROM OBSERVATIONS WHERE PATIENT_ID = :pid AND CATEGORY = :cat AND EFFECTIVE >= :from ORDER BY EFFECTIVE",
        "laboratory",
    ),
    "vital-sign": (
        "SELECT OBS_ID, TEST_CODE, TEST_NAME, VALUE, UNITS, EFFECTIVE, RECORDED, NOTE, ENTERED_IN_ERROR "
        "FROM OBSERVATIONS WHERE PATIENT_ID = :pid AND CATEGORY = :cat AND EFFECTIVE >= :from ORDER BY EFFECTIVE",
        "vital-sign",
    ),
    "encounter": (
        "SELECT CONS_ID, CONS_TYPE, CLINICIAN_ROLE, LOCATION, CONS_DATE, SUMMARY, ENTERED_IN_ERROR "
        "FROM CONSULTATIONS WHERE PATIENT_ID = :pid AND CONS_DATE >= :from",
        None,
    ),
}


def _eie(r: dict[str, Any]) -> str:
    return "entered-in-error" if r.get("ENTERED_IN_ERROR") else "confirmed"


def _to_statement(cls: str, r: dict[str, Any], system: str | None) -> RawStatement:
    native = {"view": _QUERIES[cls][0].split(" FROM ")[1].split()[0], **r}
    if cls == "condition":
        return RawStatement(
            cls, f"PROBLEMS/{r['PROBLEM_ID']}", system, r["CODE"], r["TERM"], r["STATUS"], _eie(r),
            asserted=parse_dt(r["RECORDED"]), effective_start=parse_dt(r["ONSET"]), effective_end=parse_dt(r["END_DATE"]),
            native=native, extra={"significance": r["SIGNIFICANCE"]},
        )
    if cls == "medication":
        start, review, stop = parse_dt(r["START_DATE"]), parse_dt(r["LAST_REVIEW"]), parse_dt(r["STOP_DATE"])
        asserted = stop if r["STATUS"] != "active" and stop else max(d for d in (start, review) if d)
        return RawStatement(
            cls, f"MEDICATIONS/{r['MED_ID']}", system, r["DRUG_CODE"], r["DRUG_NAME"], r["STATUS"], _eie(r),
            asserted=asserted, effective_start=start, effective_end=stop, role=r["COURSE_TYPE"],
            dose_qty=r["DOSE_QTY"], freq_per_day=r["FREQ_PER_DAY"], dose_text=r["DOSE_TEXT"], native=native,
            extra={"last_issued": r["LAST_ISSUE"], "last_review": r["LAST_REVIEW"], "stop_reason": r["STOP_REASON"]},
        )
    if cls == "allergy":
        return RawStatement(
            cls, f"ALLERGIES/{r['ALLERGY_ID']}", system, r["CODE"], r["TERM"], r["STATUS"], _eie(r),
            asserted=parse_dt(r["RECORDED"]), reaction=r["REACTION"], severity=r["SEVERITY"], native=native,
        )
    if cls == "immunization":
        return RawStatement(
            cls, f"IMMUNISATIONS/{r['IMM_ID']}", system, r["CODE"], r["TERM"], "completed", _eie(r),
            asserted=parse_dt(r["RECORDED"]), effective_start=parse_dt(r["GIVEN_DATE"]), native=native,
            extra={"performer": r["GIVEN_BY"], "batch": r["BATCH"]},
        )
    if cls in ("result", "vital-sign"):
        return RawStatement(
            cls, f"OBSERVATIONS/{r['OBS_ID']}", system, r["TEST_CODE"], r["TEST_NAME"], "final", _eie(r),
            asserted=parse_dt(r["RECORDED"]), effective_start=parse_dt(r["EFFECTIVE"]), value=r["VALUE"], unit=r["UNITS"],
            native=native, extra={"note": r["NOTE"]} if r["NOTE"] else {},
        )
    return RawStatement(
        cls, f"CONSULTATIONS/{r['CONS_ID']}", status="finished", verification=_eie(r),
        asserted=parse_dt(r["CONS_DATE"]), effective_start=parse_dt(r["CONS_DATE"]), title=r["CONS_TYPE"], text=r["SUMMARY"],
        native=native, extra={"setting": "Primary care", "location": r["LOCATION"], "clinician": r["CLINICIAN_ROLE"]},
    )


class SqlServiceAdapter(Adapter):
    interface = "sql-service"

    def _params(self, sub: SubRequest, cls: str) -> dict[str, Any]:
        sql, cat = _QUERIES[cls]
        p: dict[str, Any] = {"pid": sub.local_id}
        if ":from" in sql:
            since = sub.med_window_from if cls == "medication" and sub.med_window_from else sub.window_from
            p["from"] = since.isoformat()
        if cat:
            p["cat"] = cat
        return p

    def plan(self, sub: SubRequest) -> list[str]:
        return [f"POST /governed-query/{sub.endpoint}  -- {cls}\n{_QUERIES[cls][0]}\n-- params: {self._params(sub, cls)}" for cls in sub.classes]

    async def execute(self, sub: SubRequest) -> AdapterResponse:
        service = SERVICES.get(sub.endpoint)
        if service is None:
            raise ConnectionError(f"No governed query service registered at endpoint '{sub.endpoint}'")

        async def one(cls: str):
            return cls, await service(_QUERIES[cls][0], self._params(sub, cls), sub.context)

        results = await asyncio.gather(*(one(c) for c in sub.classes))
        statements, withheld = [], 0
        for cls, resp in results:
            withheld += resp["withheld"]
            statements += [_to_statement(cls, r, sub.code_systems.get(cls)) for r in resp["rows"]]
        return AdapterResponse(statements, withheld, "permit-with-redaction" if withheld else "permit", self.plan(sub))
