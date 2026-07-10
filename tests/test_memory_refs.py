from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from exomem import add, commands, link, memory_refs, note, preserve
from exomem import vault as vault_module

TODAY = dt.date(2026, 7, 9)


def _page(identity: str | None, body: str = "# Page\n") -> str:
    id_line = f"\nexomem_id: {identity}" if identity is not None else ""
    return f"---\ntype: insight\ncreated: 2026-07-09{id_line}\n---\n\n{body}"


def test_reference_round_trip_and_incremental_move(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    old = vault / "Knowledge Base" / "Notes" / "old.md"
    old.parent.mkdir(parents=True)
    identity = memory_refs.new_id()
    old.write_text(_page(identity), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all() == {"indexed": 1, "duplicates": 0, "malformed": 0}
    ref = memory_refs.memory_ref(identity)
    assert memory_refs.parse_memory_ref(ref) == identity
    assert memory_refs.resolve_identifier(vault, ref) == "Knowledge Base/Notes/old.md"

    index.path.unlink()
    assert memory_refs.resolve_identifier(vault, ref) == "Knowledge Base/Notes/old.md"
    assert index.available(), "canonical resolution should rebuild a missing sidecar"

    new = old.with_name("new.md")
    old.rename(new)
    index.refresh_paths([new])
    index.delete_paths(["Knowledge Base/Notes/old.md"])
    assert index.resolve(identity) == "Knowledge Base/Notes/new.md"


def test_duplicate_and_malformed_ids_are_diagnostic_and_self_healing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    identity = memory_refs.new_id()
    first = notes / "first.md"
    second = notes / "second.md"
    broken = notes / "broken.md"
    first.write_text(_page(identity), encoding="utf-8")
    second.write_text(_page(identity), encoding="utf-8")
    broken.write_text(_page("not-a-uuid"), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all() == {"indexed": 2, "duplicates": 1, "malformed": 1}
    issues = index.issues()
    assert [item["kind"] for item in issues] == ["duplicate", "duplicate", "malformed"]
    try:
        index.resolve(identity)
    except memory_refs.ReferenceError as exc:
        assert exc.code == "AMBIGUOUS_REFERENCE"
    else:
        raise AssertionError("duplicate identity unexpectedly resolved")

    second.unlink()
    index.delete_paths(["Knowledge Base/Notes/second.md"])
    assert index.resolve(identity) == "Knowledge Base/Notes/first.md"


def test_backfill_refuses_existing_duplicate_identity(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    identity = memory_refs.new_id()
    (notes / "first.md").write_text(_page(identity), encoding="utf-8")
    (notes / "second.md").write_text(_page(identity), encoding="utf-8")
    legacy = notes / "legacy.md"
    legacy.write_text(_page(None), encoding="utf-8")

    planned = memory_refs.backfill_ids(vault)
    assert planned["would_update"] == ["Knowledge Base/Notes/legacy.md"]
    assert {item["kind"] for item in planned["identity_issues"]} == {"duplicate"}
    try:
        memory_refs.backfill_ids(vault, dry_run=False)
    except memory_refs.ReferenceError as exc:
        assert exc.code == "AMBIGUOUS_REFERENCE"
    else:
        raise AssertionError("backfill unexpectedly wrote through duplicate identity")
    assert "exomem_id" not in legacy.read_text(encoding="utf-8")


def test_backfill_is_dry_run_by_default_and_preserves_content(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = vault / "Knowledge Base" / "Notes" / "legacy.md"
    path.parent.mkdir(parents=True)
    original = _page(None, "# Legacy\n\nExact body.\n")
    path.write_text(original, encoding="utf-8")

    planned = memory_refs.backfill_ids(vault)
    assert planned["dry_run"] is True
    assert planned["would_update"] == ["Knowledge Base/Notes/legacy.md"]
    assert path.read_text(encoding="utf-8") == original

    applied = memory_refs.backfill_ids(vault, dry_run=False)
    updated = path.read_text(encoding="utf-8")
    fm, body, _ = vault_module.parse_frontmatter(updated)
    assert memory_refs.normalize_id(fm["exomem_id"])
    assert body == "# Legacy\n\nExact body.\n"
    assert applied["updated"] == ["Knowledge Base/Notes/legacy.md"]


def test_governed_writers_return_canonical_refs(
    vault: Path, source_schema
) -> None:
    results = [
        add.add(
            vault,
            source_schema,
            content="Captured material.",
            source_type="article",
            title="Reference source",
            url="https://example.com/reference-source",
            today=TODAY,
        ),
        note.note(
            vault,
            content="# Reference note\n\n## Claim\n\nStable identity survives paths.\n",
            note_type="insight",
            title="Reference note",
            today=TODAY,
        ),
        link.link(
            vault,
            entity_type="concept",
            name="Stable Identity",
            summary="Path-independent memory identity.",
            today=TODAY,
        ),
        preserve.preserve(
            vault,
            scope="Reference",
            category="proof",
            filename="artifact.txt",
            content="evidence",
            description="Evidence sidecar with identity.",
            today=TODAY,
        ),
    ]

    for result in results:
        assert result.ref and result.ref.startswith(memory_refs.REF_PREFIX)
        page_path = result.sidecar_path if hasattr(result, "sidecar_path") else result.path
        raw = (vault / page_path).read_text(encoding="utf-8")
        fm = yaml.safe_load(raw.split("\n---\n", 1)[0].removeprefix("---\n"))
        assert memory_refs.memory_ref(str(fm["exomem_id"])) == result.ref


def test_read_and_edit_accept_canonical_reference(vault: Path) -> None:
    created = note.note(
        vault,
        content="# Referenced command\n\nBefore.\n",
        note_type="insight",
        title="Referenced command",
        today=TODAY,
    )
    fetched = commands.op_get(vault, path=created.ref)
    assert fetched["path"] == created.path
    assert fetched["ref"] == created.ref

    edited = commands.op_edit(
        vault,
        path=created.ref,
        why="exercise stable reference resolution",
        old_string="Before.",
        new_string="After.",
    )
    assert edited["path"] == created.path
    assert "After." in (vault / created.path).read_text(encoding="utf-8")


def test_product_move_preserves_identity_and_heals_reference(vault: Path) -> None:
    created = note.note(
        vault,
        content="# Move identity\n\nStable across a governed move.\n",
        note_type="insight",
        title="Move identity",
        today=TODAY,
    )
    before = yaml.safe_load(
        (vault / created.path)
        .read_text(encoding="utf-8")
        .split("\n---\n", 1)[0]
        .removeprefix("---\n")
    )["exomem_id"]
    destination = "Knowledge Base/Notes/Insights/moved-identity.md"

    moved = commands.op_move_file(vault, old_path=created.ref, new_path=destination)
    read = commands.op_read_memory(vault, path=created.ref)

    assert moved["new_path"] == destination
    assert read["path"] == destination
    assert read["ref"] == created.ref
    assert read["frontmatter"]["exomem_id"] == before
