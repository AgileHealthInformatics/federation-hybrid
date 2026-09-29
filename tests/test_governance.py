"""Governance rules of the control plane."""

from __future__ import annotations

import pytest

from conftest import changes_for, cr_by_title, run
from fhr.controlplane.ops import OpError, apply_ops
from fhr.controlplane.workbench import GovernanceError
from fhr.scenario import record_request


def test_seeded_with_one_version_and_demo_drafts(wb):
    assert wb.current_version() == 1
    titles = {c["title"] for c in wb.changes()}
    assert {"Approve Hospital B vital-signs mapping", "Deprecate Hospital A formulary code HA-F-0877",
            "Confirm Sam Patel's community pharmacy match", "Onboard archived previous-practice system (gp-legacy)",
            "Register materialised patient summary view (summary-view)"} == titles
    assert all(c["status"] == "draft" for c in wb.changes())


def test_records_state_their_catalogue_version(wb):
    rec = run(wb.assemble(record_request(deadline_seconds=0.5)))
    assert rec.catalogue_version == "v1"


def test_no_approval_without_impact_analysis(wb):
    cid = cr_by_title(wb, "vital-signs")
    with pytest.raises(GovernanceError, match="impact analysis"):
        wb.approve(cid, "cso", "Looks right to me.")


def test_four_eyes(wb):
    cid = wb.create_change("cso", "Self-approved", "test", [{"op": "set_source_lifecycle", "source": "imaging", "lifecycle": "suspended"}])["id"]
    run(wb.impact(cid))
    with pytest.raises(GovernanceError, match="Four-eyes"):
        wb.approve(cid, "cso", "Approving my own change.")


def test_right_role_required(wb):
    cid = cr_by_title(wb, "pharmacy match")
    run(wb.impact(cid))
    with pytest.raises(GovernanceError, match="identity-steward"):
        wb.approve(cid, "cso", "I am not the identity steward.")


def test_written_signoff_required(wb):
    cid = cr_by_title(wb, "vital-signs")
    run(wb.impact(cid))
    with pytest.raises(GovernanceError, match="sign-off"):
        wb.approve(cid, "cso", "ok")


def test_approval_applies_as_new_version(wb):
    cid = cr_by_title(wb, "vital-signs")
    run(wb.impact(cid))
    c = wb.approve(cid, "cso", "Device readings excluded at source; approved.")
    assert c["status"] == "applied" and c["applied_version"] == 2 == wb.current_version()
    rec = run(wb.assemble(record_request(deadline_seconds=0.5)))
    vitals = next(s for s in rec.sections if s.id == "vital-signs")
    assert {c.source: c.status for c in vitals.coverage}["hospital-b"] == "contributed"


def test_stale_impact_must_be_rerun(wb):
    a, b = cr_by_title(wb, "vital-signs"), cr_by_title(wb, "pharmacy match")
    run(wb.impact(a))
    run(wb.impact(b))
    wb.approve(a, "cso", "Approved after review.")
    with pytest.raises(GovernanceError, match="moved on"):
        wb.approve(b, "steward", "Verified by phone.")
    run(wb.impact(b))
    assert wb.approve(b, "steward", "Verified by phone.")["status"] == "applied"


def test_reject_and_withdraw(wb):
    cid = cr_by_title(wb, "HA-F-0877")
    assert wb.reject(cid, "cso", "Would hide a discrepancy.")["status"] == "rejected"
    other = cr_by_title(wb, "vital-signs")
    assert wb.reject(other, "analyst", "Raised in error.")["status"] == "withdrawn"
    with pytest.raises(GovernanceError):
        wb.approve(cid, "cso", "Too late to approve.")


def test_invalid_operations_rejected_at_creation(wb):
    with pytest.raises(OpError):
        wb.create_change("analyst", "Bad", "", [{"op": "set_exposure_mapping", "source": "nowhere", "record_class": "x", "lifecycle": "approved"}])
    with pytest.raises(OpError):
        wb.create_change("analyst", "Bad", "", [{"op": "set_concept_map_element", "map": "cm:icd10-snomed", "code": "E11.9", "grade": "sort-of"}])
    with pytest.raises(OpError):
        wb.create_change("analyst", "Bad", "", [{"op": "drop_table"}])


def test_ops_never_mutate_the_snapshot(wb):
    docs = wb.store.docs()
    before = str(docs)
    apply_ops(docs, [{"op": "set_source_lifecycle", "source": "gp", "lifecycle": "suspended"}])
    assert str(docs) == before


def test_restore_creates_a_new_version(wb):
    cid = cr_by_title(wb, "vital-signs")
    run(wb.impact(cid))
    wb.approve(cid, "cso", "Approved after review.")
    r = wb.create_change("analyst", "Roll back", "Reverting", [{"op": "restore_version", "version": 1}])
    run(wb.impact(r["id"]))
    c = wb.approve(r["id"], "cso", "Rollback agreed at safety review.")
    assert c["applied_version"] == 3
    assert wb.version_diff(1, 3) == []
    assert any("vital-sign" in d["item"] for d in wb.version_diff(2, 3))


def test_governance_events_recorded(wb):
    cid = cr_by_title(wb, "vital-signs")
    run(wb.impact(cid))
    wb.approve(cid, "cso", "Approved after review.")
    actions = [e["action"] for e in wb.store.events(change_id=cid)]
    assert {"created change request", "impact analysis run", "approved", "applied"} <= set(actions)


def test_request_log_holds_no_clinical_values(wb):
    rec = run(wb.assemble(record_request(deadline_seconds=0.5)))
    logged = wb.store.request(rec.request_id)
    text = str(logged)
    assert logged["version"] == 1
    assert "Metformin" not in text and "penicillin" not in text


def test_changes_for_helper_smoke(wb):
    cid = cr_by_title(wb, "vital-signs")
    imp = run(wb.impact(cid))
    assert changes_for(imp, "Jane Smith")
