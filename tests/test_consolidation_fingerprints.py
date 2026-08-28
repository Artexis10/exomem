from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.governance import consolidation_attestation, consolidation_fingerprints
from exomem.governance.consolidation_identity import ConsolidationCellIdentity

_D1 = "1" * 64
_D2 = "2" * 64
_D3 = "3" * 64
_D4 = "4" * 64
_D5 = "5" * 64
_D6 = "6" * 64
_D7 = "7" * 64
_D8 = "8" * 64
_D9 = "9" * 64


def _claims() -> dict[str, object]:
    return {
        "schema": consolidation_attestation.ATTESTATION_SCHEMA,
        "source_vault_id": "vault-source-01",
        "source_installation_id": "installation-source-01",
        "source_installation_generation": 7,
        "source_active_fence_digest": _D1,
        "source_identity_binding_digest": _D2,
        "export_operation_id": "export-operation-01",
        "quiescence_checkpoint_digest": _D2,
        "archive_sha256": _D3,
        "manifest_sha256": _D4,
        "source_census_sha256": _D5,
        "issued_at": "2026-08-28T09:45:00.000Z",
        "expires_at": "2026-08-28T10:45:00.000Z",
        "signer_key_id": f"ed25519-sha256:{_D6}",
    }


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


def _entries() -> tuple[consolidation_fingerprints.CanonicalCensusEntry, ...]:
    return (
        consolidation_fingerprints.CanonicalCensusEntry(
            path="Knowledge Base/Notes/alpha.md",
            entry_type="file",
            size=5,
            sha256=hashlib.sha256(b"alpha").hexdigest(),
        ),
        consolidation_fingerprints.CanonicalCensusEntry(
            path="Knowledge Base/Sources/\u00e9vidence.md",
            entry_type="file",
            size=8,
            sha256=hashlib.sha256("\u00e9vidence".encode()).hexdigest(),
        ),
    )


def test_source_fingerprint_has_a_fixed_closed_jcs_vector() -> None:
    result = consolidation_fingerprints.source_fingerprint(
        _claims(), authentication_proof_digest=_D7
    )

    assert result.schema == "exomem.consolidation-source-fingerprint/v1"
    assert result.authentication_proof_digest == _D7
    assert result.digest == "733289ad12b5f27c97ff2ed4853bc6f638174888db9b7f0f988b973f1081fc54"
    with pytest.raises(TypeError):
        result.verified_claims["source_vault_id"] = "vault-other"  # type: ignore[index]

    with pytest.raises(consolidation_fingerprints.ConsolidationFingerprintUnavailable):
        consolidation_fingerprints.source_fingerprint(
            {**_claims(), "caller_extension": "not-closed"},
            authentication_proof_digest=_D7,
        )


def test_canonical_census_is_permutation_stable_and_change_sensitive() -> None:
    entries = _entries()
    first = consolidation_fingerprints.canonical_content_census(entries)
    reversed_result = consolidation_fingerprints.canonical_content_census(
        tuple(reversed(entries))
    )

    assert first.entries == tuple(sorted(entries, key=lambda item: item.path))
    assert reversed_result == first
    assert first.digest == "0528d803a65b055ebe45d46936d898e230f28d57de6fa58c8ec4caa0f8331a5c"
    with pytest.raises(consolidation_fingerprints.ConsolidationFingerprintUnavailable):
        consolidation_fingerprints.canonical_content_census(
            (replace(entries[0], entry_type="symlink"), entries[1])
        )
    assert consolidation_fingerprints.canonical_content_census(
        (replace(entries[0], path="Knowledge Base/Notes/renamed.md"), entries[1])
    ).digest != first.digest
    assert consolidation_fingerprints.canonical_content_census(
        (replace(entries[0], sha256=_D8), entries[1])
    ).digest != first.digest


def test_source_content_census_excludes_receipts_but_keeps_review_state() -> None:
    manifest = {
        "files": [
            {
                "path": "Knowledge Base/.graph-commit-receipts/0123456789abcdef01234567.json",
                "size": 7,
                "sha256": _D1,
                "classification": "portable-derived",
            },
            {
                "path": "Knowledge Base/.review-state.json",
                "size": 8,
                "sha256": _D2,
                "classification": "portable-derived",
            },
            {
                "path": "Knowledge Base/Notes/alpha.md",
                "size": 9,
                "sha256": _D3,
                "classification": "canonical",
            },
        ]
    }
    baseline = consolidation_fingerprints.source_content_census_from_manifest(manifest)
    receipt_changed = {
        "files": [{**manifest["files"][0], "sha256": _D9}, *manifest["files"][1:]]
    }
    review_changed = {
        "files": [manifest["files"][0], {**manifest["files"][1], "sha256": _D9}, manifest["files"][2]]
    }

    assert tuple(item.path for item in baseline.entries) == (
        "Knowledge Base/.review-state.json",
        "Knowledge Base/Notes/alpha.md",
    )
    assert consolidation_fingerprints.source_content_census_from_manifest(
        receipt_changed
    ) == baseline
    assert consolidation_fingerprints.source_content_census_from_manifest(
        review_changed
    ).digest != baseline.digest


