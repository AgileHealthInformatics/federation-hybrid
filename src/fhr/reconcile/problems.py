"""Problems: group diagnoses that are the same condition recorded at different
levels of detail, show status disagreements, and flag active problems missing
from the GP record."""

from __future__ import annotations

from collections import Counter

from ..models import Alert, ClinicalStatement, RecordEntry
from .common import Context, cluster, join, latest, latest_by_source

ACTIVE = {"active", "recurrence", "relapse"}


def reconcile(stmts: list[ClinicalStatement], ctx: Context) -> tuple[list[RecordEntry], str | None]:
    term = ctx.term
    groups = cluster(stmts, lambda a, b: term.related(a.concept.code, b.concept.code))
    entries: list[RecordEntry] = []
    for n, g in enumerate(groups):
        per_source = latest_by_source(g)
        newest = latest(per_source.values())
        counts = Counter(s.concept.code for s in g)
        top = max(counts, key=lambda c: (counts[c], term.depth(c)))
        display = next(s.concept.display for s in g if s.concept.code == top)
        status = "active" if newest.status in ACTIVE else (newest.status or "unknown")
        flags: list[str] = []
        notes: list[str] = []
        recon = "agreed" if len(per_source) > 1 else "single-source"

        codes = {s.concept.code for s in per_source.values()}
        if len(codes) > 1:
            flags.append("recorded-at-different-detail")
            parts = []
            for s in per_source.values():
                native = s.native_code
                label = f"{s.concept.display}"
                if native and native.system != s.concept.system:
                    label += f" (native {native.code})"
                parts.append(f"{ctx.short(s.source)}: {label}")
            notes.append("Recorded at different levels of detail: " + join(parts))

        statuses = {s.source: s.status for s in per_source.values()}
        if len(set(statuses.values())) > 1:
            flags.append("status-differs")
            notes.append(
                "Status differs: "
                + join([f"{s.status} at {ctx.short(s.source)} ({ctx.ago(s.asserted)})" for s in sorted(per_source.values(), key=lambda x: x.asserted)])
                + f". Showing the most recent ({ctx.short(newest.source)})."
            )
            recon = "information"

        if status == "active" and "gp" not in per_source and ctx.answered("gp", "condition"):
            flags.append("not-in-gp-record")
            notes.append(f"Recorded as active at {', '.join(ctx.short(s) for s in per_source)} but not on the GP problem list")
            recon = "needs-review"
            ctx.alerts.append(Alert(level="warning", section="problems", entry=f"problems:{n}",
                                    text=f"{display}: recorded at {', '.join(ctx.short(s) for s in per_source)}, not on the GP problem list"))

        onset = min((s.effective_start for s in g if s.effective_start), default=None)
        entries.append(
            RecordEntry(
                id=f"problems:{n}", section="problems", key=top, display=display, status=status,
                reconciliation=recon, flags=flags, notes=notes, members=[s.id for s in g],
                sources=list(per_source), latest=newest.id,
                detail={"onset": onset, "significance": next((s.extra.get("significance") for s in g if s.extra.get("significance")), None)},
            )
        )
    entries.sort(key=lambda e: (e.status != "active", e.detail.get("significance") != "major", e.display))
    return entries, None
