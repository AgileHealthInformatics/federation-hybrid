"""Shared source behaviour: simulated outages and refusals."""

from __future__ import annotations

from typing import Any

from ..policy import verify_context

OFFLINE: set[str] = set()


class SourceOffline(ConnectionError):
    """The source cannot be reached (simulated outage)."""


class SourceRefused(Exception):
    """The source refused the request under its own policy."""


def admit(source_id: str, ctx: dict[str, Any], purposes: tuple[str, ...]) -> None:
    """Common gate every mock source applies before answering."""
    if source_id in OFFLINE:
        raise SourceOffline(f"{source_id} unreachable (simulated outage)")
    if not verify_context(ctx):
        raise SourceRefused("Request context signature invalid")
    if ctx["purpose"] not in purposes:
        raise SourceRefused(f"Purpose '{ctx['purpose']}' not accepted by local policy")
