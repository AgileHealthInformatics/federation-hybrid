"""Broker-side audit: plans, catalogue version and source decisions, correlated
by request id with source-side audit. Holds no clinical values."""

from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

_LOG: deque[dict[str, Any]] = deque(maxlen=500)


def record(event: dict[str, Any]) -> None:
    event = {"at": datetime.now(timezone.utc).isoformat(), **event}
    _LOG.append(event)
    path = os.environ.get("FHR_AUDIT_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")


def recent(limit: int = 50) -> list[dict[str, Any]]:
    return list(_LOG)[-limit:][::-1]
