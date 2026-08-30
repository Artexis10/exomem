from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import held_fs
from exomem.governance import consolidation_policy, decisions, membership, policy, store
from exomem.governance.authorization_session_lifecycle import AuthorizationSessionContext
from exomem.governance.principal import RequestPrincipal

DESTINATION_VAULT_ID = "destination-vault"
PRINCIPAL_ID = "principal:destination-owner"
MATRIX_PRINCIPAL_ID = "principal:destination-reviewer"
ISSUER = "mcp-oauth:destination-issuer"
NONCE = "run-00000000000000000001"
ISSUED_AT = "2026-08-28T09:00:00.000Z"
EXPIRES_AT = "2026-08-28T10:00:00.000Z"
VERIFIED_AT = "2026-08-28T09:30:00.000Z"
SCOPE_A = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
SCOPE_B = "01ARZ3NDEKTSV4RRFFQ69G5FA2"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _principal(
    *,
    principal_id: str = PRINCIPAL_ID,
    issuer: str = ISSUER,
    session_id: str = "destination-session",
    generation: int = 3,
    vault_id: str = DESTINATION_VAULT_ID,
) -> RequestPrincipal:
    context = AuthorizationSessionContext(
        session_id=session_id,
        principal_id=principal_id,
        issuer_family=issuer,
        cell_id="destination-cell",
        logical_vault_id=vault_id,
        keyring_id="destination-keyring",
        credential_generation=generation,
        expires_at=1_800_000_000,
    )
    return RequestPrincipal(
        audience_id=principal_id,
        surface="mcp",
        authorization_session_id=session_id,
        resolved=True,
        issuer_family=issuer,
        verified_authorization_session=context,
    )


def _attestation(
    *,
    principal: RequestPrincipal | None = None,
    purposes: tuple[str, ...] = ("audit",),
    nonce: str = NONCE,
) -> consolidation_policy.DestinationPrincipalAttestation:
    return consolidation_policy.issue_destination_principal_attestation(
        principal or _principal(),
        destination_vault_id=DESTINATION_VAULT_ID,
        purposes=purposes,
        issued_at=ISSUED_AT,
        expires_at=EXPIRES_AT,
        nonce=nonce,
    )


def _review(kind: str = "policy") -> consolidation_policy.SourceAuthorityReviewArtifact:
    return consolidation_policy.SourceAuthorityReviewArtifact(
        object_ref=f"source-{kind}",
        object_kind=kind,
        object_sha256=_digest(f"{kind}-object"),
        bundle_sha256=_digest(f"{kind}-bundle"),
        provenance_ref=f"exomem-source-artifact://sha256/{_digest(kind)}",
    )


def _documents(*, audience: str = PRINCIPAL_ID) -> dict[str, str]:
    return {
        "scopes/private.yaml": (
            "governance_version: 1\n"
            f"id: {SCOPE_A}\n"
            "name: Private notes\n"
            "default_deny: true\n"
            'paths: ["Notes/Private/**"]\n'
        ),
        "rules/private-audit.yaml": (
            "governance_version: 1\n"
            "id: 01ARZ3NDEKTSV4RRFFQ69G5FB1\n"
            f'scope_ids: ["{SCOPE_A}"]\n'
            f"audience: {audience}\n"
            "purpose: audit\n"
            "ceiling: 2\n"
            "options:\n"
            "  constraint: reviewed-only\n"
        ),
    }


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "Knowledge Base").mkdir()
    return tmp_path


def test_destination_principal_attestation_has_one_fixed_vector() -> None:
    attestation = _attestation()

    assert attestation == consolidation_policy.DestinationPrincipalAttestation(
        schema="destination-principal-attestation/v1",
        destination_vault_id=DESTINATION_VAULT_ID,
        issuer_family=ISSUER,
        surface="mcp",
        principal_id=PRINCIPAL_ID,
        purposes=("audit",),
        issued_at=ISSUED_AT,
        expires_at=EXPIRES_AT,
        authentication_binding_digest=(
            "4128ec7ce82de35d791fb00327a94018403a9d2f1b38a365720482d45633d2c9"
        ),
        nonce=NONCE,
        fingerprint="5d9235fd561319b494048c348572709ab495d59256f1d2cf89485b1ef23e2ae6",
    )
    verified = consolidation_policy.verify_destination_principal_attestation(
        attestation,
        principal=_principal(),
        destination_vault_id=DESTINATION_VAULT_ID,
        required_purpose="audit",
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )
    assert verified.attestation == attestation
    assert verified.verified_at == VERIFIED_AT


