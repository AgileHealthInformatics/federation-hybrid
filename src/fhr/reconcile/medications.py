"""Medications: reconcile what each source asserts about each medicine
(grouped at ingredient level), using dispensing as evidence of supply.

Flags, never resolutions:
  dose-discrepancy        current assertions disagree on the daily dose
  stopped-elsewhere       one source's latest assertion is stopped while another's is current
  not-on-gp-list          current elsewhere but absent from the GP list (only if the GP answered)
  not-dispensed-recently  a current GP repeat with no recent dispensing (only if the pharmacy answered)
"""

from __future__ import annotations

from ..data import parse_dt as _dt
from ..models import Alert, ClinicalStatement, RecordEntry
from .common import Context, join, latest, latest_by_source

CURRENT = {"active", "on-hold", "intended", "draft"}
PRESCRIBING = {"repeat", "acute", "discharge", "inpatient"}
REVIEW = {"dose-discrepancy", "stopped-elsewhere", "not-on-gp-list", "not-dispensed-recently"}
LABELS = {
    "dose-discrepancy": "dose differs between sources",
    "stopped-elsewhere": "stopped at one source, current at another",
    "not-on-gp-list": "not on the GP medication list",
    "not-dispensed-recently": "not dispensed recently",
}
ROLE_LABEL = {"repeat": "GP repeat", "acute": "GP acute", "clinic-list": "clinic list", "discharge": "discharge", "inpatient": "inpatient"}


def _fmt_mg(v: float) -> str:
    return f"{v:g} mg/day"


def _superseded(e: ClinicalStatement, current: list[ClinicalStatement]) -> bool:
    """An ended assertion no longer disagrees once a prescriber has started the medicine again since."""
    end = e.effective_end or e.asserted
    if end is None:
        return False
    return any(c.role in PRESCRIBING and (c.effective_start or c.asserted) and (c.effective_start or c.asserted) >= end for c in current)


def reconcile(stmts: list[ClinicalStatement], ctx: Context) -> tuple[list[RecordEntry], str | None]:
    gap = ctx.cat.rules["dispensing_gap_days"]
    by_key: dict[str, list[ClinicalStatement]] = {}
    for s in stmts:
        by_key.setdefault(s.concept_key, []).append(s)

    entries: list[RecordEntry] = []
    for n, (key, members) in enumerate(by_key.items()):
        assertions = [s for s in members if s.record_class == "medication"]
        dispenses = sorted([s for s in members if s.record_class == "dispense"], key=lambda s: s.effective_start)
        per_source = latest_by_source(assertions)

        def is_current(s: ClinicalStatement) -> bool:
            return s.status in CURRENT and (s.effective_end is None or s.effective_end > ctx.as_at)

        current = [s for s in per_source.values() if is_current(s)]

        ended = [s for s in per_source.values() if not is_current(s) and not _superseded(s, current)]
        status = "current" if current else ("past" if assertions else "dispensed only")
        display = members[0].concept.display
        flags: list[str] = []
        notes: list[str] = []

        if current and ended:
            flags.append("stopped-elsewhere")
            notes.append(
                "Status differs: "
                + join([f"{s.status} at {ctx.short(s.source)} ({ctx.ago(s.effective_end or s.asserted)}"
                        + (f", {s.extra['stop_reason']}" if s.extra.get("stop_reason") else "") + ")" for s in ended])
                + "; current at " + ", ".join(f"{ctx.short(s.source)} ({ctx.ago(s.asserted)})" for s in current)
            )

        daily = {s.source: s.daily_amount for s in current if s.daily_amount is not None}
        if len({round(v, 3) for v in daily.values()}) > 1:
            flags.append("dose-discrepancy")
            parts = []
            for s in sorted(current, key=lambda x: x.asserted, reverse=True):
                if s.daily_amount is None:
                    continue
                txt = f"{ctx.short(s.source)} {_fmt_mg(s.daily_amount)} ({ROLE_LABEL.get(s.role, s.role)}, {ctx.ago(s.asserted)}"
                if s.extra.get("last_issued"):
                    txt += f"; last issued {ctx.ago(_dt(s.extra['last_issued']))}"
                parts.append(txt + ")")
            notes.append("Daily dose differs: " + join(parts))

        if current and "gp" not in per_source and ctx.answered("gp", "medication"):
            flags.append("not-on-gp-list")
            notes.append("Current at " + ", ".join(f"{ctx.short(s.source)} ({ROLE_LABEL.get(s.role, s.role)}, {ctx.ago(s.asserted)})" for s in current)
                         + " but not on the GP medication list")
            for s in current:
                supply = s.extra.get("supply_days")
                if supply and s.asserted and not dispenses:
                    run_out = (ctx.as_at - s.asserted).days - int(supply)
                    if run_out > 0:
                        notes.append(f"{ctx.short(s.source)} supplied {supply} days at discharge; that supply would have run out about {run_out} days ago")

        gp_repeat = [s for s in current if s.source == "gp" and s.role == "repeat"]
        last_disp = dispenses[-1] if dispenses else None
        if gp_repeat:
            if ctx.answered("pharmacy", "dispense"):
                days = (ctx.as_at - last_disp.effective_start).days if last_disp else None
                if days is None or days > gap:
                    flags.append("not-dispensed-recently")
                    issued = gp_repeat[0].extra.get("last_issued")
                    notes.append(
                        (f"Last dispensed {days} days ago" if days is not None else f"No dispensing recorded in the last {(ctx.as_at - ctx.window_from).days} days")
                        + (f", although the GP last issued a prescription {ctx.ago(_dt(issued))}" if issued else "")
                    )
            else:
                st = ctx.status_of("pharmacy")
                if st is not None and st.value != "not_asked":
                    notes.append(f"Dispensing evidence unavailable (pharmacy: {st.value.replace('_', ' ')})")

        review = [f for f in flags if f in REVIEW]
        if review:
            recon = "needs-review"
        elif status != "current":
            recon = "information"
        elif len(per_source) == 1:
            recon = "single-source"
        else:
            recon = "agreed"

        indicative = latest(current) or latest(per_source.values()) or last_disp
        cells = {
            src: {
                "statement": s.id, "status": s.status, "dose_text": s.dose_text, "daily_amount": s.daily_amount,
                "asserted": s.asserted, "role": ROLE_LABEL.get(s.role, s.role), "current": is_current(s),
            }
            for src, s in per_source.items()
        }
        if last_disp:
            cells["pharmacy"] = {"statement": last_disp.id, "status": "dispensed", "asserted": last_disp.effective_start,
                                 "quantity": last_disp.extra.get("quantity"), "role": "last dispensed", "current": None,
                                 "count": len(dispenses)}
        entries.append(
            RecordEntry(
                id=f"medications:{n}", section="medications", key=key, display=display, status=status,
                reconciliation=recon, flags=flags, notes=notes, members=[s.id for s in members],
                sources=sorted({s.source for s in members}), latest=indicative.id if indicative else None,
                detail={"indicative_dose": indicative.dose_text if indicative and indicative.record_class == "medication" else None,
                        "indicative_source": indicative.source if indicative else None, "cells": cells},
            )
        )
        if review:
            ctx.alerts.append(Alert(level="warning", section="medications", entry=f"medications:{n}",
                                    text=f"{display}: " + ", ".join(LABELS[f] for f in review)))

    order = {"current": 0, "dispensed only": 1, "past": 2}
    entries.sort(key=lambda e: (order.get(e.status, 3), e.reconciliation != "needs-review", e.display))
    return entries, None
