"""Conformance harness: capability claims are tested, not trusted.

Runs a source against a candidate catalogue snapshot through the same adapter
the broker would use, and checks what it claims:

  C1  answers every record class it exposes, for the test subjects linked to it
  C2  refuses a request whose signed context has been tampered with
  C3  refuses a purpose it does not accept
  C4  returns codes that conform to the declared code system
  C5  every returned code resolves through the candidate concept maps
  C6  every statement carries a record identifier and a date
  C7  observed latency is within twice the declared expectation
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..adapters import REGISTRY, SubRequest
from ..catalogue import Catalogue
from ..identity import IdentityIndex, IdentityLink
from ..policy import request_context
from ..sources.state import SourceRefused
from ..terminology import ICD10, LOINC, SNOMED, Terminology

PATTERNS = {
    SNOMED: re.compile(r"^\d{6,18}$"),
    "http://read.info/readv2": re.compile(r"^[A-Za-z0-9.]{5}$"),
    LOINC: re.compile(r"^\d{1,7}-\d$"),
    ICD10: re.compile(r"^[A-Z]\d{2}(\.\d{1,2})?$"),
    "urn:demo:dmd": re.compile(r"^VMP-[A-Z0-9]+$"),
}
HARNESS = {"id": "conformance-harness", "role": "system", "organisation": "Federation service"}


def _check(cid: str, name: str, status: str, detail: str) -> dict[str, str]:
    return {"id": cid, "name": name, "status": status, "detail": detail}


async def run(objs: tuple[Catalogue, Terminology, IdentityIndex], source_id: str, deadline: float = 5.0) -> dict[str, Any]:
    cat, term, index = objs
    if source_id not in cat.sources:
        raise KeyError(f"{source_id} is not described in the candidate catalogue")
    src = cat.sources[source_id]
    cap = src["capability"]
    started = datetime.now(timezone.utc)
    adapter = REGISTRY.get(cap["interface"])
    checks: list[dict[str, str]] = []
    if adapter is None:
        checks.append(_check("C1", "Answers each record class", "fail", f"No adapter for interface {cap['interface']!r}"))
        return _result(source_id, started, checks, 0)

    classes = [e["record_class"] for e in src["exposes"]]
    systems = {e["record_class"]: e.get("code_system") for e in src["exposes"]}
    derived = bool(src.get("derived"))
    if derived:  # a view is keyed by the governed person identifier itself
        subjects = [(pid, IdentityLink(source_id, pid, "governed-identifier", 1.0)) for pid in index.person_ids()]
    else:
        subjects = [(pid, lk) for pid in index.person_ids() for lk in index.resolve(pid) if lk.source == source_id]
    purpose = cap["purposes_accepted"][0]
    far = started - timedelta(days=36500)

    def sub(local_id: str, ctx: dict[str, Any]) -> SubRequest:
        return SubRequest(source_id, local_id, classes, far, started, ctx, endpoint=cap.get("endpoint", source_id),
                          code_systems=systems, med_window_from=far)

    def ctx_for(p: str) -> dict[str, Any]:
        return request_context(f"conformance-{uuid.uuid4()}", HARNESS, p, False)

    # C1
    statements, latencies, errors = [], [], []
    for pid, lk in subjects:
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(adapter.execute(sub(lk.local_id, ctx_for(purpose))), timeout=deadline)
            statements += resp.statements
        except Exception as e:  # noqa: BLE001
            errors.append(f"{pid}: {type(e).__name__}: {e}")
        latencies.append(int((time.perf_counter() - t0) * 1000))
    per_class = {c: sum(1 for s in statements if s.record_class == c) for c in classes}
    if derived and not errors and not statements:
        checks.append(_check("C1", "Answers each record class", "warn", "Reachable, but nothing is stored yet: the first build follows approval"))
    elif errors:
        checks.append(_check("C1", "Answers each record class", "fail", "; ".join(errors)))
    elif not subjects:
        checks.append(_check("C1", "Answers each record class", "warn", "No test subjects are linked to this source in the identity index"))
    else:
        checks.append(_check("C1", "Answers each record class", "pass",
                             f"{len(subjects)} test subject(s); statements by class: " + ", ".join(f"{c} {n}" for c, n in per_class.items())))

    local = subjects[0][1].local_id if subjects else "TEST-0000"
    # C2
    bad = dict(ctx_for(purpose), signature="0" * 64)
    try:
        await asyncio.wait_for(adapter.execute(sub(local, bad)), timeout=deadline)
        checks.append(_check("C2", "Refuses a tampered request context", "fail", "Answered a request with an invalid signature"))
    except SourceRefused:
        checks.append(_check("C2", "Refuses a tampered request context", "pass", "Refused as expected"))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("C2", "Refuses a tampered request context", "warn", f"Did not answer, but failed rather than refused: {type(e).__name__}"))

    # C3
    others = [p for p in cat.purposes if p not in cap["purposes_accepted"]]
    if not others:
        checks.append(_check("C3", "Refuses purposes it does not accept", "pass", "Accepts every purpose in the catalogue: nothing to test"))
    else:
        try:
            await asyncio.wait_for(adapter.execute(sub(local, ctx_for(others[0]))), timeout=deadline)
            checks.append(_check("C3", "Refuses purposes it does not accept", "fail", f"Answered a '{others[0]}' request although the catalogue says it accepts only {', '.join(cap['purposes_accepted'])}"))
        except SourceRefused:
            checks.append(_check("C3", "Refuses purposes it does not accept", "pass", f"Refused '{others[0]}' as declared"))
        except Exception as e:  # noqa: BLE001
            checks.append(_check("C3", "Refuses purposes it does not accept", "warn", f"Failed rather than refused: {type(e).__name__}"))

    coded = [s for s in statements if s.code]
    # C4
    bad_codes = sorted({f"{s.code} ({s.record_class})" for s in coded
                        if systems.get(s.record_class) in PATTERNS and not PATTERNS[systems[s.record_class]].match(str(s.code))})
    checks.append(_check("C4", "Codes conform to the declared code system",
                         "fail" if bad_codes else "pass",
                         ("Do not match the declared system: " + ", ".join(bad_codes)) if bad_codes else f"{len(coded)} coded statements checked"))

    # C5
    distinct: dict[tuple[str, str], Any] = {}
    for s in coded:
        distinct.setdefault((s.code_system, s.code), s)
    unmapped = []
    for (system, code), s in distinct.items():
        res = term.resolve(system, code, s.display, include_proposed=True)
        if res is None or (s.record_class in ("medication", "dispense") and term.medicine(res.code) is None):
            unmapped.append(f"{code} '{s.display}'")
    total = len(distinct)
    pct = 100.0 if total == 0 else round(100 * (total - len(unmapped)) / total, 1)
    status = "pass" if not unmapped else ("warn" if pct >= 80 else "fail")
    checks.append(_check("C5", "Returned codes resolve through the concept maps", status,
                         f"{pct}% of {total} distinct codes resolve" + (f"; without a mapping: {', '.join(sorted(unmapped))}" if unmapped else "")))

    # C6
    missing = [s.record_id or "?" for s in statements if not s.record_id or not (s.asserted or s.effective_start)]
    checks.append(_check("C6", "Statements carry an identifier and a date", "fail" if missing else "pass",
                         f"Missing on: {', '.join(missing[:5])}" if missing else f"{len(statements)} statements checked"))

    # C7
    expected = cap.get("expected_latency_ms") or 1000
    worst = max(latencies) if latencies else 0
    checks.append(_check("C7", "Latency within twice the declared expectation", "pass" if worst <= 2 * expected else "warn",
                         f"Observed {worst} ms against {expected} ms declared"))
    if derived:
        checks += _derived_checks(cat, src, statements)
    return _result(source_id, started, checks, worst)


def _derived_checks(cat: Catalogue, src: dict[str, Any], statements) -> list[dict[str, str]]:
    """Extra checks for a materialised view: it must be governed as a projection, not a new record."""
    out = []
    policy = src.get("materialisation", {})
    missing = [k for k in ("sponsor", "use_case", "refresh", "max_age_hours", "review_date", "triggers") if not policy.get(k)]
    review_ok = bool(policy.get("review_date")) and str(policy["review_date"]) > datetime.now(timezone.utc).date().isoformat()
    out.append(_check("C8", "Materialisation policy declared (sponsor, use case, refresh, max age, review date, triggers)",
                      "fail" if missing or not review_ok else "pass",
                      (f"Missing: {', '.join(missing)}" if missing else "") + ("" if review_ok else " Review date absent or past")
                      or f"Sponsor: {policy['sponsor']}; review by {policy['review_date']}; max age {policy['max_age_hours']} h"))
    out.append(_check("C9", "Declared as derived and not authoritative",
                      "pass" if "not authoritative" in src.get("authority", "").lower() else "fail", src.get("authority", "")))
    untraced = [s.record_id for s in statements
                if (s.native.get("lineage") or {}).get("source") not in cat.sources or cat.is_derived((s.native.get("lineage") or {}).get("source", ""))]
    out.append(_check("C10", "Stored content traces to catalogued, non-derived sources",
                      "fail" if untraced else ("pass" if statements else "warn"),
                      f"Untraced: {', '.join(untraced[:5])}" if untraced else (f"{len(statements)} statements traced" if statements else "Nothing stored yet")))
    return out


def _result(source_id: str, started: datetime, checks: list[dict[str, str]], worst: int) -> dict[str, Any]:
    overall = "fail" if any(c["status"] == "fail" for c in checks) else ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"source": source_id, "run_at": started.isoformat(), "overall": overall, "checks": checks, "observed_ms": worst}
