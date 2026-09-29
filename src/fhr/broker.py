"""Federation broker, in record mode.

  validate -> pre-filter policy -> resolve identity -> resolve record classes
  -> plan -> execute in parallel under a deadline -> normalise -> suppress
  entered-in-error -> reconcile per section -> coverage -> alerts -> audit

The broker holds plans, not data: the record exists only for the lifetime of
the request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
import uuid
from datetime import datetime, timedelta, timezone

from . import audit
from .adapters import REGISTRY, WINDOWED, AdapterResponse, SubRequest
from .catalogue import Catalogue
from .identity import IdentityIndex, IdentityLink
from .models import (
    Alert,
    ClinicalStatement,
    Coverage,
    Demographics,
    ExcludedSource,
    PatientRecord,
    RecordRequest,
    SectionResult,
    SourceReport,
    SourceStatus,
)
from .normalise import SUPPRESSED_VERIFICATION, normalise
from .policy import pre_filter, request_context
from .reconcile import Context, reconcile_section
from .sources.state import SourceOffline, SourceRefused
from .terminology import Terminology

FAILED = {SourceStatus.unavailable, SourceStatus.failed, SourceStatus.not_permitted, SourceStatus.identity_uncertain}


ABSENCE_FLAGS = {"not-on-gp-list", "not-in-gp-record", "not-dispensed-recently"}

# A fallback returns (statements, built_at, record classes) for a source, or None.
Fallback = Callable[[str], "tuple[list[ClinicalStatement], datetime, list[str]] | None"]


def _age(delta: timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "under a minute"
    if mins < 120:
        return f"{mins} min"
    if mins < 48 * 60:
        return f"{mins // 60} h"
    return f"{mins // 1440} days"


class RequestRejected(ValueError):
    """The request itself is invalid."""


class FederationBroker:
    def __init__(self, catalogue: Catalogue, index: IdentityIndex, terminology: Terminology):
        self.cat = catalogue
        self.index = index
        self.term = terminology
        self._plan_cache: dict[tuple, list[str]] = {}

    def _validate(self, req: RecordRequest) -> list[str]:
        if req.purpose not in self.cat.purposes:
            raise RequestRejected(f"Unknown purpose '{req.purpose}'. Allowed: {', '.join(self.cat.purposes)}")
        if not self.index.known(req.subject):
            raise RequestRejected(f"Subject '{req.subject}' is not in the identity index")
        sections = req.sections or list(self.cat.sections)
        bad = [s for s in sections if s not in self.cat.sections]
        if bad:
            raise RequestRejected(f"Unknown section(s): {', '.join(bad)}")
        return sections

    async def handle(self, req: RecordRequest, fallback: Fallback | None = None) -> PatientRecord:
        """Assemble the record now. With a fallback (hybrid mode), any source that fails to answer
        is filled from its last materialised statements, marked stale, before reconciliation."""
        sections = self._validate(req)
        cat = self.cat
        started = datetime.now(timezone.utc)
        request_id = str(uuid.uuid4())
        as_at = (req.as_at or started).astimezone(timezone.utc)
        window_from = as_at - timedelta(days=req.history_days or cat.rules["history_window_days"])
        med_from = as_at - timedelta(days=max(req.history_days or 0, cat.rules.get("medication_history_days", 0)) or cat.rules["history_window_days"])
        ctx_signed = request_context(request_id, req.requester.model_dump(), req.purpose, req.break_glass)

        reports: dict[str, SourceReport] = {}
        excluded: list[ExcludedSource] = []
        requested: dict[str, list[str]] = {}

        def rep(sid: str, status: SourceStatus, detail: str, **kw) -> SourceReport:
            s = cat.sources[sid]
            reports[sid] = SourceReport(source=sid, label=s["label"], interface=s["capability"]["interface"], status=status, detail=detail, **kw)
            return reports[sid]

        # Derived sources (materialised views) are never federated with their own originals.
        candidates = [sid for sid in cat.sources if cat.exposes_any(sid, sections) and not cat.is_derived(sid)]
        failed: list[str] = []

        # Policy pre-filter, before identity lookup
        allowed = []
        for sid in candidates:
            if not cat.is_live(sid):
                rep(sid, SourceStatus.not_asked, f"Registered but not live (lifecycle: {cat.lifecycle(sid)})")
                continue
            d = pre_filter(cat, req.purpose, sid)
            (allowed.append(sid) if d.allowed else rep(sid, SourceStatus.not_asked, f"Excluded at pre-filter: {d.reason}"))

        # Identity (P4: sources not described in the catalogue are never queried)
        links: dict[str, IdentityLink] = {}
        for link in self.index.resolve(req.subject):
            if not cat.is_registered(link.source):
                excluded.append(ExcludedSource(source=link.source, reason="Holds records for this person but is not described in the catalogue (P4: metadata before data)"))
            else:
                links[link.source] = link
        threshold = cat.identity_thresholds.get(req.purpose, 1.0)

        planned: list[tuple[str, IdentityLink, SubRequest, list[str]]] = []
        for sid in allowed:
            link = links.get(sid)
            if link is None:
                rep(sid, SourceStatus.not_asked, "Identity index records no data for this person here")
                continue
            if link.confidence < threshold:
                rep(sid, SourceStatus.identity_uncertain, f"{link.method} match {link.confidence:.2f} is below the {threshold:.2f} threshold for {req.purpose}")
                continue
            approved, unresolved = cat.classes_for(sid, sections)
            if not approved:
                rep(sid, SourceStatus.semantics_unresolved, "Source may hold relevant data but no approved mapping exists", unresolved_classes=unresolved)
                continue
            sub = SubRequest(sid, link.local_id, approved, window_from, as_at, ctx_signed, med_window_from=med_from,
                             endpoint=cat.sources[sid]["capability"].get("endpoint", sid),
                             code_systems={c: cat.exposure(sid, c).get("code_system") for c in approved})
            adapter = REGISTRY[cat.sources[sid]["capability"]["interface"]]
            key = (cat.version, sid, tuple(approved), link.local_id, window_from.date(), as_at.date())
            self._plan_cache.setdefault(key, adapter.plan(sub))
            requested[sid] = approved
            planned.append((sid, link, sub, unresolved))

        async def run(sid: str, sub: SubRequest):
            adapter = REGISTRY[cat.sources[sid]["capability"]["interface"]]
            t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(adapter.execute(sub), timeout=req.deadline_seconds)
            except BaseException as e:  # noqa: BLE001 - every failure is reported, never swallowed
                resp = asyncio.TimeoutError() if isinstance(e, asyncio.CancelledError) else e
            return resp, int((time.perf_counter() - t0) * 1000)

        outcomes = await asyncio.gather(*(run(sid, sub) for sid, _, sub, _ in planned))
        retrieved_at = datetime.now(timezone.utc)

        statements: list[ClinicalStatement] = []
        for (sid, link, sub, unresolved), (resp, ms) in zip(planned, outcomes, strict=True):
            key = (cat.version, sid, tuple(sub.classes), link.local_id, window_from.date(), as_at.date())
            r = rep(sid, SourceStatus.complete, "", native_queries=self._plan_cache[key], unresolved_classes=unresolved, elapsed_ms=ms)
            if isinstance(resp, SourceRefused):
                r.status, r.detail = SourceStatus.not_permitted, f"Source refused: {resp}"
                requested.pop(sid, None)
                failed.append(sid)
                continue
            if isinstance(resp, SourceOffline):
                r.status, r.detail = SourceStatus.unavailable, f"Source unreachable: {resp}"
                requested.pop(sid, None)
                failed.append(sid)
                continue
            if isinstance(resp, asyncio.TimeoutError):
                r.status, r.detail = SourceStatus.unavailable, f"No response within the {req.deadline_seconds:g}s deadline; retry offered"
                requested.pop(sid, None)
                failed.append(sid)
                continue
            if not isinstance(resp, AdapterResponse):
                r.status, r.detail = SourceStatus.failed, f"Adapter or source error: {type(resp).__name__}: {resp}"
                requested.pop(sid, None)
                failed.append(sid)
                continue

            kept = 0
            for n, raw in enumerate(resp.statements):
                if raw.verification in SUPPRESSED_VERIFICATION:
                    r.suppressed += 1
                    continue
                if raw.record_class == "medication" and raw.status not in ("active", "on-hold", "intended", "draft"):
                    end = raw.effective_end or raw.asserted
                    if end and end < med_from:
                        continue
                if raw.record_class in WINDOWED and raw.effective_start and raw.effective_start < window_from:
                    continue
                st = normalise(raw, n, sid, link, cat, self.term, req.render_units, req.provenance_depth == "full", retrieved_at)
                if st is None:
                    r.unmapped += 1
                    continue
                statements.append(st)
                kept += 1
            r.items_returned = kept
            r.withheld = resp.withheld
            notes = []
            if resp.withheld:
                notes.append(f"{resp.withheld} entr{'y' if resp.withheld == 1 else 'ies'} withheld by source policy")
            if r.suppressed:
                notes.append(f"{r.suppressed} entered-in-error record{'s' if r.suppressed > 1 else ''} suppressed")
            if r.unmapped:
                notes.append(f"{r.unmapped} record{'s' if r.unmapped > 1 else ''} with no approved code mapping")
            if unresolved:
                notes.append(f"{', '.join(unresolved)}: mapping not yet approved")
            if resp.withheld or r.unmapped or unresolved:
                r.status = SourceStatus.partial
            elif not kept:
                r.status = SourceStatus.no_matching_data
            r.detail = "; ".join(notes) or "Answered fully"

        # Hybrid: fill failed sources from the materialised view, marked stale
        stale: dict[str, datetime] = {}
        if fallback is not None:
            for sid in failed:
                got = fallback(sid)
                if not got:
                    continue
                view_stmts, built_at, classes = got
                r = reports[sid]
                age = _age(as_at - built_at)
                for st in view_stmts:
                    st = st.model_copy(deep=True)
                    st.flags = [*st.flags, "stale"]
                    st.notes = [*st.notes, f"From the materialised view built {age} ago; {r.label} did not answer live"]
                    statements.append(st)
                r.detail = f"Live: {r.detail}. Showing {len(view_stmts)} statement(s) from the materialised view built {age} ago"
                r.status = SourceStatus.stale
                r.items_returned = len(view_stmts)
                requested[sid] = classes
                stale[sid] = built_at

        # Reconcile per section
        alerts: list[Alert] = []
        rctx = Context(self.term, cat, as_at, window_from, reports, requested, alerts)
        section_results = []
        for sec_id in sections:
            sec = cat.sections[sec_id]
            sec_stmts = [s for s in statements if s.section == sec_id]
            entries, summary = reconcile_section(sec_id, sec_stmts, rctx)
            by_id = {s.id: s for s in sec_stmts}
            for e in entries:
                stale_src = sorted({by_id[m].source for m in e.members if m in by_id and "stale" in by_id[m].flags})
                if stale_src:
                    e.flags.append("includes-stale")
                    e.notes.append("Includes " + ", ".join(f"{cat.sources[x]['short']} data as of {_age(as_at - stale[x])} ago" for x in stale_src)
                                   + " (materialised view; source unavailable live)")
                elif stale and set(e.flags) & ABSENCE_FLAGS and set(stale) & {"gp", "pharmacy"}:
                    e.notes.append("Absence judged against " + ", ".join(f"{cat.sources[x]['short']} data as of {_age(as_at - stale[x])} ago" for x in stale if x in ("gp", "pharmacy")))
            section_results.append(
                SectionResult(id=sec_id, label=sec["label"], ips_loinc=sec.get("ips_loinc"), entries=entries,
                              coverage=self._coverage(sec_id, sec_stmts, reports, requested), summary=summary)
            )

        # Source-level alerts
        for r in reports.values():
            if r.status in FAILED:
                alerts.append(Alert(level="warning", text=f"{r.label}: {r.status.value.replace('_', ' ')}. {r.detail}. The record may be incomplete."))
            elif r.status == SourceStatus.stale:
                alerts.append(Alert(level="warning", text=f"{r.label}: unavailable live. Showing its data from the materialised view as of {_age(as_at - stale[r.source])} ago."))
            elif r.status == SourceStatus.partial and (r.withheld or r.unresolved_classes):
                alerts.append(Alert(level="info", text=f"{r.label}: partial. {r.detail}"))
        for e in excluded:
            alerts.append(Alert(level="info", text=f"{e.source}: {e.reason}"))
        warnings = []
        if any("probabilistic-identity" in s.flags for s in statements):
            warnings.append("Some entries are linked to this person by a probabilistic identity match")
        order = {"critical": 0, "warning": 1, "info": 2}
        alerts.sort(key=lambda a: order[a.level])

        asked = [r for r in reports.values() if r.status != SourceStatus.not_asked]
        overall = "complete" if all(r.status in (SourceStatus.complete, SourceStatus.no_matching_data) for r in asked) and not excluded else "partial"

        record = PatientRecord(
            request_id=request_id, catalogue_version=cat.version, terminology_version=self.term.version,
            subject=req.subject, demographics=Demographics(**self.index.demographics(req.subject, as_at)),
            as_at=as_at, window_from=window_from, purpose=req.purpose, overall_status=overall,
            sections=section_results, statements=statements, alerts=alerts,
            sources=[reports[s] for s in cat.sources if s in reports], excluded=excluded, warnings=warnings,
            started_at=started, completed_at=datetime.now(timezone.utc),
            mode="hybrid" if fallback is not None else "federated", stale_sources=stale,
        )
        record.elapsed_ms = int((record.completed_at - started).total_seconds() * 1000)
        audit.record({
            "request_id": request_id, "catalogue_version": cat.version, "terminology_version": self.term.version,
            "requester": req.requester.model_dump(), "purpose": req.purpose, "break_glass": req.break_glass,
            "subject": req.subject, "sections": sections, "as_at": as_at.isoformat(),
            "sources": [{"source": r.source, "status": r.status.value, "statements": r.items_returned, "suppressed": r.suppressed,
                         "withheld": r.withheld, "detail": r.detail, "native_queries": r.native_queries} for r in record.sources],
            "excluded": [e.model_dump() for e in excluded], "overall_status": overall,
            "alert_count": {lvl: sum(a.level == lvl for a in alerts) for lvl in ("critical", "warning", "info")},
        })
        return record

    def _coverage(self, sec_id: str, stmts: list[ClinicalStatement], reports: dict[str, SourceReport], requested: dict[str, list[str]]) -> list[Coverage]:
        classes = set(self.cat.sections[sec_id]["record_classes"])
        out = []
        for sid, src in self.cat.sources.items():
            exposed = [e for e in src["exposes"] if e["record_class"] in classes]
            if not exposed:
                continue
            r = reports.get(sid)
            if r is None:
                continue
            if r.status in FAILED:
                out.append(Coverage(source=sid, label=src["short"], status=r.status.value, detail=r.detail))
            elif r.status == SourceStatus.stale:
                out.append(Coverage(source=sid, label=src["short"], status="stale", count=sum(1 for s in stmts if s.source == sid), detail=r.detail))
            elif r.status == SourceStatus.not_asked:
                out.append(Coverage(source=sid, label=src["short"], status="not_asked", detail=r.detail))
            elif not set(requested.get(sid, [])) & classes:
                out.append(Coverage(source=sid, label=src["short"], status="semantics_unresolved",
                                    detail="Holds this kind of record but the mapping is not yet approved"))
            else:
                n = sum(1 for s in stmts if s.source == sid)
                out.append(Coverage(source=sid, label=src["short"], status="contributed" if n else "no_data", count=n))
        return out
