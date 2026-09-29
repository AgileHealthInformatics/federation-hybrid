"""Simulated new clinical activity at the sources (demo control).

Each event writes to a mock source exactly as the real system would record
it. Nothing touches the materialised view: the view only learns of the event
when it is next rebuilt, which is the point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import fhir_server, gp_sql, pharmacy_feed

APPLIED: dict[str, str] = {}

EVENTS: dict[str, dict[str, str]] = {
    "gp-adds-bisoprolol": {
        "label": "GP adds bisoprolol to Jane Smith's repeats",
        "detail": "The practice actions the discharge summary: bisoprolol 2.5 mg once daily becomes a GP repeat.",
    },
    "pharmacy-dispenses-ramipril": {
        "label": "Pharmacy dispenses Jane Smith's ramipril",
        "detail": "Jane collects ramipril for the first time in five months.",
    },
    "hospital-b-high-potassium": {
        "label": "Hospital B reports a high potassium for Jane Smith",
        "detail": "A new result: potassium 6.1 mmol/L.",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def apply(name: str) -> dict[str, Any]:
    if name not in EVENTS:
        raise KeyError(f"No demo event {name!r}")
    if name in APPLIED:
        return {"event": name, "applied_at": APPLIED[name], "already": True}
    at = _now()
    if name == "gp-adds-bisoprolol":
        gp_sql.query_service.con.execute(
            "INSERT INTO MEDICATIONS (MED_ID, PATIENT_ID, DRUG_CODE, DRUG_NAME, DOSE_TEXT, DOSE_QTY, FREQ_PER_DAY, COURSE_TYPE, STATUS, "
            "START_DATE, STOP_DATE, STOP_REASON, LAST_ISSUE, LAST_REVIEW, ENTERED_IN_ERROR, RESTRICTED) "
            "VALUES ('MD-EV1','GP-77120','VMP-BIS25','Bisoprolol 2.5mg tablets','One tablet daily',1,1,'repeat','active',?,NULL,NULL,?,?,0,0)",
            (at, at, at),
        )
    elif name == "pharmacy-dispenses-ramipril":
        pharmacy_feed.EVENTS.append({
            "event_id": "EV-DEMO-1", "patient": "RX-33019", "type": "dispense", "at": at,
            "product": {"system": "urn:demo:dmd", "code": "VMP-RAM5", "display": "Ramipril 5mg capsules"},
            "quantity": 28, "prescriber": "Riverside Medical Practice",
        })
    elif name == "hospital-b-high-potassium":
        fhir_server.RESOURCES.append({
            "resourceType": "Observation", "id": "obs-ev-1", "status": "final",
            "category": [{"coding": [{"code": "laboratory"}]}], "subject": {"reference": "Patient/PAT-B-80211"},
            "identifier": [{"system": "urn:demo:lab-accession", "value": "ACC-B-EV1"}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "2823-3"}]},
            "effectiveDateTime": at, "issued": at, "valueQuantity": {"value": 6.1, "unit": "mmol/L"},
        })
    APPLIED[name] = at
    return {"event": name, "applied_at": at, "already": False}


def reset() -> None:
    gp_sql.query_service.con.execute("DELETE FROM MEDICATIONS WHERE MED_ID LIKE 'MD-EV%'")
    pharmacy_feed.EVENTS[:] = [e for e in pharmacy_feed.EVENTS if not e["event_id"].startswith("EV-DEMO")]
    fhir_server.RESOURCES[:] = [r for r in fhir_server.RESOURCES if not r["id"].startswith("obs-ev")]
    APPLIED.clear()


def listing() -> list[dict[str, Any]]:
    return [{"id": k, **v, "applied_at": APPLIED.get(k)} for k, v in EVENTS.items()]
