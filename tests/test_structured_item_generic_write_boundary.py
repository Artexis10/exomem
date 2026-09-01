from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import create_file as create_file_module
from exomem import edit as edit_module


@pytest.mark.parametrize(
    ("item_type", "item_id_field", "route"),
    (
        ("plan", "plan_id", "plan_memory"),
        ("record", "record_id", "record_memory"),
    ),
)
def test_generic_edit_refuses_structured_collection_items_without_changing_bytes(
    vault: Path,
    item_type: str,
    item_id_field: str,
    route: str,
) -> None:
    relative = f"Knowledge Base/{item_type.title()}s/Owned/Items/item.md"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        f"type: {item_type}\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\n"
        f"{item_id_field}: 22222222-2222-4222-8222-222222222222\n"
        "schema_version: 1\n"
        "title: Owned item\n"
        "---\n\n"
        "Original body.\n",
        encoding="utf-8",
    )
    before = target.read_bytes()

    with pytest.raises(edit_module.EditError) as raised:
        edit_module.edit(
            vault,
            path=relative,
            old_string="Original body.",
            new_string="Changed outside the owning product.",
            why="exercise the structured-item ownership boundary",
            today=dt.date(2026, 9, 1),
        )

    assert raised.value.code == "STRUCTURED_ITEM_REQUIRES_PRODUCT_ROUTE"
    assert route in raised.value.reason
    assert target.read_bytes() == before


def test_tier2_overwrite_remains_available_for_exact_structured_item_repair(
    vault: Path,
) -> None:
    relative = "Knowledge Base/Planning/Owned/Items/item.md"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    damaged = (
        "---\n"
        "type: plan\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\n"
        "plan_id: 22222222-2222-4222-8222-222222222222\n"
        "schema_version: 1\n"
        "title: Owned item\n"
        "legacy_unknown: remove me\n"
        "---\n\n"
        "Original body.\n"
    )
    repaired = damaged.replace("legacy_unknown: remove me\n", "")
    target.write_text(damaged, encoding="utf-8")

    result = create_file_module.create_file(
        vault,
        path=relative,
        content=repaired,
        overwrite=True,
        today=dt.date(2026, 9, 1),
    )

    assert result.path == relative
    assert target.read_text(encoding="utf-8") == repaired
