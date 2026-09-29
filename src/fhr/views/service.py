"""Materialised view service: build, serve, fill, check freshness, query populations.

The view is a projection of the federated record, not a copy of any source.
It is built through the broker, so it carries the record's reconciliation,
flags and provenance, plus the catalogue version it was built under. It is
registered in the catalogue as a derived source with a sponsor, a use case,
a refresh policy and a review date (P5: model selectively), and it is never
authoritative.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from ..controlplane.diff import record_diff
from ..models import Alert, ClinicalStatement, PatientRecord, RecordRequest, Requester
from ..scenario import record_request
from .store import ViewStore

BUILDER = Requester(id="summary-view-builder", role="system", organisation="Federation service")


class ViewUnavailable(Exception):
    """The view cannot serve: not registered, not live, or not built for this person."""


class ViewService:
    def __init__(self, workbench, store: ViewStore):
        self.wb = workbench
        self.store = store

    # -- registration ----------------------------------------------------------
    def view(self, version: int | None = None) -> dict[str, Any] | None:
        cat = self.wb.objects(version)[0]
        views = cat.views()
        return views[0] if views else None

    def _require(self, live: bool = True) -> dict[str, Any]:
        v = self.view()
        if v is None:
            raise ViewUnavailable("No materialised view is registered in the catalogue. Onboard one through the workbench first (P4).")
        if live and not self.wb.objects()[0].is_live(v["id"]):
            raise ViewUnavailable(f"The view {v['id']} is registered but not live (lifecycle {v.get('lifecycle')}).")
        return v

    # -- building ---------------------------------------------------------------
    async def build(self, trigger: str = "manual", subjects: list[str] | None = None) -> dict[str, Any]:
        v = self._require(live=False)
        policy = v.get("materialisation", {})
        version = self.wb.current_version()
        cat_version = f"v{version}"
        build_id = self.store.start_build(v["id"], cat_version, trigger)
        persons = subjects or self.wb.objects()[2].person_ids()
        deadline = float(policy.get("build_deadline_seconds", 8))

        async def one(pid: str) -> PatientRecord:
            req = record_request(pid, requester=BUILDER, deadline_seconds=deadline)
            return await self.wb.assemble(req, version, log=False)

        records = await asyncio.gather(*(one(p) for p in persons))
        for rec in records:
            self.store.put(v["id"], rec.subject, build_id, rec.completed_at, rec.catalogue_version, rec.overall_status, rec.model_dump_json())
        checks = self.lineage_checks(records)
        self.store.finish_build(build_id, len(records), checks)
        self.wb.store.event("summary-view builder", f"built {v['id']}", version=version,
                            detail=f"{len(records)} subject(s), trigger: {trigger}; lineage {'ok' if all(c['status'] == 'pass' for c in checks) else 'issues'}")
        return {"build_id": build_id, "view": v["id"], "catalogue_version": cat_version, "subjects": len(records), "trigger": trigger, "checks": checks}

    def lineage_checks(self, records: list[PatientRecord]) -> list[dict[str, str]]:
        cat = self.wb.objects()[0]
        orphan = [s.id for r in records for s in r.statements if s.source not in cat.sources or cat.is_derived(s.source) or not s.record_id]
        return [
            {"id": "L1", "name": "Every stored statement traces to a catalogued, non-derived source record",
             "status": "fail" if orphan else "pass",
             "detail": f"Untraceable: {', '.join(orphan[:5])}" if orphan else f"{sum(len(r.statements) for r in records)} statements traced"},
            {"id": "L2", "name": "Every stored record states the catalogue version it was built under",
             "status": "pass" if all(r.catalogue_version for r in records) else "fail",
             "detail": ", ".join(sorted({r.catalogue_version for r in records}))},
        ]

    # -- serving -------------------------------------------------------------------
    def _load(self, subject: str) -> tuple[dict[str, Any], dict[str, Any], PatientRecord]:
        v = self._require()
        row = self.store.get(v["id"], subject)
        if row is None:
            raise ViewUnavailable(f"The view has not been built for {subject} yet.")
        return v, row, PatientRecord.model_validate_json(row["record"])

    def freshness(self, v: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        policy = v.get("materialisation", {})
        built = datetime.fromisoformat(row["built_at"])
        age_h = round((datetime.now(timezone.utc) - built).total_seconds() / 3600, 1)
        max_age = float(policy.get("max_age_hours", 24))
        current = f"v{self.wb.current_version()}"
        reasons = []
        if age_h > max_age:
            reasons.append(f"Older than the {max_age:g} h refresh policy ({age_h:g} h)")
        if row["catalogue_version"] != current:
            reasons.append(f"Built under catalogue {row['catalogue_version']}; the catalogue is now {current}")
        return {
            "view": v["id"], "built_at": row["built_at"], "age_hours": age_h, "max_age_hours": max_age,
            "built_under": row["catalogue_version"], "current_catalogue": current,
            "rebuild_required": bool(reasons), "reasons": reasons, "authority": v.get("authority"),
        }

    def serve(self, subject: str) -> PatientRecord:
        t0 = datetime.now(timezone.utc)
        v, row, rec = self._load(subject)
        rec.request_id = str(uuid.uuid4())
        rec.mode = "materialised"
        rec.freshness = self.freshness(v, row)
        age = rec.freshness["age_hours"]
        rec.alerts.insert(0, Alert(level="warning" if rec.freshness["rebuild_required"] else "info",
                                   text=f"Served from the materialised view, built {age:g} h ago under catalogue {row['catalogue_version']}. "
                                        f"Derived, not authoritative: nothing that happened since is shown."
                                        + (" Rebuild required: " + "; ".join(rec.freshness["reasons"]) if rec.freshness["rebuild_required"] else "")))
        rec.started_at = t0
        rec.completed_at = datetime.now(timezone.utc)
        rec.elapsed_ms = max(1, int((rec.completed_at - t0).total_seconds() * 1000))
        return rec

    def fallback_for(self, subject: str):
        """For hybrid mode: a function giving a failed source's last materialised statements."""
        try:
            _, row, rec = self._load(subject)
        except ViewUnavailable:
            return None
        built = datetime.fromisoformat(row["built_at"])
        cat = self.wb.objects()[0]
        sections = list(cat.sections)

        def fill(source_id: str) -> tuple[list[ClinicalStatement], datetime, list[str]] | None:
            stmts = [s for s in rec.statements if s.source == source_id]
            if not stmts:
                return None
            return stmts, built, cat.classes_for(source_id, sections)[0]

        return fill

    async def record(self, req: RecordRequest, version: int | None = None) -> PatientRecord:
        """Serve a record in the requested mode."""
        if req.mode == "materialised":
            return self.serve(req.subject)
        if req.mode == "hybrid":
            if version and version != self.wb.current_version():
                raise ViewUnavailable("Hybrid mode uses the current catalogue only")
            fill = self.fallback_for(req.subject)
            rec = await self.wb.assemble(req, version, fallback=fill)
            if fill is None:
                rec.alerts.insert(0, Alert(level="info", text="Hybrid mode requested, but no materialised entry exists for this person: served fully federated."))
            else:
                v, row, _ = self._load(req.subject)
                rec.freshness = self.freshness(v, row)
            return rec
        return await self.wb.assemble(req, version)

    async def compare_live(self, subject: str) -> dict[str, Any]:
        """What has changed at the sources since the view was built.

        Relabelled for this direction of comparison: 'missing from view' is something
        a clinician would see live but not in the view (the safety risk of serving stale);
        'outdated in view' is something the view still shows that is no longer so.
        """
        v, row, mat = self._load(subject)
        live = await self.wb.assemble(record_request(subject, deadline_seconds=8.0, requester=BUILDER), log=False)
        diff = record_diff(mat, live)
        relabel = {"raises": "missing-from-view", "hides": "outdated-in-view", "visible": "other"}
        for c in diff["changes"]:
            c["safety"] = relabel[c["safety"]]
        diff["counts"] = {k: sum(c["safety"] == k for c in diff["changes"]) for k in ("missing-from-view", "outdated-in-view", "other")}
        return {"subject": subject, "name": mat.demographics.name, "freshness": self.freshness(v, row),
                "live_as_at": live.as_at.isoformat(), "diff": diff}

    async def compare_modes(self, subject: str, deadline: float = 3.0) -> dict[str, Any]:
        """The same person's record served three ways, side by side."""
        req = lambda mode: record_request(subject, mode=mode, deadline_seconds=deadline)  # noqa: E731
        fed, hyb = await asyncio.gather(self.record(req("federated")), self.record(req("hybrid")))
        try:
            mat = self.serve(subject)
        except ViewUnavailable as e:
            mat, why = None, str(e)
        else:
            why = None

        def summary(r: PatientRecord | None) -> dict[str, Any] | None:
            if r is None:
                return None
            review = [f"{s.label}: {e.display}" for s in r.sections for e in s.entries if e.reconciliation in ("needs-review", "conflict")]
            return {
                "mode": r.mode, "elapsed_ms": r.elapsed_ms, "overall": r.overall_status, "catalogue_version": r.catalogue_version,
                "sources": {s.source: s.status.value for s in r.sources},
                "stale_sources": {k: v.isoformat() for k, v in r.stale_sources.items()},
                "alerts": {lvl: sum(a.level == lvl for a in r.alerts) for lvl in ("critical", "warning", "info")},
                "entries": sum(len(s.entries) for s in r.sections), "needs_review": review,
                "documents": sum(len(s.entries) for s in r.sections if s.id == "documents"),
                "freshness": r.freshness, "as_of": (r.freshness or {}).get("built_at") or r.as_at.isoformat(),
            }

        return {"subject": subject, "federated": summary(fed), "hybrid": summary(hyb), "materialised": summary(mat), "materialised_unavailable": why}

    # -- status and population --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        v = self.view()
        if v is None:
            return {"registered": False}
        live = self.wb.objects()[0].is_live(v["id"])
        rows = self.store.summaries(v["id"])
        return {
            "registered": True, "live": live, "view": v,
            "entries": [{**r, "freshness": self.freshness(v, r)} for r in rows],
            "builds": self.store.builds(v["id"]),
        }

    QUERIES = {
        "medications-needing-review": ("medications", lambda e: e.reconciliation == "needs-review", "Medications needing review"),
        "allergy-conflicts": ("allergies", lambda e: e.reconciliation == "conflict", "Conflicting allergy information"),
        "not-on-gp-record": (None, lambda e: bool({"not-on-gp-list", "not-in-gp-record"} & set(e.flags)), "Recorded elsewhere but not on the GP record"),
        "active-diabetes": ("problems", lambda e: e.status == "active" and "diabetes" in e.display.lower(), "Active diabetes"),
    }

    def population(self, query: str) -> dict[str, Any]:
        v = self._require()
        if query not in self.QUERIES:
            raise KeyError(f"No population query {query!r}")
        section, pred, label = self.QUERIES[query]
        t0 = datetime.now(timezone.utc)
        rows, oldest = [], None
        for row in self.store.all(v["id"]):
            rec = PatientRecord.model_validate_json(row["record"])
            oldest = min(oldest or row["built_at"], row["built_at"])
            for sec in rec.sections:
                if section and sec.id != section:
                    continue
                for e in sec.entries:
                    if pred(e):
                        rows.append({"subject": rec.subject, "name": rec.demographics.name, "section": sec.label, "entry": e.display,
                                     "status": e.status, "reconciliation": e.reconciliation, "flags": e.flags, "built_at": row["built_at"]})
        ms = max(1, int((datetime.now(timezone.utc) - t0).total_seconds() * 1000))
        return {"query": query, "label": label, "rows": rows, "elapsed_ms": ms, "as_of_oldest_build": oldest,
                "note": "Answered from the materialised view: fast, and as old as its oldest entry. Federation would have to ask every source about every person."}
