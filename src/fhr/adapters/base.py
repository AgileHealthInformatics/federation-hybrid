"""Adapter contract.

An adapter translates a planned sub-request into the source's native
interface, returns every statement with its native form and identifiers,
reports the source's access decision, and never turns a failure into an empty
result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

WINDOWED = {"result", "vital-sign", "encounter", "document", "dispense"}


@dataclass
class SubRequest:
    source_id: str
    local_id: str
    classes: list[str]
    window_from: datetime
    as_at: datetime
    context: dict[str, Any]
    endpoint: str = ""
    code_systems: dict[str, str | None] = field(default_factory=dict)
    med_window_from: datetime | None = None


@dataclass
class RawStatement:
    record_class: str
    record_id: str
    code_system: str | None = None
    code: str | None = None
    display: str | None = None
    status: str | None = None
    verification: str | None = None
    asserted: datetime | None = None
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    role: str | None = None
    value: Any = None
    unit: str | None = None
    dose_qty: float | None = None
    freq_per_day: float | None = None
    dose_text: str | None = None
    reaction: str | None = None
    severity: str | None = None
    title: str | None = None
    text: str | None = None
    accession: str | None = None
    version: str | None = None
    amended: bool = False
    native: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResponse:
    statements: list[RawStatement]
    withheld: int
    decision: str
    native_queries: list[str]


class Adapter(ABC):
    interface: str

    @abstractmethod
    def plan(self, sub: SubRequest) -> list[str]:
        """Render the native queries for review and audit."""

    @abstractmethod
    async def execute(self, sub: SubRequest) -> AdapterResponse:
        """Execute against the source's governed interface."""
