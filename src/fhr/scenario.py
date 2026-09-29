"""Ready-made requests for the demonstrator."""

from __future__ import annotations

from .models import RecordRequest, Requester

CLINICIAN = Requester(id="clin-0042", role="general-practitioner", organisation="Out-of-hours service (synthetic)")


def record_request(subject: str = "NID-DEMO-1001", **overrides) -> RecordRequest:
    base = {"subject": subject, "purpose": "direct-care", "requester": CLINICIAN, "deadline_seconds": 3.0}
    base.update(overrides)
    return RecordRequest(**base)
