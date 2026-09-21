from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from exomem import adoption_proposals, adoption_run
from exomem.governance import hosted_mutation_journal
from exomem.writer_lease import IdempotencyStore


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _descriptor(
    *,
    children: int = 1,
    final_sidecar: bool = False,
    sidecar_id: str = "sidecar-0",
) -> dict[str, object]:
    required = [
        {
            "id": f"child-{index}",
            "kind": "catalog",
            "plan_sha256": _sha(f"plan-{index}".encode()),
            "requires": [] if index == 0 else [f"child-{index - 1}"],
        }
        for index in range(children)
    ]
    if final_sidecar:
        required.append(
            {
                "id": sidecar_id,
                "kind": "sidecar",
                "plan_sha256": _sha(b"sidecar-plan"),
                "requires": [required[-1]["id"]],
            }
        )
    return {
        "version": "exomem.prepared-canonical-mutation/v1",
        "scoped_idempotency_digest": "1" * 64,
        "command_digest": "2" * 64,
        "attempt_id": "3" * 24,
        "commit_token": "4" * 24,
        "command": "remember",
        "selector_digest": "5" * 64,
        "cell_id": "cell-1",
        "logical_vault_id": "vault-1",
        "registry_attachment_id": "attachment-1",
        "attachment_epoch": 7,
        "activation_store_id": "store-1",
        "required_children": required,
        "result_recipe": {
            "kind": "child-field",
            "child_id": required[-1]["id"],
            "field": "path",
        },
    }


def _recovery(
    *, children: int = 1, final_sidecar: bool = False, sidecar_id: str = "sidecar-0"
) -> hosted_mutation_journal.PreparedCanonicalMutationRecovery:
    descriptor = _descriptor(
        children=children,
        final_sidecar=final_sidecar,
        sidecar_id=sidecar_id,
    )
    results = {
        f"child-{index}": {"path": f"Notes/{index}.md"} for index in range(children)
    }
    if final_sidecar:
        results[sidecar_id] = {"path": "Notes/applied.md"}
    return hosted_mutation_journal.prepare_canonical_mutation_recovery(
        descriptor=descriptor,
        prepared_results=results,
        attempt_secret=b"s" * 32,
    )


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=4")
    connection.executescript(
        """
        CREATE TABLE governance_operation_journals (
          event_id TEXT PRIMARY KEY, operation TEXT NOT NULL, causation_id TEXT NOT NULL,
          authorization_session TEXT, principal_id TEXT NOT NULL, phase TEXT NOT NULL,
          direction TEXT NOT NULL, prior_digest TEXT NOT NULL, prepared_digest TEXT NOT NULL,
          final_digest TEXT NOT NULL, affected_ids TEXT NOT NULL,
          required_child_intents TEXT NOT NULL, required_child_terminals TEXT NOT NULL,
          proposal_id TEXT, attempt_no INTEGER, marker_required INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, blocked_reason TEXT
        );
        CREATE TABLE governance_operation_components (
          event_id TEXT NOT NULL, phase TEXT NOT NULL, ordinal INTEGER NOT NULL,
          component_kind TEXT NOT NULL, component_key TEXT NOT NULL,
          value_json TEXT NOT NULL, value_hash TEXT NOT NULL, status TEXT NOT NULL,
          PRIMARY KEY(event_id, phase, ordinal)
        );
        CREATE TABLE governance_tuple_publications (
          event_id TEXT PRIMARY KEY, publication_kind TEXT NOT NULL,
          predecessor_activation_state_digest TEXT NOT NULL,
          target_activation_state_digest TEXT NOT NULL,
          policy_generation_id TEXT NOT NULL, policy_fingerprint TEXT NOT NULL,
          projector_schema_version INTEGER NOT NULL, catalog_generation INTEGER NOT NULL,
          activation_epoch INTEGER NOT NULL, status TEXT NOT NULL, activated_at INTEGER NOT NULL
        );
        """
    )
    return connection


