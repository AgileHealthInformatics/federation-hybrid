"""Community pharmacy: a dispensing and vaccination event feed."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..data import load_json, parse_dt
from .state import admit

EVENTS: list[dict[str, Any]] = load_json("data/pharmacy.json")["events"]


async def events(url: str, ctx: dict[str, Any]) -> dict[str, Any]:
    admit("pharmacy", ctx, ("direct-care",))
    await asyncio.sleep(0.04)
    q = parse_qs(urlsplit(url).query)
    patient = q["patient"][0]
    types = set(q.get("type", ["dispense,vaccination"])[0].split(","))
    since = parse_dt(q["since"][0]) if "since" in q else None
    out = [
        e
        for e in EVENTS
        if e["patient"] == patient and e["type"] in types and (since is None or parse_dt(e["at"]) >= since)
    ]
    return {"events": out, "withheld": 0, "decision": "permit"}
