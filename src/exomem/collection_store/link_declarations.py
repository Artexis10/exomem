"""The author's check of the relation a manifest's link fields declare, run by create and revise.

A link field may name a registry relation. The registry decides which relations a collection
link may carry, through each relation's origins, so no relation key appears in code.
"""

from __future__ import annotations

from pathlib import Path

from .. import relation_registry
from .. import structured_collections as collections

#: The most admissible relations an error lists; the registry holds a handful, an overlay a few more.
_LISTED = 32


def require_relations(root: Path, proposed: collections.CollectionManifest,
                      current: collections.CollectionManifest | None = None) -> None:
    """Refuse a new or changed ``relation`` of ``proposed`` that no collection link may carry.

    A relation ``current`` already holds is not checked again, so an overlay relation that
    has since been removed never blocks a revise that leaves the field unchanged.
    """
    held = {} if current is None else {d.dotted: d.relation for d in collections.declared_links(current.schema)}
    named = [d for d in collections.declared_links(proposed.schema)
             if d.relation is not None and held.get(d.dotted) != d.relation]
    if not named:
        return
    # An overlay the caller may not read is not consulted, so the list never names its relations.
    admissible = admissible_relations(relation_registry.load_admitted(root) or relation_registry.core_registry())
    for declaration in named:
        if declaration.relation not in admissible:
            raise collections.CollectionError(
                "INVALID_LINK_RELATION",
                f"{declaration.relation!r} is not a relation a link field may carry; "
                "name one of the admissible relations",
                {"field": f"item_schema.fields.{declaration.path[0]}.relation",
                 "received": declaration.relation, "allowed": admissible[:_LISTED],
                 "example": example(declaration.path[0], admissible)},
            )


def admissible_relations(registry: relation_registry.RelationRegistry) -> list[str]:
    """The active relations of ``registry`` whose origins let a collection link carry them, sorted."""
    definitions = (*registry.core.values(), *registry.extensions.values())
    return sorted(item.key for item in definitions
                  if item.status == "active" and relation_registry.COLLECTION_LINK_ORIGIN in item.origins)


def example(field: str, admissible: list[str]) -> str:
    """A link field declaration that names the first admissible relation, for a refusal to show."""
    return f"{field}: {{type: link, relation: {admissible[0] if admissible else '<relation>'}}}"