def _publication(connection: sqlite3.Connection, *, event_id: str, epoch: int = 8) -> None:
    connection.execute(
        "INSERT INTO governance_tuple_publications VALUES (?, 'catalog', ?, ?, ?, ?, ?, ?, ?, 'committed', ?)",
        (event_id, "a" * 64, "b" * 64, "policy-1", "c" * 64, 1, epoch, epoch, 100),
    )


def test_plan_precedes_child_and_complete_child_set_mints_commit(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "governance.sqlite")
    recovery = _recovery()
    plan = hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=b"s" * 32,
        dependency_manifest={"catalog_generations": [7, 8]},
        now=10.0,
    )
    assert connection.execute(
        "SELECT phase FROM governance_operation_journals WHERE event_id=?", (plan.event_id,)
    ).fetchone() == ("allocating",)

    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    evidence = hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-0",
        publication_event_id="publication-1",
        attempt_secret=b"s" * 32,
        now=11.0,
    )
    connection.commit()

    assert evidence.complete is True
    assert connection.execute(
        "SELECT phase FROM governance_operation_journals WHERE event_id=?", (plan.event_id,)
    ).fetchone() == ("pending",)
    assert connection.execute(
        "SELECT component_kind FROM governance_operation_components WHERE event_id=? ORDER BY ordinal",
        (plan.event_id,),
    ).fetchall() == [
        ("hosted-mutation-plan/v1",),
        ("hosted-mutation-child/v1",),
        ("hosted-mutation-commit/v1",),
    ]


def test_incomplete_child_set_stays_pending_and_finalizes_without_publication(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "governance.sqlite")
    recovery = _recovery(children=2)
    hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=b"s" * 32,
        dependency_manifest={},
        now=10.0,
    )
    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    first = hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-0",
        publication_event_id="publication-1",
        attempt_secret=b"s" * 32,
        now=11.0,
    )
    connection.commit()
    assert first.complete is False

    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-2", epoch=9)
    second = hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-1",
        publication_event_id="publication-2",
        attempt_secret=b"s" * 32,
        now=12.0,
    )
    connection.commit()
    assert second.complete is True
    assert connection.execute("SELECT count(*) FROM governance_tuple_publications").fetchone() == (2,)


def test_sidecar_completion_receipt_finalizes_without_advancing_activation(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path / "governance.sqlite")
    recovery = _recovery(final_sidecar=True)
    hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=b"s" * 32,
        dependency_manifest={},
        now=10.0,
    )
    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    first = hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-0",
        publication_event_id="publication-1",
        attempt_secret=b"s" * 32,
        now=11.0,
    )
    connection.commit()
    assert first.complete is False

    transition = {
        "proposal_id": "proposal-1",
        "status": "applied",
        "applied": {"at": "2026-09-20T12:00:00Z", "result_path": "Notes/applied.md"},
    }
    receipt = hosted_mutation_journal.prepare_sidecar_completion_receipt(
        recovery=recovery,
        child_id="sidecar-0",
        transition=transition,
        attempt_secret=b"s" * 32,
    )
    complete = hosted_mutation_journal.record_sidecar_child(
        connection,
        recovery=recovery,
        child_id="sidecar-0",
        transition=transition,
        receipt=receipt,
        attempt_secret=b"s" * 32,
        now=12.0,
    )

    assert complete.complete is True
    assert connection.execute("SELECT count(*) FROM governance_tuple_publications").fetchone() == (1,)
    commit = json.loads(
        connection.execute(
            "SELECT value_json FROM governance_operation_components "
            "WHERE component_kind='hosted-mutation-commit/v1'"
        ).fetchone()[0]
    )
    assert commit["final_publication"]["event_id"] == "publication-1"


def test_sidecar_completion_receipt_rejects_substituted_transition(tmp_path: Path) -> None:
    recovery = _recovery(final_sidecar=True)
    transition = {"proposal_id": "proposal-1", "status": "applied"}
    receipt = hosted_mutation_journal.prepare_sidecar_completion_receipt(
        recovery=recovery,
        child_id="sidecar-0",
        transition=transition,
        attempt_secret=b"s" * 32,
    )
    with pytest.raises(
        hosted_mutation_journal.HostedMutationJournalError,
        match="completion receipt",
    ):
        hosted_mutation_journal.verify_sidecar_completion_receipt(
            recovery=recovery,
            child_id="sidecar-0",
            transition={**transition, "status": "proposed"},
            receipt=receipt,
            attempt_secret=b"s" * 32,
        )


