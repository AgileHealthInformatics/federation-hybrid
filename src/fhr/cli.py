"""Command line.

    fhr serve                 # record view on :8000/ and workbench on :8000/workbench
    fhr record                # Jane Smith's record under the current catalogue
    fhr demo                  # scripted walk-through: governance, then the materialised view (in memory)
    fhr reset                 # delete the local stores (control plane and views); re-seeded on next start
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _store_path() -> str:
    return os.environ.get("FHR_STORE", ".fhr/controlplane.db")


def _print_diff(title: str, people: list[dict]) -> None:
    print(f"\n  {title}")
    for p in people:
        for c in p["changes"]:
            mark = {"hides": "HIDES ", "raises": "RAISES", "visible": "      "}[c["safety"]]
            print(f"    {mark} {p['name']:<11} {c['kind']:<14} {c['section']:<12} {c['item']:<24} {c['detail']}")


async def _demo() -> int:
    from .controlplane.store import Store
    from .controlplane.workbench import GovernanceError, Workbench

    from .adapters import view as view_adapter
    from .sources import events, state
    from .views.service import ViewService
    from .views.store import ViewStore

    wb = Workbench(Store(":memory:"))
    views = ViewService(wb, ViewStore(":memory:"))
    view_adapter.STORE = views.store
    print(f"Catalogue v{wb.current_version()} seeded. Draft change requests:")
    for c in wb.changes():
        print(f"  #{c['id']}  {c['title']}  (needs: {', '.join(c['required_roles'])})")

    print("\n1. Impact of deprecating Hospital A's amlodipine formulary code (#2)")
    imp = await wb.impact(2)
    _print_diff(f"hides {imp['totals']['hides']}, raises {imp['totals']['raises']}, other {imp['totals']['visible']}", imp["people"])
    wb.reject(2, "cso", "Would hide a stopped-elsewhere discrepancy for a current patient. Map the new code first.")
    print("  -> rejected by the clinical safety officer")

    print("\n2. Onboard the archived previous-practice system (#4)")
    await wb.impact(4)
    for who, text in (("cso", "Read v2 map reviewed."), ("analyst", "Looks fine to me.")):
        try:
            wb.approve(4, who, text)
        except GovernanceError as e:
            print(f"  blocked: {e}")
    conf = await wb.conformance(4, "gp-legacy")
    print(f"  conformance: {conf['overall']}")
    for ch in conf["checks"]:
        print(f"    {ch['id']} {ch['status']:<5} {ch['name']}: {ch['detail']}")
    try:
        wb.approve(4, "analyst", "Looks fine to me, approving.")
    except GovernanceError as e:
        print(f"  blocked: {e}")
    c = wb.approve(4, "cso", "Read v2 map reviewed against source terms; historic-only authority accepted.")
    print(f"  -> applied as catalogue v{c['applied_version']}")
    _print_diff("record changes", c["impact"]["people"])

    print("\n3. Confirm Sam Patel's pharmacy match (#3)")
    await wb.impact(3)
    c = wb.approve(3, "steward", "Verified by phone: DOB and address match.")
    print(f"  -> applied as catalogue v{c['applied_version']}")
    _print_diff("record changes", c["impact"]["people"])

    print("\n4. Replay Jane Smith's record under v1 and compare with now")
    from .scenario import record_request

    rec = await wb.assemble(record_request("NID-DEMO-1001", deadline_seconds=1.0), version=1)
    rp = await wb.replay(rec.request_id)
    print(f"  catalogue v{rp['original_version']} -> v{rp['compared_version']}: {len(rp['catalogue_changes'])} catalogue changes, "
          f"{len(rp['record_changes']['changes'])} record changes")

    print("\n5. Register the materialised summary view (#5), then build it")
    cid = next(c["id"] for c in wb.changes() if "summary-view" in c["title"])
    await wb.impact(cid)
    conf = await wb.conformance(cid, "summary-view")
    print(f"  conformance before first build: {conf['overall']} (" + ", ".join(f"{c['id']} {c['status']}" for c in conf["checks"]) + ")")
    c = wb.approve(cid, "cso", "Sponsor, use case and review date confirmed; derived, not authoritative.")
    print(f"  -> applied as catalogue v{c['applied_version']}")
    b = await views.build("onboarding")
    print(f"  built for {b['subjects']} people under {b['catalogue_version']}; lineage: " + ", ".join(f"{x['id']} {x['status']}" for x in b["checks"]))

    print("\n6. Three ways to serve Jane Smith's record, with the GP offline")
    state.OFFLINE.add("gp")
    m = await views.compare_modes("NID-DEMO-1001", deadline=1.0)
    state.OFFLINE.clear()
    for mode in ("federated", "hybrid", "materialised"):
        x = m[mode]
        print(f"  {mode:<13} {x['elapsed_ms']:>5} ms  gp={x['sources'].get('gp'):<12} imaging={x['sources'].get('imaging'):<12} "
              f"needs review {len(x['needs_review'])}  documents {x['documents']}")

    print("\n7. New clinical activity after the build: what the view does not know")
    for ev in ("gp-adds-bisoprolol", "pharmacy-dispenses-ramipril"):
        events.apply(ev)
        print(f"  event: {events.EVENTS[ev]['label']}")
    cmp = await views.compare_live("NID-DEMO-1001")
    for ch in cmp["diff"]["changes"]:
        print(f"    {ch['safety']:<18} {ch['kind']:<14} {ch['item']:<12} {ch['detail'][:90]}")

    print("\n8. Population query from the view")
    pop = views.population("medications-needing-review")
    print(f"  {pop['label']}: {len(pop['rows'])} rows in {pop['elapsed_ms']} ms (as of {pop['as_of_oldest_build'][:19]})")
    for r in pop["rows"]:
        print(f"    {r['name']:<11} {r['entry']:<12} {', '.join(r['flags'])}")

    print("\n9. A catalogue change makes the view due for rebuild")
    cid = next(c["id"] for c in wb.changes() if "vital-signs" in c["title"])
    imp = await wb.impact(cid)
    print(f"  impact note: {imp.get('materialised')}")
    wb.approve(cid, "cso", "Device readings excluded at source; approved.")
    f = views.status()["entries"][0]["freshness"]
    print(f"  view entry built under {f['built_under']}, catalogue now {f['current_catalogue']}: rebuild required = {f['rebuild_required']}")
    events.reset()
    return 0


def _record(args: argparse.Namespace) -> int:
    from .adapters import view as view_adapter
    from .controlplane.store import Store
    from .controlplane.workbench import Workbench
    from .scenario import record_request
    from .views.service import ViewService
    from .views.store import ViewStore

    wb = Workbench(Store(_store_path()))
    views = ViewService(wb, ViewStore(os.environ.get("FHR_VIEW_STORE", ".fhr/views.db")))
    view_adapter.STORE = views.store
    rec = asyncio.run(views.record(record_request(args.subject, deadline_seconds=args.deadline, mode=args.mode), version=args.version))
    print(f"{rec.demographics.name} (synthetic)  catalogue {rec.catalogue_version}  mode {rec.mode}  {rec.overall_status.upper()}  {rec.elapsed_ms} ms")
    for s in rec.sources:
        print(f"  {s.source:<11} {s.status.value:<21} {s.items_returned:>3}  {s.detail}")
    for sec in rec.sections:
        print(f"\n{sec.label.upper()}" + (f"  ({sec.summary})" if sec.summary else ""))
        for e in sec.entries:
            print(f"  {e.display:<34} {e.status or '':<10} {e.reconciliation:<14} {', '.join(e.sources)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fhr", description="Hybrid federated patient record: federation, governed materialised views and the control-plane workbench")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    r = sub.add_parser("record")
    r.add_argument("--subject", default="NID-DEMO-1001")
    r.add_argument("--version", type=int)
    r.add_argument("--deadline", type=float, default=3.0)
    r.add_argument("--mode", default="federated", choices=["federated", "materialised", "hybrid"])
    sub.add_parser("demo")
    sub.add_parser("reset")
    args = p.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("fhr.api:app", host=args.host, port=args.port)
        return 0
    if args.cmd == "reset":
        for path in (Path(_store_path()), Path(os.environ.get("FHR_VIEW_STORE", ".fhr/views.db"))):
            if path.exists():
                path.unlink()
                print(f"Removed {path}")
        print("Stores will be re-seeded on next start.")
        return 0
    if args.cmd == "demo":
        return asyncio.run(_demo())
    return _record(args)


if __name__ == "__main__":
    sys.exit(main())
