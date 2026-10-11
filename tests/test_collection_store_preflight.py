"""The read-only migration preflight, maintain_memory(mode="collections-store", dry_run=true).

OpenSpec move-structured-collections-to-sqlite P1b.1-2 and design §10. Each case
names the defect only it catches; per-collection capture and proof details are
covered by test_collection_store_legacy*.py.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from record_fixtures import (
    LEDGER_COLLECTION_PATH,
    copy_dataset_fixture,
    ledger_item,
    setup_ledger_collection,
)
from test_planning_profile import _manifest as planning_manifest_text

from exomem import commands, record_formats, records, state_paths, writer_lease
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import authority, connection, legacy_import
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.init import init_vault

OWNER = owner_principal(surface="mcp")
GUEST = RequestPrincipal("principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:fixture")
EVIDENCE = "Knowledge Base/Evidence/import-a.md"
PLANNING = "Knowledge Base/Planning/Work/_collection.md"
BROKEN = "Knowledge Base/Records/Broken/_collection.md"


def call(root, principal=OWNER, **arguments):
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "maintain_memory")
    with request_scope(principal):
        return writer_lease.invoke_command(command, root, mode="collections-store", **arguments)


def ledger(root: Path, *, path=LEDGER_COLLECTION_PATH, batches=2) -> None:
    """A file Records collection with a filename recipe and a real audit chain, one event per row."""
    manifest_path = setup_ledger_collection(root)
    # Item files named by a filename recipe, as an owner-shaped collection's often are.
    manifest_path.write_text(manifest_path.read_text().replace(
        "storage:\n", "item_filename:\n  version: 1\n  fields: [slug]\nstorage:\n", 1))
    evidence = root / EVIDENCE
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("---\ntype: evidence\n---\n\nPreserved import.\n", encoding="utf-8")
    for batch in range(batches):
        manifest = collections.load_manifest(root, root / LEDGER_COLLECTION_PATH)
        records.bulk_upsert_records(
            root, LEDGER_COLLECTION_PATH, why="import a normalised export", source=EVIDENCE,
            rows=[{"item": ledger_item(slug=f"entry-{batch}-{index}")} for index in range(2)],
            expected_container_hash=record_formats.load_adapter(root, manifest).read().snapshot)
    if path != LEDGER_COLLECTION_PATH:
        # A folder copied with its history, as a person duplicating a collection would.
        shutil.copytree((root / LEDGER_COLLECTION_PATH).parent, (root / path).parent)


def vault(tmp_path) -> Path:
    root = tmp_path / "vault"
    init_vault(root)
    ledger(root)
    planning = root / PLANNING
    planning.parent.mkdir(parents=True)
    planning.write_text(planning_manifest_text())
    (planning.parent / "Items").mkdir()
    copy_dataset_fixture(root)
    broken = root / BROKEN
    broken.parent.mkdir(parents=True)
    broken.write_text("---\ntype: collection\n---\n")
    return root


def files(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def by_path(report) -> dict[str, dict]:
    return {row["path"]: row for row in report["collections"]}


def test_preflight_reports_every_collection_and_leaves_the_vault_untouched(tmp_path):
    """A preflight that writes a vault file, a store, a marker or leftover scratch; that drops
    an unreadable manifest or a dataset from its answer; or that reports a vault as migratable
    while a collection is blocked."""
    root = vault(tmp_path)
    before = files(root)
    report = call(root, dry_run=True)
    rows = by_path(report)
    assert report["verdict"] == "blocked" and report["blocked"] == [BROKEN]
    assert rows[BROKEN]["status"] == "blocked" and rows[BROKEN]["blocker"]["code"]
    assert {key: rows[LEDGER_COLLECTION_PATH][key] for key in ("status", "proof", "rows", "legacy_events",
                                                               "legacy_audit_status")} == {
        "status": "ready", "proof": "passed", "rows": 4, "legacy_events": 4, "legacy_audit_status": "ok"}
    assert {key: rows[PLANNING][key] for key in ("status", "proof", "rows", "semantic_profile")} == {
        "status": "ready", "proof": "passed", "rows": 0, "semantic_profile": "planning"}
    assert [row["status"] for row in report["collections"]
            if row["layout"] not in (None, "markdown-items", "markdown-log")] == ["skipped"]
    assert files(root) == before
    assert not authority.marker_path(root).exists() and not connection.store_path(root).exists()
    assert not list(state_paths.vault_state_dir(root).glob("collections-preflight-*"))
    with pytest.raises(ValueError, match="INVALID_ARGUMENTS"):
        call(root)


def test_a_failed_proof_names_its_collection_and_check(tmp_path, monkeypatch):
    """A preflight that hides which collection or which design §10 check failed, or that stops
    reporting the other collections after one fails."""
    root = vault(tmp_path)
    (root / BROKEN).unlink()

    def drift(captured, *args):
        legacy_import._refuse("b", "render/parse values, body, identity or natural key differ")

    monkeypatch.setattr(legacy_import, "_prove_round_trip", drift)
    report = call(root, dry_run=True)
    rows = by_path(report)
    assert report["verdict"] == "blocked" and report["blocked"] == [LEDGER_COLLECTION_PATH]
    assert rows[LEDGER_COLLECTION_PATH]["blocker"] == {
        "code": "COLLECTION_LEGACY_IMPORT_PROOF", "check": "b",
        "reason": "check(b): render/parse values, body, identity or natural key differ"}
    assert rows[PLANNING]["status"] == "ready"


def test_a_copied_collection_blocks_every_copy_of_its_identity(tmp_path):
    """A preflight that proves each collection alone and calls one copy ready although two
    folders carry one collection identity, which the store cannot hold."""
    root = tmp_path / "vault"
    init_vault(root)
    copy = "Knowledge Base/Records/Publications copy/_collection.md"
    ledger(root, path=copy, batches=1)
    report = call(root, dry_run=True)
    rows = by_path(report)
    assert report["verdict"] == "blocked" and report["blocked"] == sorted([copy, LEDGER_COLLECTION_PATH])
    assert {rows[path]["blocker"]["code"] for path in report["blocked"]} == {"AMBIGUOUS_COLLECTION"}


def test_a_non_owner_is_refused_before_any_collection_is_read(tmp_path, monkeypatch):
    """A preflight that tells a guest which collections exist or which ones are blocked."""
    root = vault(tmp_path)
    read = []
    monkeypatch.setattr(collections, "discover_collections_with_errors",
                        lambda *args, **kwargs: read.append(args) or ((), ()))
    with pytest.raises((OpError, CollectionStoreError), match="OWNER_REQUIRED|owner"):
        call(root, GUEST, dry_run=True)
    assert read == []
