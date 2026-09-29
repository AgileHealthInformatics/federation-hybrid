"""Two-stage policy: broker pre-filter, and a signed request context that each
source verifies before applying its own rules. The broker never overrides a
source decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from .catalogue import Catalogue

# Demonstrator-only key. A real deployment would use token exchange (e.g.
# OAuth 2.0 token exchange with SMART-style scopes) and managed keys.
_DEMO_KEY = b"fhr-demonstrator-not-a-secret"


@dataclass(frozen=True)
class PreFilterDecision:
    source: str
    allowed: bool
    reason: str


def pre_filter(catalogue: Catalogue, purpose: str, source_id: str) -> PreFilterDecision:
    accepted = catalogue.sources[source_id]["capability"]["purposes_accepted"]
    if purpose not in accepted:
        return PreFilterDecision(source_id, False, f"Purpose '{purpose}' not accepted (accepts: {', '.join(accepted)})")
    return PreFilterDecision(source_id, True, "Purpose accepted")


def request_context(request_id: str, requester: dict[str, Any], purpose: str, break_glass: bool) -> dict[str, Any]:
    body = {
        "request_id": request_id,
        "requester": requester,
        "purpose": purpose,
        "legal_basis": f"demo: {purpose}",
        "break_glass": break_glass,
    }
    payload = json.dumps(body, sort_keys=True).encode()
    body["signature"] = hmac.new(_DEMO_KEY, payload, hashlib.sha256).hexdigest()
    return body


def verify_context(ctx: dict[str, Any]) -> bool:
    body = {k: v for k, v in ctx.items() if k != "signature"}
    payload = json.dumps(body, sort_keys=True).encode()
    expected = hmac.new(_DEMO_KEY, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, ctx.get("signature", ""))
