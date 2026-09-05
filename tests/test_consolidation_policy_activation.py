from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

RUN_ID = "00000000-0000-4000-8000-0000000000f1"
OPERATION_ID = "00000000-0000-4000-8000-0000000000f2"
REQUEST_DIGEST = hashlib.sha256(b"policy-activation-request").hexdigest()
PLAN_DIGEST = hashlib.sha256(b"policy-activation-plan").hexdigest()
BUNDLE_DIGEST = hashlib.sha256(b"policy-activation-bundle").hexdigest()
PREIMAGE_EVENT_ID = f"{hashlib.sha256(b'preimage-event').hexdigest()}:committed"
PREIMAGE_PAYLOAD_DIGEST = hashlib.sha256(b"preimage-payload").hexdigest()
VAULT_BINDING_DIGEST = hashlib.sha256(b"policy-activation-vault").hexdigest()
POLICY_PREPARED_AT = "2026-08-31T10:11:12.345Z"


class SimulatedActivationCrash(BaseException):
    pass


def _prepared_publication():
    from exomem.governance import policy, policy_publication, schema_v4

    documents = (
        (
            "scopes/private.yaml",
            (
                b"governance_version: 1\n"
                b"id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
                b"name: private\n"
                b"paths:\n"
                b"  - Knowledge Base/Notes/**\n"
                b"default_deny: true\n"
            ),
        ),
    )
    compiled = policy.compile_documents(dict(documents))
    expected = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="vault-policy-activation",
        activation_store_id="activation-policy-activation",
        activation_epoch=1,
        activation_state_digest=hashlib.sha256(b"prior-state").hexdigest(),
        policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        policy_fingerprint=hashlib.sha256(b"prior-policy").hexdigest(),
        projector_schema_version=1,
        catalog_generation=1,
        projection_namespace_id="namespace-prior",
    )
    seed = schema_v4.PolicyGenerationSeed(
        generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
        source_documents=documents,
        source_fingerprint=compiled.fingerprint,
        conflict_digest=hashlib.sha256(b"conflicts").hexdigest(),
        compiled_policy=policy.canonical_compiled_bytes(compiled),
        policy_fingerprint=compiled.fingerprint,
        compiler_schema_version=1,
        projector_schema_version=1,
        predecessor_generation_id=expected.policy_generation_id,
        authoring_event_id=hashlib.sha256(b"authoring-event").hexdigest(),
        receipt_event_id=hashlib.sha256(b"publication-event").hexdigest(),
        created_at=1_777_777_777,
    )
    return policy_publication.prepare_policy_publication(
        expected=expected,
        policy=seed,
        catalog=None,
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="namespace-target",
            evidence=b'{"ready":true}',
            ready_at=1_777_777_777,
        ),
        dependent_grants=(),
    )


def test_consolidation_publication_identity_is_exact_and_retry_stable() -> None:
    from exomem.governance import consolidation_policy_activation

    arguments = {
        "destination_vault_id": "vault-policy-activation",
        "vault_binding_digest": VAULT_BINDING_DIGEST,
        "run_id": RUN_ID,
        "operation_id": OPERATION_ID,
        "request_digest": REQUEST_DIGEST,
        "plan_digest": PLAN_DIGEST,
        "policy_bundle_digest": BUNDLE_DIGEST,
        "preimage_terminal_event_id": PREIMAGE_EVENT_ID,
        "preimage_terminal_payload_digest": PREIMAGE_PAYLOAD_DIGEST,
        "policy_prepared_at": POLICY_PREPARED_AT,
    }

    identity = consolidation_policy_activation.derive_policy_publication_identity(
        **arguments
    )

    assert consolidation_policy_activation.derive_policy_publication_identity(
        **arguments
    ) == identity
    assert len(identity.generation_id) == 26
    assert len(identity.authoring_event_id) == 64
    assert len(identity.receipt_event_id) == 64
    assert len(identity.binding_digest) == 64
    assert identity == consolidation_policy_activation.ConsolidationPolicyPublicationIdentity(
        generation_id="01M1BMTCTS0VXP31FDD1YQ3EYC",
        authoring_event_id=(
            "f904db70565e61aa7dbfda2bdc8d13e0278167cb8bb1b9b7aacefe39e983ea5f"
        ),
        receipt_event_id=(
            "cad18fd9fbebb0c521fd40e97d339a4355bf752e0ae1df778e5c82a03cf5e29e"
        ),
        binding_digest=(
            "6ce93bde1b038422782c7c21252d845a5835d1d9b77cf45803d7bf34e13cba1b"
        ),
    )
    assert identity != consolidation_policy_activation.derive_policy_publication_identity(
        **{
            **arguments,
            "policy_bundle_digest": hashlib.sha256(b"changed-bundle").hexdigest(),
        }
    )


