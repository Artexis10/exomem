"""Task 6.9 -- bounded current-state Records lookup.

`working_set_state.current_state_for` resolves the newest Records observation
for a stateful anchor, Records first. That lookup must do bounded work: it
must never govern more than the one (or `limit`) row it actually returns, and
it must never build link governance's vault-wide candidate index to do it --
`working_set_state.py` asks for this through `late_link_projection=True`
(record_governance.query_collection / record_formats.query_collection); it
never narrows which fields it asks for, so a Records collection keeps
exactly its base-code semantic reach here, just done in bounded work. A
query that genuinely asks for a link field, or one whose row selection
cannot be proven link-independent, keeps full eager link governance
unaffected -- that half of the contract (and the disclosure regressions) is
covered in test_record_governance.py.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from record_fixtures import copy_vehicle_maintenance_fixture

from exomem import record_governance, vault, working_set_resolve, working_set_state
from exomem import structured_collections as collections


def _anchor(manifest: collections.CollectionManifest) -> working_set_resolve.ResolvedAnchor:
    """An anchor that claims `manifest` directly, through its own path."""
    return working_set_resolve.ResolvedAnchor(
        anchor_id=manifest.collection_id,
        path=manifest.path,
        ref=manifest.path,
        title=manifest.title,
        kind="resource",
        lifecycle="active",
        status="resolved",
        evidence=(),
        categories=(),
        neighbourhood=frozenset(),
    )


def _write_item(path: Path, **fields: object) -> None:
    """Write one markdown-item record, matching the real Records file shape.

    A bare-title wikilink or a memory reference is quoted, exactly as the
    checked-in vehicle-maintenance fixture quotes its own link fields;
    everything else is written unquoted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, str) and (
            value.startswith("[[") or value.startswith("exomem://")
        ):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append("Generated for a bounded current-state test.\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_collection(root: Path, name: str, exomem_id: str, fields_yaml: str) -> Path:
    """A minimal Records collection, written like real manifests elsewhere."""
    coll_dir = root / "Knowledge Base" / "Records" / name
    coll_dir.mkdir(parents=True)
    (coll_dir / "_collection.md").write_text(
        "---\n"
        "type: collection\n"
        f"exomem_id: {exomem_id}\n"
        f"title: {name}\n"
        "semantic_profile: records\n"
        "collection_version: 1\n"
        "schema_version: 1\n"
        "lifecycle: active\n"
        "storage:\n"
        "  strategy: markdown-items\n"
        "  source: Items\n"
        "  format_version: 1\n"
        "item_schema:\n"
        f"{fields_yaml}"
        "---\n\n"
        f"{name} fixture.\n",
        encoding="utf-8",
    )
    (coll_dir / "Items").mkdir()
    return coll_dir


def test_current_state_never_scans_the_vault_for_an_unrelated_link_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: the newest observation carries an unrelated bare-title link field
    and an unrelated memory-reference link field. Resolving current state
    must still succeed, and must never build link governance's vault-wide
    candidate index.
    """
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_item(
        fixture / "Events" / "2026-07-15-bare-title.md",
        type="record",
        collection_id=manifest.collection_id,
        record_id="99999999-9999-4999-8999-999999999999",
        schema_version=1,
        occurred_on="2026-07-15",
        asset="[[Vehicle]]",
        receipt="exomem://memory/22222222-2222-4222-8222-222222222222",
        status="completed",
    )

    walks = 0
    real_walk = vault.walk_vault_md

    def counting_walk(root: Path) -> object:
        nonlocal walks
        walks += 1
        return real_walk(root)

    monkeypatch.setattr(vault, "walk_vault_md", counting_walk)

    entries = working_set_state.current_state_for(tmp_path, anchors=(_anchor(manifest),))

    assert walks == 0, "current-state lookup must not scan the vault for a link field it never asked for"
    assert entries, "the newest Records observation must still surface"
    entry = entries[0]
    assert entry["source"] == "records"
    assert entry["as_of"] == "2026-07-15"
    assert "completed" in entry["statement"]
    assert "Vehicle" not in entry["statement"]
    assert "exomem://memory" not in entry["statement"]


def test_current_state_governs_exactly_the_returned_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: with many records in the claiming collection, link governance's
    per-record projector runs exactly once per returned row (limit=1) --
    never zero (that would only prove it was bypassed, not bounded) and
    never once per stored record.
    """
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    total = 60
    start = datetime.date(2026, 1, 1)
    for index in range(total):
        occurred_on = (start + datetime.timedelta(days=index)).isoformat()
        _write_item(
            fixture / "Events" / "bulk" / f"{occurred_on}-bulk.md",
            type="record",
            collection_id=manifest.collection_id,
            record_id=f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}",
            schema_version=1,
            occurred_on=occurred_on,
            asset="[[Assets/Vehicle]]",
            status="completed",
        )

    calls = 0
    real_call = record_governance._LinkProjector.__call__

    def counting_call(self: object, values: object) -> object:
        nonlocal calls
        calls += 1
        return real_call(self, values)

    monkeypatch.setattr(record_governance._LinkProjector, "__call__", counting_call)

    entries = working_set_state.current_state_for(tmp_path, anchors=(_anchor(manifest),))

    assert entries and entries[0]["source"] == "records"
    assert calls == 1, f"expected link governance run exactly once (limit=1), got {calls} calls"


