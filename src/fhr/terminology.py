"""A small terminology service over the fragment in catalogue/terminology.json.

Provides display lookup, subsumption (is-a) within the fragment, concept-map
translation with equivalence grades, and medicine / analyte lookups. In a real
deployment these calls would go to a terminology server; the interface is the
point, not the fragment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .data import load_json
from .models import Grade, TransformStep

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
DMD = "urn:demo:dmd"
VTM = "urn:demo:dmd-vtm"


@dataclass
class Resolution:
    system: str
    code: str
    display: str
    grade: Grade = Grade.exact
    steps: list[TransformStep] = field(default_factory=list)


class Terminology:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.version: str = data["version"]
        self.concepts: dict[str, dict[str, dict]] = data["concepts"]
        self.analytes: dict[str, dict] = data["analytes"]
        self.medicines: dict[str, dict] = data["medicines"]
        self.vtms: dict[str, str] = data["vtms"]
        self.maps = {m["source"]: m for m in data["concept_maps"]}
        self.system_names: dict[str, str] = data["systems"]

    def map_for(self, map_id: str) -> dict | None:
        return next((m for m in self.data["concept_maps"] if m["id"] == map_id), None)

    @classmethod
    def load(cls, path: str | None = None) -> Terminology:
        return cls(load_json(path or "catalogue/terminology.json", resolve=False))

    # -- lookups --------------------------------------------------------
    def display(self, system: str, code: str, fallback: str | None = None) -> str:
        return self.concepts.get(system, {}).get(code, {}).get("display") or fallback or code

    def ancestors(self, code: str) -> set[str]:
        out: set[str] = set()
        stack = list(self.concepts.get(SNOMED, {}).get(code, {}).get("parents", []))
        while stack:
            c = stack.pop()
            if c not in out:
                out.add(c)
                stack.extend(self.concepts.get(SNOMED, {}).get(c, {}).get("parents", []))
        return out

    def depth(self, code: str) -> int:
        return len(self.ancestors(code))

    def related(self, a: str | None, b: str | None) -> bool:
        """Same concept, or one subsumes the other (within the fragment)."""
        if not a or not b:
            return False
        return a == b or a in self.ancestors(b) or b in self.ancestors(a)

    def relation(self, a: str, b: str) -> str:
        if a == b:
            return "same"
        if a in self.ancestors(b):
            return "broader"
        if b in self.ancestors(a):
            return "narrower"
        return "unrelated"

    def is_negation(self, code: str | None) -> bool:
        return bool(code and self.concepts.get(SNOMED, {}).get(code, {}).get("negation"))

    def medicine(self, vmp: str) -> dict | None:
        return self.medicines.get(vmp)

    def analyte_for(self, loinc: str) -> str | None:
        return self.concepts.get(LOINC, {}).get(loinc, {}).get("analyte")

    # -- translation ----------------------------------------------------
    def resolve(self, system: str | None, code: str | None, display: str | None = None, include_proposed: bool = False) -> Resolution | None:
        """Translate a native code to the federation's reading of it.

        Codes in a system with a concept map are translated through it; only
        approved maps and approved elements are used unless include_proposed
        (the conformance harness uses that to measure candidate coverage).
        Returns None when there is no usable element: semantics unresolved.
        """
        if not system or not code:
            return None
        cmap = self.maps.get(system)
        if cmap is None:
            return Resolution(system, code, self.display(system, code, display))
        ok = {"approved", "proposed", "reviewed"} if include_proposed else {"approved"}
        if cmap.get("lifecycle") not in ok:
            return None
        el = next((e for e in cmap["elements"] if e["code"] == code and e.get("lifecycle", "approved") in ok), None)
        if el is None:
            return None
        target_display = self.display(cmap["target"], el["target"])
        if cmap["target"] == DMD:
            target_display = self.medicines.get(el["target"], {}).get("display", target_display)
        step = TransformStep(
            kind="concept-map",
            id=cmap["id"],
            version=cmap["version"],
            detail=f"{self.system_names.get(system, system)} {code} -> {self.system_names.get(cmap['target'], cmap['target'])} {el['target']} ({el['grade']})",
        )
        return Resolution(cmap["target"], el["target"], target_display, Grade(el["grade"]), [step])