def test_prepared_publication_record_round_trips_exact_recovery_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_policy_activation

    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    prepared = _prepared_publication()
    store = consolidation_policy_activation.ConsolidationPolicyPublicationStore(
        vault,
        run_id=RUN_ID,
        effect_ordinal=16,
    )

    created = store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        plan_digest=PLAN_DIGEST,
        policy_bundle_digest=BUNDLE_DIGEST,
        preimage_terminal_event_id=PREIMAGE_EVENT_ID,
        preimage_terminal_payload_digest=PREIMAGE_PAYLOAD_DIGEST,
        publication=prepared,
    )

    assert created.publication == prepared
    assert store.load() == created
    assert store.create(
        operation_id=OPERATION_ID,
        request_digest=REQUEST_DIGEST,
        plan_digest=PLAN_DIGEST,
        policy_bundle_digest=BUNDLE_DIGEST,
        preimage_terminal_event_id=PREIMAGE_EVENT_ID,
        preimage_terminal_payload_digest=PREIMAGE_PAYLOAD_DIGEST,
        publication=prepared,
    ) == created
    with pytest.raises(
        consolidation_policy_activation.ConsolidationPolicyActivationUnavailable,
        match="^CONSOLIDATION_POLICY_ACTIVATION_UNAVAILABLE$",
    ):
        store.create(
            operation_id=OPERATION_ID,
            request_digest=hashlib.sha256(b"changed-request").hexdigest(),
            plan_digest=PLAN_DIGEST,
            policy_bundle_digest=BUNDLE_DIGEST,
            preimage_terminal_event_id=PREIMAGE_EVENT_ID,
            preimage_terminal_payload_digest=PREIMAGE_PAYLOAD_DIGEST,
            publication=prepared,
        )


def test_policy_activation_exports_the_receipt_first_entrypoint() -> None:
    from exomem.governance import consolidation_policy_activation

    assert callable(
        consolidation_policy_activation.activate_stored_destination_policy
    )
    assert (
        consolidation_policy_activation.ConsolidationPolicyActivationResult
        is not None
    )


def test_shared_workspace_mirror_requires_caller_owned_governance_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import policy, policy_publication, receipts

    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )
    vault = tmp_path / "vault"
    prepared = _prepared_publication()
    governance = vault / "Knowledge Base" / "_Governance"
    for relative, content in prepared.policy.source_documents:
        target = governance / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    reviewed = policy.observe_authoring_snapshot(vault)
    assert reviewed is not None
    mirror = policy_publication.WorkspaceMirror(
        receipt=policy_publication.CriticalReceipt(
            event_id=receipts.critical_event_id(
                {"consolidation-workspace-mirror": "caller-authority"}
            ),
            operation="governance_policy_workspace_mirror",
            prior="1" * 64,
            prepared="2" * 64,
            target="3" * 64,
            affected_ids=("4" * 64,),
            parent_causation_id=prepared.identity.receipt_event_id,
        ),
        outcomes=frozenset({"complete", "diverged"}),
    )
    bound = policy_publication.prepare_workspace_mirror(
        prepared,
        mirror=mirror,
        reviewed=reviewed,
    )

    with pytest.raises(policy_publication.GovernanceError):
        policy_publication.run_prepared_workspace_mirror(vault, bound)


