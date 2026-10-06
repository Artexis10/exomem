"""Summary view mode: bounded generated pages instead of per-row views.

OpenSpec add-collection-query-engine design §12. A summary collection keeps
its rows only in the store, under their logical identities. Its vault views
are the manifest, held candidates and at most ``MAX_PAGES`` read-only summary
pages of at most ``MAX_PAGE_BYTES`` each. Acknowledgement commits the pages'
pending basis; reconcile renders and publishes them later. A page labels what
it counts, from which source and basis, and never claims the current state.

A populated collection never changes view mode in place: the owner copies its
accepted current values into a NEW collection of the other mode.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from .. import structured_collections as collections
from .. import vault

SUMMARY = "summary"
MAX_PAGES = 16
MAX_PAGE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = MAX_PAGES * MAX_PAGE_BYTES
MODE_CHANGE_UNSUPPORTED = "VIEW_MODE_CHANGE_UNSUPPORTED"


def page_paths(manifest: collections.CollectionManifest) -> tuple[str, ...]:
    """The declared, bounded summary pages; items mode declares none."""
    if manifest.view_mode != SUMMARY:
        return ()
    return (f"{manifest.storage.source}/_summary.md",)


def capacity() -> dict:
    """Describe's known bounds: no file-derived row cap, and no unmeasured row claim."""
    return {
        "row_limit": None,
        "bounded_by": ["store free space", "declared index budgets", "admitted query and import resources"],
        "summary_pages": {"max_pages": MAX_PAGES, "max_page_bytes": MAX_PAGE_BYTES,
                          "max_total_bytes": MAX_TOTAL_BYTES},
    }


def populated(conn: sqlite3.Connection, collection_id: str) -> bool:
    """Rows, which are never deleted, or held state make a collection populated."""
    return conn.execute(
        "SELECT EXISTS(SELECT 1 FROM items WHERE collection_id=?) "
        "OR EXISTS(SELECT 1 FROM held_candidates WHERE collection_id=?)",
        (collection_id, collection_id),
    ).fetchone()[0] == 1


def publication_outstanding(conn: sqlite3.Connection, collection_id: str) -> bool:
    """A row, page or held view is not yet published, so the mode cannot change under it.

    The manifest view is mode-independent and the revise re-renders it.
    """
    return conn.execute("SELECT 1 FROM projection_state WHERE collection_id=? AND kind<>'manifest' "
                        "AND state<>'current' LIMIT 1", (collection_id,)).fetchone() is not None


def mode_change_refused(current: str, requested: str) -> collections.CollectionError:
    return collections.CollectionError(
        MODE_CHANGE_UNSUPPORTED,
        f"a populated {current} collection cannot change to {requested} in place",
        {
            "view_mode": current,
            "requested_view_mode": requested,
            "migration": (
                f"Create a NEW collection with view_mode: {requested} and copy the accepted current "
                "values with their source provenance. This collection, its files, history and held "
                "state stay as they are."
            ),
        },
    )


def render_page(conn: sqlite3.Connection, manifest: collections.CollectionManifest,
                view_stamp: Mapping[str, str | int]) -> str:
    """Render the overview page from the committed basis named in its stamp."""
    rows = conn.execute("SELECT COUNT(*) FROM items WHERE collection_id=?",
                        (manifest.collection_id,)).fetchone()[0]
    generation = view_stamp["v"]
    frontmatter = {
        "type": "collection-summary",
        "collection_id": manifest.collection_id,
        "metric": "committed rows",
        "value": rows,
        "window": "all committed rows",
        "source": "collection store",
        "basis": {"generation": generation, "release": "owner"},
        "completeness": "complete at basis",
        "exomem_view": dict(view_stamp),
    }
    text = (
        "---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n\n"
        + f"# {manifest.title}: summary\n\n{rows} committed rows as of collection generation {generation}.\n\n"
        + "Generated read-only from the collection store. Edits here are held, not applied, and "
        + "later commits appear when this page is next published.\n"
    )
    if len(text.encode()) > MAX_PAGE_BYTES:
        raise RuntimeError("summary page exceeds its byte bound")
    return text
