from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from derived_receipt_fakes import DerivedReceiptProtocolFake

from exomem import deferred_index, server_runtime, state_paths


def _protocol():
    spec = importlib.util.find_spec("exomem.derived_receipts")
    assert spec is not None, "the frozen derived receipt protocol is missing"
    return importlib.import_module("exomem.derived_receipts")


def _drain():
    spec = importlib.util.find_spec("exomem.derived_drain")
    assert spec is not None, "the bounded derived component scheduler is missing"
    return importlib.import_module("exomem.derived_drain")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _path(
    protocol,
    rel_path: str,
    *,
    before: bytes | None,
    after: bytes | None,
    stable_memory_ref: str | None = None,
):
    return protocol.DerivedBatchPath(
        rel_path=rel_path,
        before_hash=None if before is None else _hash_bytes(before),
        after_hash=None if after is None else _hash_bytes(after),
        stable_memory_ref=stable_memory_ref,
    )


def _prepare(
    vault: Path,
    *,
    batch_id: str = "batch-1",
    generation: str = "generation-1",
    paths=(),
    required=(),
    advisory_target_fingerprint: str | None = None,
    terminal_replay_until: float | None = None,
    advisory_retention_until: float | None = None,
    now: float = 10.0,
):
    protocol = _protocol()
    return protocol.prepare_batch(
        vault,
        batch_id=batch_id,
        mutation_attempt_digest=hashlib.sha256(batch_id.encode()).hexdigest(),
        canonical_generation=generation,
        checkpoint_id=f"checkpoint-{generation}",
        paths=tuple(paths),
        required_components=frozenset(required),
        advisory_target_fingerprint=advisory_target_fingerprint,
        terminal_replay_until=terminal_replay_until,
        advisory_retention_until=advisory_retention_until,
        now=now,
    )


def _prepare_one(
    vault: Path,
    *,
    batch_id: str = "batch-1",
    generation: str = "generation-1",
    before: bytes = b"before",
    after: bytes = b"after",
    required=None,
    advisory_target_fingerprint: str | None = None,
    terminal_replay_until: float | None = None,
    now: float = 10.0,
):
    protocol = _protocol()
    rel = f"Knowledge Base/Notes/{batch_id}.md"
    target = vault / rel
    _write(target, before)
    components = (
        frozenset({protocol.DerivedComponent.LEXSTORE})
        if required is None
        else frozenset(required)
    )
    if protocol.DerivedComponent.WRITE_ADVISORY in components:
        advisory_target_fingerprint = (
            advisory_target_fingerprint or hashlib.sha256(b"target").hexdigest()
        )
        terminal_replay_until = (
            100.0 if terminal_replay_until is None else terminal_replay_until
        )
    receipt = _prepare(
        vault,
        batch_id=batch_id,
        generation=generation,
        paths=(_path(protocol, rel, before=before, after=after),),
        required=components,
        advisory_target_fingerprint=advisory_target_fingerprint,
        terminal_replay_until=terminal_replay_until,
        now=now,
    )
    return receipt, target, before, after


def _commit_and_prove(vault: Path, receipt, target: Path, after: bytes):
    protocol = _protocol()
    _write(target, after)
    proof = protocol.prove_committed(
        vault,
        receipt,
        current_generation=receipt.canonical_generation,
    )
    assert proof.outcome == "ready"
    assert protocol.publish_pending_visibility(
        vault,
        receipt,
        publisher=lambda _root, _receipt: True,
    )
    return proof


def test_additive_migration_preserves_legacy_semantic_graph_and_full_receipts(
    vault: Path,
) -> None:
    protocol = _protocol()
    rels = {
        "semantic": "Knowledge Base/Notes/legacy-semantic.md",
        "graph": "Knowledge Base/Notes/legacy-graph.md",
        "full": "Knowledge Base/Notes/legacy-full.md",
    }
    for rel in rels.values():
        _write(vault / rel, b"legacy")
    semantic = deferred_index.add_receipts(vault, [rels["semantic"]])
    graph = deferred_index.add_graph_receipts(vault, [rels["graph"]])
    full = deferred_index.add_full_receipts(vault, [rels["full"]])

    receipt = _prepare(
        vault,
        paths=(
            _path(
                protocol,
                "Knowledge Base/Notes/new.md",
                before=None,
                after=b"new",
            ),
        ),
        required={protocol.DerivedComponent.LEXSTORE},
    )

    assert receipt.state == "prepared"
    assert deferred_index.snapshot(vault) == semantic
    assert deferred_index.snapshot_graph(vault) == graph
    assert deferred_index.snapshot_full(vault) == full
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "semantic_upserts",
        "graph_upserts",
        "full_upserts",
        "maintenance_state",
        "derived_batches",
        "derived_batch_paths",
        "derived_batch_components",
        "pending_recall_rows",
        "write_advisory_results",
    } <= tables


def test_derived_batch_rows_are_bounded_and_content_free(vault: Path) -> None:
    protocol = _protocol()
    secret = "never-store-this-markdown-body-or-secret"
    rel = "Knowledge Base/Notes/private.md"
    _write(vault / rel, secret.encode())
    _prepare(
        vault,
        paths=(_path(protocol, rel, before=secret.encode(), after=b"replacement"),),
        required={protocol.DerivedComponent.LEXSTORE},
    )

    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        schema = "\n".join(
            str(row)
            for table in (
                "derived_batches",
                "derived_batch_paths",
                "derived_batch_components",
                "pending_recall_rows",
                "write_advisory_results",
            )
            for row in connection.execute(f"PRAGMA table_info({table})")
        )
        dump = "\n".join(connection.iterdump())
    assert secret not in dump
    assert not {
        "markdown",
        "body",
        "content",
        "mutation_args",
        "exception",
        "metadata",
        "title",
        "excerpt",
    } & {token.strip('"`[]').lower() for token in schema.split()}
    with pytest.raises(ValueError, match="bounded"):
        _prepare(
            vault,
            batch_id="x" * 300,
            paths=(_path(protocol, rel, before=secret.encode(), after=b"after"),),
            required={protocol.DerivedComponent.LEXSTORE},
        )