def test_adoption_completion_recovers_stored_receipt_without_replaying_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "_Adoption" / "runs" / "run-1").mkdir(
        parents=True
    )
    governance_path = tmp_path / "governance.sqlite"
    connection = _connection(governance_path)
    recovery = _recovery(
        final_sidecar=True, sidecar_id="adoption-completion-0"
    )
    hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=b"s" * 32,
        dependency_manifest={},
        now=10.0,
    )
    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-0",
        publication_event_id="publication-1",
        attempt_secret=b"s" * 32,
        now=11.0,
    )
    connection.commit()
    connection.close()

    transition = {
        "proposal_id": "proposal-1",
        "status": "applied",
        "applied": {"result_path": "Notes/applied.md"},
    }
    prior = {"proposal_id": "proposal-1", "status": "proposed", "applied": None}
    result_payloads = dict(recovery.prepared_results)
    result_payloads["adoption-completion-0"] = {
        **result_payloads["adoption-completion-0"],
        "run_id": "run-1",
        "proposal_id": "proposal-1",
        "prior": prior,
        "transition": transition,
    }
    recovery = hosted_mutation_journal.prepare_canonical_mutation_recovery(
        descriptor=recovery.descriptor,
        prepared_results=result_payloads,
        attempt_secret=b"s" * 32,
    )
    # Replace the allocating plan so it binds the exact recovery payload used
    # by the simulated restarted process.
    connection = sqlite3.connect(governance_path)
    connection.execute("DELETE FROM governance_operation_components")
    connection.execute("DELETE FROM governance_operation_journals")
    connection.execute("DELETE FROM governance_tuple_publications")
    connection.commit()
    hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=b"s" * 32,
        dependency_manifest={},
        now=10.0,
    )
    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-0",
        publication_event_id="publication-1",
        attempt_secret=b"s" * 32,
        now=11.0,
    )
    connection.commit()
    connection.close()
    adoption_run.AdoptionRunStore(vault).save_proposals(
        "run-1",
        {
            "schema_version": 1,
            "run_id": "run-1",
            "proposals": [{**prior, "status": "applying"}],
        },
    )

    from exomem.governance import store as governance_store

    monkeypatch.setattr(
        governance_store,
        "open_authorization_session_connection",
        lambda _root: sqlite3.connect(governance_path),
    )
    assert adoption_proposals.recover_hosted_completion_receipt(
        vault,
        recovery,
        attempt_secret=b"s" * 32,
        now=12.0,
    )
    with sqlite3.connect(governance_path) as verify:
        kinds = verify.execute(
            "SELECT component_kind FROM governance_operation_components ORDER BY ordinal"
        ).fetchall()
    assert kinds == [
        ("hosted-mutation-plan/v1",),
        ("hosted-mutation-child/v1",),
        ("hosted-mutation-child/v1",),
        ("hosted-mutation-commit/v1",),
    ]
    saved = adoption_run.AdoptionRunStore(vault).load_proposals("run-1")
    assert saved["proposals"] == [transition]
    assert set(saved["_hosted_completion_receipts"]) == {"proposal-1"}


def test_child_refuses_tampered_private_result(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "governance.sqlite")
    recovery = _recovery()
    hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=b"s" * 32,
        dependency_manifest={},
        now=10.0,
    )
    tampered = hosted_mutation_journal.PreparedCanonicalMutationRecovery(
        descriptor=recovery.descriptor,
        descriptor_sha256=recovery.descriptor_sha256,
        descriptor_mac=recovery.descriptor_mac,
        prepared_results={"child-0": {"path": "Notes/changed.md"}},
        result_macs=recovery.result_macs,
    )
    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    with pytest.raises(hosted_mutation_journal.HostedMutationJournalError):
        hosted_mutation_journal.record_child_in_transaction(
            connection,
            recovery=tampered,
            child_id="child-0",
            publication_event_id="publication-1",
            attempt_secret=b"s" * 32,
            now=11.0,
        )
    connection.rollback()