@pytest.mark.parametrize(
    ("crash_point", "expected"),
    (
        (None, "success"),
        ("authorization-clock-advance", "authorization-expiry-refusal"),
        ("after-policy-record", "recovered-prior"),
        ("after-inner-receipt-intent", "expired-prior-refusal"),
        ("after-tuple-custody-activation", "recovered"),
        ("after-inner-terminal", "recovered"),
        ("after-mirror", "mirror-divergence-refusal"),
    ),
)
def test_policy_activation_runs_the_real_preimage_chain_without_publishing_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str | None,
    expected: str,
) -> None:
    from exomem import vault as vault_module
    from exomem import writer_lease
    from exomem.governance import (
        consolidation_admission,
        consolidation_apply_coordinator,
        consolidation_authority,
        consolidation_fingerprints,
        consolidation_plan_store,
        consolidation_policy,
        consolidation_policy_activation,
        consolidation_receipts,
        consolidation_saga,
        consolidation_seal,
        policy,
        receipts,
        schema_v4,
        store,
    )
    from tests.test_consolidation_apply_coordinator import (
        JOURNAL_DIGEST as APPLY_JOURNAL_DIGEST,
    )
    from tests.test_consolidation_apply_coordinator import (
        OPERATION_ID as APPLY_OPERATION_ID,
    )
    from tests.test_consolidation_apply_coordinator import (
        PLAN_DIGEST as APPLY_PLAN_DIGEST,
    )
    from tests.test_consolidation_apply_coordinator import (
        REQUEST_DIGEST as APPLY_REQUEST_DIGEST,
    )
    from tests.test_consolidation_apply_coordinator import RUN_ID as APPLY_RUN_ID
    from tests.test_consolidation_apply_coordinator import (
        T0,
        T1,
        T2,
        T3,
        VAULT_BINDING,
        _append_token_reservation,
        _identity,
    )
    from tests.test_consolidation_policy import _principal
    from tests.test_governance_active_tuple import (
        LOGICAL_VAULT_ID,
        _configure_custody,
        _documents,
        _migrate_with_projection_item,
        _write_workspace,
    )

    now = int(time.time())
    # This synthetic saga uses fixed phase timestamps. Its nested governance
    # receipts must share that clock instead of rotating to the real month.
    monkeypatch.setattr(receipts, "_now", lambda: T1)
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    source = "---\ntype: note\n---\ndestination before activation\n"
    content_path = vault / "Knowledge Base" / "Notes" / "destination.md"
    content_path.parent.mkdir(parents=True)
    content_path.write_text(source, encoding="utf-8")
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly: []\n",
        encoding="utf-8",
    )
    (vault / "Knowledge Base" / ".review-state.json").write_text(
        '{"version":2,"records":{}}',
        encoding="utf-8",
    )
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(
        vault,
        path="Knowledge Base/Notes/destination.md",
        source=source,
        now=now,
    )
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    monkeypatch.setattr(
        consolidation_fingerprints,
        "load_local_identity",
        lambda *_args, **_kwargs: dataclasses.replace(
            _identity(tmp_path),
            vault_id=LOGICAL_VAULT_ID,
        ),
    )
    predecessor = _append_token_reservation(vault)
    edits = {
        "rules/external.yaml": dict(_documents(ceiling=1))[
            "rules/external.yaml"
        ].decode("utf-8")
    }
    principal = _principal(principal_id="external", vault_id=LOGICAL_VAULT_ID)
    attestation = consolidation_policy.issue_destination_principal_attestation(
        principal,
        destination_vault_id=LOGICAL_VAULT_ID,
        purposes=(),
        issued_at="2026-08-30T11:59:00.000Z",
        expires_at="2026-09-01T12:00:00.000Z",
        nonce="policy-activation-nonce",
    )
    bundle = consolidation_policy.compile_destination_policy(
        vault,
        documents=edits,
        source_authority=(),
        attestations=(attestation,),
        principal_contexts=(principal,),
        destination_vault_id=LOGICAL_VAULT_ID,
        expected_nonce="policy-activation-nonce",
        verified_at=T1,
    )
    snapshot = consolidation_fingerprints.load_local_destination_snapshot(
        vault,
        now=now,
    )
    content_census_before = (
        consolidation_fingerprints.load_local_source_content_census(vault).digest
    )
    plan = SimpleNamespace(
        digest=APPLY_PLAN_DIGEST,
        control_basis=SimpleNamespace(digest=hashlib.sha256(b"control").hexdigest()),
        preimage={
            "run_id": APPLY_RUN_ID,
            "plan_kind": "cutover",
            "destination_snapshot_fingerprint": snapshot.digest,
            "expected_destination_preimage_census_digest": (
                snapshot.canonical_census_digest
            ),
            "nonce": bundle.nonce,
            "policy_bundle_digest": bundle.digest,
        },
    )
    monkeypatch.setattr(
        consolidation_plan_store.ConsolidationPlanStore,
        "load",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        consolidation_plan_store.ConsolidationPlanStore,
        "load_policy_bundle",
        lambda *_args, **_kwargs: bundle,
    )
    monkeypatch.setattr(
        consolidation_apply_coordinator,
        "_policy_verification_timestamp",
        lambda: T1,
    )

    def refuse_content_write(*_args: object, **_kwargs: object) -> None:
        pytest.fail("policy activation invoked the content batch writer")

    monkeypatch.setattr(vault_module, "batch_atomic_write", refuse_content_write)
    live_prospective = policy.compile_prospective(vault, edits)
    assert live_prospective == bundle.prospective
    seal_store = consolidation_seal.ConsolidationSealStore(vault)
    seal_store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    admission = consolidation_admission.ConsolidationAdmission(
        vault,
        vault_binding_digest=VAULT_BINDING,
    )
    authority = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=APPLY_RUN_ID,
        operation_id=APPLY_OPERATION_ID,
        journal_digest=APPLY_JOURNAL_DIGEST,
        phase="sealing",
        action="apply",
    )
    content_before = content_path.read_bytes()
    with admission.admit_mutation() as mutation:
        control = admission.convert_control_mutation(
            mutation,
            authority=authority,
            run_id=APPLY_RUN_ID,
            operation_id=APPLY_OPERATION_ID,
            journal_digest=APPLY_JOURNAL_DIGEST,
            request_digest=APPLY_REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        )
        preparation = consolidation_apply_coordinator.prepare_apply_through_preimage(
            vault_root=vault,
            admission=admission,
            control=control,
            artifact_store=__import__(
                "exomem.governance.consolidation_intake",
                fromlist=["PrivateConsolidationArtifactStore"],
            ).PrivateConsolidationArtifactStore(
                tmp_path / "private-artifacts",
                active_vault_roots=(vault,),
            ),
            token_reservation_event_id=str(predecessor["event_id"]),
            token_reservation_payload_digest=str(
                predecessor["consolidation_event"]["payload_digest"]
            ),
            vault_binding_digest=VAULT_BINDING,
            run_id=APPLY_RUN_ID,
            operation_id=APPLY_OPERATION_ID,
            journal_digest=APPLY_JOURNAL_DIGEST,
            request_digest=APPLY_REQUEST_DIGEST,
            plan_digest=APPLY_PLAN_DIGEST,
            principal_contexts=(principal,),
            sealed_at=T1,
            drained_at=T2,
            preimage_ready_at=T3,
            now=now,
            timeout=2.0,
        )
        activation_arguments = {
            "vault_root": vault,
            "admission": admission,
            "preparation": preparation,
            "vault_binding_digest": VAULT_BINDING,
            "run_id": APPLY_RUN_ID,
            "operation_id": APPLY_OPERATION_ID,
            "journal_digest": APPLY_JOURNAL_DIGEST,
            "request_digest": APPLY_REQUEST_DIGEST,
            "plan_digest": APPLY_PLAN_DIGEST,
            "principal_contexts": (principal,),
            "policy_prepare_at": "2026-08-30T12:00:04.000Z",
            "policy_active_at": "2026-08-30T12:00:05.000Z",
            "timeout": 2.0,
        }
        authorization_clock_calls = 0

        def authorization_now() -> int:
            nonlocal authorization_clock_calls
            authorization_clock_calls += 1
            if expected == "authorization-expiry-refusal" and (
                authorization_clock_calls > 1
            ):
                return now + 7_200
            return now

        monkeypatch.setattr(
            consolidation_policy_activation,
            "_authorization_now",
            authorization_now,
        )
        monkeypatch.setattr(
            consolidation_policy_activation,
            "_policy_verification_timestamp",
            lambda: "2026-08-30T12:00:04.000Z",
            raising=False,
        )
        crashed = False

        def crash_once(point: str) -> None:
            nonlocal crashed
            if point == crash_point and not crashed:
                crashed = True
                raise SimulatedActivationCrash

        monkeypatch.setattr(
            consolidation_policy_activation,
            "_crash_point",
            crash_once,
        )
        if expected == "authorization-expiry-refusal":
            with pytest.raises(
                consolidation_policy_activation.ConsolidationPolicyActivationUnavailable
            ):
                consolidation_policy_activation.activate_stored_destination_policy(
                    **activation_arguments
                )
            result = None
        elif crash_point is None:
            result = consolidation_policy_activation.activate_stored_destination_policy(
                **activation_arguments
            )
        else:
            with pytest.raises(SimulatedActivationCrash):
                consolidation_policy_activation.activate_stored_destination_policy(
                    **activation_arguments
                )
            if expected != "recovered-prior":
                monkeypatch.setattr(
                    consolidation_policy_activation,
                    "_policy_verification_timestamp",
                    lambda: "2026-09-01T12:00:00.000Z",
                    raising=False,
                )
            if expected == "mirror-divergence-refusal":
                (vault / "Knowledge Base" / "_Governance" / "rules" / "external.yaml").write_bytes(
                    dict(_documents(ceiling=0))["rules/external.yaml"]
                )
            if expected in {
                "expired-prior-refusal",
                "mirror-divergence-refusal",
            }:
                with pytest.raises(
                    consolidation_policy_activation.ConsolidationPolicyActivationUnavailable
                ):
                    consolidation_policy_activation.activate_stored_destination_policy(
                        **activation_arguments
                    )
                result = None
            else:
                result = (
                    consolidation_policy_activation.activate_stored_destination_policy(
                        **activation_arguments
                    )
                )
                receipt_count = len(receipts.event_records(vault))
                assert (
                    consolidation_policy_activation.activate_stored_destination_policy(
                        **activation_arguments
                    )
                    == result
                )
                assert len(receipts.event_records(vault)) == receipt_count

    after = consolidation_fingerprints.load_local_destination_snapshot(vault, now=now)
    if expected in {
        "authorization-expiry-refusal",
        "expired-prior-refusal",
        "mirror-divergence-refusal",
    }:
        assert result is None
        assert seal_store.load(vault_binding_digest=VAULT_BINDING).phase == "preimage-ready"
        assert content_path.read_bytes() == content_before
        with sqlite3.connect(store.sidecar_path(vault)) as connection:
            active = schema_v4.load_active_tuple_pointer(connection)
            assert active.activation_epoch == (
                1
                if expected
                in {"authorization-expiry-refusal", "expired-prior-refusal"}
                else 2
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM governance_tuple_publications "
                "WHERE publication_kind='policy'"
            ).fetchone() == (
                (0,)
                if expected
                in {"authorization-expiry-refusal", "expired-prior-refusal"}
                else (1,)
            )
        return

    assert result is not None
    assert result.seal_state.phase == "policy-active"
    assert result.terminal.policy_fingerprint == bundle.prospective.policy.fingerprint
    assert (
        consolidation_saga._verify_policy_terminal_receipt(  # noqa: SLF001
            vault_root=vault,
            vault_binding_digest=VAULT_BINDING,
            terminal=result.terminal,
            expected_policy_fingerprint=bundle.prospective.policy.fingerprint,
        )
        == result.terminal
    )
    assert content_path.read_bytes() == content_before
    assert (
        consolidation_fingerprints.load_local_source_content_census(vault).digest
        == content_census_before
    )
    assert after.canonical_census_digest != snapshot.canonical_census_digest
    assert policy.load(vault).fingerprint == bundle.prospective.policy.fingerprint
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        active = schema_v4.load_active_tuple_pointer(connection)
        assert active.activation_epoch == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_tuple_publications "
            "WHERE publication_kind='policy'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_proposals"
        ).fetchone() == (0,)

    outer = [
        row
        for row in consolidation_receipts._active_records(vault)  # noqa: SLF001
        if row.get("event_type") == "consolidation"
    ]
    assert [
        (row["consolidation_event"]["kind"], row["phase"])
        for row in outer[-4:]
    ] == [
        ("policy-prepare", "intent"),
        ("policy-prepare", "committed"),
        ("policy-active", "intent"),
        ("policy-active", "committed"),
    ]
    record = consolidation_policy_activation.ConsolidationPolicyPublicationStore(
        vault,
        run_id=APPLY_RUN_ID,
        effect_ordinal=15,
    ).load()
    policy_receipt = consolidation_policy_activation._policy_receipt(record)  # noqa: SLF001
    mirror_receipt = consolidation_policy_activation._workspace_mirror(  # noqa: SLF001
        record
    ).receipt
    all_receipts = receipts.event_records(vault)

    def terminal_pair(event_id: str) -> list[tuple[object, object]]:
        return [
            (row.get("phase"), row.get("outcome"))
            for row in all_receipts
            if row.get("event_id") == event_id
            or row.get("causation_id") == event_id
        ]

    assert terminal_pair(policy_receipt.event_id) == [
        ("intent", None),
        ("committed", "committed"),
    ]
    assert terminal_pair(mirror_receipt.event_id) == [
        ("intent", None),
        ("committed", "complete"),
    ]
    if crash_point is None:
        predecessor_authority = record.publication.expected
        with sqlite3.connect(store.sidecar_path(vault)) as connection:
            connection.execute(
                "UPDATE active_governance_tuple SET policy_generation_id=?, "
                "policy_fingerprint=?, projector_schema_version=?, "
                "catalog_generation=? WHERE singleton=1",
                (
                    predecessor_authority.policy_generation_id,
                    predecessor_authority.policy_fingerprint,
                    predecessor_authority.projector_schema_version,
                    predecessor_authority.catalog_generation,
                ),
            )
            connection.execute(
                "UPDATE governance_activation_store SET activation_epoch=?, "
                "activation_state_digest=? WHERE singleton=1",
                (
                    predecessor_authority.activation_epoch,
                    predecessor_authority.activation_state_digest,
                ),
            )
            connection.commit()
        _configure_custody(
            monkeypatch,
            tmp_path / "custody",
            activation_epoch=predecessor_authority.activation_epoch,
            activation_state_digest=(
                predecessor_authority.activation_state_digest
            ),
            now=now,
        )

        with pytest.raises(
            consolidation_saga.PolicyFirstPublicationUnavailable
        ):
            consolidation_saga._verify_policy_terminal_receipt(  # noqa: SLF001
                vault_root=vault,
                vault_binding_digest=VAULT_BINDING,
                terminal=result.terminal,
                expected_policy_fingerprint=(
                    bundle.prospective.policy.fingerprint
                ),
            )
