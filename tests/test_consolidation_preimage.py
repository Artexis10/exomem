from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.governance import consolidation_fingerprints, consolidation_intake
from exomem.governance.consolidation_identity import ConsolidationCellIdentity

RUN_ID = "00000000-0000-4000-8000-000000000081"
OPERATION_ID = "00000000-0000-4000-8000-000000000082"
_D1 = hashlib.sha256(b"preimage-one").hexdigest()
_D2 = hashlib.sha256(b"preimage-two").hexdigest()
_D3 = hashlib.sha256(b"preimage-three").hexdigest()
_D4 = hashlib.sha256(b"preimage-four").hexdigest()
_ACTIVE_POLICY = b"governance_version: 1\n"


def _module():
    name = "exomem.governance.consolidation_preimage"
    assert importlib.util.find_spec(name) is not None, "destination preimage module is missing"
    return importlib.import_module(name)


def _identity(tmp_path: Path) -> ConsolidationCellIdentity:
    return ConsolidationCellIdentity(
        schema="exomem.consolidation-cell-identity/v1",
        cell_id="cell-destination-01",
        vault_id="vault-destination-01",
        installation_id="installation-destination-01",
        installation_generation=3,
        active_fence_digest=_D1,
        root_binding_id="attachment-destination-01",
        root_binding_digest=_D2,
        machine_key_id="key-destination-01",
        adoption_census_digest=_D3,
        clone_of_vault_id=None,
        clone_of_installation_id=None,
        clone_of_snapshot_digest=None,
        created_at=1_777_777_777,
        authentication_algorithm="HMAC-SHA256",
        record_digest=_D4,
        identity_path=tmp_path / "identity.json",
    )


def _write_destination(vault: Path) -> None:
    files: dict[str, bytes] = {
        "Knowledge Base/Notes/insight.md": (
            b"---\ntype: insight\nid: insight-1\n"
            b"citations:\n  - '[[Knowledge Base/Sources/raw]]'\n"
            b"relations:\n  supports:\n    - '[[Knowledge Base/Evidence/case.bin]]'\n"
            b"history:\n  - created\n---\n# Insight\nCanonical metadata survives.\n"
        ),
        "Knowledge Base/Notes/caf\u00e9.md": b"NFC path\n",
        "Knowledge Base/Notes/empty.bin": b"",
        "Knowledge Base/Notes/blob.bin": b"\x00\xffbinary\r\n",
        "Knowledge Base/Sources/raw.md": b"raw source\n",
        "Knowledge Base/Evidence/case.bin": b"evidence\x00bytes",
        "Knowledge Base/Records/events/item.json": b'{"id":"event-1"}',
        "Knowledge Base/Media/image.png": b"\x89PNG\r\n\x1a\n",
        "Knowledge Base/Media/image.png.md": b"---\ntype: source\n---\nmedia sidecar\n",
        "Knowledge Base/_access.yaml": b"readonly: []\n",
        "Knowledge Base/.review-state.json": b'{"version":2,"records":{}}',
        # These are control/derived state and must never enter the preimage.
        "Knowledge Base/_Consolidation/runs/older/state.json": b"older run",
        "Knowledge Base/_Governance/pending.yaml": b"unapproved pending policy",
        "Knowledge Base/.graph-commit-receipts/0123456789abcdef01234567.json": b"receipt",
        "Knowledge Base/.graph.sqlite": b"derived graph",
    }
    for relative, content in files.items():
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _patch_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        consolidation_fingerprints,
        "load_local_identity",
        lambda *_args, **_kwargs: _identity(tmp_path),
    )
    monkeypatch.setattr(
        consolidation_fingerprints,
        "_load_active_policy_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            active=SimpleNamespace(
                logical_vault_id="vault-destination-01",
                policy_fingerprint=_D1,
            ),
            source_documents=(("rules/default.yaml", _ACTIVE_POLICY),),
        ),
    )


def _prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = _module()
    vault = tmp_path / "vault"
    _write_destination(vault)
    _patch_destination(monkeypatch, tmp_path)
    snapshot = consolidation_fingerprints.load_local_destination_snapshot(vault, now=123)
    store = consolidation_intake.PrivateConsolidationArtifactStore(
        tmp_path / "private-artifacts",
        active_vault_roots=(vault,),
    )
    binding = module.DestinationPreimageBinding(
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        plan_digest=_D2,
        control_basis_digest=_D3,
        semantic_predecessor_event_id=f"{_D4}:committed",
        semantic_predecessor_digest=_D1,
        destination_snapshot_fingerprint=snapshot.digest,
        destination_census_digest=snapshot.canonical_census_digest,
    )
    return module, vault, store, binding