def test_closed_writer_journal_dependencies_are_strictly_decoded(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "governance.sqlite")
    recovery = _recovery()
    plan = hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=b"s" * 32,
        dependency_manifest={"projection_namespaces": ["namespace-1"]},
        now=10.0,
    )
    assert hosted_mutation_journal.retained_dependency_pins(connection) == {
        "catalog_generations": frozenset(),
        "component_keys": frozenset({recovery.descriptor_sha256}),
        "measurement_stores": frozenset(),
        "policy_generations": frozenset(),
        "projection_namespaces": frozenset({"namespace-1"}),
        "publications": frozenset(),
    }
    connection.execute(
        "UPDATE governance_operation_components SET value_json='{}' WHERE event_id=?",
        (plan.event_id,),
    )
    with pytest.raises(hosted_mutation_journal.HostedMutationJournalError):
        hosted_mutation_journal.retained_dependency_pins(connection)


def test_recovery_json_is_data_only() -> None:
    recovery = _recovery()
    payload = recovery.to_json()
    assert json.loads(payload)["descriptor"]["command"] == "remember"
    assert "secret" not in payload


def test_idempotency_store_persists_and_revalidates_private_preparation(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    disposition, _ = store._claim_or_inspect("key-1", "2" * 64, None)
    assert disposition == "owner"
    attempt = store._attempts["key-1"]
    descriptor = _descriptor()
    descriptor["scoped_idempotency_digest"] = _sha(b"key-1")
    descriptor["attempt_id"] = attempt.attempt_id
    descriptor["commit_token"] = attempt.commit_token
    recovery = hosted_mutation_journal.prepare_canonical_mutation_recovery(
        descriptor=descriptor,
        prepared_results={"child-0": {"path": "Notes/0.md"}},
        attempt_secret=attempt.commit_secret,
    )

    store.persist_prepared_recovery("key-1", "2" * 64, recovery)
    assert store.load_prepared_recovery("key-1", "2" * 64) == recovery
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT state, prepared_recovery_json FROM mutations WHERE key='key-1'"
        ).fetchone()
    assert row is not None and row[0] == "executing"
    assert json.loads(row[1])["descriptor_sha256"] == recovery.descriptor_sha256
    assert store.completed_terminal("key-1", "2" * 64) is None


def test_idempotency_store_refuses_substituted_attempt_preparation(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    store._claim_or_inspect("key-1", "2" * 64, None)
    attempt = store._attempts["key-1"]
    descriptor = _descriptor()
    descriptor["scoped_idempotency_digest"] = _sha(b"key-1")
    descriptor["attempt_id"] = "f" * 24
    descriptor["commit_token"] = attempt.commit_token
    recovery = hosted_mutation_journal.prepare_canonical_mutation_recovery(
        descriptor=descriptor,
        prepared_results={"child-0": {"path": "Notes/0.md"}},
        attempt_secret=attempt.commit_secret,
    )
    with pytest.raises(Exception, match="prepared mutation identity"):
        store.persist_prepared_recovery("key-1", "2" * 64, recovery)


def _committed_fixture(tmp_path: Path) -> tuple[Path, IdempotencyStore, object]:
    private_dir = tmp_path / "state"
    private_dir.mkdir(mode=0o700)
    governance_path = private_dir / "governance.sqlite"
    connection = _connection(governance_path)
    store = IdempotencyStore(private_dir / "idempotency.sqlite")
    store._claim_or_inspect("key-1", "2" * 64, None)
    attempt = store._attempts["key-1"]
    descriptor = _descriptor()
    descriptor["scoped_idempotency_digest"] = _sha(b"key-1")
    descriptor["attempt_id"] = attempt.attempt_id
    descriptor["commit_token"] = attempt.commit_token
    recovery = hosted_mutation_journal.prepare_canonical_mutation_recovery(
        descriptor=descriptor,
        prepared_results={"child-0": {"path": "Notes/0.md"}},
        attempt_secret=attempt.commit_secret,
    )
    store.persist_prepared_recovery("key-1", "2" * 64, recovery)
    hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=attempt.commit_secret,
        dependency_manifest={},
        now=10.0,
    )
    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-0",
        publication_event_id="publication-1",
        attempt_secret=attempt.commit_secret,
        now=11.0,
    )
    connection.commit()
    connection.close()
    for path in (governance_path, store.path):
        path.chmod(0o600)
    return governance_path, store, recovery