@pytest.mark.parametrize(
    "entries",
    [
        (
            consolidation_fingerprints.CanonicalCensusEntry(
                path="Knowledge Base/Notes/../secret.md",
                entry_type="file",
                size=1,
                sha256=_D1,
            ),
        ),
        (
            consolidation_fingerprints.CanonicalCensusEntry(
                path="Knowledge Base/Notes/a.md",
                entry_type="file",
                size=1,
                sha256=_D1,
            ),
            consolidation_fingerprints.CanonicalCensusEntry(
                path="Knowledge Base/Notes/a.md",
                entry_type="file",
                size=1,
                sha256=_D1,
            ),
        ),
    ],
)
def test_canonical_census_refuses_unsafe_or_duplicate_entries(
    entries: tuple[consolidation_fingerprints.CanonicalCensusEntry, ...],
) -> None:
    with pytest.raises(consolidation_fingerprints.ConsolidationFingerprintUnavailable):
        consolidation_fingerprints.canonical_content_census(entries)


def test_destination_snapshot_has_a_fixed_vector_and_binds_identity(
    tmp_path: Path,
) -> None:
    identity = _identity(tmp_path)
    census = consolidation_fingerprints.canonical_content_census(_entries())
    result = consolidation_fingerprints.destination_snapshot(
        identity,
        census=census,
        active_policy_fingerprint=_D6,
        access_state_fingerprint=_D7,
        review_state_fingerprint=_D8,
    )

    assert result.schema == "exomem.consolidation-destination-snapshot/v1"
    assert result.identity_root_binding_fingerprint == identity.record_digest
    assert result.digest == "60ac7cec68f8766953f908853eb935abb7e86fbc88e4c4c521b402a72ae0c520"

    for changed in (
        replace(identity, vault_id="vault-destination-02"),
        replace(identity, installation_id="installation-destination-02"),
        replace(identity, installation_generation=4),
        replace(identity, active_fence_digest=_D9),
        replace(identity, record_digest=_D9),
    ):
        assert consolidation_fingerprints.destination_snapshot(
            changed,
            census=census,
            active_policy_fingerprint=_D6,
            access_state_fingerprint=_D7,
            review_state_fingerprint=_D8,
        ).digest != result.digest


def test_guarded_local_snapshot_repeats_every_trusted_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = consolidation_fingerprints._DestinationSample(
        identity=_identity(tmp_path),
        entries=_entries(),
        active_policy_fingerprint=_D6,
        access_state_fingerprint=_D7,
        review_state_fingerprint=_D8,
    )
    calls: list[int] = []

    def stable(_vault: Path, *, now: int, limits: object) -> object:
        calls.append(now)
        return sample

    monkeypatch.setattr(consolidation_fingerprints, "_sample_local_destination", stable)
    result = consolidation_fingerprints.load_local_destination_snapshot(
        tmp_path / "vault", now=123
    )

    assert calls == [123, 123]
    assert result.canonical_census_digest == consolidation_fingerprints.canonical_content_census(
        sample.entries
    ).digest

    samples = iter((sample, replace(sample, review_state_fingerprint=_D9)))
    monkeypatch.setattr(
        consolidation_fingerprints,
        "_sample_local_destination",
        lambda *_args, **_kwargs: next(samples),
    )
    with pytest.raises(consolidation_fingerprints.ConsolidationFingerprintUnavailable):
        consolidation_fingerprints.load_local_destination_snapshot(
            tmp_path / "vault", now=123
        )