def test_derived_batch_records_every_closed_component_explicitly(vault: Path) -> None:
    protocol = _protocol()
    receipt = _prepare(
        vault,
        paths=(),
        required={
            protocol.DerivedComponent.FRESHNESS,
            protocol.DerivedComponent.GRAPH,
        },
    )

    assert tuple(status.component for status in receipt.components) == tuple(
        protocol.DerivedComponent
    )
    assert [component.value for component in protocol.DerivedComponent] == [
        "freshness",
        "memory_refs",
        "resolver",
        "semantic_purge",
        "lexstore",
        "graph",
        "embeddings",
        "claims",
        "write_advisory",
    ]
    assert {
        status.component: status.state for status in receipt.components
    } == {
        component: (
            "prepared"
            if component
            in {
                protocol.DerivedComponent.FRESHNESS,
                protocol.DerivedComponent.GRAPH,
            }
            else "not_required"
        )
        for component in protocol.DerivedComponent
    }


def test_advisory_result_id_is_stable_for_exact_batch_revision(vault: Path) -> None:
    protocol = _protocol()
    kwargs = dict(
        batch_id="advisory-batch",
        generation="generation-7",
        paths=(),
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_fingerprint=hashlib.sha256(b"target").hexdigest(),
        terminal_replay_until=200.0,
        now=10.0,
    )
    first = _prepare(vault, **kwargs)
    replay = _prepare(vault, **kwargs)

    first_ref = protocol.advisory_result_ref(vault, first)
    assert first_ref is not None
    assert first_ref == protocol.advisory_result_ref(vault, replay)
    assert first_ref.startswith("exomem://write-advisory-result/")


def test_advisory_result_retention_cannot_strand_replayed_terminal_ref(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt = _prepare(
        vault,
        batch_id="retained-advisory",
        paths=(),
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_fingerprint=hashlib.sha256(b"target").hexdigest(),
        terminal_replay_until=200.0,
        advisory_retention_until=20.0,
        now=10.0,
    )
    expected_ref = protocol.advisory_result_ref(vault, receipt)

    assert protocol.cleanup_advisory_results(vault, now=199.0, limit=10) == 0
    assert protocol.advisory_result_ref(vault, receipt) == expected_ref
    assert protocol.cleanup_advisory_results(vault, now=201.0, limit=10) == 1
    assert protocol.advisory_result_ref(vault, receipt) is None


def test_exact_replay_monotonically_extends_advisory_retention(vault: Path) -> None:
    protocol = _protocol()
    kwargs = dict(
        batch_id="extended-advisory",
        paths=(),
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_fingerprint=hashlib.sha256(b"target").hexdigest(),
        now=1.0,
    )
    first = _prepare(vault, terminal_replay_until=10.0, **kwargs)
    expected_ref = protocol.advisory_result_ref(vault, first)
    replay = _prepare(vault, terminal_replay_until=100.0, **kwargs)

    assert replay == first
    assert protocol.cleanup_advisory_results(vault, now=11.0, limit=10) == 0
    assert protocol.advisory_result_ref(vault, replay) == expected_ref
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT terminal_replay_until, retention_deadline "
            "FROM write_advisory_results WHERE batch_id = ?",
            (first.batch_id,),
        ).fetchone() == (100.0, 100.0)


def test_derived_batch_store_uses_resolved_state_root_not_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = _protocol()
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "isolated-state"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(state_root.resolve()))
    _prepare(vault, paths=(), required={protocol.DerivedComponent.LEXSTORE})

    expected = state_paths.vault_state_dir(vault) / ".deferred-index.sqlite"
    assert deferred_index.store_path(vault) == expected
    assert expected.is_file()
    assert not (vault / ".deferred-index.sqlite").exists()
    assert not list(vault.rglob("*derived*receipt*"))