def test_read_only_proof_reader_verifies_actual_publication_and_private_payload(
    tmp_path: Path,
) -> None:
    governance_path, store, recovery = _committed_fixture(tmp_path)
    evidence = hosted_mutation_journal.read_committed_publication_evidence(
        governance_db_path=governance_path,
        idempotency_db_path=store.path,
        selector=hosted_mutation_journal.PublicationSelectorEvidence(
            descriptor_sha256=recovery.descriptor_sha256,
            child_id="child-0",
        ),
    )
    assert evidence is not None
    assert evidence.component_kind == "hosted-mutation-child/v1"
    assert evidence.logical_vault_id == "vault-1"
    assert evidence.publication.target_activation_state_digest == "b" * 64
    assert "Notes/0.md" not in repr(evidence)


def test_proof_reader_refuses_non_owner_only_private_database(tmp_path: Path) -> None:
    governance_path, store, recovery = _committed_fixture(tmp_path)
    store.path.chmod(0o644)
    with pytest.raises(hosted_mutation_journal.HostedMutationJournalError):
        hosted_mutation_journal.read_committed_publication_evidence(
            governance_db_path=governance_path,
            idempotency_db_path=store.path,
            selector=hosted_mutation_journal.PublicationSelectorEvidence(
                descriptor_sha256=recovery.descriptor_sha256,
                child_id=None,
            ),
        )


def test_only_complete_aggregate_evidence_can_render_recipe(tmp_path: Path) -> None:
    governance_path, store, recovery = _committed_fixture(tmp_path)
    aggregate = hosted_mutation_journal.read_committed_publication_evidence(
        governance_db_path=governance_path,
        idempotency_db_path=store.path,
        selector=hosted_mutation_journal.PublicationSelectorEvidence(
            descriptor_sha256=recovery.descriptor_sha256,
            child_id=None,
        ),
    )
    verified = hosted_mutation_journal.verify_complete_canonical_evidence(
        recovery, aggregate
    )
    seen: list[object] = []

    def current_egress(value: object) -> object:
        seen.append(value)
        return {"path": value}

    assert hosted_mutation_journal.render_verified_result(
        recovery, verified, egress_filter=current_egress
    ) == {"path": "Notes/0.md"}
    assert seen == ["Notes/0.md"]

    child = hosted_mutation_journal.read_committed_publication_evidence(
        governance_db_path=governance_path,
        idempotency_db_path=store.path,
        selector=hosted_mutation_journal.PublicationSelectorEvidence(
            descriptor_sha256=recovery.descriptor_sha256,
            child_id="child-0",
        ),
    )
    with pytest.raises(hosted_mutation_journal.HostedMutationJournalError):
        hosted_mutation_journal.verify_complete_canonical_evidence(recovery, child)


def test_public_publication_selector_derives_private_component_identity(tmp_path: Path) -> None:
    governance_path, store, _recovery = _committed_fixture(tmp_path)
    evidence = hosted_mutation_journal.read_committed_publication_evidence_for_selector(
        governance_db_path=governance_path,
        idempotency_db_path=store.path,
        selector={
            "publication_event_id": "publication-1",
            "predecessor": {
                "activation_store_id": "store-1",
                "activation_epoch": 7,
                "activation_state_digest": "a" * 64,
            },
            "successor": {
                "activation_store_id": "store-1",
                "activation_epoch": 8,
                "activation_state_digest": "b" * 64,
            },
        },
    )
    assert evidence is not None
    assert evidence.component_kind == "hosted-mutation-commit/v1"
    assert evidence.publication.event_id == "publication-1"

    with pytest.raises(hosted_mutation_journal.HostedMutationJournalError):
        hosted_mutation_journal.read_committed_publication_evidence_for_selector(
            governance_db_path=governance_path,
            idempotency_db_path=store.path,
            selector={
                "publication_event_id": "publication-1",
                "predecessor": {
                    "activation_store_id": "store-1",
                    "activation_epoch": 7,
                    "activation_state_digest": "f" * 64,
                },
                "successor": {
                    "activation_store_id": "store-1",
                    "activation_epoch": 8,
                    "activation_state_digest": "b" * 64,
                },
            },
        )


