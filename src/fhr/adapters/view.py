"""Adapter for a materialised view registered as a derived source.

The broker never federates a derived source with its originals. This adapter
exists so the view can be tested like any other participant: the conformance
harness reads its stored statements back through it, in their native codes,
and checks that it enforces the signed request context and its purposes.
"""

from __future__ import annotations

from typing import Any

from ..models import PatientRecord
from ..sources.state import admit
from .base import Adapter, AdapterResponse, RawStatement, SubRequest

STORE: Any = None  # set by the application to the ViewStore
PURPOSES = ("direct-care",)


class MaterialisedViewAdapter(Adapter):
    interface = "materialised-view"

    def plan(self, sub: SubRequest) -> list[str]:
        return [f"READ view {sub.endpoint} subject={sub.local_id} classes={','.join(sub.classes)}"]

    async def execute(self, sub: SubRequest) -> AdapterResponse:
        admit(sub.source_id, sub.context, PURPOSES)
        row = STORE.get(sub.endpoint, sub.local_id) if STORE is not None else None
        statements = []
        if row:
            rec = PatientRecord.model_validate_json(row["record"])
            for s in rec.statements:
                if s.record_class not in sub.classes:
                    continue
                statements.append(RawStatement(
                    s.record_class, f"{sub.endpoint}/{sub.local_id}/{s.id}",
                    s.native_code.system if s.native_code else None, s.native_code.code if s.native_code else None,
                    s.native_code.display if s.native_code else None, s.status, s.verification,
                    asserted=s.asserted, effective_start=s.effective_start, effective_end=s.effective_end,
                    native={"lineage": {"source": s.source, "record_id": s.record_id}, "built_at": row["built_at"]},
                ))
        return AdapterResponse(statements, 0, "permit", self.plan(sub))
