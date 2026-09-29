"""Hospital B: mock FHIR R4 server.

Parses genuine FHIR search URLs (resource type, patient, category and
date/authoredon prefixes) and returns searchset Bundles.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..data import load_json, parse_dt
from .state import admit

RESOURCES: list[dict[str, Any]] = load_json("data/hospital_b.json")["resources"]
BASE = "https://hospital-b.demo/fhir"

_PATIENT_REF = {"AllergyIntolerance": "patient"}
_DATE_FIELD = {
    "Observation": lambda r: r.get("effectiveDateTime"),
    "Encounter": lambda r: r.get("period", {}).get("start"),
    "DocumentReference": lambda r: r.get("date"),
    "MedicationRequest": lambda r: r.get("authoredOn"),
}


def _patient(r: dict[str, Any]) -> str:
    return r.get(_PATIENT_REF.get(r["resourceType"], "subject"), {}).get("reference", "")


async def search(url: str, ctx: dict[str, Any]) -> dict[str, Any]:
    admit("hospital-b", ctx, ("direct-care", "secondary-use"))
    await asyncio.sleep(0.08)
    parts = urlsplit(url)
    rtype = parts.path.rstrip("/").split("/")[-1]
    q: dict[str, list[str]] = {}
    for k, v in parse_qsl(parts.query):
        q.setdefault(k, []).append(v)
    patient = q["patient"][0]
    category = q.get("category", [None])[0]
    ge = None
    for key in ("date", "authoredon"):
        for v in q.get(key, []):
            if v.startswith("ge"):
                ge = parse_dt(v[2:])

    entries = []
    for r in RESOURCES:
        if r["resourceType"] != rtype or _patient(r) != f"Patient/{patient}":
            continue
        if category and category not in {c["code"] for cat in r.get("category", []) for c in cat.get("coding", [])}:
            continue
        if ge and rtype in _DATE_FIELD:
            d = parse_dt(_DATE_FIELD[rtype](r))
            if d and d < ge:
                continue
        entries.append({"fullUrl": f"{BASE}/{rtype}/{r['id']}", "resource": r})
    return {"resourceType": "Bundle", "type": "searchset", "total": len(entries), "entry": entries}
