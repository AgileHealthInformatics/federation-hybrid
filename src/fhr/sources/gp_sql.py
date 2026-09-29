"""Current GP practice system behind the governed SQL view contract (SNOMED CT, dm+d)."""

from .governed_sql import make_service

query_service = make_service("gp", "data/gp.json", ("direct-care", "secondary-use"), 0.06)
