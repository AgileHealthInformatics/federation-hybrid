"""Versioned control-plane store (SQLite).

Every version holds a complete snapshot of the three control-plane documents
(catalogue, terminology, identity index). Versions are immutable and never
deleted; restoring an old version creates a new one. Change requests,
governance events and the record-request log live alongside.

The request log holds request parameters and the catalogue version used,
never clinical values (P12), so a past record can be re-assembled.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..data import load_json

DOCS = ("catalogue", "terminology", "identity")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
  version INTEGER PRIMARY KEY, created_at TEXT NOT NULL, summary TEXT NOT NULL,
  change_id INTEGER, author TEXT, approvers TEXT, parent INTEGER,
  catalogue TEXT NOT NULL, terminology TEXT NOT NULL, identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, rationale TEXT, author TEXT NOT NULL, created_at TEXT NOT NULL,
  ops TEXT NOT NULL, status TEXT NOT NULL, required_roles TEXT NOT NULL, approvals TEXT NOT NULL DEFAULT '[]',
  base_version INTEGER, impact TEXT, conformance TEXT NOT NULL DEFAULT '{}', applied_version INTEGER,
  decided_at TEXT, decision_note TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  change_id INTEGER, version INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS record_requests (
  request_id TEXT PRIMARY KEY, at TEXT NOT NULL, subject TEXT NOT NULL, version INTEGER NOT NULL,
  params TEXT NOT NULL, overall TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.con.executescript(_SCHEMA)
            self.con.commit()

    # -- versions ---------------------------------------------------------
    def is_empty(self) -> bool:
        return self.con.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0

    def seed_from_files(self) -> int:
        docs = {
            "catalogue": load_json("catalogue/catalogue.json", resolve=False),
            "terminology": load_json("catalogue/terminology.json", resolve=False),
            "identity": load_json("catalogue/identity_index.json", resolve=False),
        }
        return self.commit(docs, "Initial catalogue seeded from catalogue/*.json", author="system")

    def current_version(self) -> int:
        return self.con.execute("SELECT MAX(version) FROM versions").fetchone()[0]

    def docs(self, version: int | None = None) -> dict[str, Any]:
        v = version or self.current_version()
        row = self.con.execute("SELECT * FROM versions WHERE version = ?", (v,)).fetchone()
        if row is None:
            raise KeyError(f"No catalogue version {v}")
        return {d: json.loads(row[d]) for d in DOCS}

    def commit(self, docs: dict[str, Any], summary: str, author: str, approvers: list[str] | None = None,
               change_id: int | None = None) -> int:
        with self.lock:
            parent = self.current_version()
            version = (parent or 0) + 1
            docs = json.loads(json.dumps(docs))
            docs["catalogue"]["catalogue_version"] = f"v{version}"
            self.con.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (version, now(), summary, change_id, author, json.dumps(approvers or []), parent,
                 json.dumps(docs["catalogue"]), json.dumps(docs["terminology"]), json.dumps(docs["identity"])),
            )
            self.con.commit()
            return version

    def versions(self) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT version, created_at, summary, change_id, author, approvers, parent FROM versions ORDER BY version DESC"
        ).fetchall()
        return [{**dict(r), "approvers": json.loads(r["approvers"] or "[]")} for r in rows]

    # -- change requests -------------------------------------------------
    def create_change(self, title: str, rationale: str, author: str, ops: list[dict], required_roles: list[str]) -> int:
        with self.lock:
            cur = self.con.execute(
                "INSERT INTO changes (title, rationale, author, created_at, ops, status, required_roles) VALUES (?,?,?,?,?,?,?)",
                (title, rationale, author, now(), json.dumps(ops), "draft", json.dumps(sorted(required_roles))),
            )
            self.con.commit()
            return cur.lastrowid

    def change(self, cid: int) -> dict[str, Any]:
        r = self.con.execute("SELECT * FROM changes WHERE id = ?", (cid,)).fetchone()
        if r is None:
            raise KeyError(f"No change request {cid}")
        d = dict(r)
        for k in ("ops", "required_roles", "approvals", "conformance"):
            d[k] = json.loads(d[k] or ("{}" if k == "conformance" else "[]"))
        d["impact"] = json.loads(d["impact"]) if d["impact"] else None
        return d

    def changes(self) -> list[dict[str, Any]]:
        ids = [r[0] for r in self.con.execute("SELECT id FROM changes ORDER BY id DESC")]
        return [self.change(i) for i in ids]

    def update_change(self, cid: int, **fields: Any) -> None:
        enc = {k: json.dumps(v) if k in ("approvals", "conformance", "impact", "ops", "required_roles") else v for k, v in fields.items()}
        with self.lock:
            self.con.execute(f"UPDATE changes SET {', '.join(f'{k} = ?' for k in enc)} WHERE id = ?", (*enc.values(), cid))
            self.con.commit()

    # -- governance events ---------------------------------------------
    def event(self, actor: str, action: str, change_id: int | None = None, version: int | None = None, detail: str = "") -> None:
        with self.lock:
            self.con.execute("INSERT INTO events (at, actor, action, change_id, version, detail) VALUES (?,?,?,?,?,?)",
                             (now(), actor, action, change_id, version, detail))
            self.con.commit()

    def events(self, change_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q = "SELECT * FROM events" + (" WHERE change_id = ?" if change_id else "") + " ORDER BY id DESC LIMIT ?"
        args = (change_id, limit) if change_id else (limit,)
        return [dict(r) for r in self.con.execute(q, args)]

    # -- record request log --------------------------------------------
    def log_request(self, request_id: str, subject: str, version: int, params: dict[str, Any], overall: str) -> None:
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO record_requests VALUES (?,?,?,?,?,?)",
                             (request_id, now(), subject, version, json.dumps(params, default=str), overall))
            self.con.commit()

    def request(self, request_id: str) -> dict[str, Any]:
        r = self.con.execute("SELECT * FROM record_requests WHERE request_id = ?", (request_id,)).fetchone()
        if r is None:
            raise KeyError(f"No logged request {request_id}")
        return {**dict(r), "params": json.loads(r["params"])}

    def requests(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{**dict(r), "params": json.loads(r["params"])}
                for r in self.con.execute("SELECT * FROM record_requests ORDER BY at DESC LIMIT ?", (limit,))]
