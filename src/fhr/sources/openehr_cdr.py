"""Hospital A: mock openEHR Clinical Data Repository.

Receives genuine AQL text (kept for audit and display) with bound parameters,
and evaluates the parameters against compositions shaped as simplified openEHR
content. A real CDR would parse and execute the AQL.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..data import load_json, parse_dt
from .state import admit

EHRS: dict[str, list[dict[str, Any]]] = load_json("data/hospital_a.json")["ehrs"]


async def execute_aql(aql: str, params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    admit("hospital-a", ctx, ("direct-care", "secondary-use"))
    await asyncio.sleep(0.05)
    since = parse_dt(params.get("from"))
    rows = []
    for c in EHRS.get(params["ehrId"], []):
        if c["archetype"] != params["archetype"]:
            continue
        if since and parse_dt(c["committed"]) < since:
            continue
        rows.append(c)
    return {"q": aql, "rows": rows, "withheld": 0, "decision": "permit"}
