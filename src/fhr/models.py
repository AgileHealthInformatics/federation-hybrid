"""Data contracts: the record request and the federated patient record."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Grade(str, Enum):
    """Equivalence of a native code to the concept it is read as (P11)."""

    exact = "exact"
    narrower = "narrower"
    broader = "broader"
    related = "related"
    unmappable = "unmappable"


# --------------------------------------------------------------------------
# Request: a Logical Information Request in "record" mode
# --------------------------------------------------------------------------
class Requester(BaseModel):
    id: str
    role: str
    organisation: str


class RecordRequest(BaseModel):
    subject: str = Field(..., description="Governed person identifier, e.g. NID-DEMO-1001")
    sections: list[str] | None = Field(None, description="IPS-style sections; omit for all")
    as_at: datetime | None = Field(None, description="Point in time for 'current' semantics; default now")
    history_days: int | None = Field(None, ge=1, le=36500, description="Look-back for results, encounters, documents")
    purpose: str
    requester: Requester
    render_units: bool = True
    deadline_seconds: float = Field(3.0, gt=0, le=60)
    provenance_depth: Literal["summary", "full"] = "full"
    break_glass: bool = False
    mode: Literal["federated", "materialised", "hybrid"] = Field(
        "federated",
        description="federated: ask every source now. materialised: serve the governed summary view as built. "
        "hybrid: ask every source now, and fill only the sources that fail from the view, marked stale.",
    )


# --------------------------------------------------------------------------
# Result model
# --------------------------------------------------------------------------
class SourceStatus(str, Enum):
    complete = "complete"
    no_matching_data = "no_matching_data"
    partial = "partial"
    not_permitted = "not_permitted"
    unavailable = "unavailable"
    failed = "failed"
    semantics_unresolved = "semantics_unresolved"
    identity_uncertain = "identity_uncertain"
    not_asked = "not_asked"
    stale = "stale"  # unavailable live; its last materialised statements are shown instead


class TransformStep(BaseModel):
    kind: Literal["mapping", "concept-map", "transformation"]
    id: str
    version: str
    detail: str


class Coding(BaseModel):
    system: str | None = None
    code: str | None = None
    display: str | None = None


class ClinicalStatement(BaseModel):
    """One statement from one source, normalised for reading but never merged.

    Carries the native form, how it was interpreted, and where it came from.
    """

    id: str
    section: str
    record_class: str
    source: str
    source_label: str
    native_code: Coding | None = None
    concept: Coding | None = None
    concept_key: str | None = None
    grade: Grade = Grade.exact
    status: str | None = None
    verification: str | None = None
    asserted: datetime | None = None
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    role: str | None = None
    value: Any = None
    unit: str | None = None
    rendered_value: Any = None
    rendered_unit: str | None = None
    daily_amount: float | None = None
    daily_unit: str | None = None
    dose_text: str | None = None
    reaction: str | None = None
    severity: str | None = None
    title: str | None = None
    text: str | None = None
    accession: str | None = None
    version: str | None = None
    record_id: str
    authority: str
    match_method: str
    match_confidence: float
    transformation_chain: list[TransformStep] = Field(default_factory=list)
    quality: list[str] = Field(default_factory=list)
    native: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    retrieved_at: datetime


Reconciliation = Literal["agreed", "needs-review", "single-source", "information", "conflict"]


class RecordEntry(BaseModel):
    """A reconciled line in the record: a view over member statements, not a new fact."""

    id: str
    section: str
    key: str | None
    display: str
    status: str | None = None
    reconciliation: Reconciliation = "agreed"
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    latest: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Coverage(BaseModel):
    source: str
    label: str
    status: str
    count: int = 0
    detail: str = ""


class SectionResult(BaseModel):
    id: str
    label: str
    ips_loinc: str | None
    entries: list[RecordEntry]
    coverage: list[Coverage]
    summary: str | None = None


class Alert(BaseModel):
    level: Literal["critical", "warning", "info"]
    text: str
    section: str | None = None
    entry: str | None = None


class SourceReport(BaseModel):
    source: str
    label: str
    interface: str | None = None
    status: SourceStatus
    items_returned: int = 0
    suppressed: int = 0
    withheld: int = 0
    unmapped: int = 0
    detail: str = ""
    native_queries: list[str] = Field(default_factory=list)
    unresolved_classes: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None


class ExcludedSource(BaseModel):
    source: str
    reason: str


class Demographics(BaseModel):
    name: str
    birth_date: str
    age: int
    sex: str
    source: str


class PatientRecord(BaseModel):
    request_id: str
    catalogue_version: str
    terminology_version: str
    subject: str
    demographics: Demographics
    as_at: datetime
    window_from: datetime
    purpose: str
    overall_status: Literal["complete", "partial"]
    sections: list[SectionResult]
    statements: list[ClinicalStatement]
    alerts: list[Alert]
    sources: list[SourceReport]
    excluded: list[ExcludedSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    mode: Literal["federated", "materialised", "hybrid"] = "federated"
    elapsed_ms: int | None = None
    stale_sources: dict[str, datetime] = Field(default_factory=dict)
    freshness: dict[str, Any] | None = None
