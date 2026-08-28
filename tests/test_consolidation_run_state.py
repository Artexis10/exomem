"""Durable owner-only control state for governed consolidation runs."""

from __future__ import annotations

import importlib
import inspect
import itertools
import os
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import writer_lease
from exomem.governance.consolidation_intake import ConsolidationInventoryItem

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
START_OPERATION_ID = "123e4567-e89b-42d3-a456-426614174001"
CREATED_AT = "2026-08-28T12:00:00.000Z"
UPDATED_AT = "2026-08-28T12:01:00.000Z"


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)


def _run_state():
    try:
        return importlib.import_module("exomem.governance.consolidation_run_state")
    except ModuleNotFoundError:
        pytest.fail("the durable consolidation run store is missing")


def _identity(module):
    return module.ConsolidationRunIdentity(
        run_id=RUN_ID,
        start_operation_id=START_OPERATION_ID,
        run_mode="cloned-rehearsal",
        destination_vault_id="vault-destination-01",
        destination_installation_id="installation-destination-01",
        destination_generation=3,
        destination_fence_digest="1" * 64,
        destination_identity_binding_digest="2" * 64,
        source_artifact_ref="exomem-export://sha256/" + "3" * 64,
        source_attestation_ref="exomem-source-attestation://sha256/" + "4" * 64,
        archive_sha256="3" * 64,
        manifest_sha256="5" * 64,
        source_census_sha256="6" * 64,
        source_proof_digest="4" * 64,
        created_at=CREATED_AT,
    )


def _inventory(count: int = 5) -> tuple[ConsolidationInventoryItem, ...]:
    return tuple(
        ConsolidationInventoryItem(
            path=f"Knowledge Base/Notes/item-{index:04d}.md",
            size=20 + index,
            sha256=f"{index + 10:064x}",
            classification="canonical",
            artifact_ref=f"exomem-consolidation-object://sha256/{index + 10:064x}",
        )
        for index in range(count)
    )


def _store(module, vault: Path):
    return module.ConsolidationRunStore(vault)


def test_run_identity_schema_has_no_caller_or_private_body_fields() -> None:
    module = _run_state()
    fields = tuple(inspect.signature(module.ConsolidationRunIdentity).parameters)

    assert fields == (
        "run_id",
        "start_operation_id",
        "run_mode",
        "destination_vault_id",
        "destination_installation_id",
        "destination_generation",
        "destination_fence_digest",
        "destination_identity_binding_digest",
        "source_artifact_ref",
        "source_attestation_ref",
        "archive_sha256",
        "manifest_sha256",
        "source_census_sha256",
        "source_proof_digest",
        "created_at",
    )
    for forbidden in (
        "source_root",
        "archive_path",
        "artifact_path",
        "source_body",
        "conflict_text",
        "policy_body",
        "principal_id",
        "credential",
        "archive_bytes",
        "rollback_preimage",
    ):
        assert forbidden not in fields


def test_create_persists_private_canonical_state_and_restarts(tmp_path: Path) -> None:
    module = _run_state()
    vault = tmp_path / "vault"
    record = _store(module, vault).create(_identity(module), _inventory())

    assert record.run_id == RUN_ID
    assert record.revision == 1
    assert record.phase == "intake-complete"
    assert record.inventory_count == 5
    run_dir = vault / "Knowledge Base" / "_Consolidation" / "runs" / RUN_ID
    assert sorted(path.name for path in run_dir.iterdir()) == [
        "inventory.json",
        "run.json",
    ]
    if os.name != "nt":
        assert run_dir.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in run_dir.iterdir())

    restarted = _store(module, vault).load(RUN_ID)
    assert restarted == record


def test_create_is_idempotent_but_conflicting_identity_or_inventory_refuses(
    tmp_path: Path,
) -> None:
    module = _run_state()
    store = _store(module, tmp_path / "vault")
    identity = _identity(module)
    first = store.create(identity, _inventory())

    assert store.create(identity, tuple(reversed(_inventory()))) == first
    for conflicting_identity, inventory in (
        (replace(identity, source_census_sha256="8" * 64), _inventory()),
        (identity, _inventory(6)),
    ):
        with pytest.raises(module.ConsolidationRunUnavailable) as caught:
            store.create(conflicting_identity, inventory)
        assert caught.value.code == "RUN_ID_CONFLICT"
    assert store.load(RUN_ID) == first


def test_revision_compare_and_swap_survives_restart(tmp_path: Path) -> None:
    module = _run_state()
    vault = tmp_path / "vault"
    first_store = _store(module, vault)
    first_store.create(_identity(module), _inventory())

    second_store = _store(module, vault)
    updated = second_store.update_phase(
        RUN_ID,
        expected_revision=1,
        phase="reconciling",
        updated_at=UPDATED_AT,
    )
    assert updated.revision == 2
    assert updated.phase == "reconciling"

    with pytest.raises(module.ConsolidationRunUnavailable) as caught:
        first_store.update_phase(
            RUN_ID,
            expected_revision=1,
            phase="reconciled",
            updated_at="2026-08-28T12:02:00.000Z",
        )
    assert caught.value.code == "RUN_REVISION_CONFLICT"
    assert _store(module, vault).load(RUN_ID) == updated


