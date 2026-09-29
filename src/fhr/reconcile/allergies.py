"""Allergies: group reactions to the same substance class, show them under the
broadest recorded class (the safer label), and treat any 'no known allergy'
statement that sits alongside a recorded allergy as a conflict.

Distinguishes three states that a flat list cannot: allergies recorded;
no known allergies recorded; and no allergy information available.
"""

from __future__ import annotations

from ..models import Alert, ClinicalStatement, RecordEntry
from .common import Context, cluster, join, latest, latest_by_source


def reconcile(stmts: list[ClinicalStatement], ctx: Context) -> tuple[list[RecordEntry], str | None]:
    term = ctx.term
    active = [s for s in stmts if s.status in (None, "active")]
    positives = [s for s in active if not term.is_negation(s.concept.code)]
    negatives = [s for s in active if term.is_negation(s.concept.code)]
    entries: list[RecordEntry] = []

    for n, g in enumerate(cluster(positives, lambda a, b: term.related(a.concept.code, b.concept.code))):
        per_source = latest_by_source(g)
        broadest = min((s.concept.code for s in g), key=term.depth)
        display = next(s.concept.display for s in g if s.concept.code == broadest)
        notes = []
        others = [s for s in per_source.values() if s.concept.code != broadest]
        if others:
            notes.append("Also recorded as " + join([f"{s.concept.display} at {ctx.short(s.source)} ({term.relation(s.concept.code, broadest)})" for s in others]))
        reactions = [f"{s.reaction} ({ctx.short(s.source)})" for s in per_source.values() if s.reaction]
        entries.append(
            RecordEntry(
                id=f"allergies:{n}", section="allergies", key=broadest, display=display, status="active",
                reconciliation="agreed" if len(per_source) > 1 else "single-source",
                flags=["recorded-at-different-detail"] if others else [], notes=notes, members=[s.id for s in g],
                sources=list(per_source), latest=latest(g).id,
                detail={"reactions": reactions, "severity": [s.severity for s in g if s.severity]},
            )
        )

    for s in negatives:
        n = len(entries)
        e = RecordEntry(
            id=f"allergies:{n}", section="allergies", key=s.concept.code, display=f"{s.concept.display}",
            status="negation", reconciliation="single-source", members=[s.id], sources=[s.source], latest=s.id,
            detail={"negation": True},
        )
        if positives:
            e.flags.append("conflicts-with-recorded-allergy")
            e.reconciliation = "conflict"
            recorded = ", ".join(sorted({x.display for x in entries if not x.detail.get("negation")}))
            e.notes.append(f"Recorded by {ctx.short(s.source)} {ctx.ago(s.asserted)}, but {recorded} is recorded elsewhere")
            ctx.alerts.append(Alert(level="critical", section="allergies", entry=e.id,
                                    text=f"Conflicting allergy information: {ctx.short(s.source)} recorded '{s.concept.display}' "
                                         f"({ctx.ago(s.asserted)}), but {recorded} is recorded elsewhere"))
        entries.append(e)

    summary = None
    if not positives:
        silent = [src for src, st in _allergy_sources(ctx) if st not in ("contributed", "no_data")]
        if negatives:
            summary = "No known allergies recorded by " + ", ".join(sorted({ctx.short(s.source) for s in negatives}))
        else:
            summary = "No allergy information available"
        if silent:
            summary += f". Not confirmed by {', '.join(ctx.short(s) for s in silent)} (did not answer)"
    return entries, summary


def _allergy_sources(ctx: Context):
    for sid in ctx.cat.sources:
        if ctx.cat.exposure(sid, "allergy") is None:
            continue
        if ctx.answered(sid, "allergy"):
            yield sid, "contributed"
        elif ctx.status_of(sid) is None or ctx.status_of(sid).value == "not_asked":
            continue
        else:
            yield sid, ctx.status_of(sid).value
