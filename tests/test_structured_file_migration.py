from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import ANY

import pytest

from exomem import planning
from exomem.structured_collections import CollectionError

COLLECTION_ID = "2db90f18-70df-4e41-986e-2d7d7db1caca"
FIRST_ID = "991acdd4-16b9-4396-8220-2cb37b7e8516"
SECOND_ID = "39ce48db-3c0b-49d5-832e-eb350fe20c7d"
RECORD_COLLECTION_ID = "8d116ca9-377a-40aa-9658-d6ec87ad442b"
RECORD_ID = "b60a0dfc-b10a-4219-9868-d894d40f1c85"
VIEW_COLLECTION_ID = "3977b0c0-77d7-41ac-bb4d-a94553a00fcb"


def _manifest() -> str:
    return f"""---
type: collection
exomem_id: {COLLECTION_ID}
title: Planning work
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    parent:
      type: link
      link_kind: plan
item_filename:
  version: 1
  fields: [title]
item_presentation:
  version: 1
  title: title
  summary: [kind, status]
  relationships: [parent]
---
"""


def _item(item_id: str, title: str, *, parent: str | None = None) -> str:
    parent_line = "" if parent is None else f"parent: {parent}\n"
    return f"""---
type: plan
collection_id: {COLLECTION_ID}
plan_id: {item_id}
schema_version: 1
title: {title}
kind: work-item
status: captured
{parent_line}---

Authored note.
"""


def _seed(tmp_path: Path) -> tuple[str, Path, Path]:
    kb = tmp_path / "Knowledge Base"
    kb.mkdir()
    (kb / "log.md").write_text("# Log\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Planning/Work/_collection.md"
    planning.create_collection(
        tmp_path,
        manifest_path,
        _manifest(),
        why="create migration fixture",
        scaffold=True,
    )
    items = kb / "Planning" / "Work" / "Items"
    first = items / f"{FIRST_ID}.md"
    second = items / f"{SECOND_ID}.md"
    first.write_text(_item(FIRST_ID, "Improve onboarding"), encoding="utf-8")
    second.write_text(
        _item(
            SECOND_ID,
            "Ship follow-up",
            parent=f"exomem://plan/{COLLECTION_ID}/{FIRST_ID}",
        ),
        encoding="utf-8",
    )
    return manifest_path, first, second


def _tree_hash(root: Path) -> str:
    payload = []
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file()
        and (candidate.suffix == ".md" or "_Governance/structured-files" in candidate.as_posix())
    ):
        payload.append(path.relative_to(root).as_posix().encode())
        payload.append(path.read_bytes())
    return hashlib.sha256(b"\0".join(payload)).hexdigest()


def test_preview_is_deterministic_exact_and_read_only(tmp_path: Path) -> None:
    from exomem import structured_files

    manifest_path, _first, _second = _seed(tmp_path)
    before = _tree_hash(tmp_path)

    one = structured_files.preview(tmp_path, manifest_path)
    two = structured_files.preview(tmp_path, manifest_path)

    assert one == two
    assert _tree_hash(tmp_path) == before
    assert one["operation"] == "preview"
    assert len(one["plan_id"]) == 64
    assert len(one["source_snapshot"]) == 64
    assert one["blockers"] == []
    assert sorted(move["to"] for move in one["moves"]) == [
        "Knowledge Base/Planning/Work/Items/Improve onboarding.md",
        "Knowledge Base/Planning/Work/Items/Ship follow-up.md",
    ]
    assert len(one["presentations"]) == 2
    assert one["totals"] == {
        "moves": 2,
        "presentations": 2,
        "inbound_rewrites": 0,
        "blockers": 0,
    }
    assert all(
        set(change) == {"item_key", "path", "action", "before_hash", "after_hash"}
        for change in one["presentations"]
    )
    assert all(
        set(move) == {"item_key", "from", "to", "before_hash", "after_hash"}
        for move in one["moves"]
    )


def test_preview_resolves_collisions_with_stable_identity_suffixes(tmp_path: Path) -> None:
    from exomem import structured_files

    manifest_path, _first, second = _seed(tmp_path)
    second.write_text(_item(SECOND_ID, "Improve onboarding"), encoding="utf-8")

    result = structured_files.preview(tmp_path, manifest_path)

    assert len(result["collisions"]) == 1
    collision = result["collisions"][0]
    assert collision["path"].endswith("/Improve onboarding.md")
    assert collision["item_keys"] == sorted([FIRST_ID, SECOND_ID])
    assert len(set(collision["resolved_paths"])) == 2
    assert any(
        item_id.replace("-", "")[:8] in path
        for item_id in (FIRST_ID, SECOND_ID)
        for path in collision["resolved_paths"]
    )


