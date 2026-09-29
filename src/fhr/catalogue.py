"""Metadata catalogue: sources, their capabilities, the record classes each
exposes (with mapping lifecycle), IPS-style sections and transformations.

The broker plans against a specific catalogue version, recorded in every result.
"""

from __future__ import annotations

from typing import Any

from .data import load_json


class Catalogue:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.version: str = data["catalogue_version"]
        self.purposes: list[str] = data["purposes"]
        self.identity_thresholds: dict[str, float] = data["identity_thresholds"]
        self.rules: dict[str, Any] = data["reconciliation"]
        self.sections = {s["id"]: s for s in data["sections"]}
        self.sources = {s["id"]: s for s in data["sources"]}
        self.transformations = list(data.get("transformations", []))
        self.section_for_class = {c: s["id"] for s in data["sections"] for c in s["record_classes"]}

    @classmethod
    def load(cls, path: str | None = None) -> Catalogue:
        return cls(load_json(path or "catalogue/catalogue.json", resolve=False))

    # -- P4: federate metadata before data -------------------------------
    QUERYABLE = ("live", "retiring")

    def is_registered(self, source_id: str) -> bool:
        return source_id in self.sources

    def lifecycle(self, source_id: str) -> str:
        return self.sources[source_id].get("lifecycle", "live")

    def is_derived(self, source_id: str) -> bool:
        """A materialised view registered as a source: governed like one, never federated with its own originals."""
        return bool(self.sources[source_id].get("derived"))

    def views(self) -> list[dict]:
        return [s for s in self.sources.values() if s.get("derived")]

    def is_live(self, source_id: str) -> bool:
        return self.lifecycle(source_id) in self.QUERYABLE

    def exposure(self, source_id: str, record_class: str) -> dict | None:
        for e in self.sources[source_id]["exposes"]:
            if e["record_class"] == record_class:
                return e
        return None

    def classes_for(self, source_id: str, sections: list[str]) -> tuple[list[str], list[str]]:
        """(approved record classes, unresolved record classes) for the requested sections."""
        wanted = {c for s in sections for c in self.sections[s]["record_classes"]}
        approved, unresolved = [], []
        for e in self.sources[source_id]["exposes"]:
            if e["record_class"] not in wanted:
                continue
            if e["mapping"].get("lifecycle") == "approved":
                approved.append(e["record_class"])
            else:
                unresolved.append(e["record_class"])
        return approved, unresolved

    def exposes_any(self, source_id: str, sections: list[str]) -> bool:
        a, u = self.classes_for(source_id, sections)
        return bool(a or u)

    def transformation(self, analyte: str, from_unit: str | None, to_unit: str | None) -> dict | None:
        for t in self.transformations:
            if t.get("analyte") == analyte and t["from_unit"] == from_unit and t["to_unit"] == to_unit:
                return t
        return None

    # -- P9: portable metadata (CDIF-style JSON-LD) -----------------------
    def to_cdif_jsonld(self) -> dict[str, Any]:
        graph = []
        for s in self.sources.values():
            graph.append(
                {
                    "@id": f"urn:fhr:source:{s['id']}",
                    "@type": ["schema:Dataset", "dcat:Dataset"],
                    "schema:name": s["label"],
                    "schema:publisher": {"@type": "schema:Organization", "schema:name": s["owner"]},
                    "schema:version": self.version,
                    "dcat:accessService": {"@type": "dcat:DataService", "dcterms:conformsTo": s["capability"]["interface"]},
                    "schema:conditionsOfAccess": "Purposes accepted: " + ", ".join(s["capability"]["purposes_accepted"]),
                    "schema:variableMeasured": [
                        {
                            "@type": ["schema:PropertyValue", "cdi:InstanceVariable"],
                            "@id": f"urn:fhr:{s['id']}:{e['record_class']}",
                            "schema:name": f"{e['record_class']} ({e['native']})",
                            "schema:propertyID": e.get("code_system"),
                            "fhr:mappingLifecycle": e["mapping"].get("lifecycle"),
                        }
                        for e in s["exposes"]
                    ],
                }
            )
        return {
            "@context": {
                "schema": "https://schema.org/",
                "dcat": "http://www.w3.org/ns/dcat#",
                "dcterms": "http://purl.org/dc/terms/",
                "cdi": "http://ddialliance.org/Specification/DDI-CDI/1.0/RDF/",
                "fhr": "urn:fhr:",
            },
            "@graph": graph,
        }
