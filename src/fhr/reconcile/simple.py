"""Immunisations, results, vital signs, encounters and documents."""

from __future__ import annotations

from ..models import ClinicalStatement, RecordEntry
from .common import Context, cluster, latest_by_source


def immunizations(stmts: list[ClinicalStatement], ctx: Context) -> tuple[list[RecordEntry], str | None]:
    window = ctx.cat.rules["same_event_days"]

    def same(a: ClinicalStatement, b: ClinicalStatement) -> bool:
        return (
            a.concept.code == b.concept.code
            and a.effective_start is not None
            and b.effective_start is not None
            and abs((a.effective_start - b.effective_start).days) <= window
        )

    entries = []
    for n, g in enumerate(cluster(stmts, same)):
        per_source = latest_by_source(g)
        given = min(s.effective_start for s in g)
        flags, notes = [], []
        if len(per_source) > 1:
            flags.append("possible-duplicate")
            notes.append("Same vaccination recorded by " + " and ".join(ctx.short(s) for s in per_source)
                         + f" (given {ctx.ago(given)}); shown once, both records kept")
        entries.append(RecordEntry(
            id=f"immunizations:{n}", section="immunizations", key=g[0].concept.code, display=g[0].concept.display,
            status="completed", reconciliation="information", flags=flags, notes=notes, members=[s.id for s in g],
            sources=list(per_source), latest=g[0].id,
            detail={"date": given, "batch": next((s.extra.get("batch") for s in g if s.extra.get("batch")), None)},
        ))
    entries.sort(key=lambda e: e.detail["date"], reverse=True)
    return entries, None


def observations(stmts: list[ClinicalStatement], ctx: Context, section: str) -> tuple[list[RecordEntry], str | None]:
    by_key: dict[str, list[ClinicalStatement]] = {}
    for s in stmts:
        by_key.setdefault(s.concept_key, []).append(s)
    entries = []
    for n, (key, g) in enumerate(by_key.items()):
        g.sort(key=lambda s: s.effective_start)
        last = g[-1]
        prev = g[-2] if len(g) > 1 else None

        def val(s):
            return s.rendered_value if s.rendered_value is not None else s.value

        # Group flags say what the series contains; per-value detail stays on each statement.
        flags = sorted({f for s in g for f in s.flags if f in ("amended", "derived")})
        notes = list(dict.fromkeys(n for s in g for n in s.notes if "estimating equation" in n))
        for s in g:
            if "amended" in s.flags or "derived" in s.flags:
                own = [n for n in s.notes if "estimating equation" not in n]
                what = "amended" if "amended" in s.flags else "converted to " + str(s.rendered_unit)
                notes.append(f"Value from {ctx.ago(s.effective_start)} ({ctx.short(s.source)}) {what}" + (f". Source note: {own[0]}" if own else ""))
        # probable duplicates: same accession reported by more than one source
        acc: dict[str, set[str]] = {}
        for s in g:
            if s.accession:
                acc.setdefault(s.accession, set()).add(s.source)
        if any(len(v) > 1 for v in acc.values()):
            flags.append("possible-duplicate")
        entries.append(RecordEntry(
            id=f"{section}:{n}", section=section, key=key, display=last.concept.display, status="final",
            reconciliation="information", flags=flags, notes=notes, members=[s.id for s in g],
            sources=sorted({s.source for s in g}), latest=last.id,
            detail={
                "value": val(last), "unit": last.rendered_unit or last.unit, "date": last.effective_start,
                "previous": val(prev) if prev else None, "count": len(g),
                "series": [{"id": s.id, "t": s.effective_start, "v": val(s), "source": s.source, "flags": s.flags} for s in g],
            },
        ))
    entries.sort(key=lambda e: e.display)
    return entries, None


def events(stmts: list[ClinicalStatement], ctx: Context, section: str) -> tuple[list[RecordEntry], str | None]:
    entries = []
    for n, s in enumerate(sorted(stmts, key=lambda s: s.effective_start or s.asserted, reverse=True)):
        entries.append(RecordEntry(
            id=f"{section}:{n}", section=section, key=None, display=s.title or section, status=s.status,
            reconciliation="information", members=[s.id], sources=[s.source], latest=s.id,
            detail={"start": s.effective_start, "end": s.effective_end, "setting": s.extra.get("setting"),
                    "location": s.extra.get("location"), "reason": s.extra.get("reason"),
                    "doc_type": s.extra.get("doc_type"), "author": s.extra.get("author"), "text": s.text},
        ))
    return entries, None
