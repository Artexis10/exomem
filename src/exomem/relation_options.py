"""The specific relations that fit an edge between two pages.

One producer for every surface that offers a specific relation in place of a
generic one: the pre-commit barrier today, the post-commit advisory and the
relation generators next. A relation fits when it is active, is not the
generic relation, declares endpoints of its own, and both pages are named by
them. A relation with no endpoints of its own is never offered: nothing says
which pages it joins, and an extension that only inherits its parent's is
already covered by the parent. Fits rank by specificity: those that name both
pages by their own type or role come before those that need `entity:*`; in
each group an extension (the vault's own vocabulary) comes before the core
relations, and the registry's own order breaks the remaining ties, so the same
edge always gets the same options.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import relation_endpoints, relation_registry

#: The most options one refusal or advisory carries.
MAX_OPTIONS = 4

_NAMES_BOTH_PAGES = 0
_NEEDS_A_WILDCARD = 1


def fitting_options(
    registry: relation_registry.RelationRegistry,
    subject: relation_endpoints.PageKind,
    target: relation_endpoints.PageKind,
    types: Any,
    *,
    accepts: Callable[[str], bool] | None = None,
    limit: int = MAX_OPTIONS,
) -> list[dict[str, Any]]:
    """Up to `limit` relations that join `subject` to `target`, most specific first.

    `accepts` lets a caller drop a relation its scope does not allow for the page
    (a project or note-type scope); the producer itself reads only the registry.
    """
    fits: list[tuple[int, int, relation_registry.RelationDefinition]] = []
    for rank, definitions in ((0, registry.extensions), (1, registry.core)):
        for definition in definitions.values():
            if definition.status != "active" or definition.generic:
                continue
            if definition.endpoints is None:
                continue
            specificity = _specificity(definition.endpoints, definition.direction, subject, target, types)
            if specificity is None or (accepts is not None and not accepts(definition.key)):
                continue
            fits.append((specificity, rank, definition))
    # A stable sort: ties keep the registry's order.
    fits.sort(key=lambda fit: fit[:2])
    return [
        {
            "key": definition.key,
            "label": relation_registry.display_label(registry, definition.key),
            "description": definition.description,
            "parent": definition.parent,
        }
        for _, _, definition in fits[:limit]
    ]


def _specificity(
    endpoints: relation_endpoints.Endpoints,
    direction: str,
    subject: relation_endpoints.PageKind,
    target: relation_endpoints.PageKind,
    types: Any,
) -> int | None:
    """How firmly the endpoints name the two pages; None when they do not."""

    def named(tokens: tuple[str, ...], kind: relation_endpoints.PageKind) -> bool:
        # An undecided answer (None) never fits: an option must be sure.
        return relation_endpoints.fits(tokens, kind, types) is True

    # A symmetric relation reads the same either way round.
    orientations = [(subject, target)]
    if direction == "symmetric":
        orientations.append((target, subject))
    specificity: int | None = None
    for first, second in orientations:
        if named(relation_endpoints.without_wildcards(endpoints.subject), first) and named(
            relation_endpoints.without_wildcards(endpoints.object), second
        ):
            return _NAMES_BOTH_PAGES
        if named(endpoints.subject, first) and named(endpoints.object, second):
            specificity = _NEEDS_A_WILDCARD
    return specificity
