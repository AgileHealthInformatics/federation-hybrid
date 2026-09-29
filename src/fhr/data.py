"""Loading of catalogue and synthetic source data.

Source data files write dates as relative tokens so the demonstrator always
looks current: ``@-62`` is 62 days before start-up at 09:00 UTC and
``@-62T14`` is the same day at 14:00.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ROOT = Path(os.environ.get("FHR_ROOT") or Path(__file__).resolve().parents[2])

_TOKEN = re.compile(r"^@-(\d+)(?:T(\d{1,2}))?$")


def days_ago(days: int, hour: int = 9) -> datetime:
    return (NOW - timedelta(days=days)).replace(hour=hour, minute=0, second=0)


def resolve_dates(obj: Any) -> Any:
    """Replace relative date tokens with ISO 8601 strings, recursively."""
    if isinstance(obj, str):
        m = _TOKEN.match(obj)
        if m:
            return days_ago(int(m.group(1)), int(m.group(2) or 9)).isoformat()
        return obj
    if isinstance(obj, list):
        return [resolve_dates(x) for x in obj]
    if isinstance(obj, dict):
        return {k: resolve_dates(v) for k, v in obj.items()}
    return obj


def load_json(relative: str, resolve: bool = True) -> Any:
    data = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    return resolve_dates(data) if resolve else data


def parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
