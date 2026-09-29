"""Differences between catalogue versions, and between two assemblies of the
same patient's record."""

from __future__ import annotations

from typing import Any

from ..models import PatientRecord

REVIEW_FLAGS = {
    "dose-discrepancy", "stopped-elsewhere", "not-on-gp-list", "not-dispensed-recently",
    "not-in-gp-record", "conflicts-with-recorded-allergy",
}


# ---------------------------------------------------------------------------
# Catalogue content
# ---------------------------------------------------------------------------
def catalogue_diff(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    sa = {s["id"]: s for s in a["catalogue"]["sources"]}
    sb = {s["id"]: s for s in b["catalogue"]["sources"]}
    for sid in sb.keys() - sa.keys():
        out.append({"area": "source", "item": sid, "change": f"registered (lifecycle {sb[sid].get('lifecycle')})"})
    for sid in sa.keys() - sb.keys():
        out.append({"area": "source", "item": sid, "change": "removed"})
    for sid in sa.keys() & sb.keys():
        if sa[sid].get("lifecycle") != sb[sid].get("lifecycle"):
            out.append({"area": "source", "item": sid, "change": f"lifecycle {sa[sid].get('lifecycle')} -> {sb[sid].get('lifecycle')}"})
        ea = {e["record_class"]: e for e in sa[sid]["exposes"]}
        eb = {e["record_class"]: e for e in sb[sid]["exposes"]}
        for rc in ea.keys() & eb.keys():
            la, lb = ea[rc]["mapping"].get("lifecycle"), eb[rc]["mapping"].get("lifecycle")
            if la != lb:
                out.append({"area": "record-class mapping", "item": f"{sid} / {rc}", "change": f"{la} -> {lb} (v{eb[rc]['mapping'].get('version')})"})

    ma = {m["id"]: m for m in a["terminology"]["concept_maps"]}
    mb = {m["id"]: m for m in b["terminology"]["concept_maps"]}
    for mid in mb.keys() - ma.keys():
        out.append({"area": "concept map", "item": mid, "change": f"added ({len(mb[mid]['elements'])} elements)"})
    for mid in ma.keys() & mb.keys():
        xa = {e["code"]: e for e in ma[mid]["elements"]}
        xb = {e["code"]: e for e in mb[mid]["elements"]}
        for code in xb.keys() - xa.keys():
            out.append({"area": "concept map", "item": f"{mid} {code}", "change": f"added -> {xb[code]['target']} ({xb[code]['grade']}, {xb[code].get('lifecycle', 'approved')})"})
        for code in xa.keys() & xb.keys():
            changes = [f"{f} {xa[code].get(f, 'approved' if f == 'lifecycle' else None)} -> {xb[code].get(f, 'approved' if f == 'lifecycle' else None)}"
                       for f in ("target", "grade", "lifecycle")
                       if xa[code].get(f, "approved" if f == "lifecycle" else None) != xb[code].get(f, "approved" if f == "lifecycle" else None)]
            if changes:
                out.append({"area": "concept map", "item": f"{mid} {code}", "change": "; ".join(changes)})

    pa = {(p["person_id"], lk["source"]): lk for p in a["identity"]["persons"] for lk in p["links"]}
    pb = {(p["person_id"], lk["source"]): lk for p in b["identity"]["persons"] for lk in p["links"]}
    for key in pa.keys() & pb.keys():
        x, y = pa[key], pb[key]
        if (x["method"], x["confidence"], x.get("status")) != (y["method"], y["confidence"], y.get("status")):
            out.append({"area": "identity link", "item": f"{key[0]} -> {key[1]}",
                        "change": f"{x['method']} {x['confidence']} -> {y['method']} {y['confidence']}" + (f" ({y['status']})" if y.get("status") else "")})
    return out


# ---------------------------------------------------------------------------
# Record content
# ---------------------------------------------------------------------------
def _entries(r: PatientRecord) -> dict[tuple[str, str], Any]:
    return {(s.id, e.display): (s.label, e) for s in r.sections for e in s.entries}


def record_diff(before: PatientRecord, after: PatientRecord) -> dict[str, Any]:
    """What a clinician would see differently. Classified for safety review:
    'hides' = a discrepancy, alert or source disappears; 'raises' = new ones appear."""
    changes: list[dict[str, Any]] = []
    eb, ea = _entries(before), _entries(after)

    def summary(e) -> dict[str, Any]:
        return {"status": e.status, "reconciliation": e.reconciliation, "flags": e.flags, "sources": e.sources}

    for key in sorted(ea.keys() - eb.keys()):
        label, e = ea[key]
        changes.append({"kind": "entry added", "section": label, "item": e.display, "detail": f"{e.status or ''} from {', '.join(e.sources)}",
                        "safety": "raises" if set(e.flags) & REVIEW_FLAGS else "visible", "after": summary(e)})
    for key in sorted(eb.keys() - ea.keys()):
        label, e = eb[key]
        changes.append({"kind": "entry removed", "section": label, "item": e.display, "detail": f"was {e.status or ''} from {', '.join(e.sources)}",
                        "safety": "hides" if set(e.flags) & REVIEW_FLAGS or e.reconciliation in ("needs-review", "conflict") else "visible",
                        "before": summary(e)})
    for key in sorted(ea.keys() & eb.keys()):
        label, x = eb[key]
        _, y = ea[key]
        parts, safety = [], "visible"
        if x.status != y.status:
            parts.append(f"status {x.status} -> {y.status}")
        if x.reconciliation != y.reconciliation:
            parts.append(f"reconciliation {x.reconciliation} -> {y.reconciliation}")
            if x.reconciliation in ("needs-review", "conflict") and y.reconciliation not in ("needs-review", "conflict"):
                safety = "hides"
            elif y.reconciliation in ("needs-review", "conflict"):
                safety = "raises"
        gone, new = set(x.flags) - set(y.flags), set(y.flags) - set(x.flags)
        if gone:
            parts.append("flags removed: " + ", ".join(sorted(gone)))
            if gone & REVIEW_FLAGS:
                safety = "hides"
        if new:
            parts.append("flags added: " + ", ".join(sorted(new)))
            if new & REVIEW_FLAGS and safety != "hides":
                safety = "raises"
        sx, sy = set(x.sources), set(y.sources)
        if sx != sy:
            if sy - sx:
                parts.append("now also from " + ", ".join(sorted(sy - sx)))
            if sx - sy:
                parts.append("no longer from " + ", ".join(sorted(sx - sy)))
        if parts:
            changes.append({"kind": "entry changed", "section": label, "item": y.display, "detail": "; ".join(parts),
                            "safety": safety, "before": summary(x), "after": summary(y)})

    sb = {s.source: s for s in before.sources}
    sa = {s.source: s for s in after.sources}
    for sid in sorted(sb.keys() | sa.keys()):
        x, y = sb.get(sid), sa.get(sid)
        xs, ys = (x.status.value if x else "absent"), (y.status.value if y else "absent")
        if xs != ys:
            changes.append({"kind": "source status", "section": "Sources", "item": sid, "detail": f"{xs} -> {ys}",
                            "safety": "hides" if ys in ("unavailable", "failed", "not_asked", "identity_uncertain", "absent") and xs in ("complete", "partial") else "visible"})
    xb = {e.source for e in before.excluded}
    xa = {e.source for e in after.excluded}
    for sid in sorted(xb - xa):
        changes.append({"kind": "source status", "section": "Sources", "item": sid, "detail": "no longer excluded as uncatalogued", "safety": "visible"})

    # Clinical alerts carry a section; source-level alerts do not. A source-level
    # warning that disappears because the source now answers is not hiding anything:
    # the source-status change above says so.
    ab = {(a.level, a.text, a.section) for a in before.alerts}
    aa = {(a.level, a.text, a.section) for a in after.alerts}
    for lvl, text, _sec in sorted(aa - ab, key=str):
        changes.append({"kind": "alert added", "section": "Alerts", "item": lvl, "detail": text,
                        "safety": "raises" if lvl != "info" else "visible"})
    for lvl, text, sec in sorted(ab - aa, key=str):
        changes.append({"kind": "alert removed", "section": "Alerts", "item": lvl, "detail": text,
                        "safety": "hides" if lvl != "info" and sec else "visible"})

    return {
        "subject": after.subject,
        "name": after.demographics.name,
        "changes": changes,
        "counts": {k: sum(c["safety"] == k for c in changes) for k in ("hides", "raises", "visible")},
        "overall": {"before": before.overall_status, "after": after.overall_status},
    }