def test_receipt_prepare_failure_leaves_canonical_untouched(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = _protocol()
    rel = "Knowledge Base/Notes/prepare-guard.md"
    target = vault / rel
    before = b"before"
    after = b"after"
    _write(target, before)

    def prepare_then_replace() -> None:
        protocol.prepare_batch(
            vault,
            batch_id="prepare-guard",
            mutation_attempt_digest=hashlib.sha256(b"attempt").hexdigest(),
            canonical_generation="generation-1",
            checkpoint_id="checkpoint-1",
            paths=(_path(protocol, rel, before=before, after=after),),
            required_components={protocol.DerivedComponent.LEXSTORE},
        )
        target.write_bytes(after)

    monkeypatch.setattr(
        deferred_index,
        "_connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("down")),
    )

    with pytest.raises(sqlite3.OperationalError, match="down"):
        prepare_then_replace()
    assert target.read_bytes() == before


def test_frozen_protocol_fake_matches_public_typed_shapes() -> None:
    protocol = _protocol()
    fake = DerivedReceiptProtocolFake()
    path = _path(
        protocol,
        "Knowledge Base/Notes/fake.md",
        before=None,
        after=b"fake",
    )
    receipt = fake.prepare_batch(
        Path("vault"),
        batch_id="fake-batch",
        mutation_attempt_digest=hashlib.sha256(b"fake").hexdigest(),
        canonical_generation="generation-fake",
        checkpoint_id="checkpoint-fake",
        paths=(path,),
        required_components={
            protocol.DerivedComponent.LEXSTORE,
            protocol.DerivedComponent.WRITE_ADVISORY,
        },
        advisory_target_fingerprint=hashlib.sha256(b"target").hexdigest(),
        terminal_replay_until=100.0,
        now=1.0,
    )
    proof = fake.prove_committed(
        Path("vault"), receipt, current_generation="generation-fake"
    )
    status = fake.component_status(
        Path("vault"), receipt, protocol.DerivedComponent.LEXSTORE
    )
    assert isinstance(path, protocol.DerivedBatchPath)
    assert isinstance(receipt, protocol.DerivedBatchReceipt)
    assert isinstance(proof, protocol.DerivedBatchProof)
    assert isinstance(status, protocol.DerivedComponentStatus)
    assert fake.publish_pending_visibility(
        Path("vault"),
        receipt,
        publisher=lambda _root, _receipt: True,
    ) is True
    fake.signal_components(Path("vault"), receipt)
    assert fake.advisory_result_ref(Path("vault"), receipt)
    assert fake.call_order == (
        "prepare_batch",
        "prove_committed",
        "component_status",
        "publish_pending_visibility",
        "signal_components",
        "advisory_result_ref",
    )
    for seam in (
        "prepare_batch",
        "prove_committed",
        "publish_pending_visibility",
        "signal_components",
        "component_status",
        "advisory_result_ref",
    ):
        production = inspect.signature(getattr(protocol, seam))
        fake_signature = inspect.signature(getattr(DerivedReceiptProtocolFake, seam))
        assert tuple(production.parameters) == tuple(fake_signature.parameters)[1:]
    fake.inject("signal_components", RuntimeError("injected"))
    with pytest.raises(RuntimeError, match="injected"):
        fake.signal_components(Path("vault"), receipt)


def test_complete_before_state_aborts_prepared_batch(vault: Path) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(vault)

    proof = protocol.prove_committed(
        vault,
        receipt,
        current_generation=receipt.canonical_generation,
        known_uncommitted=True,
    )

    assert proof.outcome == "aborted"
    assert protocol.component_status(
        vault, receipt, protocol.DerivedComponent.LEXSTORE
    ).state == "aborted"


def test_exact_after_state_activates_only_required_components(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(
        vault,
        required={
            protocol.DerivedComponent.LEXSTORE,
            protocol.DerivedComponent.GRAPH,
        },
    )
    proof = _commit_and_prove(vault, receipt, target, after)

    assert proof.ready_components == (
        protocol.DerivedComponent.LEXSTORE,
    )
    states = {
        component: protocol.component_status(vault, receipt, component).state
        for component in protocol.DerivedComponent
    }
    assert states[protocol.DerivedComponent.LEXSTORE] == "ready"
    assert states[protocol.DerivedComponent.GRAPH] == "prepared"
    assert all(
        state == "not_required"
        for component, state in states.items()
        if component
        not in {
            protocol.DerivedComponent.LEXSTORE,
            protocol.DerivedComponent.GRAPH,
        }
    )


def test_mixed_or_stale_state_never_activates_components(vault: Path) -> None:
    protocol = _protocol()
    before = (b"before-a", b"before-b")
    after = (b"after-a", b"after-b")
    rels = (
        "Knowledge Base/Notes/mixed-a.md",
        "Knowledge Base/Notes/mixed-b.md",
    )
    for rel, value in zip(rels, before, strict=True):
        _write(vault / rel, value)
    receipt = _prepare(
        vault,
        batch_id="mixed",
        paths=tuple(
            _path(protocol, rel, before=old, after=new)
            for rel, old, new in zip(rels, before, after, strict=True)
        ),
        required={protocol.DerivedComponent.LEXSTORE},
    )
    _write(vault / rels[0], after[0])

    mixed = protocol.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    )
    assert mixed.outcome == "reconcile_required"
    assert protocol.component_status(
        vault, receipt, protocol.DerivedComponent.LEXSTORE
    ).state == "reconcile_required"

    _write(vault / rels[1], after[1])
    stale = protocol.prove_committed(
        vault, receipt, current_generation="generation-stale"
    )
    assert stale.outcome == "reconcile_required"
    assert protocol.claim_ready_components(
        vault, owner="worker", limit=10, lease_seconds=30, now=20.0
    ) == ()


def test_newer_exact_custody_supersedes_only_after_visibility_is_live(
    vault: Path,
) -> None:
    protocol = _protocol()
    rel = "Knowledge Base/Notes/superseded.md"
    before, middle, after = b"before", b"middle", b"after"
    _write(vault / rel, before)
    old = _prepare(
        vault,
        batch_id="old-batch",
        generation="generation-1",
        paths=(_path(protocol, rel, before=before, after=middle),),
        required={protocol.DerivedComponent.LEXSTORE},
        now=1.0,
    )
    _write(vault / rel, middle)
    assert protocol.prove_committed(
        vault, old, current_generation="generation-1"
    ).outcome == "ready"
    newer = _prepare(
        vault,
        batch_id="new-batch",
        generation="generation-2",
        paths=(_path(protocol, rel, before=middle, after=after),),
        required={protocol.DerivedComponent.LEXSTORE},
        now=2.0,
    )
    _write(vault / rel, after)
    assert protocol.prove_committed(
        vault, newer, current_generation="generation-2"
    ).outcome == "ready"

    before_visibility = protocol.prove_committed(
        vault, old, current_generation="generation-2"
    )
    assert before_visibility.outcome == "reconcile_required"
    assert protocol.publish_pending_visibility(
        vault,
        newer,
        publisher=lambda _root, _receipt: True,
    ) is True
    superseded = protocol.prove_committed(
        vault, old, current_generation="generation-2"
    )
    assert superseded.outcome == "superseded"
    assert protocol.component_status(
        vault, old, protocol.DerivedComponent.LEXSTORE
    ).state == "superseded"


def test_pending_visibility_requires_a_successful_real_publisher(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(vault, batch_id="visibility")
    _write(target, after)
    assert protocol.prove_committed(
        vault,
        receipt,
        current_generation=receipt.canonical_generation,
    ).outcome == "ready"

    with pytest.raises(RuntimeError, match="publisher"):
        protocol.publish_pending_visibility(vault, receipt)
    with pytest.raises(RuntimeError, match="publisher"):
        protocol.publish_pending_visibility(
            vault,
            receipt,
            publisher=lambda _root, _receipt: False,
        )

    def unavailable(_root: Path, _receipt) -> bool:
        raise RuntimeError("publisher unavailable")

    with pytest.raises(RuntimeError, match="publisher unavailable"):
        protocol.publish_pending_visibility(
            vault,
            receipt,
            publisher=unavailable,
        )
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT state FROM pending_recall_rows WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone() == ("prepared",)

    assert protocol.publish_pending_visibility(
        vault,
        receipt,
        publisher=lambda _root, current: current.batch_id == receipt.batch_id,
    )
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT state FROM pending_recall_rows WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone() == ("live",)


def test_after_hash_proof_fails_closed_when_source_mutates_before_transition(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(vault, batch_id="proof-race")
    _write(target, after)
    original = protocol._canonical_path_state

    def mutate_after_observation(root: Path, path) -> str:
        observed = original(root, path)
        _write(target, b"newer")
        return observed

    monkeypatch.setattr(protocol, "_canonical_path_state", mutate_after_observation)
    proof = protocol.prove_committed(
        vault,
        receipt,
        current_generation=receipt.canonical_generation,
    )

    assert proof.outcome == "reconcile_required"
    assert protocol.component_status(
        vault,
        receipt,
        protocol.DerivedComponent.LEXSTORE,
    ).state == "reconcile_required"


def test_completion_fails_closed_when_source_mutates_before_transition(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(vault, batch_id="completion-race")
    _commit_and_prove(vault, receipt, target, after)
    [claimed] = protocol.claim_ready_components(
        vault,
        owner="worker",
        limit=1,
        lease_seconds=30.0,
        now=20.0,
    )
    original = protocol._canonical_path_state

    def mutate_after_observation(root: Path, path) -> str:
        observed = original(root, path)
        _write(target, b"newer")
        return observed

    monkeypatch.setattr(protocol, "_canonical_path_state", mutate_after_observation)

    assert protocol.complete_component(
        vault,
        claimed,
        current_generation=receipt.canonical_generation,
        now=21.0,
    ) is False
    assert protocol.component_status(
        vault,
        receipt,
        protocol.DerivedComponent.LEXSTORE,
    ).state == "claimed"


def test_expired_component_claim_is_reclaimable_after_restart(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(vault, now=0.0)
    _commit_and_prove(vault, receipt, target, after)

    [first] = protocol.claim_ready_components(
        vault, owner="process-one", limit=1, lease_seconds=5.0, now=1.0
    )
    assert protocol.claim_ready_components(
        vault, owner="process-two", limit=1, lease_seconds=5.0, now=5.9
    ) == ()
    [reclaimed] = protocol.claim_ready_components(
        vault, owner="process-two", limit=1, lease_seconds=5.0, now=6.1
    )
    assert reclaimed.revision == first.revision
    assert reclaimed.lease_revision > first.lease_revision
    assert reclaimed.claim_owner == "process-two"


def test_retryable_failure_rotates_and_persists_bounded_backoff(vault: Path) -> None:
    protocol = _protocol()
    first, first_target, _before, first_after = _prepare_one(
        vault, batch_id="first", now=1.0
    )
    second, second_target, _before, second_after = _prepare_one(
        vault, batch_id="second", now=2.0
    )
    _commit_and_prove(vault, first, first_target, first_after)
    _commit_and_prove(vault, second, second_target, second_after)
    [claimed] = protocol.claim_ready_components(
        vault, owner="worker", limit=1, lease_seconds=30.0, now=3.0
    )
    assert claimed.batch_id == "first"
    failed = protocol.retry_component(
        vault,
        claimed,
        failure_code="dispatch_failed",
        now=3.0,
        base_backoff_seconds=5.0,
        max_backoff_seconds=20.0,
    )

    assert failed.state == "retryable"
    assert failed.attempt_count == 1
    assert failed.next_attempt_at == 8.0
    [next_claim] = protocol.claim_ready_components(
        vault, owner="worker", limit=1, lease_seconds=30.0, now=3.1
    )
    assert next_claim.batch_id == "second"
    reopened = protocol.component_status(
        vault, first, protocol.DerivedComponent.LEXSTORE
    )
    assert reopened.failure_code == "dispatch_failed"
    assert reopened.next_attempt_at == 8.0


def test_older_revision_cannot_clear_newer_custody(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(vault, now=0.0)
    _commit_and_prove(vault, receipt, target, after)
    [old] = protocol.claim_ready_components(
        vault, owner="old-worker", limit=1, lease_seconds=1.0, now=1.0
    )
    [new] = protocol.claim_ready_components(
        vault, owner="new-worker", limit=1, lease_seconds=10.0, now=2.1
    )

    assert new.revision == old.revision
    assert new.lease_revision > old.lease_revision
    assert protocol.complete_component(
        vault,
        old,
        current_generation=receipt.canonical_generation,
        now=2.2,
    ) is False
    assert protocol.component_status(
        vault, receipt, protocol.DerivedComponent.LEXSTORE
    ).lease_revision == new.lease_revision
    assert protocol.complete_component(
        vault,
        new,
        current_generation=receipt.canonical_generation,
        now=2.3,
    ) is True


def test_claim_attempts_preserve_pending_and_advisory_component_lineage(
    vault: Path,
) -> None:
    protocol = _protocol()
    target_fingerprint = hashlib.sha256(b"target").hexdigest()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="lineage",
        required={
            protocol.DerivedComponent.LEXSTORE,
            protocol.DerivedComponent.WRITE_ADVISORY,
        },
        advisory_target_fingerprint=target_fingerprint,
        terminal_replay_until=100.0,
        now=0.0,
    )
    _commit_and_prove(vault, receipt, target, after)

    [first] = protocol.claim_ready_components(
        vault,
        owner="first-worker",
        limit=1,
        lease_seconds=1.0,
        now=1.0,
    )
    [restarted] = protocol.claim_ready_components(
        vault,
        owner="restart-worker",
        limit=1,
        lease_seconds=10.0,
        now=2.1,
    )
    assert first.component is protocol.DerivedComponent.LEXSTORE
    assert restarted.component is protocol.DerivedComponent.LEXSTORE
    assert restarted.revision == first.revision == 1
    assert restarted.lease_revision > first.lease_revision
    assert protocol.complete_component(
        vault,
        first,
        current_generation=receipt.canonical_generation,
        now=2.2,
    ) is False
    retried = protocol.retry_component(
        vault,
        restarted,
        failure_code="dispatch_failed",
        now=2.3,
        base_backoff_seconds=1.0,
        max_backoff_seconds=2.0,
    )
    [final] = protocol.claim_ready_components(
        vault,
        owner="final-worker",
        limit=1,
        lease_seconds=10.0,
        now=retried.next_attempt_at,
    )
    assert final.revision == 1
    assert final.lease_revision > restarted.lease_revision
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        pending_revision = connection.execute(
            "SELECT component_revision FROM pending_recall_rows WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone()
        advisory_revision = connection.execute(
            "SELECT component_revision FROM write_advisory_results WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone()
    assert pending_revision == (1,)
    assert advisory_revision == (1,)


def test_component_dependencies_gate_one_slot_in_frozen_order(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="dependency-one",
        required=set(protocol.DerivedComponent),
    )
    _commit_and_prove(vault, receipt, target, after)

    [first] = protocol.claim_ready_components(
        vault,
        owner="worker",
        limit=1,
        lease_seconds=30.0,
        now=20.0,
    )
    assert first.component is protocol.DerivedComponent.FRESHNESS
    assert all(
        protocol.component_status(vault, receipt, component).state == "prepared"
        for component in tuple(protocol.DerivedComponent)[1:]
    )


def test_component_dependencies_preserve_multi_batch_fairness_after_retry(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipts = []
    for index in range(2):
        receipt, target, _before, after = _prepare_one(
            vault,
            batch_id=f"dependency-{index}",
            required=set(protocol.DerivedComponent),
            now=float(index),
        )
        _commit_and_prove(vault, receipt, target, after)
        receipts.append(receipt)

    claims = protocol.claim_ready_components(
        vault,
        owner="worker",
        limit=4,
        lease_seconds=30.0,
        now=20.0,
    )
    assert [(status.batch_id, status.component) for status in claims] == [
        ("dependency-0", protocol.DerivedComponent.FRESHNESS),
        ("dependency-1", protocol.DerivedComponent.FRESHNESS),
    ]
    failed = protocol.retry_component(
        vault,
        claims[0],
        failure_code="dispatch_failed",
        now=20.0,
        base_backoff_seconds=5.0,
        max_backoff_seconds=5.0,
    )
    assert protocol.complete_component(
        vault,
        claims[1],
        current_generation=receipts[1].canonical_generation,
        now=20.1,
    )
    [next_claim] = protocol.claim_ready_components(
        vault,
        owner="worker-two",
        limit=4,
        lease_seconds=30.0,
        now=20.2,
    )
    assert next_claim.batch_id == "dependency-1"
    assert next_claim.component is protocol.DerivedComponent.MEMORY_REFS
    [retried] = protocol.claim_ready_components(
        vault,
        owner="worker-three",
        limit=1,
        lease_seconds=30.0,
        now=failed.next_attempt_at,
    )
    assert retried.batch_id == "dependency-0"
    assert retried.component is protocol.DerivedComponent.FRESHNESS


def test_prepared_receipt_never_authorizes_canonical_replay(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(vault)
    proof = _commit_and_prove(vault, receipt, target, after)

    assert proof.canonical_replay_authorized is False
    assert not hasattr(receipt, "mutation_arguments")
    assert "replay_canonical" not in {
        name for name, _value in inspect.getmembers(protocol, inspect.isfunction)
    }


def test_pending_components_schedule_on_server_start_without_new_write(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = _protocol()
    drain = _drain()
    receipt, target, _before, after = _prepare_one(vault)
    _commit_and_prove(vault, receipt, target, after)
    scheduled = threading.Event()

    def observe_start(root: Path):
        assert root == vault
        assert protocol.component_status(
            root, receipt, protocol.DerivedComponent.LEXSTORE
        ).state == "ready"
        scheduled.set()
        return SimpleNamespace(stop=lambda: None)

    monkeypatch.setattr(drain, "start", observe_start)
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(vault)

    activation._activate()
    try:
        assert scheduled.wait(timeout=1.0)
        assert activation.derived_drain is not None
    finally:
        activation._stop_background_workers()


def test_restart_drain_proves_prepared_commit_from_independent_generation(
    vault: Path,
) -> None:
    protocol = _protocol()
    drain = _drain()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="restart-proof",
    )
    _write(target, after)
    observations: list[Path] = []
    dispatched: list[object] = []

    completed = drain.drain_once(
        vault,
        dispatch=lambda _root, status: dispatched.append(status) or True,
        observe_current_generation=lambda root: observations.append(root)
        or "generation-1",
        visibility_publisher=lambda _root, current: current.batch_id
        == receipt.batch_id,
        limit=10,
        now=20.0,
    )

    assert completed == 1
    # Recovery proof, pre-dispatch proof, and completion proof each observe the
    # live generation independently; no claimed custody reuses stale truth.
    assert observations == [vault, vault, vault]
    assert [status.component for status in dispatched] == [
        protocol.DerivedComponent.LEXSTORE
    ]
    assert protocol.component_status(
        vault,
        receipt,
        protocol.DerivedComponent.LEXSTORE,
    ).state == "completed"


def test_local_runtime_starts_and_stops_derived_drain_before_watcher_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Worker:
        def stop(self) -> None:
            events.append("derived:stop")

    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    monkeypatch.setattr(
        server_runtime,
        "_start_derived_drain",
        lambda root: events.append(f"derived:{root}") or Worker(),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda root: events.append(f"watcher:{root}"),
    )
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _root: None)
    activation = server_runtime.LocalRuntimeActivation(vault)

    activation._activate()
    activation._stop_background_workers()

    assert events == [
        f"derived:{vault}",
        f"watcher:{vault}",
        "derived:stop",
    ]


def test_hosted_runtime_owns_prompt_derived_drain_and_clean_shutdown(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Worker:
        def start(self) -> None:
            events.append("derived:restart")

        def stop(self) -> None:
            events.append("derived:stop")

    class Lifecycle:
        def __init__(self) -> None:
            self.workers: list[tuple[object, object | None]] = []

        def complete_startup(self, **_kwargs):
            return SimpleNamespace(phase="active")

        def set_worker_status(self, *_args, **_kwargs) -> None:
            pass

        def set_mutation_authority(self, *_args, **_kwargs) -> None:
            pass

        def register_background_worker(self, *, stopper, starter=None) -> None:
            self.workers.append((stopper, starter))

    lifecycle = Lifecycle()
    config = SimpleNamespace(
        vault_root=vault,
        state_root=vault.parent / "hosted-state",
        cell_id="cell-test",
        vault_id=None,
        authorization_session_replica_id=None,
        requires_dynamic_security=False,
        service_credential="credential",
        resource_limits=SimpleNamespace(worker_count=0),
        apply_process_environment=lambda: None,
        has_feature=lambda _feature: False,
    )
    from exomem import state_migration

    monkeypatch.setattr(state_migration, "require_vault_state_ready", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_metrics_persistence", lambda: None)
    monkeypatch.setattr(
        server_runtime,
        "HostedCellLifecycle",
        lambda *_args, **_kwargs: lifecycle,
    )
    monkeypatch.setattr(server_runtime, "_initialize_hosted_security", lambda _config: None)
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _root: SimpleNamespace(source_types=()),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _root: "")
    monkeypatch.setattr(
        server_runtime.projection_runtime,
        "preactivate_projection_runtime",
        lambda _root: None,
    )
    monkeypatch.setattr(
        server_runtime,
        "probe_hosted_mutation_authority",
        lambda _root: (True, "HOSTED_READY"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_retrieval_runtime",
        lambda _root: events.append("retrieval"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_derived_drain",
        lambda _root: events.append("derived:start") or Worker(),
    )

    runtime = server_runtime._initialize_locked_hosted_runtime(
        config,
        SimpleNamespace(),
    )

    assert events[:2] == ["derived:start", "retrieval"]
    assert runtime.derived_drain is not None
    assert len(lifecycle.workers) == 1
    stopper, starter = lifecycle.workers[0]
    stopper()
    assert starter is not None
    starter()
    assert events[-2:] == ["derived:stop", "derived:restart"]


def test_hosted_registered_stop_fails_closed_before_lifecycle_deadline(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted registration must bound stop, not block past a quiesce deadline."""
    from exomem.hosted_runtime import HostedCellLifecycle, HostedLifecycleError

    drain = _drain()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="hosted-deadline",
    )
    _commit_and_prove(vault, receipt, target, after)
    entered = threading.Event()
    release = threading.Event()

    def dispatch(_root: Path, _status) -> bool:
        entered.set()
        assert release.wait(timeout=5.0)
        return True

    worker = drain.DerivedDrain(
        vault,
        dispatch=dispatch,
        observe_current_generation=lambda _root: "generation-1",
    )
    state_root = vault.parent / "hosted-deadline-state"
    lifecycle = HostedCellLifecycle(
        SimpleNamespace(cell_id="cell-derived-deadline", state_root=state_root)
    )
    config = SimpleNamespace(
        vault_root=vault,
        state_root=state_root,
        cell_id="cell-derived-deadline",
        vault_id=None,
        authorization_session_replica_id=None,
        requires_dynamic_security=False,
        service_credential="credential",
        resource_limits=SimpleNamespace(worker_count=0),
        apply_process_environment=lambda: None,
        has_feature=lambda _feature: False,
    )
    from exomem import state_migration

    monkeypatch.setattr(state_migration, "require_vault_state_ready", lambda _root: None)
    monkeypatch.setattr(server_runtime, "_start_metrics_persistence", lambda: None)
    monkeypatch.setattr(
        server_runtime,
        "HostedCellLifecycle",
        lambda *_args, **_kwargs: lifecycle,
    )
    monkeypatch.setattr(server_runtime, "_initialize_hosted_security", lambda _config: None)
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _root: SimpleNamespace(source_types=()),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _root: "")
    monkeypatch.setattr(
        server_runtime.projection_runtime,
        "preactivate_projection_runtime",
        lambda _root: None,
    )
    monkeypatch.setattr(
        server_runtime,
        "probe_hosted_mutation_authority",
        lambda _root: (True, "HOSTED_READY"),
    )
    monkeypatch.setattr(server_runtime, "_start_retrieval_runtime", lambda _root: None)
    monkeypatch.setattr(
        server_runtime,
        "_start_derived_drain",
        lambda _root: worker.start(),
    )

    runtime = server_runtime._initialize_locked_hosted_runtime(config, SimpleNamespace())
    try:
        # The hosted cell registered exactly this live worker; nothing in the
        # test hand-wrote the stopper the lifecycle will call.
        assert runtime.derived_drain is worker
        assert entered.wait(timeout=2.0)

        started = time.monotonic()
        with pytest.raises(
            HostedLifecycleError,
            match="background writer could not stop safely",
        ):
            lifecycle.quiesce(timeout=0.01)
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, (
            "the registered hosted stopper blocked "
            f"{elapsed:.3f}s while dispatch was still live"
        )
        assert lifecycle.snapshot().phase == "quiescing"
        assert worker._thread is not None and worker._thread.is_alive()
        assert drain._ACTIVE.get(drain._key(vault)) is worker
    finally:
        release.set()

    assert worker._thread is not None
    worker._thread.join(timeout=5.0)
    assert lifecycle.quiesce(timeout=2.0).phase == "quiesced"
    assert drain._ACTIVE.get(drain._key(vault)) is None


def test_failed_component_rotates_behind_ready_work(vault: Path) -> None:
    protocol = _protocol()
    drain = _drain()
    receipts = []
    for index in range(2):
        receipt, target, _before, after = _prepare_one(
            vault, batch_id=f"rotate-{index}", now=float(index)
        )
        _commit_and_prove(vault, receipt, target, after)
        receipts.append(receipt)
    calls: list[str] = []

    def dispatch(_root: Path, status) -> bool:
        calls.append(status.batch_id)
        return status.batch_id == "rotate-1"

    assert drain.drain_once(
        vault,
        dispatch=dispatch,
        observe_current_generation=lambda _root: "generation-1",
        limit=2,
        now=10.0,
    ) == 1
    assert calls == ["rotate-0", "rotate-1"]
    failed = protocol.component_status(
        vault, receipts[0], protocol.DerivedComponent.LEXSTORE
    )
    assert failed.state == "retryable"
    assert failed.next_attempt_at > 10.0


def test_quiet_or_constrained_mode_keeps_one_correctness_progress_slot() -> None:
    drain = _drain()

    assert drain.progress_limit(mode_name="quiet", resource_limit=0) == 1
    assert drain.progress_limit(mode_name="normal", resource_limit=0) == 1
    assert drain.progress_limit(mode_name="performance", resource_limit=0) == 1


def test_exact_component_custody_does_not_mint_full_upsert_debt(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(
        vault,
        required={
            protocol.DerivedComponent.LEXSTORE,
            protocol.DerivedComponent.GRAPH,
            protocol.DerivedComponent.EMBEDDINGS,
        },
    )
    _commit_and_prove(vault, receipt, target, after)

    assert deferred_index.snapshot_full(vault) == []
    assert deferred_index.full_status(vault)["count"] == 0
    assert protocol.component_status(
        vault,
        receipt,
        protocol.DerivedComponent.LEXSTORE,
    ).state == "ready"
    assert all(
        protocol.component_status(vault, receipt, component).state == "prepared"
        for component in (
            protocol.DerivedComponent.GRAPH,
            protocol.DerivedComponent.EMBEDDINGS,
        )
    )


def test_bounded_drain_pass_never_claims_more_than_limit(vault: Path) -> None:
    protocol = _protocol()
    drain = _drain()
    for index in range(5):
        receipt, target, _before, after = _prepare_one(
            vault, batch_id=f"bounded-{index}", now=float(index)
        )
        _commit_and_prove(vault, receipt, target, after)
    claimed: list[str] = []

    assert drain.drain_once(
        vault,
        dispatch=lambda _root, status: claimed.append(status.batch_id) or True,
        observe_current_generation=lambda _root: "generation-1",
        limit=2,
        now=10.0,
    ) == 2
    assert len(claimed) == 2
    remaining = protocol.claim_ready_components(
        vault, owner="inspection", limit=10, lease_seconds=10.0, now=11.0
    )
    assert len(remaining) == 3


def test_hosted_quiesce_fails_closed_until_dispatch_thread_is_dead(
    vault: Path,
) -> None:
    from exomem.hosted_runtime import HostedCellLifecycle, HostedLifecycleError

    drain = _drain()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="hosted-quiesce",
    )
    _commit_and_prove(vault, receipt, target, after)
    entered = threading.Event()
    release = threading.Event()

    def dispatch(_root: Path, _status) -> bool:
        entered.set()
        assert release.wait(timeout=5.0)
        return True

    worker = drain.start(
        vault,
        dispatch=dispatch,
        observe_current_generation=lambda _root: "generation-1",
    )
    assert entered.wait(timeout=2.0)
    config = SimpleNamespace(
        cell_id="cell-derived-quiesce",
        state_root=vault.parent / "hosted-state",
    )
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    lifecycle.register_background_worker(
        stopper=lambda: worker.stop(timeout=0.01),
        starter=worker.start,
    )

    try:
        with pytest.raises(
            HostedLifecycleError,
            match="background writer could not stop safely",
        ):
            lifecycle.quiesce(timeout=0.05)
        assert lifecycle.snapshot().phase == "quiescing"
        assert worker._thread is not None and worker._thread.is_alive()
        assert drain._ACTIVE.get(drain._key(vault)) is worker
    finally:
        release.set()

    assert worker._thread is not None
    worker._thread.join(timeout=2.0)
    assert lifecycle.quiesce(timeout=2.0).phase == "quiesced"
    assert worker._thread is not None and not worker._thread.is_alive()
    assert drain._ACTIVE.get(drain._key(vault)) is None


def test_hosted_resume_restores_one_registered_owner_and_prompt_signals(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drain = _drain()
    passed = threading.Event()
    pass_count = 0

    def observe_pass(*_args, **_kwargs) -> int:
        nonlocal pass_count
        pass_count += 1
        passed.set()
        return 0

    monkeypatch.setattr(drain, "drain_once", observe_pass)
    worker = drain.start(vault)
    try:
        assert passed.wait(timeout=2.0)
        worker.stop()
        assert drain._ACTIVE.get(drain._key(vault)) is None

        passed.clear()
        worker.start()
        assert passed.wait(timeout=2.0)
        assert drain._ACTIVE.get(drain._key(vault)) is worker

        observed = pass_count
        passed.clear()
        drain.signal(vault)
        assert passed.wait(timeout=2.0)
        assert pass_count > observed
        assert drain.start(vault) is worker
    finally:
        worker.stop()


def test_recovery_callback_exception_is_bounded_and_later_pass_progresses(
    vault: Path,
) -> None:
    protocol = _protocol()
    drain = _drain()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="callback-recovery",
        now=1.0,
    )
    _write(target, after)

    def broken_observer(_root: Path) -> str:
        raise KeyError("observer unavailable")

    assert drain.drain_once(
        vault,
        dispatch=lambda _root, _status: True,
        observe_current_generation=broken_observer,
        visibility_publisher=lambda _root, _receipt: True,
        limit=1,
        now=3.0,
    ) == 0
    deferred = protocol.component_status(
        vault,
        receipt,
        protocol.DerivedComponent.LEXSTORE,
    )
    assert deferred.state == "prepared"
    assert deferred.failure_code == "handler_unavailable"
    assert deferred.attempt_count == 1
    assert deferred.next_attempt_at > 3.0

    dispatched: list[str] = []
    assert drain.drain_once(
        vault,
        dispatch=lambda _root, status: dispatched.append(status.batch_id) or True,
        observe_current_generation=lambda _root: "generation-1",
        visibility_publisher=lambda _root, _receipt: True,
        limit=1,
        now=deferred.next_attempt_at,
    ) == 1
    assert dispatched == [receipt.batch_id]
    assert protocol.component_status(
        vault,
        receipt,
        protocol.DerivedComponent.LEXSTORE,
    ).state == "completed"


def test_scheduler_pass_exception_keeps_registered_worker_signalable(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drain = _drain()
    failed_pass = threading.Event()
    recovered_pass = threading.Event()
    attempts = 0

    def flaky_pass(*_args, **_kwargs) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            failed_pass.set()
            raise KeyError("observer unavailable")
        recovered_pass.set()
        return 0

    monkeypatch.setattr(drain, "drain_once", flaky_pass)
    worker = drain.start(vault)
    try:
        assert failed_pass.wait(timeout=2.0)
        assert worker._thread is not None and worker._thread.is_alive()
        assert drain._ACTIVE.get(drain._key(vault)) is worker

        drain.signal(vault)
        assert recovered_pass.wait(timeout=2.0)
        assert worker._thread is not None and worker._thread.is_alive()
        assert drain._ACTIVE.get(drain._key(vault)) is worker
    finally:
        worker.stop()


def test_rejected_schema_migration_repairs_durable_and_lease_lineage(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(
        vault,
        batch_id="rejected-schema",
        required={protocol.DerivedComponent.WRITE_ADVISORY},
    )
    store = deferred_index.store_path(vault)
    with sqlite3.connect(store) as connection:
        connection.execute(
            "UPDATE derived_batch_components SET revision = 3 "
            "WHERE batch_id = ? AND component = 'write_advisory'",
            (receipt.batch_id,),
        )
        connection.execute(
            "ALTER TABLE derived_batch_components DROP COLUMN lease_revision"
        )

    repaired = protocol.component_status(
        vault,
        receipt,
        protocol.DerivedComponent.WRITE_ADVISORY,
    )
    assert repaired.revision == 1
    assert repaired.lease_revision == 2
    with sqlite3.connect(store) as connection:
        component_lineage = connection.execute(
            "SELECT revision, lease_revision FROM derived_batch_components "
            "WHERE batch_id = ? AND component = 'write_advisory'",
            (receipt.batch_id,),
        ).fetchone()
        result_lineage = connection.execute(
            "SELECT component_revision FROM write_advisory_results "
            "WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone()
    assert component_lineage == (1, 2)
    assert result_lineage == (1,)


def test_newer_live_generation_supersedes_old_ready_work_before_dispatch(
    vault: Path,
) -> None:
    protocol = _protocol()
    drain = _drain()
    old, target, _before, middle = _prepare_one(
        vault,
        batch_id="old-ready",
        generation="generation-1",
        after=b"middle",
        now=1.0,
    )
    _commit_and_prove(vault, old, target, middle)
    rel = old.paths[0].rel_path
    newer = _prepare(
        vault,
        batch_id="new-ready",
        generation="generation-2",
        paths=(_path(protocol, rel, before=middle, after=b"after"),),
        required={protocol.DerivedComponent.LEXSTORE},
        now=3.0,
    )
    _write(target, b"after")
    assert protocol.prove_committed(
        vault,
        newer,
        current_generation="generation-2",
        now=4.0,
    ).outcome == "ready"
    assert protocol.publish_pending_visibility(
        vault,
        newer,
        publisher=lambda _root, _receipt: True,
        now=4.0,
    )
    dispatched: list[str] = []

    assert drain.drain_once(
        vault,
        dispatch=lambda _root, status: dispatched.append(status.batch_id) or True,
        observe_current_generation=lambda _root: "generation-2",
        visibility_publisher=lambda _root, _receipt: True,
        limit=1,
        now=10.0,
    ) == 0
    assert dispatched == []
    assert protocol.component_status(
        vault,
        old,
        protocol.DerivedComponent.LEXSTORE,
    ).state == "superseded"
