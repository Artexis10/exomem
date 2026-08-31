"""Canonical governed-surface execution for consolidation verification."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

VAULT_BINDING = hashlib.sha256(b"verification-registry-vault").hexdigest()
JOURNAL_DIGEST = hashlib.sha256(b"verification-registry-journal").hexdigest()
PLAN_DIGEST = hashlib.sha256(b"verification-registry-plan").hexdigest()
RUN_ID = "00000000-0000-4000-8000-000000000092"
OPERATION_ID = "00000000-0000-4000-8000-000000000093"
VERIFIED_AT = "2026-08-31T12:00:00.000Z"


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
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _context(
    vault: Path,
    manifest,
    *,
    principals: tuple[object, ...] = (),
):
    from exomem.governance import consolidation_authority, consolidation_verification

    return consolidation_verification.VerificationProbeContext(
        vault_root=vault,
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        plan_digest=PLAN_DIGEST,
        canonical_census_digest=hashlib.sha256(b"registry-census").hexdigest(),
        verification_basis_digest=hashlib.sha256(b"registry-basis").hexdigest(),
        verified_at=VERIFIED_AT,
        principals=principals,
        authority=consolidation_authority.issue_authority(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            phase="verifying",
            action="verify",
        ),
        contract=manifest.contracts[0],
    )


def _owner_manifest(expected_result_digest: str):
    from exomem.governance import consolidation_verification_manifest

    return consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            {
                "probe_id": "owner-read-memory",
                "executor_id": "canonical-governance-surface-v1",
                "surface": "rest",
                "principal_kind": "owner",
                "principal_id": "owner",
                "purpose": "consolidation-verification",
                "command_name": "read_memory",
                "arguments": {
                    "path": "Knowledge Base/Notes/public.md",
                    "frontmatter_only": True,
                },
                "expected_result_digest": expected_result_digest,
            },
        ),
        negative_contracts=(
            {
                "probe_id": "owner-absent-memory",
                "executor_id": "canonical-governance-surface-v1",
                "surface": "rest",
                "principal_kind": "owner",
                "principal_id": "owner",
                "purpose": "consolidation-verification",
                "command_name": "read_memory",
                "arguments": {"path": "Knowledge Base/Notes/absent.md"},
                "expected_result_digest": hashlib.sha256(b"unused-negative").hexdigest(),
            },
        ),
    )


def test_owner_probe_crosses_real_dispatch_egress_receipt_and_rest_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import server_rest, writer_lease
    from exomem.governance import (
        consolidation_verification_registry,
        egress,
        principal,
    )

    vault = tmp_path / "vault"
    path = vault / "Knowledge Base" / "Notes" / "public.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Public\n\nVisible.\n", encoding="utf-8")
    expected_data = {
        "path": "Knowledge Base/Notes/public.md",
        "frontmatter": {},
        "has_frontmatter": False,
    }
    expected_wire = consolidation_verification_registry.render_rest_verification_wire(
        success=True,
        data=expected_data,
    )
    expected_digest = consolidation_verification_registry.verification_wire_result_digest(
        "rest",
        expected_wire,
    )
    manifest = _owner_manifest(expected_digest)
    calls: list[str] = []

    def wrap(module, name: str, label: str) -> None:
        original = getattr(module, name)

        def traced(*args, **kwargs):
            calls.append(label)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, traced)

    wrap(principal, "resolve_rest_principal", "identity")
    wrap(principal, "request_scope", "principal-scope")
    wrap(writer_lease, "invoke_command", "dispatch")
    wrap(egress, "postfilter", "scrubber")
    wrap(egress, "emit_boundary_receipt", "receipt")
    wrap(server_rest.RestJSONResponse, "render", "response")

    terminal = consolidation_verification_registry.run_probe(
        manifest.verification_plan.positive_probes[0],
        _context(vault, manifest),
    )

    assert terminal.result_digest == expected_digest
    assert terminal.outcome == "passed"
    assert {"identity", "principal-scope", "dispatch", "scrubber", "receipt", "response"} <= set(
        calls
    )


def test_mandatory_graph_probe_executes_the_live_product_route_without_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import commands, embeddings, find
    from exomem.governance import (
        consolidation_verification_manifest,
        consolidation_verification_registry,
    )

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    vault = tmp_path / "vault"
    public = vault / "Knowledge Base" / "Notes" / "public.md"
    related = vault / "Knowledge Base" / "Notes" / "related.md"
    public.parent.mkdir(parents=True)
    public.write_text("# Public\n\n[[Knowledge Base/Notes/related.md]]\n", encoding="utf-8")
    related.write_text("# Related\n\nGraph neighbour.\n", encoding="utf-8")

    def forbid_model(*_args, **_kwargs):
        raise AssertionError("mandatory graph verification must not invoke a model lane")

    monkeypatch.setattr(embeddings, "embed_texts", forbid_model)
    monkeypatch.setattr(find, "find", forbid_model)
    arguments = {
        "operation": "graph-context",
        "path": "Knowledge Base/Notes/public.md",
        "include_model_suggestions": False,
        "depth": 1,
    }
    expected = commands.op_connect_memory(vault, **arguments)
    wire = consolidation_verification_registry.render_rest_verification_wire(
        success=True,
        data=expected,
    )
    contract = {
        "probe_id": "owner-graph-context",
        "executor_id": "canonical-governance-surface-v1",
        "surface": "rest",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "consolidation-verification",
        "command_name": "connect_memory",
        "arguments": arguments,
        "expected_result_digest": (
            consolidation_verification_registry.verification_wire_result_digest("rest", wire)
        ),
    }
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(contract,),
        negative_contracts=({**contract, "probe_id": "owner-graph-context-negative"},),
    )

    terminal = consolidation_verification_registry.run_probe(
        manifest.verification_plan.positive_probes[0],
        _context(vault, manifest),
    )

    assert terminal.result_digest == contract["expected_result_digest"]
    assert terminal.outcome == "passed"


def test_probe_refuses_forged_authority_and_mismatched_contract_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import writer_lease
    from exomem.governance import consolidation_verification_registry

    expected_wire = consolidation_verification_registry.render_rest_verification_wire(
        success=True,
        data={"unused": True},
    )
    manifest = _owner_manifest(
        consolidation_verification_registry.verification_wire_result_digest(
            "rest",
            expected_wire,
        )
    )
    probe = manifest.verification_plan.positive_probes[0]
    context = _context(tmp_path / "vault", manifest)
    monkeypatch.setattr(
        writer_lease,
        "invoke_command",
        lambda *_args, **_kwargs: pytest.fail("dispatch must not run"),
    )

    for changed in (
        dataclasses.replace(context, authority=object()),
        dataclasses.replace(context, contract=manifest.negative_contracts[0]),
    ):
        with pytest.raises(
            consolidation_verification_registry.ConsolidationVerificationRegistryUnavailable,
            match="^CONSOLIDATION_VERIFICATION_REGISTRY_UNAVAILABLE$",
        ):
            consolidation_verification_registry.run_probe(probe, changed)


def test_delegated_probe_revalidates_live_session_and_destination_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import writer_lease
    from exomem.governance import (
        authorization_session_lifecycle,
        consolidation_plan_store,
        consolidation_policy,
        consolidation_verification_manifest,
        consolidation_verification_registry,
        principal,
    )

    audience = "delegated-user"
    fingerprint = hashlib.sha256(b"delegated-attestation").hexdigest()
    projected = {"path": "Knowledge Base/Notes/approved.md", "body": "approved"}
    wire = consolidation_verification_registry.render_rest_verification_wire(
        success=True,
        data=projected,
    )
    expected_digest = consolidation_verification_registry.verification_wire_result_digest(
        "rest",
        wire,
    )
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            {
                "probe_id": "delegated-approved",
                "executor_id": "canonical-governance-surface-v1",
                "surface": "rest",
                "principal_kind": "delegated",
                "principal_id": audience,
                "principal_attestation_fingerprint": fingerprint,
                "purpose": "support",
                "command_name": "read_memory",
                "arguments": {"path": "Knowledge Base/Notes/approved.md"},
                "expected_result_digest": expected_digest,
            },
        ),
        negative_contracts=(
            {
                "probe_id": "delegated-denied",
                "executor_id": "canonical-governance-surface-v1",
                "surface": "rest",
                "principal_kind": "delegated",
                "principal_id": audience,
                "principal_attestation_fingerprint": fingerprint,
                "purpose": "support",
                "command_name": "read_memory",
                "arguments": {"path": "Knowledge Base/Notes/private.md"},
                "expected_result_digest": hashlib.sha256(b"unused-denied").hexdigest(),
            },
        ),
    )
    session = authorization_session_lifecycle.AuthorizationSessionContext(
        session_id="session-1",
        principal_id=audience,
        issuer_family="rest-cf-access",
        cell_id="cell-1",
        logical_vault_id="vault-1",
        keyring_id="keyring-1",
        credential_generation=1,
        expires_at=2_000_000_000,
    )
    who = principal.RequestPrincipal(
        audience_id=audience,
        surface="rest",
        authorization_session_id=session.session_id,
        resolved=True,
        issuer_family=session.issuer_family,
        verified_authorization_session=session,
    )
    attestation = SimpleNamespace(fingerprint=fingerprint)
    bundle = SimpleNamespace(
        attestations=(attestation,),
        destination_vault_id="vault-1",
        nonce="nonce-1",
    )
    calls: list[tuple[str, object]] = []
    fresh_now = 2_000_000_001
    fresh_verified_at = "2033-05-18T03:33:21.000Z"
    monkeypatch.setattr(
        consolidation_verification_registry,
        "_fresh_verification_time",
        lambda: (fresh_now, fresh_verified_at),
    )
    monkeypatch.setattr(
        consolidation_verification_registry,
        "_revalidate_session",
        lambda _root, value, *, now: calls.append(("session", now)) or value,
    )
    monkeypatch.setattr(
        consolidation_plan_store.ConsolidationPlanStore,
        "load_policy_bundle",
        lambda _store, _run_id, *, plan_kind, plan_digest: bundle,
    )
    monkeypatch.setattr(
        consolidation_policy,
        "verify_destination_principal_attestation",
        lambda *args, **kwargs: (
            calls.append(("attestation", kwargs["verified_at"])) or SimpleNamespace()
        ),
    )
    monkeypatch.setattr(
        writer_lease,
        "invoke_command",
        lambda *_args, **_kwargs: projected,
    )

    terminal = consolidation_verification_registry.run_probe(
        manifest.verification_plan.positive_probes[0],
        _context(tmp_path / "vault", manifest, principals=(who,)),
    )

    assert terminal.result_digest == expected_digest
    assert calls == [
        ("session", fresh_now),
        ("attestation", fresh_verified_at),
    ]


@pytest.mark.parametrize(
    "component",
    ["identity-scope", "postfilter", "error-filter", "receipt", "response"],
)
def test_probe_refuses_component_failure_that_looks_like_a_public_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    from exomem import server_rest
    from exomem.governance import (
        consolidation_verification_manifest,
        consolidation_verification_registry,
        egress,
        principal,
    )

    vault = tmp_path / "vault"
    private = vault / "Knowledge Base" / "Notes" / "private.md"
    private.parent.mkdir(parents=True)
    if component != "error-filter":
        private.write_text("# Private\n", encoding="utf-8")
    expected_digest = hashlib.sha256(f"{component}-public-result".encode()).hexdigest()
    contract = {
        "probe_id": "owner-hidden-memory",
        "executor_id": "canonical-governance-surface-v1",
        "surface": "rest",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "consolidation-verification",
        "command_name": "read_memory",
        "arguments": {"path": "Knowledge Base/Notes/private.md"},
        "expected_result_digest": expected_digest,
    }
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            {
                **contract,
                "probe_id": "owner-visible-memory",
                "arguments": {"path": "Knowledge Base/Notes/public.md"},
            },
        ),
        negative_contracts=(contract,),
    )
    context = dataclasses.replace(
        _context(vault, manifest),
        contract=manifest.negative_contracts[0],
    )
    target = {
        "identity-scope": (principal, "request_scope"),
        "postfilter": (egress, "postfilter"),
        "error-filter": (egress, "postfilter_error"),
        "receipt": (egress, "emit_boundary_receipt"),
        "response": (server_rest.RestJSONResponse, "render"),
    }[component]

    def fail_component(*_args, **_kwargs):
        raise ValueError("NOT_FOUND: path does not exist")

    monkeypatch.setattr(*target, fail_component)

    with pytest.raises(
        consolidation_verification_registry.ConsolidationVerificationRegistryUnavailable,
        match="^CONSOLIDATION_VERIFICATION_REGISTRY_UNAVAILABLE$",
    ):
        consolidation_verification_registry.run_probe(
            manifest.verification_plan.negative_probes[0],
            context,
        )


def test_probe_refuses_commands_disabled_on_the_live_rest_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import writer_lease
    from exomem.governance import (
        consolidation_verification_manifest,
        consolidation_verification_registry,
    )

    expected_digest = hashlib.sha256(b"disabled-tier2-wire").hexdigest()
    tier2_contract = {
        "probe_id": "owner-tier2-query",
        "executor_id": "canonical-governance-surface-v1",
        "surface": "rest",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "consolidation-verification",
        "command_name": "query_dataset",
        "arguments": {"query": "select 1"},
        "expected_result_digest": expected_digest,
    }
    manifest = consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(tier2_contract,),
        negative_contracts=({**tier2_contract, "probe_id": "owner-tier2-query-negative"},),
    )
    monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    monkeypatch.setattr(
        writer_lease,
        "invoke_command",
        lambda *_args, **_kwargs: pytest.fail("disabled REST command must not dispatch"),
    )

    with pytest.raises(
        consolidation_verification_registry.ConsolidationVerificationRegistryUnavailable,
        match="^CONSOLIDATION_VERIFICATION_REGISTRY_UNAVAILABLE$",
    ):
        consolidation_verification_registry.run_probe(
            manifest.verification_plan.positive_probes[0],
            _context(tmp_path / "vault", manifest),
        )
