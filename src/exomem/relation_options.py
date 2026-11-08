"""The ranked menu of specific relations that can replace a generic one between two pages.

One producer for every surface that offers a specific relation in place of the generic
relation: the pre-commit barrier and the relation generators. A relation is admissible
when it is active, is not the generic relation, the caller's scope allows it, and the
pages fit its endpoints where it has any. A relation with no endpoints of its own or
from its parent joins any two pages, so it is admissible for every pair.

Admissible relations rank by four keys, so the same edge always gets the same menu:

1. a relation whose own endpoints name both pages comes before one that needs a wildcard
   or declares none;
2. a relation the vault already authors between pages of the same note types comes first,
   most used first (`pair_usage`; a surface without a corpus passes no usage);
3. an extension (the vault's own vocabulary) comes before a core relation;
4. the registry's own order.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import relation_endpoints, relation_registry

#: The most options one refusal or generator carries.
MAX_OPTIONS = 8

_NAMES_BOTH_PAGES = 0
_OTHERWISE = 1


def ranked_options(
    registry: relation_registry.RelationRegistry,
    subject: relation_endpoints.PageKind | None,
    target: relation_endpoints.PageKind | None,
    types: Any,
    *,
    accepts: Callable[[str], bool] | None = None,
    usage: Mapping[str, int] | None = None,
    limit: int = MAX_OPTIONS,
) -> list[dict[str, Any]]:
    """Up to `limit` admissible relations, best first.

    A page kind of None (the note type cannot say what the page is, or the caller must not
    use it) never fits a declared endpoint, so a relation with endpoints is left out.
    `accepts` lets a caller drop a relation its scope does not allow for the page; `usage`
    counts, by key, how often the vault authors each relation between pages like these.
    """
    usage = usage or {}
    fits: list[tuple[int, int, int, int, relation_registry.RelationDefinition]] = []
    order = 0
    for rank, definitions in ((0, registry.extensions), (1, registry.core)):
        for definition in definitions.values():
            order += 1
            if definition.status != "active" or definition.generic:
                continue
            if accepts is not None and not accepts(definition.key):
                continue
            endpoints = registry.endpoints_of(definition.key)
            tier = _OTHERWISE
            if endpoints is not None:
                if subject is None or target is None:
                    continue
                specificity = _specificity(endpoints, definition.direction, subject, target, types)
                if specificity is None:
                    continue
                # Only endpoints the relation declares itself name the pages; a parent's do not.
                tier = specificity if definition.endpoints is not None else _OTHERWISE
            fits.append((tier, -usage.get(definition.key, 0), rank, order, definition))
    fits.sort(key=lambda fit: fit[:4])
    return [
        {
            "key": definition.key,
            "label": relation_registry.display_label(registry, definition.key),
            "description": definition.description,
            "parent": definition.parent,
        }
        for *_, definition in fits[:limit]
    ]


def page_class(source_kind: str | None, page_type: str | None) -> tuple[str, str] | None:
    """What one end of an authored fact is, for counting: its unit kind, else its page's note type.

    `file` and `unresolved` are the kinds a fact reports when it has no unit; a page with no
    note type, and an end the fact cannot resolve, have no class.
    """
    if source_kind not in (None, "file", "unresolved"):
        return ("unit", source_kind)  # type: ignore[return-value]
    return ("page", page_type) if page_type else None


def pair_usage(
    facts: Iterable[Any],
    registry: relation_registry.RelationRegistry,
    subject: tuple[str, str] | None,
    target: tuple[str, str] | None,
    visible: Callable[[str], bool],
) -> Counter[str]:
    """How often the vault authors each specific relation from `subject` pages to `target` pages.

    Counts authored relation facts other than the generic relation and the plain link. With no
    `target` class every authored target counts, so a page the writer must not learn about
    never changes the answer: a fact counts only when both of its pages are visible to the
    writer, and the target of the edge being refused is not read at all.
    """
    counts: Counter[str] = Counter()
    if subject is None:
        return counts
    for fact in facts:
        key = fact.canonical_relation
        if (
            not fact.authored
            or fact.origin not in relation_registry.AUTHORED_ORIGINS
            or not key
            or registry.is_generic(key)
            or key == registry.link_key
            or fact.target_status != "resolved"
            or page_class(fact.source_kind, fact.authored_page_type) != subject
        ):
            continue
        if target is not None and page_class(fact.target_kind, fact.target_page_type) != target:
            continue
        if visible(fact.authored_path) and visible(fact.logical_target_path):
            counts[key] += 1
    return counts


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
            specificity = _OTHERWISE
    return specificity