def test_inventory_pages_are_deterministic_bounded_and_permutation_stable(
    tmp_path: Path,
) -> None:
    module = _run_state()
    rows = _inventory(451)
    observed: list[tuple[str, tuple[str, ...]]] = []
    for ordinal, permutation in enumerate((rows, tuple(reversed(rows)))):
        store = _store(module, tmp_path / f"vault-{ordinal}")
        record = store.create(_identity(module), permutation)
        cursor = None
        paths: list[str] = []
        while True:
            page = store.page_inventory(RUN_ID, cursor=cursor, limit=200)
            assert len(page.items) <= 200
            assert page.inventory_digest == record.inventory_digest
            paths.extend(item.path for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        observed.append((record.inventory_digest, tuple(paths)))

    assert observed[0] == observed[1]
    assert observed[0][1] == tuple(sorted(item.path for item in rows))

    store = _store(module, tmp_path / "vault-0")
    for limit in (0, 201):
        with pytest.raises(module.ConsolidationRunUnavailable):
            store.page_inventory(RUN_ID, cursor=None, limit=limit)
    with pytest.raises(module.ConsolidationRunUnavailable):
        store.page_inventory(RUN_ID, cursor="caller-selected-offset", limit=3)


@pytest.mark.parametrize(
    "bad_item",
    (
        ConsolidationInventoryItem(
            path="/private/source.md",
            size=1,
            sha256="a" * 64,
            classification="canonical",
            artifact_ref="exomem-consolidation-object://sha256/" + "a" * 64,
        ),
        ConsolidationInventoryItem(
            path="Knowledge Base/../secret.md",
            size=1,
            sha256="a" * 64,
            classification="canonical",
            artifact_ref="exomem-consolidation-object://sha256/" + "a" * 64,
        ),
        ConsolidationInventoryItem(
            path="Knowledge Base/Notes/safe.md",
            size=1,
            sha256="a" * 64,
            classification="canonical",
            artifact_ref="/private/artifact.bin",
        ),
    ),
)
def test_run_state_rejects_absolute_or_nonopaque_inventory_facts(
    tmp_path: Path,
    bad_item: ConsolidationInventoryItem,
) -> None:
    module = _run_state()
    with pytest.raises(module.ConsolidationRunUnavailable) as caught:
        _store(module, tmp_path / "vault").create(_identity(module), (bad_item,))
    assert caught.value.code == "RUN_INPUT_INVALID"


def test_run_files_never_persist_bodies_credentials_or_private_paths(
    tmp_path: Path,
) -> None:
    module = _run_state()
    vault = tmp_path / "vault"
    store = _store(module, vault)
    store.create(_identity(module), _inventory(20))
    store.update_phase(
        RUN_ID,
        expected_revision=1,
        phase="reconciling",
        updated_at=UPDATED_AT,
    )

    persisted = b"\n".join(
        path.read_bytes()
        for path in sorted(
            (vault / "Knowledge Base" / "_Consolidation").rglob("*")
        )
        if path.is_file()
    )
    for forbidden in (
        str(tmp_path).encode(),
        b"source-only sentinel",
        b"conflict explanation",
        b"policy: allow-everything",
        b"principal@example.test",
        b"Bearer credential",
        b"rollback preimage body",
        b"PK\x03\x04",
    ):
        assert forbidden not in persisted


def test_missing_inventory_and_tampered_run_fail_closed_without_repair(
    tmp_path: Path,
) -> None:
    module = _run_state()
    vault = tmp_path / "vault"
    store = _store(module, vault)
    store.create(_identity(module), _inventory())
    run_dir = vault / "Knowledge Base" / "_Consolidation" / "runs" / RUN_ID

    inventory = run_dir / "inventory.json"
    inventory_before = inventory.read_bytes()
    inventory.unlink()
    with pytest.raises(module.ConsolidationRunUnavailable) as missing:
        _store(module, vault).load(RUN_ID)
    assert missing.value.code == "RUN_STATE_CORRUPT"

    # Restore through an exact create is forbidden while the canonical run exists.
    with pytest.raises(module.ConsolidationRunUnavailable):
        store.create(_identity(module), _inventory())

    inventory.write_bytes(inventory_before)
    run_file = run_dir / "run.json"
    before = run_file.read_bytes()
    run_file.write_bytes(before.replace(b'"revision":1', b'"revision":"invalid"'))
    tampered = run_file.read_bytes()
    with pytest.raises(module.ConsolidationRunUnavailable) as corrupt:
        store.update_phase(
            RUN_ID,
            expected_revision=1,
            phase="reconciling",
            updated_at=UPDATED_AT,
        )
    assert corrupt.value.code == "RUN_STATE_CORRUPT"
    assert run_file.read_bytes() == tampered


def test_inventory_resource_admission_precedes_run_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _run_state()
    monkeypatch.setattr(module, "_MAX_INVENTORY_ITEMS", 2)
    vault = tmp_path / "vault"

    with pytest.raises(module.ConsolidationRunUnavailable) as caught:
        _store(module, vault).create(_identity(module), _inventory(3))
    assert caught.value.code == "RUN_RESOURCE_LIMIT"
    assert not (vault / "Knowledge Base" / "_Consolidation").exists()


def test_inventory_order_has_one_canonical_encoding(tmp_path: Path) -> None:
    module = _run_state()
    rows = _inventory(4)
    encodings: list[bytes] = []
    for ordinal, permutation in enumerate(itertools.permutations(rows)):
        vault = tmp_path / f"vault-{ordinal}"
        _store(module, vault).create(_identity(module), tuple(permutation))
        encodings.append(
            (
                vault
                / "Knowledge Base"
                / "_Consolidation"
                / "runs"
                / RUN_ID
                / "inventory.json"
            ).read_bytes()
        )
    assert len(set(encodings)) == 1
