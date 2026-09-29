"""Normalisation: read each native statement through the catalogue and
terminology, without discarding what the source actually said.

A statement is read as a concept (with an equivalence grade and the mappings
used), rendered into a preferred unit only if asked (native kept), and tagged
with identity confidence and quality. Statements whose code has no approved
mapping are not guessed at: they are counted as unmapped.
"""

from __future__ import annotations

from datetime import datetime

from .adapters import RawStatement
from .catalogue import Catalogue
from .identity import IdentityLink
from .models import ClinicalStatement, Coding, Grade, TransformStep
from .terminology import LOINC, VTM, Terminology

SUPPRESSED_VERIFICATION = {"entered-in-error", "refuted"}


def normalise(
    raw: RawStatement,
    n: int,
    source_id: str,
    link: IdentityLink,
    cat: Catalogue,
    term: Terminology,
    render_units: bool,
    full_provenance: bool,
    retrieved_at: datetime,
) -> ClinicalStatement | None:
    src = cat.sources[source_id]
    exp = cat.exposure(source_id, raw.record_class)
    section = cat.section_for_class[raw.record_class]
    chain = [
        TransformStep(
            kind="mapping",
            id=exp["mapping"]["id"],
            version=exp["mapping"]["version"],
            detail=f"{src['short']} {exp['native']} read as {section} ({raw.record_class})",
        )
    ]
    concept: Coding | None = None
    key: str | None = None
    grade = Grade.exact
    native_code: Coding | None = None
    rendered_value = None
    rendered_unit = None
    daily: float | None = None
    flags: list[str] = []
    notes: list[str] = []

    if raw.code:
        native_code = Coding(system=raw.code_system, code=raw.code, display=raw.display)
        res = term.resolve(raw.code_system, raw.code, raw.display)
        if res is None:
            return None
        chain += res.steps
        grade = res.grade
        if raw.record_class in ("medication", "dispense"):
            med = term.medicine(res.code)
            if med is None:
                return None
            concept = Coding(system=VTM, code=med["vtm"], display=term.vtms[med["vtm"]])
            key = f"vtm:{med['vtm']}"
            if raw.dose_qty and raw.freq_per_day:
                daily = round(float(raw.dose_qty) * float(raw.freq_per_day) * float(med["strength_mg"]), 3)
        elif raw.record_class in ("result", "vital-sign"):
            analyte = term.analyte_for(res.code) or res.code
            a = term.analytes.get(analyte, {})
            concept = Coding(system=LOINC, code=a.get("code", res.code), display=a.get("label", res.display))
            key = f"analyte:{analyte}"
            target = a.get("unit")
            if render_units:
                if target and raw.unit != target:
                    t = cat.transformation(analyte, raw.unit, target)
                    if t:
                        rendered_value = round(float(raw.value) * t["factor"] + t.get("offset", 0), 1)
                        rendered_unit = target
                        chain.append(TransformStep(kind="transformation", id=t["id"], version=t["version"], detail=t["description"]))
                        flags.append("derived")
                    else:
                        rendered_value, rendered_unit = raw.value, raw.unit
                        notes.append("No approved transformation to the preferred unit; shown as recorded")
                else:
                    rendered_value, rendered_unit = raw.value, raw.unit
            if a.get("note"):
                notes.append(a["note"])
        else:
            concept = Coding(system=res.system, code=res.code, display=res.display)
            key = f"{res.system}|{res.code}"

    if raw.amended:
        flags.append("amended")
    if link.method == "probabilistic":
        flags.append("probabilistic-identity")
    if raw.extra.get("note"):
        notes.append(str(raw.extra["note"]))

    return ClinicalStatement(
        id=f"{source_id}:{raw.record_class}:{n}",
        section=section,
        record_class=raw.record_class,
        source=source_id,
        source_label=src["label"],
        native_code=native_code,
        concept=concept,
        concept_key=key,
        grade=grade,
        status=(raw.status or "").lower() or None,
        verification=raw.verification,
        asserted=raw.asserted,
        effective_start=raw.effective_start,
        effective_end=raw.effective_end,
        role=raw.role,
        value=raw.value,
        unit=raw.unit,
        rendered_value=rendered_value,
        rendered_unit=rendered_unit,
        daily_amount=daily,
        daily_unit="mg" if daily is not None else None,
        dose_text=raw.dose_text,
        reaction=raw.reaction,
        severity=raw.severity,
        title=raw.title,
        text=raw.text,
        accession=raw.accession,
        version=raw.version,
        record_id=raw.record_id,
        authority=src["authority"],
        match_method=link.method,
        match_confidence=link.confidence,
        transformation_chain=chain,
        quality=list(src.get("quality", [])),
        native=raw.native if full_provenance else {"record_id": raw.record_id},
        extra={k: v for k, v in raw.extra.items() if k != "note"},
        flags=flags,
        notes=notes,
        retrieved_at=retrieved_at,
    )
