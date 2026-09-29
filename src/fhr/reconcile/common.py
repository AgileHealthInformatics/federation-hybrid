"""Shared reconciliation context and helpers.

Reconciliation groups statements that are about the same thing and reports how
they agree or differ. It never merges them into a new fact and never resolves a
disagreement on the clinician's behalf: it shows the most recent assertion as
indicative, with the others alongside.

One rule runs through every section: absence of evidence is only reported
when the source that would hold the evidence actually answered.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from ..catalogue import Catalogue
from ..models import Alert, ClinicalStatement, SourceReport, SourceStatus
from ..terminology import Terminology

# A stale source (served from the materialised view) is still evidence, as of its build time; the broker notes its age.
ANSWERED = {SourceStatus.complete, SourceStatus.partial, SourceStatus.no_matching_data, SourceStatus.stale}


@dataclass
class Context:
    term: Terminology
    cat: Catalogue
    as_at: datetime
    window_from: datetime
    reports: dict[str, SourceReport]
    requested: dict[str, list[str]]  # source -> record classes actually queried
    alerts: list[Alert]

    def answered(self, source: str, record_class: str) -> bool:
        r = self.reports.get(source)
        return bool(r and r.status in ANSWERED and record_class in self.requested.get(source, []))

    def status_of(self, source: str) -> SourceStatus | None:
        r = self.reports.get(source)
        return r.status if r else None

    def short(self, source: str) -> str:
        return self.cat.sources[source]["short"] if source in self.cat.sources else source

    def ago(self, dt: datetime | None) -> str:
        if dt is None:
            return "date unknown"
        d = (self.as_at - dt).days
        if d <= 0:
            return "today"
        if d == 1:
            return "1 day ago"
        if d < 60:
            return f"{d} days ago"
        if d < 730:
            return f"{round(d / 30.44)} months ago"
        return f"{round(d / 365.25)} years ago"


def cluster(items: list[ClinicalStatement], same: Callable[[ClinicalStatement, ClinicalStatement], bool]) -> list[list[ClinicalStatement]]:
    """Union-find clustering, preserving first-seen order."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if same(items[i], items[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[ClinicalStatement]] = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    return list(groups.values())


def latest(items: Iterable[ClinicalStatement]) -> ClinicalStatement | None:
    items = [i for i in items if i.asserted is not None]
    return max(items, key=lambda s: s.asserted) if items else None


def latest_by_source(items: Iterable[ClinicalStatement]) -> dict[str, ClinicalStatement]:
    out: dict[str, ClinicalStatement] = {}
    for s in items:
        cur = out.get(s.source)
        if cur is None or (s.asserted and (cur.asserted is None or s.asserted > cur.asserted)):
            out[s.source] = s
    return out


def join(parts: list[str]) -> str:
    return "; ".join(p for p in parts if p)
