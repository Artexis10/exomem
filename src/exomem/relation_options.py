"""The specific relations that fit an edge between two pages.

One producer for every surface that offers a specific relation in place of a
generic one: the pre-commit barrier today, the post-commit advisory and the
relation generators next. A relation fits when it is active, is not the
generic relation, declares endpoints (its own or its parent's), and both
pages are named by them. A relation with no endpoints is never offered, since
nothing says which pages it joins. The order is the registry's own, so the
same edge always gets the same options.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import relation_endpoints, relation_registry

#: The most options one refusal or advisory carries.
MAX_OPTIONS = 4


def fitting_options(
    registry: relation_registry.RelationRegistry,
    subject: relation_endpoints.PageKind,
    target: relation_endpoints.PageKind,
    types: Any,
    *,
    accepts: Callable[[str], bool] | None = None,
    limit: int = MAX_OPTIONS,
) -> list[dict[str, Any]]:
    """Up to `limit` relations that join `subject` to `target`, in registry order.

    `accepts` lets a caller drop a relation its scope does not allow for the page
    (a project or note-type scope); the producer itself reads only the registry.
    """
    options: list[dict[str, Any]] = []
    for definition in (*registry.core.values(), *registry.extensions.values()):
        if definition.status != "active" or definition.generic:
            continue
        endpoints = registry.endpoints_of(definition.key)
        if endpoints is None or not _joins(endpoints, definition.direction, subject, target, types):
            continue
        if accepts is not None and not accepts(definition.key):
            continue
        options.append(
            {
                "key": definition.key,
                "label": relation_registry.display_label(registry, definition.key),
                "description": definition.description,
                "parent": definition.parent,
            }
        )
        if len(options) == limit:
            break
    return options


def _joins(
    endpoints: relation_endpoints.Endpoints,
    direction: str,
    subject: relation_endpoints.PageKind,
    target: relation_endpoints.PageKind,
    types: Any,
) -> bool:
    def named(tokens: tuple[str, ...], kind: relation_endpoints.PageKind) -> bool:
        # An undecided answer (None) never fits: an option must be sure.
        return relation_endpoints.fits(tokens, kind, types) is True

    if named(endpoints.subject, subject) and named(endpoints.object, target):
        return True
    # A symmetric relation reads the same either way round.
    return direction == "symmetric" and named(endpoints.subject, target) and named(
        endpoints.object, subject
    )
