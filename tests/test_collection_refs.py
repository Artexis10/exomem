"""A row query never overwrites a field the collection stores."""

from __future__ import annotations

from exomem import collection_refs, page_ids


def _rows(*extra: dict) -> dict:
    return {"rows": [{"record_id": "r1", "item_version": "v1", **fields} for fields in extra]}


def test_the_short_id_never_replaces_a_stored_id_or_row_id_field() -> None:
    short = page_ids.part_id("r1", "v1")
    marked = collection_refs.mark_rows(
        _rows({}, {"id": "mine"}, {"id": "mine", "row_id": "also mine"}), profile="records"
    )["rows"]
    assert marked[0]["id"] == short
    assert (marked[1]["id"], marked[1]["row_id"]) == ("mine", short)
    assert (marked[2]["id"], marked[2]["row_id"]) == ("mine", "also mine")
