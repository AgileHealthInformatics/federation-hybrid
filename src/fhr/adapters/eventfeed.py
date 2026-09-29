"""Event-feed adapter (community pharmacy dispensing and vaccinations)."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from ..data import parse_dt
from ..sources import pharmacy_feed
from .base import Adapter, AdapterResponse, RawStatement, SubRequest

BASE = "https://pharmacy.demo/feed/events"


class EventFeedAdapter(Adapter):
    interface = "event-feed"

    def _urls(self, sub: SubRequest) -> list[tuple[str, str]]:
        out = []
        if "dispense" in sub.classes:
            out.append(("dispense", f"{BASE}?{urlencode({'patient': sub.local_id, 'type': 'dispense', 'since': sub.window_from.isoformat()})}"))
        if "immunization" in sub.classes:
            out.append(("immunization", f"{BASE}?{urlencode({'patient': sub.local_id, 'type': 'vaccination'})}"))
        return out

    def plan(self, sub: SubRequest) -> list[str]:
        return [f"GET {u}" for _, u in self._urls(sub)]

    async def execute(self, sub: SubRequest) -> AdapterResponse:
        results = await asyncio.gather(*(pharmacy_feed.events(u, sub.context) for _, u in self._urls(sub)))
        statements = []
        for resp in results:
            for e in resp["events"]:
                at = parse_dt(e["at"])
                p = e["product"]
                if e["type"] == "dispense":
                    statements.append(RawStatement("dispense", e["event_id"], p["system"], p["code"], p["display"], "completed", "confirmed",
                                                   asserted=at, effective_start=at, role="dispense", native=e,
                                                   extra={"quantity": e["quantity"], "prescriber": e.get("prescriber")}))
                else:
                    statements.append(RawStatement("immunization", e["event_id"], p["system"], p["code"], p["display"], "completed", "confirmed",
                                                   asserted=at, effective_start=at, native=e,
                                                   extra={"performer": "Community pharmacy", "batch": e.get("batch"), "site": e.get("site")}))
        return AdapterResponse(statements, 0, "permit", self.plan(sub))
