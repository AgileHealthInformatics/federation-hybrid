"""W3C PROV export (PROV-JSON shape).

Statements are entities derived from native values through mapping and
transformation activities; reconciled entries are entities derived from their
member statements through a reconciliation activity, so a clinician-facing
line can be traced back to every native record behind it.
"""

from __future__ import annotations

from typing import Any

from .models import PatientRecord


def to_prov(record: PatientRecord) -> dict[str, Any]:
    entity: dict[str, Any] = {}
    activity: dict[str, Any] = {}
    agent: dict[str, Any] = {"fhr:broker": {"prov:type": "prov:SoftwareAgent", "fhr:catalogueVersion": record.catalogue_version}}
    gen: dict[str, Any] = {}
    der: dict[str, Any] = {}
    assoc: dict[str, Any] = {}

    for s in record.sources:
        agent[f"fhr:source/{s.source}"] = {"prov:type": "prov:Organization", "prov:label": s.label}

    for st in record.statements:
        native = f"fhr:native/{st.id}"
        stmt = f"fhr:statement/{st.id}"
        retrieval = f"fhr:retrieval/{record.request_id}/{st.source}"
        entity[native] = {"fhr:sourceRecord": st.record_id, "prov:value": st.value, "fhr:code": st.native_code.code if st.native_code else None}
        entity[stmt] = {"fhr:concept": st.concept.code if st.concept else None, "fhr:grade": st.grade.value,
                        "prov:value": st.rendered_value if st.rendered_value is not None else st.value}
        activity.setdefault(retrieval, {"prov:type": "fhr:Retrieval", "prov:endTime": st.retrieved_at.isoformat()})
        assoc[f"_:a-{st.id}"] = {"prov:activity": retrieval, "prov:agent": f"fhr:source/{st.source}"}
        gen[f"_:g-{st.id}"] = {"prov:entity": native, "prov:activity": retrieval}
        prev = native
        for n, step in enumerate(st.transformation_chain):
            act = f"fhr:{step.kind}/{step.id}@{step.version}"
            activity.setdefault(act, {"prov:type": f"fhr:{step.kind}", "prov:label": step.detail})
            nxt = stmt if n == len(st.transformation_chain) - 1 else f"fhr:intermediate/{st.id}/{n}"
            entity.setdefault(nxt, {})
            der[f"_:d-{st.id}-{n}"] = {"prov:generatedEntity": nxt, "prov:usedEntity": prev, "prov:activity": act}
            prev = nxt

    for sec in record.sections:
        for e in sec.entries:
            ent = f"fhr:entry/{e.id}"
            act = f"fhr:reconciliation/{record.request_id}/{e.id}"
            entity[ent] = {"prov:label": e.display, "fhr:reconciliation": e.reconciliation, "fhr:flags": e.flags}
            activity[act] = {"prov:type": "fhr:Reconciliation"}
            assoc[f"_:a-{e.id}"] = {"prov:activity": act, "prov:agent": "fhr:broker"}
            for m in e.members:
                der[f"_:r-{e.id}-{m}"] = {"prov:generatedEntity": ent, "prov:usedEntity": f"fhr:statement/{m}", "prov:activity": act}

    return {
        "prefix": {"prov": "http://www.w3.org/ns/prov#", "fhr": "urn:fhr:"},
        "entity": entity, "activity": activity, "agent": agent,
        "wasGeneratedBy": gen, "wasDerivedFrom": der, "wasAssociatedWith": assoc,
    }