def test_public_selector_refuses_forged_candidate_borrowing_old_private_proof(
    tmp_path: Path,
) -> None:
    governance_path, store, recovery = _committed_fixture(tmp_path)
    with sqlite3.connect(governance_path) as connection:
        _publication(connection, event_id="publication-2", epoch=9)
        connection.execute(
            "UPDATE governance_tuple_publications SET "
            "predecessor_activation_state_digest=?, target_activation_state_digest=? "
            "WHERE event_id='publication-2'",
            ("b" * 64, "d" * 64),
        )
        forged = {
            "descriptor_sha256": recovery.descriptor_sha256,
            "publication": {"event_id": "publication-2"},
        }
        encoded = json.dumps(forged, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO governance_operation_journals "
            "SELECT 'forged-event', operation, causation_id, authorization_session, "
            "principal_id, phase, direction, prior_digest, prepared_digest, final_digest, "
            "affected_ids, required_child_intents, required_child_terminals, proposal_id, "
            "attempt_no, marker_required, created_at, updated_at, blocked_reason "
            "FROM governance_operation_journals LIMIT 1"
        )
        connection.execute(
            "INSERT INTO governance_operation_components VALUES "
            "('forged-event', 'committed', 1, 'hosted-mutation-child/v1', "
            "'child-0', ?, ?, 'complete')",
            (encoded, _sha(encoded.encode())),
        )

    with pytest.raises(
        hosted_mutation_journal.HostedMutationJournalError,
        match="selector",
    ):
        hosted_mutation_journal.read_committed_publication_evidence_for_selector(
            governance_db_path=governance_path,
            idempotency_db_path=store.path,
            selector={
                "publication_event_id": "publication-2",
                "predecessor": {
                    "activation_store_id": "store-1",
                    "activation_epoch": 8,
                    "activation_state_digest": "b" * 64,
                },
                "successor": {
                    "activation_store_id": "store-1",
                    "activation_epoch": 9,
                    "activation_state_digest": "d" * 64,
                },
            },
        )


def test_public_selector_ignores_large_unrelated_retained_history(tmp_path: Path) -> None:
    governance_path, store, _recovery = _committed_fixture(tmp_path)
    with sqlite3.connect(governance_path) as connection:
        encoded = "{}"
        for index in range(258):
            event_id = f"unrelated-{index}"
            connection.execute(
                "INSERT INTO governance_operation_journals "
                "SELECT ?, operation, causation_id, authorization_session, principal_id, "
                "phase, direction, prior_digest, prepared_digest, final_digest, affected_ids, "
                "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
                "marker_required, created_at, updated_at, blocked_reason "
                "FROM governance_operation_journals LIMIT 1",
                (event_id,),
            )
            connection.execute(
                "INSERT INTO governance_operation_components VALUES "
                "(?, 'committed', 1, 'hosted-mutation-child/v1', 'unrelated', ?, ?, 'complete')",
                (event_id, encoded, _sha(encoded.encode())),
            )

    evidence = hosted_mutation_journal.read_committed_publication_evidence_for_selector(
        governance_db_path=governance_path,
        idempotency_db_path=store.path,
        selector={
            "publication_event_id": "publication-1",
            "predecessor": {
                "activation_store_id": "store-1",
                "activation_epoch": 7,
                "activation_state_digest": "a" * 64,
            },
            "successor": {
                "activation_store_id": "store-1",
                "activation_epoch": 8,
                "activation_state_digest": "b" * 64,
            },
        },
    )
    assert evidence is not None
    assert evidence.publication.event_id == "publication-1"


