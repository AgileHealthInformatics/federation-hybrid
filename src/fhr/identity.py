"""Identity and location index.

Tells the broker where to ask, never what the answer is. Holds local
identifiers with match method and confidence, plus demographics from the
demographics authority. No clinical content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .data import load_json


@dataclass(frozen=True)
class IdentityLink:
    source: str
    local_id: str
    method: str
    confidence: float


class IdentityIndex:
    def __init__(self, data: dict[str, Any]):
        self._persons = {p["person_id"]: p for p in data["persons"]}

    @classmethod
    def load(cls, path: str | None = None) -> IdentityIndex:
        return cls(load_json(path or "catalogue/identity_index.json", resolve=False))

    def known(self, person_id: str) -> bool:
        return person_id in self._persons

    def persons(self) -> list[dict[str, str]]:
        return [
            {"person_id": k, "name": v["demographics"]["name"], "birth_date": v["demographics"]["birth_date"]}
            for k, v in self._persons.items()
        ]

    def demographics(self, person_id: str, as_at: datetime) -> dict[str, Any]:
        d = dict(self._persons[person_id]["demographics"])
        born = date.fromisoformat(d["birth_date"])
        ref = as_at.date()
        d["age"] = ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))
        return d

    def resolve(self, person_id: str) -> list[IdentityLink]:
        """Usable links. Links a steward has rejected are never used."""
        person = self._persons.get(person_id, {})
        return [
            IdentityLink(lk["source"], lk["local_id"], lk["method"], float(lk["confidence"]))
            for lk in person.get("links", [])
            if lk.get("status") != "rejected"
        ]

    def links(self) -> list[dict[str, Any]]:
        """All links with their person, including rejected ones, for stewardship."""
        return [{"person_id": pid, "name": p["demographics"]["name"], **lk} for pid, p in self._persons.items() for lk in p.get("links", [])]

    def person_ids(self) -> list[str]:
        return list(self._persons)
