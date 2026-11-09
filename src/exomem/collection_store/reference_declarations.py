"""The author's check of a manifest's declared row references, run by create and revise.

A declaration names its target by collection id, which an author can type without reading the
target. The check therefore refuses every target the author could not have read with one generic
error that holds only the author's own text. Only a target the author may read gets an error
that names what is wrong with it.
"""

from __future__ import annotations

from .. import structured_collections as collections

#: Natural-key field types whose values a reference can match by one text value.
_KEY_TYPES = ("string", "date", "datetime")


def require_targets(operation, proposed: collections.CollectionManifest,
                    current: collections.CollectionManifest | None = None) -> None:
    """Refuse a new or changed declaration of ``proposed`` whose target cannot be referenced.

    A declaration ``current`` already holds is not checked again, so a target that has since
    been deleted or withheld from the author never blocks a revise that leaves it unchanged.
    ``operation`` is the writer's authorization, which decides what the author may read.
    """
    held = set() if current is None else set(collections.declared_references(current.schema))
    for declaration in collections.declared_references(proposed.schema):
        if declaration not in held:
            _require_target(operation, declaration)


def _readable(operation, cid: str) -> collections.CollectionManifest | None:
    """The target's manifest when the author may read it and all of its natural key, else None."""
    try:
        # A read decision, because a write decision would also withhold a collection that the author
        # reads but cannot write, and the author only reads the target.
        if operation.decision(operation.manifest_subject(cid), mutation=False).level < 6:
            return None
        target = operation.field_manifest(cid)
        plan = operation.field_plan(target)
    except collections.CollectionError:
        return None
    return target if len(plan.manifest.schema.natural_key) == len(target.schema.natural_key) else None


def _require_target(operation, declaration: collections.ReferenceDeclaration) -> None:
    field = f"{declaration.location}.references.collection"
    cid = declaration.collection
    generic = collections.CollectionError(
        "INVALID_REFERENCE_TARGET",
        "the target must be the exomem_id of a collection in this vault that you can read",
        {"field": field, "received": cid, "expected": "the exomem_id of a collection you can read",
         "example": "references: {collection: 15dd81cd-9ae2-488c-bd9e-14771d86343e}"},
    )
    # A missing collection, one the author cannot read and one whose key the author cannot read
    # all raise the same error, so the error shows nothing the author could not read.
    row = operation.conn.execute("SELECT derived FROM collections WHERE collection_id=?", (cid,)).fetchone()
    target = None if row is None else _readable(operation, cid)
    if target is None:
        raise generic
    key = target.schema.natural_key
    unsupported = {"field": field, "expected": "a collection whose natural key is one field of type "
                   f"{', '.join(_KEY_TYPES[:-1])} or {_KEY_TYPES[-1]}",
                   "example": "pick a collection that keeps one natural-key field, such as a code or an id"}
    if len(key) != 1:
        raise collections.CollectionError(
            "UNSUPPORTED_REFERENCE_TARGET",
            "a reference matches one text value, so the target's natural key must be a single field",
            {**unsupported, "received": list(key)},
        )
    key_type = target.schema.fields[key[0]].type
    if key_type not in _KEY_TYPES:
        raise collections.CollectionError(
            "UNSUPPORTED_REFERENCE_TARGET",
            "a reference matches text, so the target's natural-key field must be a string, date or datetime",
            {**unsupported, "received": key_type},
        )
    if row[0]:
        raise collections.CollectionError(
            "UNSUPPORTED_REFERENCE_TARGET",
            "a derived collection keeps its rows outside the store, so a reference cannot resolve through it",
            {**unsupported, "received": "a derived collection"},
        )
