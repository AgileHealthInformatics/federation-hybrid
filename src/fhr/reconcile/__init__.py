"""Per-section reconciliation."""

from __future__ import annotations

from ..models import ClinicalStatement, RecordEntry
from . import allergies, medications, problems, simple
from .common import Context


def reconcile_section(section: str, stmts: list[ClinicalStatement], ctx: Context) -> tuple[list[RecordEntry], str | None]:
    if section == "problems":
        return problems.reconcile(stmts, ctx)
    if section == "medications":
        return medications.reconcile(stmts, ctx)
    if section == "allergies":
        return allergies.reconcile(stmts, ctx)
    if section == "immunizations":
        return simple.immunizations(stmts, ctx)
    if section in ("results", "vital-signs"):
        return simple.observations(stmts, ctx, section)
    return simple.events(stmts, ctx, section)


__all__ = ["Context", "reconcile_section"]
