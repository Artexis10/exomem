"""The days on which a served unit's currency changes, for a cache that keeps it until then."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from . import relation_registry, semantic_units, working_set_currency


def unit_provenance(unit: semantic_units.SemanticUnit, *, registry: relation_registry.RelationRegistry | None) -> dict[str, Any]:
    """The authored currency fields of a unit and the targets of its `supersedes` relations, as a served item carries them.

    The language schema fixes the three date fields; the relation families come from the registry.
    """
    return {
        **{key: getattr(unit, key) for key in ("as_of", "until", "check_by") if getattr(unit, key)},
        "supersedes_targets": working_set_currency.relation_targets(
            [relation.to_dict() for relation in unit.relations], registry=registry,
        ),
    }


def boundaries(provenance: Mapping[str, Any], *, today: date) -> list[date]:
    """The days after `today` on which the authored `until` or `check_by` of a unit changes its currency."""
    found: list[date] = []
    for field in ("until", "check_by"):  # Closed typed date fields; each changes currency the following day.
        if value := provenance.get(field):
            authored = date.fromisoformat(value)
            # The last day of the calendar has no following day, so that date never ends.
            if authored < date.max:
                boundary = authored + timedelta(days=1)
                if boundary > today:
                    found.append(boundary)
    return found