def test_destination_policy_bundle_is_one_exact_canonical_round_trip(tmp_path: Path) -> None:
    plan = consolidation_policy.compile_destination_policy(
        _vault(tmp_path),
        documents=_documents(),
        source_authority=(_review(),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    raw = consolidation_policy.canonical_destination_policy_bundle(plan)
    parsed = json.loads(raw)

    assert parsed["schema"] == "exomem.consolidation-destination-policy/v1"
    assert parsed["nonce"] == NONCE
    assert "digest" not in parsed
    assert "plan_digest" not in parsed
    assert len(raw) == 3330
    assert hashlib.sha256(raw).hexdigest() == (
        "1e6c6476b332f65fed44c2a1088518b14debcd713dcb9961b7de25e3b90d7b7e"
    )
    assert plan.digest == "3194d49690ff9e7c924077323f99f766641a157afee28850bda65cc1cd72cde6"
    assert consolidation_policy.parse_destination_policy_bundle(raw) == plan
    assert consolidation_policy.destination_policy_bundle_digest(plan) == plan.digest

    changed = raw.replace(b'"destination-vault"', b'"destination-other"')
    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.parse_destination_policy_bundle(changed)


@pytest.mark.parametrize(
    ("mutation", "principal", "vault_id", "purpose", "nonce", "verified_at"),
    [
        (
            {"schema": "source-export-attestation/v1"},
            None,
            DESTINATION_VAULT_ID,
            "audit",
            NONCE,
            VERIFIED_AT,
        ),
        (
            {"destination_vault_id": "source-vault"},
            None,
            DESTINATION_VAULT_ID,
            "audit",
            NONCE,
            VERIFIED_AT,
        ),
        (
            {"issuer_family": "mcp-oauth:other"},
            None,
            DESTINATION_VAULT_ID,
            "audit",
            NONCE,
            VERIFIED_AT,
        ),
        ({"surface": "hosted"}, None, DESTINATION_VAULT_ID, "audit", NONCE, VERIFIED_AT),
        (
            {"principal_id": "principal:caller-selected"},
            None,
            DESTINATION_VAULT_ID,
            "audit",
            NONCE,
            VERIFIED_AT,
        ),
        ({"purposes": ("research",)}, None, DESTINATION_VAULT_ID, "audit", NONCE, VERIFIED_AT),
        (
            {"issued_at": "2026-08-28T09:45:00.000Z"},
            None,
            DESTINATION_VAULT_ID,
            "audit",
            NONCE,
            VERIFIED_AT,
        ),
        (
            {"expires_at": "2026-08-28T09:15:00.000Z"},
            None,
            DESTINATION_VAULT_ID,
            "audit",
            NONCE,
            VERIFIED_AT,
        ),
        ({"nonce": "source-run"}, None, DESTINATION_VAULT_ID, "audit", NONCE, VERIFIED_AT),
        ({}, {"session_id": "other-session"}, DESTINATION_VAULT_ID, "audit", NONCE, VERIFIED_AT),
        ({}, {"generation": 4}, DESTINATION_VAULT_ID, "audit", NONCE, VERIFIED_AT),
        ({}, None, "other-vault", "audit", NONCE, VERIFIED_AT),
        ({}, None, DESTINATION_VAULT_ID, "research", NONCE, VERIFIED_AT),
        ({}, None, DESTINATION_VAULT_ID, "audit", "other-run", VERIFIED_AT),
        ({}, None, DESTINATION_VAULT_ID, "audit", NONCE, "2026-08-28T10:00:00.001Z"),
    ],
)
def test_destination_attestation_refuses_forgery_copy_staleness_and_replay(
    mutation: dict[str, object],
    principal: dict[str, object] | None,
    vault_id: str,
    purpose: str,
    nonce: str,
    verified_at: str,
) -> None:
    attestation = replace(_attestation(), **mutation)
    live_principal = _principal(**(principal or {}))

    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.verify_destination_principal_attestation(
            attestation,
            principal=live_principal,
            destination_vault_id=vault_id,
            required_purpose=purpose,
            expected_nonce=nonce,
            verified_at=verified_at,
        )


@pytest.mark.parametrize(
    "kind",
    [
        "access_control",
        "audience",
        "authorization_session",
        "credential",
        "grant",
        "policy",
        "receipt",
        "release_approval",
        "review_authority",
        "runtime_binding",
        "token",
    ],
)
def test_source_authority_is_digest_only_review_input_never_destination_authority(
    tmp_path: Path, kind: str
) -> None:
    plan = consolidation_policy.compile_destination_policy(
        _vault(tmp_path),
        documents=_documents(),
        source_authority=(_review(kind),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    assert plan.prospective.policy.rules[0].audience == PRINCIPAL_ID
    assert (
        plan.source_authority_review_digest
        == consolidation_policy.source_authority_review_digest((_review(kind),))
    )
    assert plan.document_edits == tuple(sorted(_documents().items()))
    assert plan.source_authority == (_review(kind),)
    assert set(_review(kind).__slots__) == {
        "object_ref",
        "object_kind",
        "object_sha256",
        "bundle_sha256",
        "provenance_ref",
    }
    assert not hasattr(plan, "source_documents")
    assert not hasattr(plan, "source_credentials")


def test_destination_policy_requires_fresh_attestation_for_every_named_principal(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.compile_destination_policy(
            vault,
            documents=_documents(),
            source_authority=(_review(),),
            attestations=(),
            principal_contexts=(),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )


def test_representative_disclosure_principal_requires_its_own_fresh_attestation(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    reviewer = _principal(
        principal_id=MATRIX_PRINCIPAL_ID,
        session_id="reviewer-session",
    )
    reviewer_attestation = _attestation(
        principal=reviewer,
        purposes=("research",),
    )

    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.compile_destination_policy(
            vault,
            documents=_documents(),
            source_authority=(_review(),),
            attestations=(_attestation(),),
            principal_contexts=(_principal(), reviewer),
            representative_principal_purposes={MATRIX_PRINCIPAL_ID: ("research",)},
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )

    plan = consolidation_policy.compile_destination_policy(
        vault,
        documents=_documents(),
        source_authority=(_review(),),
        attestations=(_attestation(), reviewer_attestation),
        principal_contexts=(_principal(), reviewer),
        representative_principal_purposes={MATRIX_PRINCIPAL_ID: ("research",)},
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )
    assert plan.named_principals == (PRINCIPAL_ID, MATRIX_PRINCIPAL_ID)
    assert plan.principal_requirements == (
        (PRINCIPAL_ID, ("audit",)),
        (MATRIX_PRINCIPAL_ID, ("research",)),
    )
    assert (
        consolidation_policy.revalidate_destination_policy(
            vault,
            plan,
            principal_contexts=(_principal(), reviewer),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )
        == plan
    )
    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.revalidate_destination_policy(
            vault,
            plan,
            principal_contexts=(_principal(),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )

    source_attestation = consolidation_policy.issue_destination_principal_attestation(
        _principal(vault_id="source-vault"),
        destination_vault_id="source-vault",
        purposes=("audit",),
        issued_at=ISSUED_AT,
        expires_at=EXPIRES_AT,
        nonce=NONCE,
    )
    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.compile_destination_policy(
            vault,
            documents=_documents(),
            source_authority=(_review(),),
            attestations=(source_attestation,),
            principal_contexts=(_principal(vault_id="source-vault"),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )


def test_coincident_source_audience_provenance_cannot_supply_destination_authority(
    tmp_path: Path,
) -> None:
    source_audience = replace(
        _review("audience"),
        object_ref=PRINCIPAL_ID,
        provenance_ref=f"exomem-source-artifact://sha256/{_digest('source-audience')}",
    )

    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.compile_destination_policy(
            _vault(tmp_path),
            documents=_documents(audience=source_audience.object_ref),
            source_authority=(source_audience,),
            attestations=(),
            principal_contexts=(),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )


def test_empty_source_authority_and_no_new_destination_documents_are_valid(
    tmp_path: Path,
) -> None:
    plan = consolidation_policy.compile_destination_policy(
        _vault(tmp_path),
        documents={},
        source_authority=(),
        attestations=(),
        principal_contexts=(),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    assert plan.prospective.policy.scopes == {}
    assert plan.prospective.policy.rules == ()
    assert plan.source_authority == ()
    assert plan.named_principals == ()


def test_prospective_policy_prevents_sibling_grant_crossover(tmp_path: Path) -> None:
    documents = _documents()
    del documents["rules/private-audit.yaml"]
    documents.update(
        {
            "scopes/sibling.yaml": (
                "governance_version: 1\n"
                f"id: {SCOPE_B}\n"
                "name: Sibling\n"
                "default_deny: true\n"
                'paths: ["Notes/Private/**"]\n'
            ),
            "grants/sibling.yaml": (
                "governance_version: 1\n"
                "id: 01ARZ3NDEKTSV4RRFFQ69G5FB3\n"
                "kind: standing\n"
                f'scope_ids: ["{SCOPE_B}"]\n'
                f"audience: {PRINCIPAL_ID}\n"
                "ceiling: 5\n"
            ),
        }
    )
    plan = consolidation_policy.compile_destination_policy(
        _vault(tmp_path),
        documents=documents,
        source_authority=(_review(),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    decision = decisions.decide(
        (SCOPE_A, SCOPE_B),
        audience=PRINCIPAL_ID,
        purpose="audit",
        policy=plan.prospective.policy,
    )
    assert decision.level == 0
    assert decision.default_deny_scope_ids == (SCOPE_A, SCOPE_B)
    contributions = {item.scope_id: item for item in decision.scope_contributions}
    assert contributions[SCOPE_A].final_ceiling == 0
    assert contributions[SCOPE_B].final_ceiling == 5


def test_prospective_policy_preserves_deny_dominance_over_a_grant(tmp_path: Path) -> None:
    documents = _documents()
    documents["scopes/private.yaml"] = documents["scopes/private.yaml"].replace(
        "default_deny: true\n", ""
    )
    documents["rules/private-audit.yaml"] = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB2\n"
        f'scope_ids: ["{SCOPE_A}"]\n'
        f"audience: {PRINCIPAL_ID}\n"
        "kind: org_cap\n"
        "ceiling: 0\n"
    )
    documents["grants/private.yaml"] = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB3\n"
        "kind: standing\n"
        f'scope_ids: ["{SCOPE_A}"]\n'
        f"audience: {PRINCIPAL_ID}\n"
        "ceiling: 6\n"
    )
    plan = consolidation_policy.compile_destination_policy(
        _vault(tmp_path),
        documents=documents,
        source_authority=(_review(),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    decision = decisions.decide((SCOPE_A,), audience=PRINCIPAL_ID, policy=plan.prospective.policy)
    assert decision.level == 0
    assert decision.scope_contributions[0].grant_contribution == 6
    assert decision.scope_contributions[0].organization_cap == 0


def test_prospective_policy_preserves_conservative_option_meet(tmp_path: Path) -> None:
    documents = _documents()
    documents["rules/private-audit.yaml"] = documents["rules/private-audit.yaml"].replace(
        "purpose: audit\n", ""
    )
    documents["rules/private-audit-other.yaml"] = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB5\n"
        f'scope_ids: ["{SCOPE_A}"]\n'
        f"audience: {PRINCIPAL_ID}\n"
        "ceiling: 2\n"
        "options:\n"
        "  constraint: independently-reviewed\n"
    )
    plan = consolidation_policy.compile_destination_policy(
        _vault(tmp_path),
        documents=documents,
        source_authority=(_review(),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    decision = decisions.decide(
        (SCOPE_A,),
        audience=PRINCIPAL_ID,
        purpose=None,
        policy=plan.prospective.policy,
    )
    assert decision.level == 1
    assert "constraint" not in decision.options
    assert decision.options["constraint_ambiguous"] is True


def test_prospective_policy_compiles_bridge_and_exact_release_for_fresh_principal(
    tmp_path: Path,
) -> None:
    documents = _documents()
    documents["rules/private-audit.yaml"] = documents["rules/private-audit.yaml"].replace(
        "  constraint: reviewed-only\n",
        "  constraint: reviewed-only\n  bridge: destination-review\n",
    )
    documents["grants/exact-release.yaml"] = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB6\n"
        "kind: release\n"
        "path: Knowledge Base/Notes/released.md\n"
        "ref: exomem://memory/00000000-0000-0000-0000-000000000001\n"
        f"content_hash: {'a' * 64}\n"
        f"to_audience: {PRINCIPAL_ID}\n"
        "released_at: '2026-08-28T09:00:00Z'\n"
        "why: Destination owner reviewed the exact release\n"
        "bridge_scope: destination-review\n"
        "bridge_of:\n"
        "  - ref: exomem://memory/00000000-0000-0000-0000-000000000002\n"
        "    path: Knowledge Base/Sources/source.md\n"
        f"    content_hash: {'b' * 64}\n"
        f"    restriction_signature: {'c' * 64}\n"
        "options:\n"
        "  strip_provenance:\n"
        "    - exomem://memory/00000000-0000-0000-0000-000000000002\n"
    )

    plan = consolidation_policy.compile_destination_policy(
        _vault(tmp_path),
        documents=documents,
        source_authority=(_review(),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    assert plan.prospective.policy.rules[0].options["bridge"] == "destination-review"
    assert plan.prospective.policy.release_grants[0].to_audience == PRINCIPAL_ID


def test_prospective_semantic_scope_keeps_non_markdown_membership_unresolved(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    asset = vault / "Knowledge Base" / "Notes" / "opaque.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"\x00private")
    documents = _documents()
    documents["scopes/private.yaml"] = (
        "governance_version: 1\n"
        f"id: {SCOPE_A}\n"
        "name: Private sources\n"
        "default_deny: true\n"
        'types: ["source"]\n'
    )
    plan = consolidation_policy.compile_destination_policy(
        vault,
        documents=documents,
        source_authority=(_review(),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )

    outcome = membership.evaluate_path_only(
        vault,
        "Knowledge Base/Notes/opaque.bin",
        plan.prospective.policy,
    )
    assert outcome.state == "unresolved"
    with pytest.raises(membership.MembershipUnresolved):
        outcome.require_classified()


def test_prospective_policy_refuses_pending_guard_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = iter(
        (
            {"state": "clear", "generation": "guard-before", "event_ids": ()},
            {"state": "clear", "generation": "guard-after", "event_ids": ()},
        )
    )
    monkeypatch.setattr(store, "guard_generation_probe", lambda _vault: next(probes))

    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.compile_destination_policy(
            _vault(tmp_path),
            documents=_documents(),
            source_authority=(_review(),),
            attestations=(_attestation(),),
            principal_contexts=(_principal(),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )


def test_prospective_policy_refuses_document_change_during_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    scope = vault / "Knowledge Base" / "_Governance" / "scopes" / "existing.yaml"
    scope.parent.mkdir(parents=True)
    scope.write_text(_documents()["scopes/private.yaml"], encoding="utf-8")
    changed = False

    def barrier(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase == "after_read" and not changed:
            changed = True
            scope.write_text(
                _documents()["scopes/private.yaml"].replace("Private notes", "Changed"),
                encoding="utf-8",
            )

    monkeypatch.setattr(policy, "_authoring_snapshot_barrier", barrier)

    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.compile_destination_policy(
            vault,
            documents=_documents(),
            source_authority=(_review(),),
            attestations=(_attestation(),),
            principal_contexts=(_principal(),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )
    assert changed


def test_prospective_policy_refuses_a_preexisting_conflict_copy(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    governance = vault / "Knowledge Base" / "_Governance" / "scopes"
    governance.mkdir(parents=True)
    (governance / "private.yaml").write_text(_documents()["scopes/private.yaml"], encoding="utf-8")
    (governance / "private.sync-conflict-20260828-120000-ABCDEFG.yaml").write_text(
        _documents()["scopes/private.yaml"], encoding="utf-8"
    )

    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.compile_destination_policy(
            vault,
            documents=_documents(),
            source_authority=(_review(),),
            attestations=(_attestation(),),
            principal_contexts=(_principal(),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )


def test_apply_revalidation_refuses_policy_or_session_drift(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plan = consolidation_policy.compile_destination_policy(
        vault,
        documents=_documents(),
        source_authority=(_review(),),
        attestations=(_attestation(),),
        principal_contexts=(_principal(),),
        destination_vault_id=DESTINATION_VAULT_ID,
        expected_nonce=NONCE,
        verified_at=VERIFIED_AT,
    )
    assert (
        consolidation_policy.revalidate_destination_policy(
            vault,
            plan,
            principal_contexts=(_principal(),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )
        == plan
    )

    forged_snapshot = replace(
        plan.prospective.snapshot,
        governance_root_identity=held_fs.StableIdentity(7, 11, "directory", 1),
    )
    forged_plan = replace(
        plan,
        prospective=replace(plan.prospective, snapshot=forged_snapshot),
    )
    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.revalidate_destination_policy(
            vault,
            forged_plan,
            principal_contexts=(_principal(),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )

    governance = vault / "Knowledge Base" / "_Governance" / "rules"
    governance.mkdir(parents=True)
    (governance / "foreign.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB4\n"
        f'scope_ids: ["{SCOPE_A}"]\n'
        f"audience: {PRINCIPAL_ID}\n"
        "ceiling: 6\n",
        encoding="utf-8",
    )
    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.revalidate_destination_policy(
            vault,
            plan,
            principal_contexts=(_principal(),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )

    with pytest.raises(consolidation_policy.DestinationPolicyUnavailable):
        consolidation_policy.revalidate_destination_policy(
            vault,
            plan,
            principal_contexts=(_principal(session_id="rotated-session"),),
            destination_vault_id=DESTINATION_VAULT_ID,
            expected_nonce=NONCE,
            verified_at=VERIFIED_AT,
        )