def test_materializes_every_canonical_destination_byte_and_publishes_manifest_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, vault, store, binding = _prepared(tmp_path, monkeypatch)

    result = module.materialize_local_destination_preimage(
        vault,
        binding=binding,
        artifact_store=store,
        now=123,
    )
    verified = module.verify_destination_preimage(
        result.manifest_ref,
        binding=binding,
        artifact_store=store,
    )

    assert verified == result
    assert result.schema == "exomem.consolidation-destination-preimage/v1"
    assert result.destination_census_digest == binding.destination_census_digest
    paths = {entry.path for entry in result.entries}
    assert {
        "Knowledge Base/Notes/insight.md",
        "Knowledge Base/Notes/empty.bin",
        "Knowledge Base/Notes/blob.bin",
        "Knowledge Base/Notes/caf\u00e9.md",
        "Knowledge Base/Sources/raw.md",
        "Knowledge Base/Evidence/case.bin",
        "Knowledge Base/Records/events/item.json",
        "Knowledge Base/Media/image.png",
        "Knowledge Base/Media/image.png.md",
        "Knowledge Base/_access.yaml",
        "Knowledge Base/.review-state.json",
        "Knowledge Base/_Governance/rules/default.yaml",
    } <= paths
    assert all("_Consolidation/" not in path for path in paths)
    assert all(".graph-commit-receipts/" not in path for path in paths)
    assert "Knowledge Base/.graph.sqlite" not in paths

    policy = next(
        entry
        for entry in result.entries
        if entry.path == "Knowledge Base/_Governance/rules/default.yaml"
    )
    assert store.resolve_object(policy.artifact_ref).read_bytes() == _ACTIVE_POLICY
    assert result.entry_count == len(result.entries)
    assert result.total_bytes == sum(entry.size for entry in result.entries)


def test_preimage_objects_are_independent_of_later_destination_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, vault, store, binding = _prepared(tmp_path, monkeypatch)
    original = (vault / "Knowledge Base/Notes/insight.md").read_bytes()
    result = module.materialize_local_destination_preimage(
        vault,
        binding=binding,
        artifact_store=store,
        now=123,
    )
    entry = next(
        item for item in result.entries if item.path == "Knowledge Base/Notes/insight.md"
    )

    (vault / entry.path).write_bytes(b"changed after materialization")

    assert store.resolve_object(entry.artifact_ref).read_bytes() == original
    assert module.verify_destination_preimage(
        result.manifest_ref,
        binding=binding,
        artifact_store=store,
    ) == result


def test_stale_snapshot_refuses_before_private_artifact_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, vault, store, binding = _prepared(tmp_path, monkeypatch)
    (vault / "Knowledge Base/Notes/insight.md").write_bytes(b"drifted")

    with pytest.raises(module.ConsolidationPreimageUnavailable):
        module.materialize_local_destination_preimage(
            vault,
            binding=binding,
            artifact_store=store,
            now=123,
        )

    assert not store.root.exists()


@pytest.mark.parametrize("resource_case", ["quota", "space"])
def test_capacity_refusal_precedes_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_case: str,
) -> None:
    module, vault, store, binding = _prepared(tmp_path, monkeypatch)
    limits = module.DestinationPreimageLimits(
        max_files=100_000,
        max_total_bytes=1 if resource_case == "quota" else (1 << 40),
        minimum_free_bytes=0,
    )
    if resource_case == "space":
        monkeypatch.setattr(module, "_available_bytes", lambda _path: 0)

    with pytest.raises(module.ConsolidationPreimageUnavailable):
        module.materialize_local_destination_preimage(
            vault,
            binding=binding,
            artifact_store=store,
            now=123,
            resource_limits=limits,
        )

    assert not store.root.exists()


def test_unsafe_link_or_changed_path_refuses_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, vault, store, binding = _prepared(tmp_path, monkeypatch)
    target = vault / "Knowledge Base/Notes/insight.md"
    target.unlink()
    target.symlink_to(vault / "Knowledge Base/Sources/raw.md")

    with pytest.raises(module.ConsolidationPreimageUnavailable):
        module.materialize_local_destination_preimage(
            vault,
            binding=binding,
            artifact_store=store,
            now=123,
        )

    assert not (store.root / "preimages").exists()


@pytest.mark.parametrize("damage", ["missing-object", "changed-object", "manifest"])
def test_verification_refuses_lost_partial_or_mismatched_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    module, vault, store, binding = _prepared(tmp_path, monkeypatch)
    result = module.materialize_local_destination_preimage(
        vault,
        binding=binding,
        artifact_store=store,
        now=123,
    )
    if damage == "manifest":
        store.resolve_preimage(result.manifest_ref).write_bytes(b"{}")
    else:
        object_path = store.resolve_object(result.entries[0].artifact_ref)
        if damage == "missing-object":
            object_path.unlink()
        else:
            original = object_path.read_bytes()
            replacement = (b"x" if original[:1] != b"x" else b"y") + original[1:]
            object_path.write_bytes(replacement)

    with pytest.raises(module.ConsolidationPreimageUnavailable):
        module.verify_destination_preimage(
            result.manifest_ref,
            binding=binding,
            artifact_store=store,
        )


def test_control_and_receipt_churn_does_not_change_preimage_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, vault, store, binding = _prepared(tmp_path, monkeypatch)
    first = module.materialize_local_destination_preimage(
        vault,
        binding=binding,
        artifact_store=store,
        now=123,
    )
    (vault / "Knowledge Base/_Consolidation/runs/older/state.json").write_bytes(
        b"new control state"
    )
    (vault / "Knowledge Base/.graph-commit-receipts/0123456789abcdef01234567.json").write_bytes(
        b"new physical receipt head"
    )

    replay = module.materialize_local_destination_preimage(
        vault,
        binding=binding,
        artifact_store=store,
        now=123,
    )

    assert replay == first
