"""Imaging provider: mock DICOMweb QIDO-RS plus report retrieval.

Slow by default (FHR_IMAGING_DELAY seconds, default 5) so the default request
shows a source reported as unavailable rather than silently empty.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..data import load_json, parse_dt
from .state import admit

STUDIES: list[dict[str, Any]] = load_json("data/imaging.json")["studies"]


def delay() -> float:
    try:
        return float(os.environ.get("FHR_IMAGING_DELAY", "5"))
    except ValueError:
        return 5.0


async def qido_with_reports(url: str, ctx: dict[str, Any]) -> dict[str, Any]:
    admit("imaging", ctx, ("direct-care",))
    await asyncio.sleep(delay())
    q = parse_qs(urlsplit(url).query)
    pid = q["PatientID"][0]
    since = parse_dt(q["since"][0]) if "since" in q else None
    studies = [s for s in STUDIES if s["patient_id"] == pid and (since is None or parse_dt(s["study_date"]) >= since)]
    return {"studies": studies, "withheld": 0, "decision": "permit"}
