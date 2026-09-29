"""The workbench service: governance rules around the versioned control plane.

Rules enforced here, not in the interface:
  * the live catalogue changes only by applying an approved change request;
  * every change request has an impact analysis against the current version
    before it can be approved (re-run if the catalogue has moved on);
  * approval needs every required role (clinical safety officer for catalogue
    and terminology content, identity steward for identity links), from
    someone other than the author, with a written sign-off;
  * a source cannot be made live without a conformance run against the
    candidate snapshot that did not fail;
  * every action is recorded as a governance event.

Personas stand in for authenticated users in this demonstrator.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..broker import FederationBroker
from ..catalogue import Catalogue
from ..data import load_json
from ..identity import IdentityIndex
from ..models import PatientRecord, RecordRequest
from ..scenario import record_request
from ..terminology import Terminology
from . import conformance as conformance_harness
from .diff import catalogue_diff, record_diff
from .ops import CLINICAL, IDENTITY, apply_ops, describe, roles_for
from .store import Store, now

PERSONAS = {
    "analyst": {"id": "analyst", "name": "A. Rowe", "role": "informatics-analyst", "label": "Informatics analyst"},
    "cso": {"id": "cso", "name": "Dr C. Morgan", "role": CLINICAL, "label": "Clinical safety officer"},
    "steward": {"id": "steward", "name": "P. Shah", "role": IDENTITY, "label": "Identity steward"},
}


class GovernanceError(Exception):
    """An action that the governance rules do not allow."""


Objects = tuple[Catalogue, Terminology, IdentityIndex]


def objects_from(docs: dict[str, Any]) -> Objects:
    return Catalogue(docs["catalogue"]), Terminology(docs["terminology"]), IdentityIndex(docs["identity"])


class Workbench:
    def __init__(self, store: Store, seed_demo: bool = True):
        self.store = store
        self._objs: dict[int, Objects] = {}
        self._brokers: dict[int, FederationBroker] = {}
        if store.is_empty():
            v = store.seed_from_files()
            store.event("system", "seeded", version=v, detail="Catalogue, terminology and identity index loaded from files")
            if seed_demo:
                self._seed_demo_changes()

    # -- snapshots and brokers -------------------------------------------
    def current_version(self) -> int:
        return self.store.current_version()

    def objects(self, version: int | None = None) -> Objects:
        v = version or self.current_version()
        if v not in self._objs:
            self._objs[v] = objects_from(self.store.docs(v))
        return self._objs[v]

    def broker(self, version: int | None = None) -> FederationBroker:
        v = version or self.current_version()
        if v not in self._brokers:
            self._brokers[v] = FederationBroker(*self._with_order(self.objects(v)))
        return self._brokers[v]

    @staticmethod
    def _with_order(objs: Objects) -> tuple[Catalogue, IdentityIndex, Terminology]:
        cat, term, index = objs
        return cat, index, term

    async def assemble(self, req: RecordRequest, version: int | None = None, log: bool = True, fallback=None) -> PatientRecord:
        v = version or self.current_version()
        record = await self.broker(v).handle(req, fallback=fallback)
        if log:
            params = req.model_dump(mode="json")
            params["as_at"] = record.as_at.isoformat()
            self.store.log_request(record.request_id, req.subject, v, params, record.overall_status)
        return record

    # -- personas ----------------------------------------------------------
    @staticmethod
    def persona(pid: str) -> dict[str, str]:
        if pid not in PERSONAS:
            raise GovernanceError(f"Unknown persona {pid!r}")
        return PERSONAS[pid]

    # -- change requests ---------------------------------------------------
    def create_change(self, persona: str, title: str, rationale: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        p = self.persona(persona)
        if not ops:
            raise GovernanceError("A change request needs at least one operation")
        if not title.strip():
            raise GovernanceError("A change request needs a title")
        roles = roles_for(ops)
        apply_ops(self.store.docs(), ops, self.store.docs)  # validate against the current version
        cid = self.store.create_change(title.strip(), rationale.strip(), p["id"], ops, roles)
        self.store.event(p["name"], "created change request", change_id=cid, detail="; ".join(describe(o) for o in ops))
        return self.change(cid)

    def import_pack(self, persona: str, name: str) -> dict[str, Any]:
        pack = load_json(f"onboarding/{name}.json", resolve=False)
        ops = [{"op": "register_source", "source": pack["source"]}]
        ops += [{"op": "add_concept_map", "map": m} for m in pack.get("concept_maps", [])]
        return self.create_change(persona, pack["title"], pack.get("rationale", ""), ops)

    def change(self, cid: int) -> dict[str, Any]:
        c = self.store.change(cid)
        c["operations"] = [describe(o) for o in c["ops"]]
        c["author_name"] = PERSONAS.get(c["author"], {}).get("name", c["author"])
        c["newly_live"] = self._newly_live(c) if c["status"] == "draft" else []
        c["stale"] = bool(c["impact"]) and c["status"] == "draft" and c["base_version"] != self.current_version()
        c["events"] = self.store.events(change_id=cid)
        return c

    def changes(self) -> list[dict[str, Any]]:
        return [self.change(c["id"]) for c in self.store.changes()]

    def candidate_docs(self, change: dict[str, Any]) -> dict[str, Any]:
        return apply_ops(self.store.docs(), change["ops"], self.store.docs)

    def _newly_live(self, change: dict[str, Any]) -> list[str]:
        base = Catalogue(self.store.docs()["catalogue"])
        cand = Catalogue(self.candidate_docs(change)["catalogue"])
        return [s for s in cand.sources if cand.is_live(s) and not (s in base.sources and base.is_live(s))]

    def _draft(self, cid: int) -> dict[str, Any]:
        c = self.store.change(cid)
        if c["status"] != "draft":
            raise GovernanceError(f"Change request {cid} is {c['status']}, not draft")
        return c

    # -- impact analysis ---------------------------------------------------
    async def impact(self, cid: int, deadline: float = 1.0) -> dict[str, Any]:
        c = self._draft(cid)
        base_v = self.current_version()
        base = self.broker(base_v)
        cand_objs = objects_from(self.candidate_docs(c))
        cand = FederationBroker(*self._with_order(cand_objs))
        persons = sorted(set(self.objects(base_v)[2].person_ids()) | set(cand_objs[2].person_ids()))
        as_at = datetime.now(timezone.utc)

        async def one(pid: str):
            req = record_request(pid, as_at=as_at, deadline_seconds=deadline)
            before, after = await asyncio.gather(base.handle(req), cand.handle(req))
            return record_diff(before, after)

        people = await asyncio.gather(*(one(p) for p in persons))
        result = {
            "run_at": now(),
            "base_version": base_v,
            "catalogue_changes": catalogue_diff(self.store.docs(base_v), self.candidate_docs(c)),
            "people": people,
            "totals": {k: sum(p["counts"][k] for p in people) for k in ("hides", "raises", "visible")},
            "note": "Records assembled for every person in the identity index under the current and the candidate catalogue, at the same moment, from the same sources.",
        }
        views = [s["id"] for s in cand_objs[0].views() if cand_objs[0].is_live(s["id"])]
        if views and c["ops"] and not all(o["op"] == "register_source" and o["source"].get("derived") for o in c["ops"]):
            result["materialised"] = (f"Applying this change moves the catalogue on from v{base_v}: the materialised view "
                                      f"{', '.join(views)} will then be marked for rebuild, and until rebuilt it serves records as they were under the older catalogue.")
        self.store.update_change(cid, impact=result, base_version=base_v)
        self.store.event("system", "impact analysis run", change_id=cid, version=base_v,
                         detail=f"{result['totals']['hides']} hides, {result['totals']['raises']} raises, {result['totals']['visible']} other changes")
        return result

    # -- conformance ---------------------------------------------------------
    async def conformance(self, cid: int, source_id: str) -> dict[str, Any]:
        c = self._draft(cid)
        result = await conformance_harness.run(objects_from(self.candidate_docs(c)), source_id)
        result["base_version"] = self.current_version()
        conf = c["conformance"]
        conf[source_id] = result
        self.store.update_change(cid, conformance=conf)
        self.store.event("system", f"conformance run: {source_id}", change_id=cid, detail=f"overall {result['overall']}")
        return result

    async def conformance_live(self, source_id: str) -> dict[str, Any]:
        """Re-test a source already in the current catalogue (periodic re-testing)."""
        result = await conformance_harness.run(self.objects(), source_id)
        self.store.event("system", f"conformance re-test: {source_id}", version=self.current_version(), detail=f"overall {result['overall']}")
        return result

    # -- decisions -------------------------------------------------------------
    def approve(self, cid: int, persona: str, signoff: str) -> dict[str, Any]:
        p = self.persona(persona)
        c = self._draft(cid)
        if p["id"] == c["author"]:
            raise GovernanceError("Four-eyes rule: the author cannot approve their own change request")
        if p["role"] not in c["required_roles"]:
            raise GovernanceError(f"This change needs approval from: {', '.join(c['required_roles'])}. {p['label']} is not one of them")
        if any(a["role"] == p["role"] for a in c["approvals"]):
            raise GovernanceError(f"Already approved by a {p['label'].lower()}")
        if len(signoff.strip()) < 10:
            raise GovernanceError("A written sign-off (at least 10 characters) is required")
        if not c["impact"]:
            raise GovernanceError("Run the impact analysis before approving")
        current = self.current_version()
        if c["base_version"] != current:
            raise GovernanceError(f"The catalogue has moved on (now v{current}) since the impact analysis (v{c['base_version']}); re-run it")
        for sid in self._newly_live(c):
            run = c["conformance"].get(sid)
            if not run:
                raise GovernanceError(f"Source {sid} would become live: run the conformance tests first")
            if run["overall"] == "fail":
                raise GovernanceError(f"Source {sid} failed conformance; it cannot be made live")
            if run.get("base_version") != current:
                raise GovernanceError(f"Conformance for {sid} was run against an older catalogue; re-run it")

        approvals = c["approvals"] + [{"persona": p["id"], "name": p["name"], "role": p["role"], "at": now(), "signoff": signoff.strip()}]
        self.store.update_change(cid, approvals=approvals)
        self.store.event(p["name"], "approved", change_id=cid, detail=signoff.strip())
        if set(c["required_roles"]) <= {a["role"] for a in approvals}:
            docs = self.candidate_docs(c)
            author = PERSONAS.get(c["author"], {}).get("name", c["author"])
            v = self.store.commit(docs, c["title"], author=author, approvers=[a["name"] for a in approvals], change_id=cid)
            self.store.update_change(cid, status="applied", applied_version=v, decided_at=now())
            self.store.event("system", "applied", change_id=cid, version=v, detail=f"Catalogue now v{v}")
        return self.change(cid)

    def reject(self, cid: int, persona: str, reason: str) -> dict[str, Any]:
        p = self.persona(persona)
        c = self._draft(cid)
        if p["role"] not in c["required_roles"] and p["id"] != c["author"]:
            raise GovernanceError("Only a required approver can reject, or the author can withdraw")
        status = "withdrawn" if p["id"] == c["author"] else "rejected"
        self.store.update_change(cid, status=status, decided_at=now(), decision_note=reason.strip())
        self.store.event(p["name"], status, change_id=cid, detail=reason.strip())
        return self.change(cid)

    # -- versions and replay ---------------------------------------------------
    def versions(self) -> list[dict[str, Any]]:
        return self.store.versions()

    def version_diff(self, a: int, b: int) -> list[dict[str, str]]:
        return catalogue_diff(self.store.docs(a), self.store.docs(b))

    async def replay(self, request_id: str, compare_version: int | None = None) -> dict[str, Any]:
        logged = self.store.request(request_id)
        req = RecordRequest(**logged["params"])
        original_v = logged["version"]
        other_v = compare_version or self.current_version()
        before, after = await asyncio.gather(self.assemble(req, original_v, log=False), self.assemble(req, other_v, log=False))
        return {
            "request_id": request_id, "subject": logged["subject"], "as_at": logged["params"].get("as_at"),
            "original_version": original_v, "compared_version": other_v,
            "catalogue_changes": catalogue_diff(self.store.docs(original_v), self.store.docs(other_v)),
            "record_changes": record_diff(before, after),
            "note": "Re-assembled at the original point in time under each catalogue version. Source content is re-fetched; the demonstrator's sources do not change, so differences come from the catalogue.",
        }

    # -- identity stewardship ----------------------------------------------------
    def identity_queue(self) -> list[dict[str, Any]]:
        cat, _, index = self.objects()
        threshold = cat.identity_thresholds.get("direct-care", 0.9)
        pending = {(o["person"], o["source"]): c["id"] for c in self.store.changes() if c["status"] == "draft"
                   for o in c["ops"] if o["op"] == "identity_decision"}
        out = []
        for lk in index.links():
            state = lk.get("status") or ("below threshold" if lk["confidence"] < threshold else ("probabilistic" if lk["method"] == "probabilistic" else "ok"))
            out.append({**lk, "state": state, "threshold": threshold, "catalogued": lk["source"] in cat.sources,
                        "pending_change": pending.get((lk["person_id"], lk["source"]))})
        order = {"below threshold": 0, "probabilistic": 1, "rejected": 2, "confirmed": 3, "ok": 4}
        return sorted(out, key=lambda x: (order.get(x["state"], 5), x["person_id"], x["source"]))

    # -- demo seeding ------------------------------------------------------------
    def _seed_demo_changes(self) -> None:
        self.create_change(
            "analyst", "Approve Hospital B vital-signs mapping",
            "Hospital B now excludes unvalidated device readings from the published vital-signs view. Requesting clinical sign-off so ward observations appear in the record.",
            [{"op": "set_exposure_mapping", "source": "hospital-b", "record_class": "vital-sign", "lifecycle": "approved"}],
        )
        self.create_change(
            "analyst", "Deprecate Hospital A formulary code HA-F-0877",
            "Hospital A pharmacy reports that local code HA-F-0877 (amlodipine 5 mg) will be reused for a new product. Proposing to deprecate the mapping.",
            [{"op": "set_concept_map_element", "map": "cm:ha-formulary-dmd", "code": "HA-F-0877", "lifecycle": "deprecated"}],
        )
        self.create_change(
            "analyst", "Confirm Sam Patel's community pharmacy match",
            "Verified with the pharmacy by phone: date of birth and address match; surname spelled differently on the pharmacy system.",
            [{"op": "identity_decision", "person": "NID-DEMO-1002", "source": "pharmacy", "decision": "confirm",
              "note": "Verified by phone with pharmacy; surname spelling differs"}],
        )
        self.import_pack("analyst", "gp-legacy")
        self.import_pack("analyst", "summary-view")
