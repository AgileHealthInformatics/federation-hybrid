"""Archived previous-practice system, published through the same governed view
contract but coded in Read v2 and a legacy drug dictionary.

It exists (and holds records) before the federation knows about it. The broker
will not query it until it is described in the catalogue, passes conformance
and is made live through an approved change request.
"""

from .governed_sql import make_service

query_service = make_service("gp-legacy", "data/gp_legacy.json", ("direct-care",), 0.09)
