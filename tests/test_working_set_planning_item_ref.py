"""A turn about one Planning item is served that item (close-memory-loop task 6.12).

The activation index holds one `plan` anchor per active Planning item. Its
reported identity is the item's own page, the page the agent reads or updates,
never the collection manifest the item is filed under: a turn about one intended
item got the collection's ref and a `plan:<collection>#<title>` unit, and the
agent then had to find the item again. The collection stays the anchor's home
(`path`), so items filed together remain complementary. Invented collection and
items only.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from test_planning_mutation import _manifest

from exomem import (
    commands,
    planning,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
)

MANIFEST = "Knowledge Base/Planning/Harbour Works/_collection.md"
ITEMS = {
    str(UUID(int=41)): "Resurface the harbour slipway",
    str(UUID(int=42)): "Replace the lighthouse lantern",
}


@pytest.fixture
def planned(vault: Path) -> Path:
    planning.create_collection(
        vault, MANIFEST, _manifest() + "\nHarbour works.\n", why="seed a planning collection"
    )
    for plan_id, title in ITEMS.items():
        planning.add(
            vault,
            MANIFEST,
            plan_id=plan_id,
            item={"title": title, "kind": "outcome"},
            why="seed a planning item",
        )
    return vault


def _item_pages(vault: Path) -> dict[str, str]:
    """Title -> the item's own page, read off the files the writer made."""
    folder = vault / Path(MANIFEST).parent / "Items"
    pages: dict[str, str] = {}
    for page in sorted(folder.glob("*.md")):
        text = page.read_text(encoding="utf-8")
        for title in ITEMS.values():
            if f"title: {title}" in text:
                pages[title] = page.relative_to(vault).as_posix()
    assert set(pages) == set(ITEMS.values())
    return pages


def test_each_plan_anchor_is_its_own_item_page(planned: Path) -> None:
    index = working_set_index.WorkingSetIndex(planned)
    try:
        index.rebuild()
        rows = [row for row in index.anchors() if row.kind == "plan"]
    finally:
        index.close()

    assert {row.title: working_set_resolve.anchor_ref(row) for row in rows} == _item_pages(planned)
    assert {row.path for row in rows} == {MANIFEST}


def test_a_turn_about_one_item_serves_that_item_page(planned: Path) -> None:
    working_set_runtime.reset_caches_for_tests()
    page = _item_pages(planned)["Resurface the harbour slipway"]
    packet = commands.op_activate_context(
        planned, turn="Next up: resurface the harbour slipway."
    )

    resolved = [anchor for anchor in packet["anchors"] if anchor["status"] == "resolved"]
    assert [(anchor["kind"], anchor["ref"]) for anchor in resolved] == [("plan", page)]
    assert {unit["ref"] for unit in packet["units"] if unit["role"] == "active_plans"} <= {page}
    assert MANIFEST not in {anchor["ref"] for anchor in packet["anchors"]}
