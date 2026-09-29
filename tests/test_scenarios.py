"""The four seeded scenarios: what each change does to real records."""

from __future__ import annotations

import pytest

from conftest import changes_for, cr_by_title, run
from fhr.controlplane.workbench import GovernanceError
from fhr.scenario import record_request


def entry(rec, section, display):
    sec = next(s for s in rec.sections if s.id == section)
    return next((e for e in sec.entries if e.display == display), None)


# -- Deprecating a mapping can hide a safety discrepancy -------------------------
def test_deprecating_formulary_code_is_flagged_as_hiding(wb):
    imp = run(wb.impact(cr_by_title(wb, "HA-F-0877")))
    hides = [c for c in changes_for(imp) if c["safety"] == "hides"]
    assert any(c["item"] == "Amlodipine" and "stopped-elsewhere" in c["detail"] for c in hides)
    assert any(c["kind"] == "alert removed" for c in hides)
    assert imp["catalogue_changes"] == [{"area": "concept map", "item": "cm:ha-formulary-dmd HA-F-0877", "change": "lifecycle approved -> deprecated"}]


# -- Onboarding a source -------------------------------------------------------------
def test_onboarding_gated_on_conformance(wb):
    cid = cr_by_title(wb, "gp-legacy")
    run(wb.impact(cid))
    with pytest.raises(GovernanceError, match="conformance"):
        wb.approve(cid, "cso", "Read v2 map reviewed.")


def test_conformance_harness_on_legacy_system(wb):
    cid = cr_by_title(wb, "gp-legacy")
    result = run(wb.conformance(cid, "gp-legacy"))
    checks = {c["id"]: c for c in result["checks"]}
    assert result["overall"] == "warn"
    assert all(checks[k]["status"] == "pass" for k in ("C1", "C2", "C3", "C4", "C6", "C7"))
    assert checks["C5"]["status"] == "warn" and "1371." in checks["C5"]["detail"]


def test_onboarding_adds_history_to_the_record(wb):
    cid = cr_by_title(wb, "gp-legacy")
    run(wb.impact(cid))
    run(wb.conformance(cid, "gp-legacy"))
    c = wb.approve(cid, "cso", "Read v2 map reviewed; historic-only authority accepted.")
    assert c["status"] == "applied"
    rec = run(wb.assemble(record_request(deadline_seconds=0.5)))
    assert rec.excluded == []  # no longer refused under P4
    legacy = next(s for s in rec.sources if s.source == "gp-legacy")
    assert legacy.status.value == "partial" and legacy.unmapped == 1  # 1371. has no mapping
    assert "gp-legacy" in entry(rec, "problems", "Type 2 diabetes mellitus").sources
    assert entry(rec, "problems", "Migraine").status == "resolved"
    assert entry(rec, "medications", "Gliclazide").status == "past"


def test_legacy_stop_superseded_by_later_prescribing(wb):
    cid = cr_by_title(wb, "gp-legacy")
    run(wb.impact(cid))
    run(wb.conformance(cid, "gp-legacy"))
    wb.approve(cid, "cso", "Read v2 map reviewed; historic-only authority accepted.")
    rec = run(wb.assemble(record_request(deadline_seconds=0.5)))
    met = entry(rec, "medications", "Metformin")
    assert "gp-legacy" in met.sources
    assert "stopped-elsewhere" not in met.flags  # stopped at transfer, restarted by the new GP
    assert "dose-discrepancy" in met.flags  # the real issue is unchanged


def test_mapping_a_missing_code_resolves_the_warning(wb):
    cid = cr_by_title(wb, "gp-legacy")
    run(wb.impact(cid))
    run(wb.conformance(cid, "gp-legacy"))
    wb.approve(cid, "cso", "Read v2 map reviewed; historic-only authority accepted.")
    new = wb.create_change("analyst", "Map Read 1371.", "Found in conformance run", [
        {"op": "set_concept_map_element", "map": "cm:readv2-snomed", "code": "1371.", "target": "266919005", "grade": "exact",
         "lifecycle": "approved", "display": "Never smoked tobacco"}])
    run(wb.impact(new["id"]))
    wb.approve(new["id"], "cso", "Mapping checked against SNOMED CT.")
    result = run(wb.conformance_live("gp-legacy"))
    assert result["overall"] == "pass"


# -- Identity stewardship ---------------------------------------------------------------
def test_confirming_identity_reveals_a_new_discrepancy(wb):
    cid = cr_by_title(wb, "pharmacy match")
    imp = run(wb.impact(cid))
    sam = changes_for(imp, "Sam Patel")
    assert any(c["item"] == "Beclometasone" and c["safety"] == "raises" for c in sam)
    assert any(c["item"] == "pharmacy" and "identity_uncertain -> complete" in c["detail"] for c in sam)
    assert not any(c["safety"] == "hides" for c in sam)  # a cleared source warning is not hiding anything


def test_rejecting_identity_removes_the_source(wb):
    new = wb.create_change("analyst", "Reject Jane's pharmacy link", "Wrong person", [
        {"op": "identity_decision", "person": "NID-DEMO-1001", "source": "pharmacy", "decision": "reject"}])
    imp = run(wb.impact(new["id"]))
    jane = changes_for(imp, "Jane Smith")
    assert any(c["item"] == "Ramipril" and c["safety"] == "hides" for c in jane)
    queue = wb.identity_queue()
    assert any(x["pending_change"] == new["id"] for x in queue)


# -- Replay ---------------------------------------------------------------------------------
def test_replay_under_original_and_current_versions(wb):
    rec = run(wb.assemble(record_request(deadline_seconds=0.5)))
    cid = cr_by_title(wb, "HA-F-0877")
    run(wb.impact(cid))
    wb.approve(cid, "cso", "Accepted: code will be reused next month.")
    rp = run(wb.replay(rec.request_id))
    assert rp["original_version"] == 1 and rp["compared_version"] == 2
    assert any(c["item"] == "Amlodipine" for c in rp["record_changes"]["changes"])
    same = run(wb.replay(rec.request_id, 1))
    assert same["record_changes"]["changes"] == []


def test_record_under_a_historic_version(wb):
    cid = cr_by_title(wb, "vital-signs")
    run(wb.impact(cid))
    wb.approve(cid, "cso", "Approved after review.")
    old = run(wb.assemble(record_request(deadline_seconds=0.5), version=1))
    new = run(wb.assemble(record_request(deadline_seconds=0.5)))
    assert old.catalogue_version == "v1" and new.catalogue_version == "v2"
    cov = lambda r: {c.source: c.status for c in next(s for s in r.sections if s.id == "vital-signs").coverage}  # noqa: E731
    assert cov(old)["hospital-b"] == "semantics_unresolved" and cov(new)["hospital-b"] == "contributed"
