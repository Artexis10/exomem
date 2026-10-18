"""Canonical page consumers agree on the exact bytes, including newline edits."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import activation_manifest, deferred_write_advisory, memory_refs, semantic_index
from exomem import find as find_module
from exomem import vault as vault_module


@pytest.mark.parametrize(
    "source",
    [
        "---\ntype: insight\n---\n\n- [fact] café ^one\n",
        "---\r\ntype: insight\r\n---\r\n\r\n- [fact] café ^one\r\n",
        "---\r\ntype: insight\n---\r\n\n- [fact] café ^one\r\n",
        "\ufeff---\r\ntype: insight\r\n---\r\n\r\n- [fact] café ^one\r\n",
    ],
    ids=["lf", "crlf", "mixed", "bom"],
)
def test_current_page_consumers_and_replacement_share_stored_byte_identity(
    tmp_path: Path, source: str
) -> None:
    path = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    path.parent.mkdir(parents=True)
    original = source.encode("utf-8")
    path.write_bytes(original)
    rel = path.relative_to(tmp_path).as_posix()
    expected = hashlib.sha256(original).hexdigest()

    text, guard = vault_module.read_guarded_text(tmp_path, path)
    state = semantic_index.build_parent_index_state(tmp_path, path)
    shared = semantic_index.build_parent_index_state(tmp_path, path, source=text)
    census = activation_manifest.build_census(tmp_path)
    parsed = find_module._parse_page(path, path.stat().st_mtime, tmp_path, content=original)

    assert parsed is not None and parsed.frontmatter_valid
    assert parsed.page_type == "insight"
    assert guard.expected_content_hash == vault_module.content_hash(text) == expected
    assert state.parent_source_hash == shared.parent_source_hash == expected
    assert census.by_path[rel].source_hash == expected
    assert deferred_write_advisory._observe_fingerprint(tmp_path, rel) == expected
    assert semantic_index.validate_parent_record(
        tmp_path,
        parent_path=rel,
        parent_generation_value=state.parent_generation,
        parent_source_hash=expected,
        parser_version=state.parser_version,
    ).current

    replacement = text.replace("café", "naïve")
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path, replacement, expected_hash=expected, guard=guard)],
        vault_root=tmp_path,
    )
    assert path.read_bytes() == replacement.encode("utf-8")


@pytest.mark.parametrize("closing", ["", "---not-a-fence\nbody\n", "----\nbody\n"])
def test_malformed_frontmatter_remains_invalid(tmp_path: Path, closing: str) -> None:
    path = tmp_path / "page.md"
    raw = ("\ufeff---\r\ntype: insight\r\n" + closing).encode("utf-8")
    path.write_bytes(raw)

    parsed = find_module._parse_page(path, path.stat().st_mtime, tmp_path, content=raw)

    assert parsed is not None and not parsed.frontmatter_valid
    assert vault_module.parse_frontmatter(raw.decode("utf-8"))[2] is None
    assert path.read_bytes() == raw


def test_frontmatter_closing_fence_at_eof_remains_valid(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    raw = b"---\ntype: insight\n---"
    path.write_bytes(raw)

    parsed = find_module._parse_page(path, path.stat().st_mtime, tmp_path, content=raw)

    assert parsed is not None and parsed.frontmatter_valid
    assert parsed.page_type == vault_module.parse_frontmatter(raw.decode("utf-8"))[0]["type"]


def test_newline_only_concurrent_edit_refuses_a_stale_replacement(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    path.write_bytes(b"first\r\nsecond\r\n")
    text, _guard = vault_module.read_guarded_text(tmp_path, path)
    expected = vault_module.content_hash(text)
    changed = b"first\nsecond\n"
    path.write_bytes(changed)

    with pytest.raises(vault_module.ContentHashMismatchError):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(path, "replacement\n", expected_hash=expected)],
            vault_root=tmp_path,
        )

    assert path.read_bytes() == changed


def test_snapshot_check_refuses_newline_drift_after_the_initial_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "page.md"
    original = b"first\nsecond\n"
    changed = b"first\r\nsecond\r\n"
    path.write_bytes(original)
    capture = vault_module._capture_batch_snapshot

    def change_before_capture(target, **kwargs):
        if target == path:
            path.write_bytes(changed)
        return capture(target, **kwargs)

    monkeypatch.setattr(vault_module, "_capture_batch_snapshot", change_before_capture)
    with pytest.raises(vault_module.ContentHashMismatchError):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(
                path, "replacement\n", expected_hash=hashlib.sha256(original).hexdigest()
            )],
            vault_root=tmp_path,
        )

    assert path.read_bytes() == changed


@pytest.mark.parametrize(
    "original",
    [
        b"---\r\ntype: source\r\ntitle: Example  \r\n---\r\n\r\nBody.\r\n",
        b"---\r\ntype: source\ntitle: Example  \r\n---\n\r\nBody.\n",
        b"\xef\xbb\xbf---\r\ntype: source\r\n---\r\n\r\nBody.\r\n",
    ],
    ids=["crlf", "mixed", "bom"],
)
def test_backfill_inserts_only_the_identity_field(tmp_path: Path, original: bytes) -> None:
    path = tmp_path / "Knowledge Base" / "Sources" / "legacy.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(original)

    planned = memory_refs.backfill_ids(tmp_path)
    assert path.read_bytes() == original
    assert planned["would_update"] == ["Knowledge Base/Sources/legacy.md"]
    applied = memory_refs.backfill_ids(tmp_path, dry_run=False)

    installed = path.read_bytes()
    frontmatter, _, _ = vault_module.parse_frontmatter(installed.decode("utf-8"))
    identity = frontmatter["exomem_id"]
    inserted = f"\r\nexomem_id: {identity}".encode("utf-8")
    assert installed.count(inserted) == 1
    assert installed.replace(inserted, b"", 1) == original
    rel = applied["updated"][0]
    digest = hashlib.sha256(installed).hexdigest()
    assert memory_refs.holds_content_identities(tmp_path, {rel: digest}) == {rel: True}


def test_backfill_refuses_a_page_changed_after_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Knowledge Base" / "Sources" / "legacy.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"---\r\ntype: source\r\n---\r\nOriginal.\r\n")
    changed = b"---\r\ntype: source\r\n---\r\nConcurrent edit.\r\n"
    insert = memory_refs.add_id_to_markdown

    def change_after_read(markdown):
        updated = insert(markdown)
        path.write_bytes(changed)
        return updated

    monkeypatch.setattr(memory_refs, "add_id_to_markdown", change_after_read)
    with pytest.raises(vault_module.ContentHashMismatchError):
        memory_refs.backfill_ids(tmp_path, dry_run=False)

    assert path.read_bytes() == changed


def test_recovery_refreshes_a_legacy_derived_hash_without_changing_the_page(tmp_path: Path) -> None:
    from exomem import lexstore

    path = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    path.parent.mkdir(parents=True)
    original = b"---\r\ntype: insight\r\n---\r\n- [fact] recovered token ^one\r\n"
    path.write_bytes(original)
    rel = path.relative_to(tmp_path).as_posix()
    store = lexstore.get_store(tmp_path)
    assert store.rebuild_atomic()
    legacy = hashlib.sha256(original.replace(b"\r\n", b"\n")).hexdigest()
    with store._connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM semantic_units WHERE parent_path = ?", (rel,)
        ).fetchone()[0] == 1
        conn.execute(
            "UPDATE semantic_units SET parent_source_hash = ? WHERE parent_path = ?",
            (legacy, rel),
        )

    assert store.upsert_paths([path])

    with store._connect() as conn:
        assert conn.execute(
            "SELECT parent_source_hash FROM semantic_units WHERE parent_path = ?", (rel,)
        ).fetchall() == [(hashlib.sha256(original).hexdigest(),)]
    assert path.read_bytes() == original


def test_saved_contract_hashes_and_overwrite_use_its_current_bytes(tmp_path: Path) -> None:
    from exomem import memory_schema

    contract = {"schema_version": 1, "name": "example"}
    saved = memory_schema.save_contract(tmp_path, contract)
    path = tmp_path / saved["path"]
    current = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(current)
    expected = hashlib.sha256(current).hexdigest()

    _contract, digest, _path = memory_schema.load_contract(tmp_path, "example")
    assert digest == expected
    assert memory_schema.load_saved_contracts(tmp_path)[0].content_hash == expected
    updated = memory_schema.save_contract(tmp_path, contract, expected_hash=expected)
    assert updated["previous_hash"] == expected
    assert updated["content_hash"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_saved_contract_refuses_drift_after_its_expected_hash_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import memory_schema

    contract = {"schema_version": 1, "name": "example"}
    saved = memory_schema.save_contract(tmp_path, contract)
    path = tmp_path / saved["path"]
    changed = path.read_bytes().replace(b"\n", b"\r\n")
    dump = memory_schema.yaml.safe_dump

    def change_before_render(*args, **kwargs):
        path.write_bytes(changed)
        return dump(*args, **kwargs)

    monkeypatch.setattr(memory_schema.yaml, "safe_dump", change_before_render)
    with pytest.raises(vault_module.ContentHashMismatchError):
        memory_schema.save_contract(tmp_path, contract, expected_hash=saved["content_hash"])

    assert path.read_bytes() == changed


def test_bounded_embedding_preparation_keeps_the_parent_byte_identity(tmp_path: Path) -> None:
    from exomem import find_corpus, runtime_resources

    path = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    path.parent.mkdir(parents=True)
    original = b"---\r\ntype: insight\r\n---\r\n- [fact] bounded token ^one\r\n"
    path.write_bytes(original)
    expected = hashlib.sha256(original).hexdigest()

    with runtime_resources.semantic_preparation(tmp_path, path, expected_hash=expected) as preparation:
        parsed = find_corpus.parse_page(
            path, path.stat().st_mtime, tmp_path, content=preparation.source.encode("utf-8")
        )
        state = semantic_index.current_parent_index_state(tmp_path, path, source=preparation.source)

        assert preparation.source.encode("utf-8") == original
        assert parsed is not None and parsed.snapshot_hash == expected
        assert state.parent_source_hash == expected