def test_local_snapshot_excludes_control_churn_but_binds_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base/Notes"
    notes.mkdir(parents=True)
    (notes / "alpha.md").write_text("alpha\n", encoding="utf-8")
    (vault / "Knowledge Base/Sources").mkdir()
    (vault / "Knowledge Base/Sources/evidence.md").write_text(
        "evidence\n", encoding="utf-8"
    )
    (vault / "Knowledge Base/_access.yaml").write_text(
        "readonly: []\n", encoding="utf-8"
    )
    (vault / "Knowledge Base/.review-state.json").write_text(
        '{"version":2,"records":{}}', encoding="utf-8"
    )
    policy_documents = (("rules/default.yaml", b"governance_version: 1\n"),)
    policy_fingerprint = _D6

    monkeypatch.setattr(
        consolidation_fingerprints,
        "load_local_identity",
        lambda *_args, **_kwargs: _identity(tmp_path),
    )

    def active(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            active=SimpleNamespace(
                logical_vault_id="vault-destination-01",
                policy_fingerprint=policy_fingerprint,
            ),
            source_documents=policy_documents,
        )

    monkeypatch.setattr(
        consolidation_fingerprints, "_load_active_policy_snapshot", active
    )

    baseline = consolidation_fingerprints.load_local_destination_snapshot(vault, now=123)
    source_baseline = consolidation_fingerprints.load_local_source_content_census(vault)
    assert baseline.access_state_fingerprint == hashlib.sha256(b"readonly: []\n").hexdigest()
    assert baseline.review_state_fingerprint == hashlib.sha256(
        b'{"version":2,"records":{}}'
    ).hexdigest()
    (vault / "Knowledge Base/_Consolidation/runs/run-1").mkdir(parents=True)
    (vault / "Knowledge Base/_Consolidation/runs/run-1/state.json").write_text(
        "matching alpha secret", encoding="utf-8"
    )
    (vault / "Knowledge Base/_Governance/events").mkdir(parents=True)
    (vault / "Knowledge Base/_Governance/events/receipt.json").write_text(
        "receipt churn", encoding="utf-8"
    )
    (vault / "Knowledge Base/.graph.sqlite").write_bytes(b"derived")
    assert (
        consolidation_fingerprints.load_local_destination_snapshot(vault, now=123)
        == baseline
    )
    assert consolidation_fingerprints.load_local_source_content_census(
        vault
    ) == source_baseline

    (notes / "alpha.md").write_text("changed alpha\n", encoding="utf-8")
    assert consolidation_fingerprints.load_local_destination_snapshot(
        vault, now=123
    ).digest != baseline.digest
    assert consolidation_fingerprints.load_local_source_content_census(
        vault
    ).digest != source_baseline.digest
    (notes / "alpha.md").write_text("alpha\n", encoding="utf-8")

    (vault / "Knowledge Base/_access.yaml").write_text(
        "readonly:\n  - external\n", encoding="utf-8"
    )
    assert consolidation_fingerprints.load_local_destination_snapshot(
        vault, now=123
    ).digest != baseline.digest
    (vault / "Knowledge Base/_access.yaml").write_text(
        "readonly: []\n", encoding="utf-8"
    )

    (vault / "Knowledge Base/.review-state.json").write_text(
        '{"version":2,"records":{"a":{}}}', encoding="utf-8"
    )
    assert consolidation_fingerprints.load_local_destination_snapshot(
        vault, now=123
    ).digest != baseline.digest
    (vault / "Knowledge Base/.review-state.json").write_text(
        '{"version":2,"records":{}}', encoding="utf-8"
    )

    policy_documents = (("rules/default.yaml", b"governance_version: 2\n"),)
    policy_fingerprint = _D9
    assert consolidation_fingerprints.load_local_destination_snapshot(
        vault, now=123
    ).digest != baseline.digest


def test_custom_kb_review_state_stales_source_and_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
    vault = tmp_path / "vault"
    notes = vault / "Brain/Notes"
    notes.mkdir(parents=True)
    (notes / "alpha.md").write_text("alpha\n", encoding="utf-8")
    review = vault / "Brain/.review-state.json"
    review.write_text('{"version":2,"records":{}}', encoding="utf-8")
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
                policy_fingerprint=_D6,
            ),
            source_documents=(("rules/default.yaml", b"governance_version: 1\n"),),
        ),
    )

    source_before = consolidation_fingerprints.load_local_source_content_census(vault)
    destination_before = consolidation_fingerprints.load_local_destination_snapshot(
        vault, now=123
    )
    review.write_text(
        '{"version":2,"records":{"changed":{}}}', encoding="utf-8"
    )
    source_after = consolidation_fingerprints.load_local_source_content_census(vault)
    destination_after = consolidation_fingerprints.load_local_destination_snapshot(
        vault, now=123
    )

    assert "Brain/.review-state.json" in {item.path for item in source_before.entries}
    assert source_after.digest != source_before.digest
    assert (
        destination_after.review_state_fingerprint
        != destination_before.review_state_fingerprint
    )
    assert destination_after.digest != destination_before.digest
