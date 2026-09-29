"""Typed change operations on the control-plane documents.

Each operation is small, validated, and described in plain words. A change
request is a list of operations applied together to produce a candidate
snapshot; nothing is applied to the live catalogue except through an approved
change request.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

MAPPING_LIFECYCLE = {"proposed", "reviewed", "approved", "deprecated"}
SOURCE_LIFECYCLE = {"onboarding", "live", "retiring", "suspended", "retired"}
GRADES = {"exact", "narrower", "broader", "related", "unmappable"}

CLINICAL = "clinical-safety-officer"
IDENTITY = "identity-steward"

OP_ROLES = {
    "set_exposure_mapping": CLINICAL,
    "set_concept_map_element": CLINICAL,
    "add_concept_map": CLINICAL,
    "register_source": CLINICAL,
    "set_source_lifecycle": CLINICAL,
    "restore_version": CLINICAL,
    "identity_decision": IDENTITY,
}


class OpError(ValueError):
    pass


def roles_for(ops: list[dict[str, Any]]) -> list[str]:
    roles = set()
    for op in ops:
        if op.get("op") not in OP_ROLES:
            raise OpError(f"Unknown operation {op.get('op')!r}")
        roles.add(OP_ROLES[op["op"]])
    return sorted(roles)


def describe(op: dict[str, Any]) -> str:
    k = op["op"]
    if k == "set_exposure_mapping":
        return f"Set {op['source']} {op['record_class']} mapping to {op['lifecycle']}"
    if k == "set_concept_map_element":
        parts = [f"{f} {op[f]}" for f in ("target", "grade", "lifecycle") if op.get(f)]
        return f"Concept map {op['map']}: {op['code']} -> " + ", ".join(parts)
    if k == "add_concept_map":
        m = op["map"]
        return f"Add concept map {m['id']} ({len(m['elements'])} elements, {m['source']} -> {m['target']})"
    if k == "register_source":
        s = op["source"]
        return f"Register source {s['id']} ({s['capability']['interface']}, lifecycle {s.get('lifecycle', 'onboarding')}, {len(s['exposes'])} record classes)"
    if k == "set_source_lifecycle":
        return f"Set source {op['source']} lifecycle to {op['lifecycle']}"
    if k == "identity_decision":
        return f"{op['decision'].capitalize()} identity link {op['person']} -> {op['source']}"
    if k == "restore_version":
        return f"Restore catalogue content from version v{op['version']}"
    return k


def _bump(version: str, to_approved: bool = False) -> str:
    try:
        major, minor = (int(x) for x in str(version).split(".")[:2])
    except ValueError:
        return "1.0"
    if to_approved and major == 0:
        return "1.0"
    return f"{major}.{minor + 1}"


def apply_ops(docs: dict[str, Any], ops: list[dict[str, Any]], load_version: Callable[[int], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a new snapshot with the operations applied. Never mutates `docs`."""
    out = copy.deepcopy(docs)
    for op in ops:
        _apply(out, op, load_version)
    return out


def _source(cat: dict, sid: str) -> dict:
    s = next((s for s in cat["sources"] if s["id"] == sid), None)
    if s is None:
        raise OpError(f"Unknown source {sid!r}")
    return s


def _apply(d: dict[str, Any], op: dict[str, Any], load_version) -> None:
    cat, term, ident = d["catalogue"], d["terminology"], d["identity"]
    k = op.get("op")
    if k == "set_exposure_mapping":
        if op["lifecycle"] not in MAPPING_LIFECYCLE:
            raise OpError(f"Mapping lifecycle must be one of {sorted(MAPPING_LIFECYCLE)}")
        exp = next((e for e in _source(cat, op["source"])["exposes"] if e["record_class"] == op["record_class"]), None)
        if exp is None:
            raise OpError(f"{op['source']} does not expose {op['record_class']}")
        m = exp["mapping"]
        m["version"] = _bump(m.get("version", "0.1"), op["lifecycle"] == "approved")
        m["lifecycle"] = op["lifecycle"]
        if op.get("note"):
            m["note"] = op["note"]
    elif k == "set_concept_map_element":
        cmap = next((m for m in term["concept_maps"] if m["id"] == op["map"]), None)
        if cmap is None:
            raise OpError(f"Unknown concept map {op['map']!r}")
        el = next((e for e in cmap["elements"] if e["code"] == op["code"]), None)
        if el is None:
            if not op.get("target") or not op.get("grade"):
                raise OpError("A new element needs a target and a grade")
            el = {"code": op["code"], "display": op.get("display", op["code"])}
            cmap["elements"].append(el)
        if op.get("grade") and op["grade"] not in GRADES:
            raise OpError(f"Grade must be one of {sorted(GRADES)}")
        if op.get("lifecycle") and op["lifecycle"] not in MAPPING_LIFECYCLE:
            raise OpError(f"Lifecycle must be one of {sorted(MAPPING_LIFECYCLE)}")
        for f in ("target", "grade", "lifecycle", "display"):
            if op.get(f):
                el[f] = op[f]
        el.setdefault("lifecycle", "approved")
        cmap["version"] = _bump(cmap.get("version", "1.0"))
    elif k == "add_concept_map":
        m = copy.deepcopy(op["map"])
        if any(x["id"] == m["id"] or x["source"] == m["source"] for x in term["concept_maps"]):
            raise OpError(f"A concept map for {m['source']} already exists")
        for f in ("id", "version", "lifecycle", "source", "target", "elements"):
            if f not in m:
                raise OpError(f"Concept map missing {f!r}")
        term["concept_maps"].append(m)
    elif k == "register_source":
        s = copy.deepcopy(op["source"])
        if any(x["id"] == s["id"] for x in cat["sources"]):
            raise OpError(f"Source {s['id']!r} is already registered")
        for f in ("id", "label", "short", "owner", "authority", "capability", "exposes"):
            if f not in s:
                raise OpError(f"Source description missing {f!r}")
        if s.get("lifecycle", "onboarding") not in SOURCE_LIFECYCLE:
            raise OpError("Invalid source lifecycle")
        for e in s["exposes"]:
            if e["record_class"] not in {c for sec in cat["sections"] for c in sec["record_classes"]}:
                raise OpError(f"Unknown record class {e['record_class']!r}")
        cat["sources"].append(s)
    elif k == "set_source_lifecycle":
        if op["lifecycle"] not in SOURCE_LIFECYCLE:
            raise OpError(f"Source lifecycle must be one of {sorted(SOURCE_LIFECYCLE)}")
        _source(cat, op["source"])["lifecycle"] = op["lifecycle"]
    elif k == "identity_decision":
        if op["decision"] not in ("confirm", "reject"):
            raise OpError("Decision must be confirm or reject")
        person = next((p for p in ident["persons"] if p["person_id"] == op["person"]), None)
        if person is None:
            raise OpError(f"Unknown person {op['person']!r}")
        link = next((lk for lk in person["links"] if lk["source"] == op["source"]), None)
        if link is None:
            raise OpError(f"No link from {op['person']} to {op['source']}")
        link["previous"] = {"method": link["method"], "confidence": link["confidence"], "status": link.get("status")}
        if op["decision"] == "confirm":
            link.update(method="steward-verified", confidence=1.0, status="confirmed")
        else:
            link["status"] = "rejected"
        if op.get("note"):
            link["note"] = op["note"]
    elif k == "restore_version":
        if load_version is None:
            raise OpError("Restoring needs access to the version store")
        old = load_version(int(op["version"]))
        for key in ("catalogue", "terminology", "identity"):
            d[key] = copy.deepcopy(old[key])
    else:
        raise OpError(f"Unknown operation {k!r}")