def _prepared_for(store: IdempotencyStore, key: str, digest: str, **overrides):
    """One authenticated preparation bound to this store's live attempt."""

    attempt = store._attempts[key]
    descriptor = _descriptor()
    descriptor["scoped_idempotency_digest"] = _sha(key.encode())
    descriptor["attempt_id"] = attempt.attempt_id
    descriptor["commit_token"] = attempt.commit_token
    descriptor["command_digest"] = digest
    descriptor.update(overrides)
    return hosted_mutation_journal.prepare_canonical_mutation_recovery(
        descriptor=descriptor,
        prepared_results={"child-0": {"path": "Notes/0.md"}},
        attempt_secret=attempt.commit_secret,
    )


def test_a_second_preparation_for_one_attempt_is_refused_as_stale(tmp_path: Path) -> None:
    """The prepared payload is frozen once, and a later one is not an update.

    Re-preparing the same attempt with different content means the two
    processes disagree about what the mutation is. Overwriting would let the
    second definition recover effects the first one published, so the store
    keeps the payload it already bound and refuses. Re-persisting the byte
    identical payload stays a no-op, because a retried preparation on the
    happy path must not be an error.
    """

    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    store._claim_or_inspect("key-1", "2" * 64, None)
    first = _prepared_for(store, "key-1", "2" * 64)
    store.persist_prepared_recovery("key-1", "2" * 64, first)

    # Idempotent for the identical payload.
    store.persist_prepared_recovery("key-1", "2" * 64, first)
    assert store.load_prepared_recovery("key-1", "2" * 64) == first

    stale = _prepared_for(store, "key-1", "2" * 64, selector_digest="9" * 64)
    assert stale != first
    with pytest.raises(Exception, match="prepared mutation payload"):
        store.persist_prepared_recovery("key-1", "2" * 64, stale)

    # The refusal changed nothing: the first definition is still the binding one.
    assert store.load_prepared_recovery("key-1", "2" * 64) == first


def test_preparation_without_a_claimed_attempt_has_no_evidence_to_bind(
    tmp_path: Path,
) -> None:
    """A payload that no live attempt vouches for cannot be persisted.

    This is the missing-evidence case: without the attempt there is no commit
    secret to authenticate the preparation against, so accepting it would
    store an unauthenticated recovery plan that a later process would trust.
    """

    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    store._claim_or_inspect("key-1", "2" * 64, None)
    recovery = _prepared_for(store, "key-1", "2" * 64)

    # A different key was never claimed, so it has no attempt.
    with pytest.raises(Exception, match="prepared mutation attempt"):
        store.persist_prepared_recovery("key-2", "2" * 64, recovery)

    assert store.load_prepared_recovery("key-2", "2" * 64) is None


def test_a_crash_before_the_first_canonical_effect_leaves_preparation_unfinished(
    tmp_path: Path,
) -> None:
    """Preparation is not a success, and a reopened store must not read it as one.

    The precommit cut: the payload is durable and no canonical effect has
    happened. A fresh process has to find an attempt still executing with no
    terminal, so the mutation can be carried forward or abandoned -- never
    replayed as a completed one.
    """

    path = tmp_path / "state" / "idempotency.sqlite"
    store = IdempotencyStore(path)
    store._claim_or_inspect("key-1", "2" * 64, None)
    recovery = _prepared_for(store, "key-1", "2" * 64)
    store.persist_prepared_recovery("key-1", "2" * 64, recovery)

    # Reopen: a new process sees only what reached the disk.
    reopened = IdempotencyStore(path)
    assert reopened.load_prepared_recovery("key-1", "2" * 64) == recovery
    assert reopened.completed_terminal("key-1", "2" * 64) is None
    with sqlite3.connect(path) as connection:
        state, result = connection.execute(
            "SELECT state, result FROM mutations WHERE key='key-1'"
        ).fetchone()
    assert state == "executing"
    assert result is None
