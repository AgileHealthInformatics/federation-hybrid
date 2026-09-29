"""Data-plane store for materialised views.

Deliberately separate from the control-plane store: this one holds clinical
content (a projection of the federated record), the control plane holds none.
One current entry per view and subject, plus a log of builds.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
  build_id INTEGER PRIMARY KEY, view_id TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  catalogue_version TEXT, trigger TEXT, subjects INTEGER, checks TEXT
);
CREATE TABLE IF NOT EXISTS entries (
  view_id TEXT NOT NULL, subject TEXT NOT NULL, build_id INTEGER NOT NULL, built_at TEXT NOT NULL,
  catalogue_version TEXT NOT NULL, overall TEXT, record TEXT NOT NULL,
  PRIMARY KEY (view_id, subject)
);
"""


class ViewStore:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.con.executescript(_SCHEMA)
            self.con.commit()

    def start_build(self, view_id: str, catalogue_version: str, trigger: str) -> int:
        with self.lock:
            cur = self.con.execute("INSERT INTO builds (view_id, started_at, catalogue_version, trigger) VALUES (?,?,?,?)",
                                   (view_id, _now(), catalogue_version, trigger))
            self.con.commit()
            return cur.lastrowid

    def finish_build(self, build_id: int, subjects: int, checks: list[dict[str, str]]) -> None:
        with self.lock:
            self.con.execute("UPDATE builds SET finished_at = ?, subjects = ?, checks = ? WHERE build_id = ?",
                             (_now(), subjects, json.dumps(checks), build_id))
            self.con.commit()

    def put(self, view_id: str, subject: str, build_id: int, built_at: datetime, catalogue_version: str, overall: str, record_json: str) -> None:
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?)",
                             (view_id, subject, build_id, built_at.isoformat(), catalogue_version, overall, record_json))
            self.con.commit()

    def get(self, view_id: str, subject: str) -> dict[str, Any] | None:
        r = self.con.execute("SELECT * FROM entries WHERE view_id = ? AND subject = ?", (view_id, subject)).fetchone()
        return dict(r) if r else None

    def all(self, view_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute("SELECT * FROM entries WHERE view_id = ? ORDER BY subject", (view_id,))]

    def summaries(self, view_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute(
            "SELECT subject, build_id, built_at, catalogue_version, overall FROM entries WHERE view_id = ? ORDER BY subject", (view_id,))]

    def builds(self, view_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.con.execute("SELECT * FROM builds WHERE view_id = ? ORDER BY build_id DESC LIMIT ?", (view_id, limit))
        return [{**dict(r), "checks": json.loads(r["checks"] or "[]")} for r in rows]

    def age_by(self, view_id: str, hours: float) -> None:
        """Demo control: pretend time has passed since the last build."""
        with self.lock:
            for r in self.all(view_id):
                t = datetime.fromisoformat(r["built_at"]) - timedelta(hours=hours)
                rec = json.loads(r["record"])
                self.con.execute("UPDATE entries SET built_at = ?, record = ? WHERE view_id = ? AND subject = ?",
                                 (t.isoformat(), json.dumps(_shift(rec, hours)), view_id, r["subject"]))
            self.con.commit()


def _shift(rec: dict[str, Any], hours: float) -> dict[str, Any]:
    """Move the record's own assembly timestamps back with its build time (values are untouched)."""
    for k in ("started_at", "completed_at"):
        rec[k] = (datetime.fromisoformat(rec[k]) - timedelta(hours=hours)).isoformat()
    for st in rec.get("statements", []):
        st["retrieved_at"] = (datetime.fromisoformat(st["retrieved_at"]) - timedelta(hours=hours)).isoformat()
    return rec


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