def test_preview_plans_mutable_inbound_rewrites_and_blocks_append_only_links(
    tmp_path: Path,
) -> None:
    from exomem import structured_files

    manifest_path, first, _second = _seed(tmp_path)
    note = tmp_path / "Knowledge Base" / "Notes" / "decision.md"
    note.parent.mkdir()
    note.write_text(f"See [[{first.relative_to(tmp_path).as_posix()[:-3]}]].\n", encoding="utf-8")

    mutable = structured_files.preview(tmp_path, manifest_path)

    assert mutable["blockers"] == []
    assert mutable["inbound_rewrites"] == [
        {
            "path": "Knowledge Base/Notes/decision.md",
            "before_hash": hashlib.sha256(note.read_bytes()).hexdigest(),
            "after_hash": ANY,
            "links": 1,
        }
    ]

    source = tmp_path / "Knowledge Base" / "Sources" / "raw.md"
    source.parent.mkdir()
    source.write_text(f"Raw [[{first.relative_to(tmp_path).as_posix()[:-3]}]].\n", encoding="utf-8")

    blocked = structured_files.preview(tmp_path, manifest_path)

    assert blocked["totals"]["blockers"] == 1
    assert blocked["blockers"] == [
        {
            "code": "IMMUTABLE_INBOUND_LINK",
            "reason": "an inbound link is not transactionally writable",
        }
    ]


def test_preview_reports_ambiguous_bare_inbound_links(tmp_path: Path) -> None:
    from exomem import structured_files

    manifest_path, first, _second = _seed(tmp_path)
    duplicate = tmp_path / "Knowledge Base" / "Notes" / first.name
    duplicate.parent.mkdir()
    duplicate.write_text("# Different target\n", encoding="utf-8")
    linker = duplicate.with_name("ambiguous.md")
    linker.write_text(f"See [[{first.stem}]].\n", encoding="utf-8")

    result = structured_files.preview(tmp_path, manifest_path)

    assert {
        "code": "AMBIGUOUS_INBOUND_LINK",
        "reason": "an inbound wikilink cannot be resolved to one moved item",
    } in result["blockers"]


def test_preview_is_bounded_and_marks_a_partial_plan_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import structured_files

    manifest_path, _first, _second = _seed(tmp_path)
    monkeypatch.setattr(structured_files, "_MAX_PLAN_ITEMS", 1)

    result = structured_files.preview(tmp_path, manifest_path)

    assert result["truncated"] is True
    assert len(result["moves"]) == 1
    assert {
        "code": "STRUCTURED_FILE_PLAN_LIMIT",
        "reason": "collection has too many items for one migration",
    } in result["blockers"]


def test_apply_moves_and_renders_atomically_then_replays(tmp_path: Path) -> None:
    from exomem import structured_files

    manifest_path, first, second = _seed(tmp_path)
    plan = structured_files.preview(tmp_path, manifest_path)

    committed = structured_files.apply(
        tmp_path,
        manifest_path,
        plan_id=plan["plan_id"],
        source_snapshot=plan["source_snapshot"],
        why="make the collection readable",
    )

    assert committed["outcome"] == "committed"
    assert not first.exists()
    assert not second.exists()
    readable = first.with_name("Improve onboarding.md")
    follower = second.with_name("Ship follow-up.md")
    assert readable.is_file()
    assert "# Improve onboarding" in readable.read_text(encoding="utf-8")
    follower_text = follower.read_text(encoding="utf-8")
    assert "# Ship follow-up" in follower_text
    assert (
        "[[Knowledge Base/Planning/Work/Items/Improve onboarding|Improve onboarding]]"
        in follower_text
    )
    assert structured_files.preview(tmp_path, manifest_path)["moves"] == []
    assert structured_files.preview(tmp_path, manifest_path)["presentations"] == []

    replay = structured_files.apply(
        tmp_path,
        manifest_path,
        plan_id=plan["plan_id"],
        source_snapshot=plan["source_snapshot"],
        why="make the collection readable",
    )

    assert replay["outcome"] == "replayed"
    assert replay["plan_id"] == plan["plan_id"]
    assert committed["inverse"] == replay["inverse"]


def test_apply_rewrites_mutable_inbound_links_in_the_same_terminal(
    tmp_path: Path,
) -> None:
    from exomem import structured_files

    manifest_path, first, _second = _seed(tmp_path)
    note = tmp_path / "Knowledge Base" / "Notes" / "decision.md"
    note.parent.mkdir()
    note.write_text(f"See [[{first.relative_to(tmp_path).as_posix()[:-3]}]].\n", encoding="utf-8")
    before_hash = hashlib.sha256(note.read_bytes()).hexdigest()
    plan = structured_files.preview(tmp_path, manifest_path)

    result = structured_files.apply(
        tmp_path,
        manifest_path,
        plan_id=plan["plan_id"],
        source_snapshot=plan["source_snapshot"],
        why="apply reviewed link migration",
    )

    assert "[[Knowledge Base/Planning/Work/Items/Improve onboarding]]" in note.read_text(
        encoding="utf-8"
    )
    inverse = next(
        entry
        for entry in result["inverse"]
        if entry["after_path"] == note.relative_to(tmp_path).as_posix()
    )
    assert inverse["before_hash"] == before_hash
    assert inverse["after_hash"] == hashlib.sha256(note.read_bytes()).hexdigest()


