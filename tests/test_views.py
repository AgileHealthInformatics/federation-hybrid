"""The materialised view: governed as a derived source, served three ways."""

from __future__ import annotations

import pytest

from conftest import cr_by_title, register_view, run
from fhr.scenario import record_request
from fhr.sources import events, state
from fhr.views.service import ViewUnavailable


def src(rec, sid):
    return next(s for s in rec.sources if s.source == sid).status.value


def entry(rec, section, display):
    sec = next(s for s in rec.sections if s.id == section)
    return next((e for e in sec.entries if e.display == display), None)


# -- P4: a view must be registered before it is built or served --------------------
def test_view_refused_until_registered(wb, views):
    with pytest.raises(ViewUnavailable, match="registered"):
        views.serve("NID-DEMO-1001")
    with pytest.raises(ViewUnavailable):
        run(views.build())


def test_conformance_for_a_derived_source(wb, views):
    cid = cr_by_title(wb, "summary-view")
    before = {c["id"]: c["status"] for c in run(wb.conformance(cid, "summary-view"))["checks"]}
    assert before["C1"] == "warn" and before["C10"] == "warn"  # nothing stored yet
    assert before["C2"] == before["C3"] == before["C8"] == before["C9"] == "pass"
    register_view(wb, views)
    after = run(wb.conformance_live("summary-view"))
    assert after["overall"] == "pass"


def test_policy_is_mandatory(wb, views):
    from fhr.controlplane.conformance import _derived_checks

    cat = wb.objects()[0]
    bad = {"authority": "Derived", "materialisation": {"sponsor": "x"}}
    checks = {c["id"]: c["status"] for c in _derived_checks(cat, bad, [])}
    assert checks["C8"] == "fail" and checks["C9"] == "fail"


def test_view_is_never_federated_with_its_originals(wb, views):
    register_view(wb, views)
    rec = run(wb.assemble(record_request(deadline_seconds=0.5)))
    assert "summary-view" not in {s.source for s in rec.sources}


def test_build_records_lineage_and_version(wb, views):
    register_view(wb, views, build=False)
    b = run(views.build())
    assert b["catalogue_version"] == "v2" and b["subjects"] == 2
    assert all(c["status"] == "pass" for c in b["checks"])


# -- Serving ---------------------------------------------------------------------------
def test_materialised_is_fast_and_states_its_age(wb, views):
    register_view(wb, views)
    rec = views.serve("NID-DEMO-1001")
    assert rec.mode == "materialised" and rec.elapsed_ms < 200
    assert rec.freshness["built_under"] == "v2" and not rec.freshness["rebuild_required"]
    assert "Derived, not authoritative" in rec.alerts[0].text


def test_view_holds_what_a_slow_source_gives_a_patient_build(wb, views, monkeypatch):
    monkeypatch.setenv("FHR_IMAGING_DELAY", "1.5")
    register_view(wb, views)  # builds with an 8 s deadline
    live = run(wb.assemble(record_request(deadline_seconds=0.5)))
    assert src(live, "imaging") == "unavailable"
    assert src(views.serve("NID-DEMO-1001"), "imaging") == "complete"


def test_hybrid_fills_only_failed_sources_and_marks_them_stale(wb, views):
    register_view(wb, views)
    state.OFFLINE.add("gp")
    fed = run(views.record(record_request(deadline_seconds=0.5)))
    hyb = run(views.record(record_request(deadline_seconds=0.5, mode="hybrid")))
    assert src(fed, "gp") == "unavailable" and src(hyb, "gp") == "stale"
    assert src(hyb, "hospital-a") == "complete"  # live sources stay live
    assert "gp" in hyb.stale_sources
    met = entry(hyb, "medications", "Metformin")
    assert "includes-stale" in met.flags and "dose-discrepancy" in met.flags
    assert entry(fed, "medications", "Metformin").flags == []  # without the GP the discrepancy is invisible


def test_hybrid_absence_flags_state_their_evidence_age(wb, views):
    register_view(wb, views)
    state.OFFLINE.add("gp")
    hyb = run(views.record(record_request(deadline_seconds=0.5, mode="hybrid")))
    bis = entry(hyb, "medications", "Bisoprolol")
    assert "not-on-gp-list" in bis.flags
    assert any("Absence judged against GP data as of" in n for n in bis.notes)


# -- Freshness ------------------------------------------------------------------------
def test_new_activity_is_missing_from_the_view(wb, views):
    register_view(wb, views)
    events.apply("gp-adds-bisoprolol")
    events.apply("pharmacy-dispenses-ramipril")
    cmp = run(views.compare_live("NID-DEMO-1001"))
    kinds = {(c["safety"], c["item"]) for c in cmp["diff"]["changes"]}
    assert ("outdated-in-view", "Ramipril") in kinds
    assert ("missing-from-view", "warning") in kinds  # bisoprolol not dispensed: a new live alert
    # the view itself still says what it said
    assert "not-on-gp-list" in entry(views.serve("NID-DEMO-1001"), "medications", "Bisoprolol").flags


def test_rebuild_catches_up(wb, views):
    register_view(wb, views)
    events.apply("pharmacy-dispenses-ramipril")
    run(views.build("after activity"))
    assert entry(views.serve("NID-DEMO-1001"), "medications", "Ramipril").reconciliation == "agreed"


def test_age_policy(wb, views):
    register_view(wb, views)
    views.store.age_by("summary-view", 30)
    f = views.serve("NID-DEMO-1001").freshness
    assert f["rebuild_required"] and "24 h refresh policy" in f["reasons"][0]


def test_catalogue_change_marks_view_for_rebuild(wb, views):
    register_view(wb, views)
    cid = cr_by_title(wb, "vital-signs")
    imp = run(wb.impact(cid))
    assert "marked for rebuild" in imp["materialised"]
    wb.approve(cid, "cso", "Device readings excluded at source; approved.")
    f = views.serve("NID-DEMO-1001").freshness
    assert f["rebuild_required"] and "catalogue is now v3" in f["reasons"][0]


# -- Population ---------------------------------------------------------------------------
def test_population_query(wb, views):
    register_view(wb, views)
    pop = views.population("medications-needing-review")
    names = {(r["name"], r["entry"]) for r in pop["rows"]}
    assert ("Jane Smith", "Metformin") in names and pop["elapsed_ms"] < 500
    with pytest.raises(KeyError):
        views.population("everything")


def test_compare_modes(wb, views):
    register_view(wb, views)
    state.OFFLINE.add("gp")
    m = run(views.compare_modes("NID-DEMO-1001", deadline=0.5))
    assert m["federated"]["sources"]["gp"] == "unavailable"
    assert m["hybrid"]["sources"]["gp"] == "stale"
    assert m["materialised"]["sources"]["gp"] == "complete"
    assert len(m["hybrid"]["needs_review"]) > len(m["federated"]["needs_review"])
