"""A governed SQL query service, as a reusable mock.

Publishes the primary-care view contract over an in-memory SQLite database,
accepts only parameterised SELECTs on those views, applies the owner's
restrictions itself and reports how many rows it withheld. Entered-in-error
rows are returned with their flag; handling them is the consumer's job.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from ..data import load_json
from .state import SourceRefused, admit

VIEWS = ("PROBLEMS", "MEDICATIONS", "ALLERGIES", "IMMUNISATIONS", "OBSERVATIONS", "CONSULTATIONS")
_ALLOWED = re.compile(r"^\s*SELECT\s.+\sFROM\s+(" + "|".join(VIEWS) + r")\s+WHERE\s", re.IGNORECASE | re.DOTALL)

Service = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


def make_service(source_id: str, data_file: str, purposes: tuple[str, ...], latency: float) -> Service:
    tables = load_json(data_file)["tables"]
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    id_cols: dict[str, str] = {}
    for name in VIEWS:
        rows = tables.get(name, [])
        if not rows:
            continue
        cols = list(rows[0].keys())
        id_cols[name] = cols[0]
        con.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in cols)})", [tuple(r[c] for c in cols) for r in rows])

    async def query_service(sql: str, params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        admit(source_id, ctx, purposes)
        await asyncio.sleep(latency)
        m = _ALLOWED.match(sql)
        if not m or ";" in sql:
            raise SourceRefused("Query not permitted by governed service: only parameterised SELECTs on published views")
        view = m.group(1).upper()
        if view not in id_cols:  # published in the contract but holding nothing
            return {"rows": [], "withheld": 0, "decision": "permit"}
        id_col = id_cols[view]
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        if rows and id_col not in rows[0]:
            raise SourceRefused(f"Governed service requires {id_col} in the projection")
        restricted = {r[0] for r in con.execute(f"SELECT {id_col} FROM {view} WHERE RESTRICTED = 1")}
        released = [r for r in rows if r[id_col] not in restricted]
        return {
            "rows": released,
            "withheld": len(rows) - len(released),
            "decision": "permit-with-redaction" if len(rows) != len(released) else "permit",
        }

    query_service.con = con  # demo events write here to simulate new clinical activity
    return query_service
