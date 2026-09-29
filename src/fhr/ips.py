"""Export the federated record as an IPS-shaped FHIR R4 document Bundle.

This is a purpose-specific projection (P3), generated on request and not
stored. Each entry carries every source coding (so ICD-10 and SNOMED CT sit
side by side), its reconciliation state, and a Provenance resource naming the
contributing organisations. It follows the International Patient Summary
structure but has not been validated against the IPS profiles.
"""

from __future__ import annotations

import uuid
from typing import Any

from .models import ClinicalStatement, PatientRecord, RecordEntry
from .terminology import SNOMED

TAG = {"system": "urn:fhr:tags", "code": "SYNTHETIC", "display": "Synthetic demonstrator data"}


def _uuid() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _codeable(entry: RecordEntry, by_id: dict[str, ClinicalStatement]) -> dict[str, Any]:
    codings, seen = [], set()
    for sid in entry.members:
        s = by_id[sid]
        for c in (s.concept, s.native_code):
            if c and c.code and (c.system, c.code) not in seen:
                seen.add((c.system, c.code))
                codings.append({"system": c.system, "code": c.code, "display": c.display})
    return {"coding": codings, "text": entry.display}


def _recon_ext(entry: RecordEntry) -> list[dict[str, Any]]:
    ext = [{"url": "urn:fhr:reconciliation", "valueCode": entry.reconciliation}]
    ext += [{"url": "urn:fhr:flag", "valueCode": f} for f in entry.flags]
    return ext


def to_ips(record: PatientRecord) -> dict[str, Any]:
    by_id = {s.id: s for s in record.statements}
    patient_ref = _uuid()
    org_refs = {s.source: _uuid() for s in record.sources}
    resources: list[tuple[str, dict[str, Any]]] = []
    sections = []

    def add(res: dict[str, Any]) -> str:
        ref = _uuid()
        res.setdefault("meta", {})["tag"] = [TAG]
        resources.append((ref, res))
        return ref

    def provenance(target: str, entry: RecordEntry) -> None:
        add({
            "resourceType": "Provenance",
            "target": [{"reference": target}],
            "recorded": record.completed_at.isoformat(),
            "agent": [{"type": {"text": "source"}, "who": {"reference": org_refs[s], "display": s}} for s in entry.sources if s in org_refs],
            "entity": [{"role": "source", "what": {"identifier": {"value": by_id[m].record_id}}} for m in entry.members],
        })

    for sec in record.sections:
        if not sec.ips_loinc:
            continue
        refs = []
        for e in sec.entries:
            latest = by_id.get(e.latest) if e.latest else None
            res: dict[str, Any] | None = None
            if sec.id == "problems":
                res = {"resourceType": "Condition", "subject": {"reference": patient_ref}, "code": _codeable(e, by_id),
                       "clinicalStatus": {"coding": [{"code": "active" if e.status == "active" else "resolved"}]},
                       "extension": _recon_ext(e)}
                if e.detail.get("onset"):
                    res["onsetDateTime"] = e.detail["onset"].isoformat()
            elif sec.id == "medications":
                res = {"resourceType": "MedicationStatement", "subject": {"reference": patient_ref},
                       "status": "active" if e.status == "current" else "completed",
                       "medicationCodeableConcept": _codeable(e, by_id), "extension": _recon_ext(e)}
                if e.detail.get("indicative_dose"):
                    res["dosage"] = [{"text": e.detail["indicative_dose"]}]
                if e.notes:
                    res["note"] = [{"text": n} for n in e.notes]
            elif sec.id == "allergies":
                if e.detail.get("negation"):
                    res = {"resourceType": "AllergyIntolerance", "patient": {"reference": patient_ref},
                           "code": {"coding": [{"system": SNOMED, "code": e.key, "display": e.display}]}, "extension": _recon_ext(e)}
                else:
                    res = {"resourceType": "AllergyIntolerance", "patient": {"reference": patient_ref},
                           "clinicalStatus": {"coding": [{"code": "active"}]}, "code": _codeable(e, by_id),
                           "reaction": [{"description": r} for r in e.detail.get("reactions", [])], "extension": _recon_ext(e)}
                if e.notes:
                    res["note"] = [{"text": n} for n in e.notes]
            elif sec.id == "immunizations":
                res = {"resourceType": "Immunization", "patient": {"reference": patient_ref}, "status": "completed",
                       "vaccineCode": _codeable(e, by_id), "occurrenceDateTime": e.detail["date"].isoformat(), "extension": _recon_ext(e)}
            elif sec.id in ("results", "vital-signs") and latest:
                res = {"resourceType": "Observation", "status": latest.status or "final", "subject": {"reference": patient_ref},
                       "category": [{"coding": [{"code": "laboratory" if sec.id == "results" else "vital-signs"}]}],
                       "code": {"coding": [{"system": latest.concept.system, "code": latest.concept.code, "display": latest.concept.display}]},
                       "effectiveDateTime": latest.effective_start.isoformat(),
                       "valueQuantity": {"value": e.detail["value"], "unit": e.detail["unit"]}}
            elif sec.id == "encounters" and latest:
                res = {"resourceType": "Encounter", "status": "finished", "subject": {"reference": patient_ref},
                       "type": [{"text": e.display}], "period": {"start": latest.effective_start.isoformat()}}
            if res is None:
                continue
            ref = add(res)
            provenance(ref, e)
            refs.append({"reference": ref})
        section: dict[str, Any] = {"title": sec.label, "code": {"coding": [{"system": "http://loinc.org", "code": sec.ips_loinc}]}}
        if refs:
            section["entry"] = refs
        else:
            section["emptyReason"] = {"text": sec.summary or "No information available from the sources that answered"}
        sections.append(section)

    d = record.demographics
    composition = {
        "resourceType": "Composition", "status": "final", "meta": {"tag": [TAG]},
        "type": {"coding": [{"system": "http://loinc.org", "code": "60591-5", "display": "Patient summary Document"}]},
        "subject": {"reference": patient_ref}, "date": record.completed_at.isoformat(),
        "author": [{"display": "Federated patient record broker (demonstrator)"}],
        "title": f"Federated patient summary ({record.overall_status})", "section": sections,
        "extension": [{"url": "urn:fhr:catalogue-version", "valueString": record.catalogue_version},
                      {"url": "urn:fhr:request-id", "valueString": record.request_id}],
    }
    patient = {"resourceType": "Patient", "meta": {"tag": [TAG]},
               "identifier": [{"system": "urn:fhr:demo-person-id", "value": record.subject}],
               "name": [{"text": f"{d.name} (synthetic)"}], "gender": d.sex, "birthDate": d.birth_date}
    orgs = [(org_refs[s.source], {"resourceType": "Organization", "meta": {"tag": [TAG]}, "name": s.label}) for s in record.sources]

    entries = [{"fullUrl": _uuid(), "resource": composition}, {"fullUrl": patient_ref, "resource": patient}]
    entries += [{"fullUrl": ref, "resource": r} for ref, r in orgs + resources]
    return {
        "resourceType": "Bundle", "type": "document", "meta": {"tag": [TAG]},
        "identifier": {"system": "urn:fhr:request", "value": record.request_id},
        "timestamp": record.completed_at.isoformat(), "entry": entries,
    }