def test_apply_refuses_stale_item_without_mutation(tmp_path: Path) -> None:
    from exomem import structured_files

    manifest_path, first, _second = _seed(tmp_path)
    plan = structured_files.preview(tmp_path, manifest_path)
    first.write_text(first.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    before = _tree_hash(tmp_path)

    with pytest.raises(CollectionError, match="^STALE_STRUCTURED_FILE_PLAN:"):
        structured_files.apply(
            tmp_path,
            manifest_path,
            plan_id=plan["plan_id"],
            source_snapshot=plan["source_snapshot"],
            why="apply reviewed migration",
        )

    assert _tree_hash(tmp_path) == before


def test_apply_rolls_back_every_rename_when_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import structured_files

    manifest_path, first, second = _seed(tmp_path)
    plan = structured_files.preview(tmp_path, manifest_path)
    before = _tree_hash(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(structured_files.vault, "batch_atomic_write", fail)

    with pytest.raises(CollectionError, match="^STRUCTURED_FILE_PUBLICATION_FAILED:"):
        structured_files.apply(
            tmp_path,
            manifest_path,
            plan_id=plan["plan_id"],
            source_snapshot=plan["source_snapshot"],
            why="apply reviewed migration",
        )

    assert first.is_file()
    assert second.is_file()
    assert not first.with_name("Improve onboarding.md").exists()
    assert not second.with_name("Ship follow-up.md").exists()
    assert _tree_hash(tmp_path) == before


def test_maintain_memory_exposes_preview_and_exact_apply_once(tmp_path: Path) -> None:
    from exomem.commands import op_maintain_memory
    from exomem.mutation_terminal import valid_structured_files_receipt

    manifest_path, _first, _second = _seed(tmp_path)

    plan = op_maintain_memory(
        tmp_path,
        mode="structured-files",
        collection=manifest_path,
    )
    receipt = op_maintain_memory(
        tmp_path,
        mode="structured-files",
        collection=manifest_path,
        apply=True,
        plan_id=plan["plan_id"],
        source_snapshot=plan["source_snapshot"],
        why="apply the reviewed readable representation",
    )

    assert receipt["outcome"] == "committed"
    assert valid_structured_files_receipt(receipt)


def test_maintain_memory_structured_files_classifies_preview_read_only_and_apply_mutating() -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        command for command in product_commands_for("mcp") if command.name == "maintain_memory"
    )

    assert invocation_is_read_only(command, {"mode": "structured-files", "collection": "x"})
    assert not invocation_is_read_only(
        command,
        {"mode": "structured-files", "collection": "x", "apply": True},
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"apply": False},
        {"apply": True},
        {"plan_id": "a" * 64},
        {"source_snapshot": "b" * 64},
    ],
)
def test_maintain_memory_refuses_false_or_partial_apply_selectors(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    from exomem.commands import op_maintain_memory

    with pytest.raises(ValueError, match="^INVALID_ARGUMENTS:"):
        op_maintain_memory(
            tmp_path,
            mode="structured-files",
            collection="Knowledge Base/Planning/Work/_collection.md",
            **arguments,
        )


def _records_manifest_without_presentation() -> str:
    return f"""---
type: collection
exomem_id: {RECORD_COLLECTION_ID}
title: Production events
semantic_profile: records
collection_version: 1
records_reader: 2
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on, title, event_type]
  fields:
    occurred_on: {{type: date, required: true}}
    title: {{type: string, required: true}}
    event_type: {{type: string, required: true}}
    status: {{type: string}}
item_filename:
  version: 1
  fields: [occurred_on, title, event_type]
---
"""


def _legacy_record_item() -> str:
    digest = "0" * 64
    return f"""---
type: record
collection_id: {RECORD_COLLECTION_ID}
record_id: {RECORD_ID}
schema_version: 1
occurred_on: 2026-08-26
title: Published launch reel
event_type: published
status: confirmed
---

<!-- exomem-item-presentation:v1 recipe=sha256:{digest} item=sha256:{digest} -->
# Opaque generated record
<!-- /exomem-item-presentation:v1 -->

Authored production observation.
"""


def _seed_acceptance_vault(root: Path) -> dict[str, str]:
    """Build a copied-vault analogue without touching a live Obsidian vault."""
    from exomem import structured_collections as collections

    planning_manifest, first, second = _seed(root)
    second.write_text(
        _item(
            SECOND_ID,
            "Improve onboarding",
            parent=f"exomem://plan/{COLLECTION_ID}/{FIRST_ID}",
        ),
        encoding="utf-8",
    )
    mutable_linker = root / "Knowledge Base" / "Notes" / "migration-context.md"
    mutable_linker.parent.mkdir(parents=True, exist_ok=True)
    mutable_linker.write_text(
        f"See [[{first.relative_to(root).as_posix()[:-3]}]].\n", encoding="utf-8"
    )

    records_manifest = root / "Knowledge Base" / "Records" / "Production" / "_collection.md"
    records_manifest.parent.mkdir(parents=True, exist_ok=True)
    records_manifest.write_text(_records_manifest_without_presentation(), encoding="utf-8")
    records_items = records_manifest.parent / "Events"
    records_items.mkdir()
    record = records_items / f"{RECORD_ID}.md"
    record.write_text(_legacy_record_item(), encoding="utf-8")
    immutable_linker = root / "Knowledge Base" / "Sources" / "raw-import.md"
    immutable_linker.parent.mkdir(parents=True, exist_ok=True)
    immutable_linker.write_text(
        f"Raw [[{record.relative_to(root).as_posix()[:-3]}]].\n", encoding="utf-8"
    )

    drift_manifest = root / "Knowledge Base" / "Planning" / "View drift" / "_collection.md"
    drift_manifest.parent.mkdir(parents=True, exist_ok=True)
    drift_manifest.write_text(
        _manifest()
        .replace(COLLECTION_ID, VIEW_COLLECTION_ID)
        .replace("title: Planning work", "title: View drift")
        .removesuffix("---\n")
        + """views:
  invalid-now:
    query:
      filters:
        - {column: horizon, op: eq, value: now}
---
""",
        encoding="utf-8",
    )
    (drift_manifest.parent / "Items").mkdir()

    # Prove the Records manifest itself remains loadable before returning the
    # fixture paths used by the end-to-end acceptance.
    collections.load_manifest(root, records_manifest)
    return {
        "planning": planning_manifest,
        "records": records_manifest.relative_to(root).as_posix(),
        "drift": drift_manifest.relative_to(root).as_posix(),
        "mutable_linker": mutable_linker.relative_to(root).as_posix(),
    }


def test_copied_vault_acceptance_migrates_readable_obsidian_graph_without_hiding_debt(
    tmp_path: Path,
) -> None:
    from exomem import record_formats, record_governance, structured_files
    from exomem import structured_collections as collections

    fixture = _seed_acceptance_vault(tmp_path)

    drift = planning.inspect(tmp_path, fixture["drift"])
    assert any(item["code"] == "INVALID_SAVED_VIEW" for item in drift["diagnostics"])

    records_manifest = collections.load_manifest(tmp_path, fixture["records"])
    records_inspection = record_formats.inspect_collection(tmp_path, records_manifest)
    assert {item["state"] for item in records_inspection.presentation} == {
        "filename_drift",
        "orphan_presentation",
    }
    blocked_records = structured_files.preview(tmp_path, fixture["records"])
    assert blocked_records["blockers"] == [
        {
            "code": "IMMUTABLE_INBOUND_LINK",
            "reason": "an inbound link is not transactionally writable",
        }
    ]
    assert blocked_records["presentations"][0]["action"] == "remove"

    before = _tree_hash(tmp_path)
    preview = structured_files.preview(tmp_path, fixture["planning"])
    assert _tree_hash(tmp_path) == before
    assert len(preview["collisions"]) == 1
    targets = {move["item_key"]: move["to"] for move in preview["moves"]}

    structured_files.apply(
        tmp_path,
        fixture["planning"],
        plan_id=preview["plan_id"],
        source_snapshot=preview["source_snapshot"],
        why="accept the disposable Obsidian representation fixture",
    )

    for item_id, target in targets.items():
        assert Path(target).name.startswith("Improve onboarding")
        text = (tmp_path / target).read_text(encoding="utf-8")
        assert "# Improve onboarding" in text
        if item_id == SECOND_ID:
            first_target = targets[FIRST_ID].removesuffix(".md")
            assert f"[[{first_target}|Improve onboarding]]" in text
    mutable = (tmp_path / fixture["mutable_linker"]).read_text(encoding="utf-8")
    assert f"[[{targets[FIRST_ID].removesuffix('.md')}]]" in mutable

    healthy = record_governance.inspect_collection(tmp_path, fixture["planning"])
    assert healthy["diagnostics"] == []
    assert healthy["presentation"]["items"] == []
    second_preview = structured_files.preview(tmp_path, fixture["planning"])
    assert second_preview["totals"] == {
        "moves": 0,
        "presentations": 0,
        "inbound_rewrites": 0,
        "blockers": 0,
    }