def test_current_state_still_reports_a_path_shaped_link_state_field(tmp_path: Path) -> None:
    """M4a: `location` is a link field and one of `_STATE_FIELDS`. A
    path-shaped target needs no candidate index and must still resolve and
    be reported, exactly as on base.
    """
    coll_dir = _write_collection(
        tmp_path,
        "places",
        "11111111-2222-4333-8444-555555555555",
        "  natural_key: [observed_on, location]\n"
        "  fields:\n"
        "    observed_on:\n"
        "      type: date\n"
        "      required: true\n"
        "    location:\n"
        "      type: link\n"
        "      required: true\n",
    )
    garage = tmp_path / "Knowledge Base" / "Places" / "Garage.md"
    garage.parent.mkdir(parents=True)
    garage.write_text("# Garage\n", encoding="utf-8")
    _write_item(
        coll_dir / "Items" / "2026-07-20-a.md",
        type="record",
        collection_id="11111111-2222-4333-8444-555555555555",
        record_id="11111111-1111-4111-8111-111111111111",
        schema_version=1,
        observed_on="2026-07-20",
        location="[[Places/Garage]]",
    )
    manifest = collections.load_manifest(tmp_path, coll_dir / "_collection.md")

    entries = working_set_state.current_state_for(tmp_path, anchors=(_anchor(manifest),))

    assert entries and entries[0]["source"] == "records"
    assert entries[0]["statement"] == "location: [[Places/Garage]]"


def test_current_state_falls_through_a_bare_title_link_to_a_truthful_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4b: `location` (a `_STATE_FIELDS` name) is a bare-title link that
    cannot resolve without a cold candidate-index build. Current state must
    not disclose it, must not scan the vault, and must still report a
    truthful statement built from the record's other (scalar) fields.
    """
    coll_dir = _write_collection(
        tmp_path,
        "bare-places",
        "22222222-3333-4444-8555-666666666666",
        "  natural_key: [observed_on, location]\n"
        "  fields:\n"
        "    observed_on:\n"
        "      type: date\n"
        "      required: true\n"
        "    location:\n"
        "      type: link\n"
        "      required: true\n"
        "    provider:\n"
        "      type: string\n",
    )
    _write_item(
        coll_dir / "Items" / "2026-07-20-a.md",
        type="record",
        collection_id="22222222-3333-4444-8555-666666666666",
        record_id="11111111-1111-4111-8111-111111111111",
        schema_version=1,
        observed_on="2026-07-20",
        location="[[Garage]]",
        provider="Northside Garage",
    )
    manifest = collections.load_manifest(tmp_path, coll_dir / "_collection.md")

    walks = 0
    real_walk = vault.walk_vault_md

    def counting_walk(root: Path) -> object:
        nonlocal walks
        walks += 1
        return real_walk(root)

    monkeypatch.setattr(vault, "walk_vault_md", counting_walk)

    entries = working_set_state.current_state_for(tmp_path, anchors=(_anchor(manifest),))

    assert walks == 0
    assert entries and entries[0]["source"] == "records"
    statement = entries[0]["statement"]
    assert "Garage" not in statement.replace("Northside Garage", "")
    assert "provider: Northside Garage" in statement


def test_current_state_reports_an_all_link_schema_when_its_link_resolves(tmp_path: Path) -> None:
    """M4c: every declared field is a link (no scalar field at all). Base
    code never special-cases this; a resolving path-shaped link must still
    produce a Records-sourced entry.
    """
    coll_dir = _write_collection(
        tmp_path,
        "all-link",
        "33333333-4444-4555-8666-777777777777",
        "  natural_key: [asset]\n  fields:\n    asset:\n      type: link\n      required: true\n",
    )
    (tmp_path / "Knowledge Base" / "Assets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base" / "Assets" / "Vehicle.md").write_text("# Vehicle\n", encoding="utf-8")
    _write_item(
        coll_dir / "Items" / "a.md",
        type="record",
        collection_id="33333333-4444-4555-8666-777777777777",
        record_id="22222222-2222-4222-8222-222222222222",
        schema_version=1,
        asset="[[Assets/Vehicle]]",
    )
    manifest = collections.load_manifest(tmp_path, coll_dir / "_collection.md")

    entries = working_set_state.current_state_for(tmp_path, anchors=(_anchor(manifest),))

    assert entries and entries[0]["source"] == "records"
    assert entries[0]["statement"] == "asset: [[Assets/Vehicle]]"
