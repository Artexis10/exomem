"""Immutable, plan-bound contracts for consolidation verification probes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

RUN_ID = "00000000-0000-4000-8000-000000000091"
PLAN_DIGEST = hashlib.sha256(b"verification-manifest-plan").hexdigest()
ATTESTATION_FINGERPRINT = hashlib.sha256(b"verification-manifest-attestation").hexdigest()


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import writer_lease

    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner_contract() -> dict[str, object]:
    return {
        "probe_id": "owner-note-full",
        "executor_id": "canonical-governance-surface-v1",
        "surface": "rest",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "owner-verification",
        "command_name": "read_memory",
        "arguments": {
            "path": "Knowledge Base/Notes/destination.md",
            "include_raw": True,
        },
        "expected_result_digest": _digest("owner-note-full:wire"),
    }


def _delegated_contract() -> dict[str, object]:
    return {
        "probe_id": "delegated-approved-projection",
        "executor_id": "canonical-governance-surface-v1",
        "surface": "rest",
        "principal_kind": "delegated",
        "principal_id": "external",
        "principal_attestation_fingerprint": ATTESTATION_FINGERPRINT,
        "purpose": "support",
        "command_name": "ask_memory",
        "arguments": {
            "query": "approved compiled abstraction",
            "mode": "keyword",
            "graph": False,
            "rerank": False,
            "limit": 10,
        },
        "expected_result_digest": _digest("delegated-approved-projection:wire"),
    }


def _graph_contract() -> dict[str, object]:
    return {
        **_delegated_contract(),
        "probe_id": "delegated-approved-graph",
        "command_name": "connect_memory",
        "arguments": {
            "operation": "graph-context",
            "path": "Knowledge Base/Notes/destination.md",
            "include_model_suggestions": False,
            "depth": 1,
        },
        "expected_result_digest": _digest("delegated-approved-graph:wire"),
    }


def _owner_keyword_contract() -> dict[str, object]:
    contract = {
        **_delegated_contract(),
        "probe_id": "owner-keyword",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "owner-verification",
        "expected_result_digest": _digest("owner-keyword:wire"),
    }
    contract.pop("principal_attestation_fingerprint")
    return contract


def _negative_contract() -> dict[str, object]:
    return {
        **_delegated_contract(),
        "probe_id": "delegated-private-body-absent",
        "command_name": "read_memory",
        "arguments": {
            "path": "Knowledge Base/Notes/private.md",
            "include_raw": True,
        },
        "expected_result_digest": _digest("delegated-private-body-absent:wire"),
    }


def _manifest():
    from exomem.governance import consolidation_verification_manifest

    return consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(_owner_contract(), _delegated_contract(), _graph_contract()),
        negative_contracts=(_negative_contract(),),
    )


def _install_stored_plan(
    monkeypatch: pytest.MonkeyPatch,
    manifest,
    *,
    attestation_fingerprint: str = ATTESTATION_FINGERPRINT,
    principal_requirements: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("external", ("support",)),
    ),
) -> None:
    from exomem.governance import consolidation_plan_store

    plan = SimpleNamespace(
        digest=PLAN_DIGEST,
        preimage={
            "run_id": RUN_ID,
            "plan_kind": "cutover",
            "verification_plan": {
                "schema": "exomem.consolidation-verification-plan/v1",
                "positive_probe_digest": manifest.verification_plan.positive_probe_digest,
                "negative_probe_digest": manifest.verification_plan.negative_probe_digest,
            },
        },
    )
    policy_bundle = SimpleNamespace(
        attestations=tuple(
            SimpleNamespace(
                principal_id=principal_id,
                fingerprint=attestation_fingerprint,
                purposes=purposes,
            )
            for principal_id, purposes in principal_requirements
        ),
        principal_requirements=principal_requirements,
    )
    monkeypatch.setattr(
        consolidation_plan_store.ConsolidationPlanStore,
        "load",
        lambda _store, _run_id, *, plan_kind, plan_digest: plan,
    )
    monkeypatch.setattr(
        consolidation_plan_store.ConsolidationPlanStore,
        "load_policy_bundle",
        lambda _store, _run_id, *, plan_kind, plan_digest: policy_bundle,
    )


def test_manifest_is_canonical_round_trippable_and_builds_the_exact_probe_plan() -> None:
    from exomem.governance import consolidation_verification_manifest

    manifest = _manifest()
    raw = consolidation_verification_manifest.canonical_verification_manifest(manifest)

    assert consolidation_verification_manifest.parse_verification_manifest(raw) == manifest
    assert manifest.positive_contracts[0].probe_kind == "positive"
    assert manifest.negative_contracts[0].probe_kind == "negative"
    assert tuple(contract.contract_digest for contract in manifest.contracts) == tuple(
        probe.contract_digest for probe in manifest.verification_plan.probes
    )
    assert tuple(contract.expected_result_digest for contract in manifest.contracts) == tuple(
        probe.expected_result_digest for probe in manifest.verification_plan.probes
    )
    assert consolidation_verification_manifest.contract_arguments(
        manifest.positive_contracts[0]
    ) == {"include_raw": True, "path": "Knowledge Base/Notes/destination.md"}


@pytest.mark.parametrize(
    "changed",
    [
        {"authorization_session_credential": "must-never-persist"},
        {"principal_scope": "caller-selected"},
        {"authority": {"phase": "verifying"}},
    ],
)
def test_manifest_rejects_transport_identity_and_authority_arguments(
    changed: dict[str, object],
) -> None:
    from exomem.governance import consolidation_verification_manifest

    contract = _owner_contract()
    contract["arguments"] = changed

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.build_verification_manifest(
            positive_contracts=(contract,),
            negative_contracts=(_negative_contract(),),
        )


def test_owner_only_manifest_store_replays_exact_bytes_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    vault = tmp_path / "vault"
    manifest = _manifest()
    _install_stored_plan(monkeypatch, manifest)
    store = consolidation_verification_manifest.ConsolidationVerificationManifestStore(vault)

    assert store.persist(RUN_ID, PLAN_DIGEST, manifest) == manifest
    assert store.persist(RUN_ID, PLAN_DIGEST, manifest) == manifest
    assert store.load(RUN_ID, PLAN_DIGEST) == manifest
    assert store.path(RUN_ID, PLAN_DIGEST).name == "verification-manifest.json"

    restarted = consolidation_verification_manifest.ConsolidationVerificationManifestStore(vault)
    assert restarted.load(RUN_ID, PLAN_DIGEST) == manifest
    assert restarted.path(RUN_ID, PLAN_DIGEST).read_bytes() == (
        consolidation_verification_manifest.canonical_verification_manifest(manifest)
    )


@pytest.mark.parametrize("replacement", [None, b"{}"])
def test_manifest_store_refuses_missing_or_tampered_contract_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes | None,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    vault = tmp_path / "vault"
    manifest = _manifest()
    _install_stored_plan(monkeypatch, manifest)
    store = consolidation_verification_manifest.ConsolidationVerificationManifestStore(vault)
    store.persist(RUN_ID, PLAN_DIGEST, manifest)
    path = store.path(RUN_ID, PLAN_DIGEST)
    if replacement is None:
        path.unlink()
    else:
        path.write_bytes(replacement)

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        store.load(RUN_ID, PLAN_DIGEST)


def test_manifest_store_refuses_unattested_or_plan_mismatched_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    vault = tmp_path / "vault"
    manifest = _manifest()
    _install_stored_plan(
        monkeypatch,
        manifest,
        attestation_fingerprint=_digest("changed-attestation"),
    )

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(vault).persist(
            RUN_ID, PLAN_DIGEST, manifest
        )


@pytest.mark.parametrize("missing_kind", ["positive", "negative"])
def test_manifest_store_requires_both_probe_kinds_for_every_delegated_purpose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_kind: str,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    support_positive = _delegated_contract()
    support_negative = _negative_contract()
    audit_positive = {
        **_delegated_contract(),
        "probe_id": "delegated-audit-approved",
        "purpose": "audit",
        "expected_result_digest": _digest("delegated-audit-approved:wire"),
    }
    audit_negative = {
        **_negative_contract(),
        "probe_id": "delegated-audit-denied",
        "purpose": "audit",
        "expected_result_digest": _digest("delegated-audit-denied:wire"),
    }
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            _owner_contract(),
            support_positive,
            *((audit_positive,) if missing_kind == "negative" else ()),
        ),
        negative_contracts=(
            support_negative,
            *((audit_negative,) if missing_kind == "positive" else ()),
        ),
    )
    _install_stored_plan(
        monkeypatch,
        manifest,
        principal_requirements=(("external", ("audit", "support")),),
    )

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(
            tmp_path / "vault"
        ).persist(RUN_ID, PLAN_DIGEST, manifest)


def test_manifest_store_requires_a_positive_owner_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(_delegated_contract(),),
        negative_contracts=(_negative_contract(),),
    )
    _install_stored_plan(monkeypatch, manifest)

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(
            tmp_path / "vault"
        ).persist(RUN_ID, PLAN_DIGEST, manifest)


def test_manifest_store_accepts_the_complete_owner_and_delegated_purpose_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    audit_positive = {
        **_delegated_contract(),
        "probe_id": "delegated-audit-approved",
        "purpose": "audit",
        "expected_result_digest": _digest("delegated-audit-approved:wire"),
    }
    audit_negative = {
        **_negative_contract(),
        "probe_id": "delegated-audit-denied",
        "purpose": "audit",
        "expected_result_digest": _digest("delegated-audit-denied:wire"),
    }
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            _owner_contract(),
            _delegated_contract(),
            _graph_contract(),
            audit_positive,
        ),
        negative_contracts=(_negative_contract(), audit_negative),
    )
    _install_stored_plan(
        monkeypatch,
        manifest,
        principal_requirements=(("external", ("audit", "support")),),
    )

    store = consolidation_verification_manifest.ConsolidationVerificationManifestStore(
        tmp_path / "vault"
    )
    assert store.persist(RUN_ID, PLAN_DIGEST, manifest) == manifest


def test_manifest_store_accepts_an_owner_only_destination_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    owner_negative = {
        **_owner_contract(),
        "probe_id": "owner-private-absent",
        "arguments": {
            "path": "Knowledge Base/Notes/private.md",
            "include_raw": True,
        },
        "expected_result_digest": _digest("owner-private-absent:wire"),
    }
    owner_keyword = _owner_keyword_contract()
    owner_graph = {
        **_graph_contract(),
        "probe_id": "owner-graph",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "owner-verification",
        "expected_result_digest": _digest("owner-graph:wire"),
    }
    owner_graph.pop("principal_attestation_fingerprint")
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(_owner_contract(), owner_keyword, owner_graph),
        negative_contracts=(owner_negative,),
    )
    _install_stored_plan(monkeypatch, manifest, principal_requirements=())

    store = consolidation_verification_manifest.ConsolidationVerificationManifestStore(
        tmp_path / "vault"
    )
    assert store.persist(RUN_ID, PLAN_DIGEST, manifest) == manifest


@pytest.mark.parametrize(
    ("missing_row", "positive_contracts", "negative_contracts"),
    [
        (
            "keyword",
            (_owner_contract(), _graph_contract()),
            (_negative_contract(),),
        ),
        (
            "graph",
            (_owner_contract(), _delegated_contract()),
            (_negative_contract(),),
        ),
        (
            "raw-read",
            (_owner_keyword_contract(), _delegated_contract(), _graph_contract()),
            (_negative_contract(),),
        ),
        (
            "security",
            (_owner_contract(), _delegated_contract(), _graph_contract()),
            (
                {
                    **_negative_contract(),
                    "probe_id": "optional-vector-negative",
                    "command_name": "ask_memory",
                    "arguments": {
                        "query": "private body",
                        "mode": "vector",
                        "graph": False,
                        "limit": 10,
                    },
                    "expected_result_digest": _digest("optional-vector-negative:wire"),
                },
            ),
        ),
    ],
)
def test_manifest_store_rejects_missing_mandatory_non_model_verification_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_row: str,
    positive_contracts: tuple[dict[str, object], ...],
    negative_contracts: tuple[dict[str, object], ...],
) -> None:
    from exomem.governance import consolidation_verification_manifest

    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=positive_contracts,
        negative_contracts=negative_contracts,
    )
    _install_stored_plan(monkeypatch, manifest)

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(
            tmp_path / f"vault-{missing_row}"
        ).persist(RUN_ID, PLAN_DIGEST, manifest)


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "query": "approved compiled abstraction",
            "mode": "vector",
            "graph": False,
            "rerank": False,
            "limit": 10,
        },
        {
            "query": "approved compiled abstraction",
            "mode": "hybrid",
            "graph": False,
            "rerank": False,
            "limit": 10,
        },
        {
            "query": "approved compiled abstraction",
            "mode": "keyword",
            "graph": False,
            "rerank": True,
            "limit": 10,
        },
        {
            "query": "approved compiled abstraction",
            "mode": "keyword",
            "graph": False,
            "limit": 10,
        },
        {
            "query": "approved compiled abstraction",
            "mode": "keyword",
            "graph": False,
            "rerank": False,
            "deep": True,
            "limit": 10,
        },
        {
            "query": "approved compiled abstraction",
            "mode": "keyword",
            "graph": False,
            "rerank": False,
            "deep": "true",
            "limit": 10,
        },
        {
            "query": "approved compiled abstraction",
            "mode": "keyword",
            "graph": False,
            "rerank": False,
            "graph_enrich": "1",
            "limit": 10,
        },
        {
            "query": "   ",
            "mode": "keyword",
            "graph": False,
            "rerank": False,
            "limit": 10,
        },
    ],
)
def test_non_mandatory_recall_profile_cannot_satisfy_the_keyword_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    from exomem.governance import consolidation_verification_manifest

    optional_recall = {
        **_delegated_contract(),
        "probe_id": "optional-model-recall",
        "arguments": arguments,
        "expected_result_digest": _digest("optional-model-recall:wire"),
    }
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(_owner_contract(), _graph_contract(), optional_recall),
        negative_contracts=(_negative_contract(),),
    )
    _install_stored_plan(monkeypatch, manifest)

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(
            tmp_path / "vault-optional-model"
        ).persist(RUN_ID, PLAN_DIGEST, manifest)


@pytest.mark.parametrize("missing_row", ["graph", "raw-read"])
def test_truthy_string_flag_cannot_satisfy_a_mandatory_non_model_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_row: str,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    raw = _owner_contract()
    graph = _graph_contract()
    if missing_row == "graph":
        graph["arguments"] = {
            **graph["arguments"],
            "include_model_suggestions": "true",
        }
    else:
        raw["arguments"] = {
            **raw["arguments"],
            "frontmatter_only": "true",
        }
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(raw, _delegated_contract(), graph),
        negative_contracts=(_negative_contract(),),
    )
    _install_stored_plan(monkeypatch, manifest)

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(
            tmp_path / f"vault-truthy-{missing_row}"
        ).persist(RUN_ID, PLAN_DIGEST, manifest)


def test_non_rest_contracts_cannot_satisfy_the_in_process_mandatory_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_manifest

    positive = tuple(
        {**contract, "surface": "mcp"}
        for contract in (_owner_contract(), _delegated_contract(), _graph_contract())
    )
    negative = ({**_negative_contract(), "surface": "mcp"},)
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=positive,
        negative_contracts=negative,
    )
    _install_stored_plan(monkeypatch, manifest)

    with pytest.raises(
        consolidation_verification_manifest.ConsolidationVerificationManifestUnavailable,
        match="^CONSOLIDATION_VERIFICATION_MANIFEST_UNAVAILABLE$",
    ):
        consolidation_verification_manifest.ConsolidationVerificationManifestStore(
            tmp_path / "vault-mcp-only"
        ).persist(RUN_ID, PLAN_DIGEST, manifest)
