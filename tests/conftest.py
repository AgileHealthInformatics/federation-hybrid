from __future__ import annotations

import asyncio
import os

os.environ.setdefault("FHR_STORE", ":memory:")
os.environ.setdefault("FHR_VIEW_STORE", ":memory:")
os.environ.setdefault("FHR_IMAGING_DELAY", "0.1")  # let the slow imaging mock answer, to keep the suite quick

import pytest  # noqa: E402

from fhr.controlplane.store import Store  # noqa: E402
from fhr.controlplane.workbench import Workbench  # noqa: E402
from fhr.sources import state  # noqa: E402


@pytest.fixture
def wb() -> Workbench:
    """A fresh control plane with the seeded demo change requests."""
    return Workbench(Store(":memory:"))


@pytest.fixture
def views(wb):
    from fhr.adapters import view as view_adapter
    from fhr.views.service import ViewService
    from fhr.views.store import ViewStore

    svc = ViewService(wb, ViewStore(":memory:"))
    view_adapter.STORE = svc.store
    return svc


@pytest.fixture(autouse=True)
def no_events():
    from fhr.sources import events

    events.reset()
    yield
    events.reset()


def register_view(wb, views, build: bool = True):
    cid = cr_by_title(wb, "summary-view")
    run(wb.impact(cid))
    run(wb.conformance(cid, "summary-view"))
    wb.approve(cid, "cso", "Sponsor, use case and review date confirmed.")
    if build:
        run(views.build("test"))


@pytest.fixture(autouse=True)
def no_outages():
    state.OFFLINE.clear()
    yield
    state.OFFLINE.clear()


def run(coro):
    return asyncio.run(coro)


def cr_by_title(wb: Workbench, fragment: str) -> int:
    return next(c["id"] for c in wb.changes() if fragment in c["title"])


def changes_for(impact_or_diff, name: str | None = None):
    people = impact_or_diff["people"] if "people" in impact_or_diff else [impact_or_diff]
    return [c for p in people if name is None or p["name"] == name for c in p["changes"]]
