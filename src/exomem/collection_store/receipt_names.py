"""What a person calls the collection and the row that a Records write receipt reports.

A receipt of a store-backed write names the collection page and the pages it rendered, which a person
never asked for. The collection's title and the row's own label are what they recognise. Both come from
what the collection defines: its manifest title, then its declared presentation title field or its
natural-key fields. Nothing here knows a field name, and a collection that declares none gives no label.

The read is the caller's own: a row is named only to a caller who reads it whole, and only while it still
holds the committed version the receipt names. Anything else gives no label, so a receipt never states
what its caller could not read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..governance import egress
from . import related_pages
from .row_binding import whole_row_at

#: Longest row label a sentence carries. A bound on one sentence's size, not a vocabulary.
LABEL_CHARS = 80


def row_label(manifest, values: Mapping[str, Any]) -> str | None:
    """The row's declared title if it has one, else its natural-key text values; None when the collection declares neither."""
    presentation = manifest.item_presentation
    declared = values.get(presentation.title.field) if presentation is not None else None
    names = (declared.strip(),) if type(declared) is str and declared.strip() else related_pages.subject_names(manifest, values)
    label = " ".join(", ".join(names).split())
    if not label:
        return None
    return label if len(label) <= LABEL_CHARS else label[:LABEL_CHARS - 1].rstrip() + "…"


def owns(writer, collection_id: str) -> bool:
    """Whether the store holds this collection.

    A store-mode vault runs every write on the store thread, also one to a collection that only files hold.
    Such a collection is named by its file, never by the store.
    """
    return writer.connection.execute(
        "SELECT 1 FROM collections WHERE collection_id=?", (collection_id,)).fetchone() is not None


def receipt_names(writer, collection_id: str, item_key: str, after_item_hash: str | None) -> tuple[str | None, str | None]:
    """The collection title and the row label this caller may read, each None when it may not or cannot be proven.

    The title is the collection manifest's, released to a caller whose decision on it is full disclosure.
    The label needs the row at the version the receipt committed.
    """
    operation = writer._operation
    with writer.read_collection(collection_id) as manifest:
        released = operation.decision(operation.manifest_subject(collection_id)).level >= egress.LEVEL_FULL
        title = manifest.title if released else None
        row = None if after_item_hash is None else whole_row_at(writer, collection_id, item_key, after_item_hash)
        if row is None:
            return title, None
        return title, row_label(manifest, json.loads(row["values_json"]))
