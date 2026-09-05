from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import sqlite3
import threading
import time
from dataclasses import replace
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
    advisory_target_rel_path: str | None = None,
    advisory_target_fingerprint: str | None = None,
    terminal_replay_until: float | None = None,
    advisory_retention_until: float | None = None,
    now: float = 10.0,
):
    protocol = _protocol()
    kwargs = {
        "batch_id": batch_id,
        "mutation_attempt_digest": hashlib.sha256(batch_id.encode()).hexdigest(),
        "canonical_generation": generation,
        "checkpoint_id": f"checkpoint-{generation}",
        "paths": tuple(paths),
        "required_components": frozenset(required),
        "advisory_target_fingerprint": advisory_target_fingerprint,
        "terminal_replay_until": terminal_replay_until,
        "advisory_retention_until": advisory_retention_until,
        "now": now,
    }
    if advisory_target_rel_path is not None:
        kwargs["advisory_target_rel_path"] = advisory_target_rel_path
    return protocol.prepare_batch(vault, **kwargs)


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
        # The frozen contract binds the advisory target fingerprint to the
        # prepared after hash, so the default has to be exactly that.
        advisory_target_fingerprint = advisory_target_fingerprint or _hash_bytes(after)
        terminal_replay_until = (
            100.0 if terminal_replay_until is None else terminal_replay_until
        )
    receipt = _prepare(
        vault,
        batch_id=batch_id,
        generation=generation,
        paths=(_path(protocol, rel, before=before, after=after),),
        required=components,
        advisory_target_rel_path=(
            rel
            if protocol.DerivedComponent.WRITE_ADVISORY in components
            else None
        ),
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
    rel = "Knowledge Base/Notes/advisory-batch.md"
    kwargs = dict(
        batch_id="advisory-batch",
        generation="generation-7",
        paths=(_path(protocol, rel, before=None, after=b"target"),),
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_rel_path=rel,
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
    rel = "Knowledge Base/Notes/retained-advisory.md"
    receipt = _prepare(
        vault,
        batch_id="retained-advisory",
        paths=(_path(protocol, rel, before=None, after=b"target"),),
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_rel_path=rel,
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
    rel = "Knowledge Base/Notes/extended-advisory.md"
    kwargs = dict(
        batch_id="extended-advisory",
        paths=(_path(protocol, rel, before=None, after=b"target"),),
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_rel_path=rel,
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
        advisory_target_rel_path=path.rel_path,
        advisory_target_fingerprint=_hash_bytes(b"fake"),
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
        "snapshot_pending_visibility",
        "pending_visibility_snapshot_is_current",
        "retire_pending_visibility",
        "read_advisory_result",
        "publish_advisory_result",
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

    # Corrected under orchestrator ruling R1. Once every path IS in its exact
    # intended after-state, the batch is ready -- and stays ready when the
    # vault's global checkpoint has moved on, because that checkpoint advances
    # on every write to any page and says nothing about whether THIS batch's
    # bytes are current. The mixed-state refusal above is the guard; the
    # generation was a redundancy that only held for a per-path generation the
    # vault does not have.
    _write(vault / rels[1], after[1])
    stale = protocol.prove_committed(
        vault, receipt, current_generation="generation-stale"
    )
    assert stale.outcome == "ready"
    assert protocol.component_status(
        vault, receipt, protocol.DerivedComponent.LEXSTORE
    ).state == "ready"
    # Still unclaimable, but now for the right reason: the batch's pending
    # visibility has not been published, and the store refuses to hand out a
    # component whose overlay rows are still `prepared`. Activation and
    # claimability are separate gates, and only the second one is about
    # publication.
    assert protocol.claim_ready_components(
        vault, owner="worker", limit=10, lease_seconds=30, now=20.0
    ) == ()
    assert protocol.publish_pending_visibility(
        vault, receipt, publisher=lambda _root, _receipt: True
    )
    claimed = protocol.claim_ready_components(
        vault, owner="worker", limit=10, lease_seconds=30, now=21.0
    )
    assert [status.component for status in claimed] == [
        protocol.DerivedComponent.LEXSTORE
    ]


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
    target_fingerprint = _hash_bytes(b"after")
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

        def readiness(self):
            return SimpleNamespace(ready=True)

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
        binding=None,
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

    runtime = server_runtime._initialize_locked_hosted_runtime(
        config, SimpleNamespace(), binding=None,
    )
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


def _prepare_advisory_claim(
    vault: Path,
    *,
    batch_id: str,
    target_fingerprint: str | None = None,
    now: float = 1.0,
):
    protocol = _protocol()
    body = f"after-{batch_id}".encode()
    fingerprint = target_fingerprint or _hash_bytes(body)
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id=batch_id,
        after=body,
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_fingerprint=fingerprint,
        terminal_replay_until=100.0,
        now=now,
    )
    _commit_and_prove(vault, receipt, target, after)
    [claimed] = protocol.claim_ready_components(
        vault,
        owner=f"worker-{batch_id}",
        limit=1,
        lease_seconds=30.0,
        now=now + 1.0,
    )
    assert claimed.component is protocol.DerivedComponent.WRITE_ADVISORY
    return receipt, claimed, fingerprint


def _advisory_candidate(protocol, *, suffix: str = "a"):
    identity = hashlib.sha256(f"candidate-{suffix}".encode()).hexdigest()
    review_id = identity[:24]
    return protocol.DerivedAdvisoryCandidate(
        counterpart_rel_path=f"Knowledge Base/Notes/counterpart-{suffix}.md",
        counterpart_fingerprint=identity,
        warning=f"Potential overlap with counterpart {suffix}.",
        advisory_ref=f"advisory:{review_id}",
        review_ref=f"exomem://review/write-advisory/{review_id}",
        triage_fingerprint=hashlib.sha256(
            f"triage-{suffix}".encode()
        ).hexdigest()[:24],
    )


def test_pending_snapshot_is_complete_empty_and_hydrates_already_live_rows(
    vault: Path,
) -> None:
    protocol = _protocol()

    empty = protocol.snapshot_pending_visibility(vault, limit=8)
    assert empty.outcome == "complete"
    assert empty.batches == ()
    assert protocol.pending_visibility_snapshot_is_current(
        vault, empty.snapshot_generation
    )

    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="restart-live",
    )
    _commit_and_prove(vault, receipt, target, after)

    reopened = protocol.snapshot_pending_visibility(vault, limit=8)
    assert reopened.outcome == "complete"
    assert len(reopened.batches) == 1
    assert reopened.batches[0].receipt.batch_id == receipt.batch_id
    assert tuple(row.state for row in reopened.batches[0].rows) == ("live",)
    assert reopened.batches[0].rows[0].rel_path == receipt.paths[0].rel_path
    assert protocol.pending_visibility_snapshot_is_current(
        vault, reopened.snapshot_generation
    )


def test_pending_snapshot_overflow_never_returns_a_complete_prefix(vault: Path) -> None:
    protocol = _protocol()
    for index in range(2):
        _prepare_one(vault, batch_id=f"overflow-{index}")

    snapshot = protocol.snapshot_pending_visibility(vault, limit=1)

    assert snapshot.outcome == "overflow"
    assert snapshot.batches == ()
    assert snapshot.failure_code == "pending_visibility_overflow"


def test_pending_snapshot_corrupt_or_unsafe_row_is_unprovable(vault: Path) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(
        vault,
        batch_id="unsafe-snapshot",
    )
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE pending_recall_rows SET rel_path = '../escape.md' "
            "WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)

    assert snapshot.outcome == "unprovable"
    assert snapshot.batches == ()
    assert snapshot.failure_code == "pending_visibility_unprovable"


def test_pending_snapshot_generation_fence_detects_concurrent_mutation(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(
        vault,
        batch_id="generation-fence",
    )
    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)
    assert protocol.pending_visibility_snapshot_is_current(
        vault, snapshot.snapshot_generation
    )

    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE pending_recall_rows SET updated_at = updated_at + 1 "
            "WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    assert not protocol.pending_visibility_snapshot_is_current(
        vault, snapshot.snapshot_generation
    )


def _snapshot_batch(protocol, vault: Path, batch_id: str):
    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)
    assert snapshot.outcome == "complete"
    return next(
        batch for batch in snapshot.batches if batch.receipt.batch_id == batch_id
    )


def test_pending_retirement_is_exact_and_older_generation_cannot_retire_newer(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="exact-retirement",
    )
    _commit_and_prove(vault, receipt, target, after)
    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)
    [batch] = snapshot.batches

    assert protocol.retire_pending_visibility(vault, batch).outcome == "retired"
    assert protocol.snapshot_pending_visibility(vault, limit=8).batches == ()

    # Custody that grew after the snapshot is only partly represented by it.
    # Exact retirement refuses rather than retiring the represented prefix and
    # declaring the batch converged while a live row still shadows recall.
    grown, grown_target, _before, grown_after = _prepare_one(
        vault,
        batch_id="grown-retirement",
    )
    _commit_and_prove(vault, grown, grown_target, grown_after)
    grown_batch = _snapshot_batch(protocol, vault, grown.batch_id)
    extra = "Knowledge Base/Notes/grown-extra.md"
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "INSERT INTO derived_batch_paths(batch_id, rel_path, before_hash, "
            "after_hash, stable_memory_ref) VALUES (?, ?, NULL, ?, NULL)",
            (grown.batch_id, extra, hashlib.sha256(b"grown-extra").hexdigest()),
        )
        connection.execute(
            "INSERT INTO pending_recall_rows(batch_id, rel_path, component_revision, "
            "canonical_generation, state, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, 'live', 1, 1)",
            (grown.batch_id, extra, grown.canonical_generation),
        )

    assert protocol.retire_pending_visibility(vault, grown_batch).outcome == "stale"
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pending_recall_rows "
            "WHERE batch_id = ? AND state = 'retired'",
            (grown.batch_id,),
        ).fetchone() == (0,)

    newer, newer_target, _before, newer_after = _prepare_one(
        vault,
        batch_id="stale-retirement",
        generation="generation-old",
    )
    _commit_and_prove(vault, newer, newer_target, newer_after)
    stale_batch = _snapshot_batch(protocol, vault, newer.batch_id)
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE pending_recall_rows SET canonical_generation = ?, "
            "component_revision = component_revision + 1 WHERE batch_id = ?",
            ("generation-new", newer.batch_id),
        )

    refused = protocol.retire_pending_visibility(vault, stale_batch)
    assert refused.outcome == "stale"
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT state FROM pending_recall_rows WHERE batch_id = ?",
            (newer.batch_id,),
        ).fetchone() == ("live",)


def test_advisory_prepare_requires_exact_target_path_and_replays_it(vault: Path) -> None:
    protocol = _protocol()
    rel_a = "Knowledge Base/Notes/target-a.md"
    rel_b = "Knowledge Base/Notes/target-b.md"
    paths = (
        _path(protocol, rel_a, before=None, after=b"a"),
        _path(protocol, rel_b, before=None, after=b"b"),
    )
    fingerprint = _hash_bytes(b"a")

    with pytest.raises(ValueError, match="target path"):
        _prepare(
            vault,
            batch_id="missing-target-path",
            paths=paths,
            required={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_fingerprint=fingerprint,
            terminal_replay_until=100.0,
        )
    with pytest.raises(ValueError, match="prepared batch"):
        _prepare(
            vault,
            batch_id="foreign-target-path",
            paths=paths,
            required={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_rel_path="Knowledge Base/Notes/other.md",
            advisory_target_fingerprint=fingerprint,
            terminal_replay_until=100.0,
        )

    receipt = _prepare(
        vault,
        batch_id="exact-target-path",
        paths=paths,
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_rel_path=rel_a,
        advisory_target_fingerprint=fingerprint,
        terminal_replay_until=100.0,
    )
    replay = _prepare(
        vault,
        batch_id="exact-target-path",
        paths=paths,
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_rel_path=rel_a,
        advisory_target_fingerprint=fingerprint,
        terminal_replay_until=120.0,
    )
    assert replay == receipt
    with pytest.raises(ValueError, match="different custody"):
        _prepare(
            vault,
            batch_id="exact-target-path",
            paths=paths,
            required={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_rel_path=rel_b,
            advisory_target_fingerprint=_hash_bytes(b"b"),
            terminal_replay_until=120.0,
        )


def test_advisory_ready_round_trip_supports_two_or_zero_candidates(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="ready-two",
    )
    candidates = (
        _advisory_candidate(protocol, suffix="a"),
        _advisory_candidate(protocol, suffix="b"),
    )

    publication = protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=candidates,
        observed_target_fingerprint=fingerprint,
        now=3.0,
    )
    result = protocol.read_advisory_result(
        vault,
        protocol.advisory_result_ref(vault, receipt),
        now=3.0,
    )
    assert publication.outcome == "published"
    assert result is not None
    assert result.state == "ready"
    assert result.target_rel_path == receipt.paths[0].rel_path
    assert result.candidates == candidates

    zero_receipt, zero_claimed, zero_fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="ready-zero",
        now=4.0,
    )
    assert protocol.publish_advisory_result(
        vault,
        zero_claimed,
        state="ready",
        observed_target_fingerprint=zero_fingerprint,
        now=6.0,
    ).outcome == "published"
    zero = protocol.read_advisory_result(
        vault,
        protocol.advisory_result_ref(vault, zero_receipt),
        now=6.0,
    )
    assert zero is not None and zero.state == "ready" and zero.candidates == ()


def test_advisory_failed_result_is_closed_and_content_free(vault: Path) -> None:
    protocol = _protocol()
    receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="failed-result",
    )

    assert protocol.publish_advisory_result(
        vault,
        claimed,
        state="failed",
        failure_code="handler_unavailable",
        observed_target_fingerprint=fingerprint,
        now=3.0,
    ).outcome == "published"
    result = protocol.read_advisory_result(
        vault,
        protocol.advisory_result_ref(vault, receipt),
        now=3.0,
    )
    assert result is not None
    assert result.state == "failed"
    assert result.failure_code == "handler_unavailable"
    assert result.candidates == ()
    with pytest.raises(ValueError, match="closed"):
        protocol.publish_advisory_result(
            vault,
            claimed,
            state="failed",
            failure_code="raw exception: secret body",
            observed_target_fingerprint=fingerprint,
            now=3.0,
        )


def test_advisory_publication_refuses_wrong_claim_and_supersedes_wrong_target(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="publication-cas",
    )
    candidate = _advisory_candidate(protocol)

    for stale in (
        replace(claimed, revision=claimed.revision + 1),
        replace(claimed, lease_revision=claimed.lease_revision + 1),
        replace(claimed, claim_owner="different-worker"),
        replace(claimed, canonical_generation="different-generation"),
    ):
        assert protocol.publish_advisory_result(
            vault,
            stale,
            state="ready",
            candidates=(candidate,),
            observed_target_fingerprint=fingerprint,
            now=3.0,
        ).outcome == "stale_claim"

    assert protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(candidate,),
        observed_target_fingerprint=hashlib.sha256(b"new-target").hexdigest(),
        now=3.0,
    ).outcome == "superseded"
    superseded = protocol.read_advisory_result(
        vault,
        protocol.advisory_result_ref(vault, receipt),
        now=3.0,
    )
    assert superseded is not None
    assert superseded.state == "superseded"
    assert superseded.candidates == ()


def test_advisory_publication_is_idempotent_but_conflicting_replay_is_stale(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="publication-replay",
    )
    candidate = _advisory_candidate(protocol)
    kwargs = dict(
        state="ready",
        candidates=(candidate,),
        observed_target_fingerprint=fingerprint,
        now=3.0,
    )

    assert protocol.publish_advisory_result(vault, claimed, **kwargs).outcome == (
        "published"
    )
    assert protocol.publish_advisory_result(vault, claimed, **kwargs).outcome == (
        "already_published"
    )
    assert protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(),
        observed_target_fingerprint=fingerprint,
        now=3.0,
    ).outcome == "stale_claim"

    # The durable result is independently reusable after the publishing process dies.
    result_ref = protocol.advisory_result_ref(vault, receipt)
    assert protocol.read_advisory_result(vault, result_ref, now=4.0).candidates == (
        candidate,
    )


def test_advisory_candidate_and_count_bounds_are_enforced(vault: Path) -> None:
    protocol = _protocol()
    with pytest.raises(ValueError, match="warning"):
        replace(_advisory_candidate(protocol), warning="x" * 301)
    _receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="candidate-bounds",
    )
    with pytest.raises(ValueError, match="eight"):
        protocol.publish_advisory_result(
            vault,
            claimed,
            state="ready",
            candidates=tuple(
                _advisory_candidate(protocol, suffix=str(index))
                for index in range(9)
            ),
            observed_target_fingerprint=fingerprint,
            now=3.0,
        )


def test_advisory_cleanup_explicitly_removes_candidates_after_both_deadlines(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="candidate-cleanup",
    )
    protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(_advisory_candidate(protocol),),
        observed_target_fingerprint=fingerprint,
        now=3.0,
    )
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE write_advisory_results SET retention_deadline = 5, "
            "terminal_replay_until = 10 WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    assert protocol.cleanup_advisory_results(vault, now=6.0, limit=8) == 0
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM write_advisory_result_candidates"
        ).fetchone() == (1,)
    assert protocol.cleanup_advisory_results(vault, now=11.0, limit=8) == 1
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM write_advisory_result_candidates"
        ).fetchone() == (0,)


def test_legacy_and_old_writer_advisory_rows_fail_closed_after_migration(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(
        vault,
        batch_id="legacy-result",
        required={protocol.DerivedComponent.WRITE_ADVISORY},
    )
    result_ref = protocol.advisory_result_ref(vault, receipt)
    store = deferred_index.store_path(vault)

    with sqlite3.connect(store) as connection:
        connection.execute(
            "UPDATE write_advisory_results SET target_rel_path = NULL "
            "WHERE batch_id = ?",
            (receipt.batch_id,),
        )
    legacy = protocol.read_advisory_result(vault, result_ref, now=20.0)
    assert legacy is not None
    assert legacy.state == "failed"
    assert legacy.failure_code == "legacy_result_unverifiable"
    assert legacy.candidates == ()

    with sqlite3.connect(store) as connection:
        connection.execute(
            "INSERT INTO write_advisory_results(result_id, batch_id, "
            "component_revision, target_fingerprint, counterpart_fingerprint, "
            "state, failure_code, advisory_ref, review_ref, retention_deadline, "
            "terminal_replay_until, publication_revision, published_at, "
            "created_at, updated_at) VALUES "
            "('old-writer-result', ?, 1, ?, NULL, 'pending', NULL, NULL, NULL, "
            "100, 100, 1, NULL, 1, 1)",
            (receipt.batch_id + "-old-writer", hashlib.sha256(b"old").hexdigest()),
        )
    old_writer = protocol.read_advisory_result(
        vault,
        "exomem://write-advisory-result/old-writer-result",
        now=20.0,
    )
    assert old_writer is not None
    assert old_writer.state == "failed"
    assert old_writer.failure_code == "legacy_result_unverifiable"
    assert old_writer.candidates == ()

    # A live claim cannot publish onto a row whose target identity is absent:
    # no observed fingerprint can prove a target path that was never recorded.
    unverifiable, claimed, fingerprint = _prepare_advisory_claim(
        vault,
        batch_id="legacy-publication",
    )
    with sqlite3.connect(store) as connection:
        connection.execute(
            "UPDATE write_advisory_results SET target_rel_path = NULL "
            "WHERE batch_id = ?",
            (unverifiable.batch_id,),
        )
    assert protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(_advisory_candidate(protocol),),
        observed_target_fingerprint=fingerprint,
        now=3.0,
    ).outcome == "stale_claim"
    with sqlite3.connect(store) as connection:
        assert connection.execute(
            "SELECT count(*) FROM write_advisory_result_candidates AS c "
            "JOIN write_advisory_results AS r ON r.result_id = c.result_id "
            "WHERE r.batch_id = ?",
            (unverifiable.batch_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT state, publication_revision FROM write_advisory_results "
            "WHERE batch_id = ?",
            (unverifiable.batch_id,),
        ).fetchone() == ("pending", 1)


def _ensure_accepted_parent_derived_schema(connection: sqlite3.Connection) -> None:
    """Install the exact accepted lifecycle DDL before the additive extension."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS derived_batches (
            schema_version INTEGER NOT NULL CHECK(schema_version = 1),
            batch_id TEXT PRIMARY KEY CHECK(length(batch_id) BETWEEN 1 AND 128),
            mutation_attempt_digest TEXT NOT NULL
                CHECK(length(mutation_attempt_digest) = 64),
            canonical_generation TEXT NOT NULL
                CHECK(length(canonical_generation) BETWEEN 1 AND 128),
            checkpoint_id TEXT NOT NULL
                CHECK(length(checkpoint_id) BETWEEN 1 AND 128),
            state TEXT NOT NULL CHECK(state IN (
                'prepared', 'ready', 'completed', 'aborted', 'superseded',
                'reconcile_required'
            )),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            failure_code TEXT CHECK(
                failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64
            )
        );
        CREATE TABLE IF NOT EXISTS derived_batch_paths (
            batch_id TEXT NOT NULL,
            rel_path TEXT NOT NULL CHECK(length(rel_path) BETWEEN 1 AND 1024),
            before_hash TEXT CHECK(before_hash IS NULL OR length(before_hash) = 64),
            after_hash TEXT CHECK(after_hash IS NULL OR length(after_hash) = 64),
            stable_memory_ref TEXT CHECK(
                stable_memory_ref IS NULL OR length(stable_memory_ref) BETWEEN 1 AND 256
            ),
            PRIMARY KEY(batch_id, rel_path),
            FOREIGN KEY(batch_id) REFERENCES derived_batches(batch_id)
        );
        CREATE TABLE IF NOT EXISTS derived_batch_components (
            batch_id TEXT NOT NULL,
            component TEXT NOT NULL CHECK(component IN (
                'freshness', 'memory_refs', 'resolver', 'semantic_purge',
                'lexstore', 'graph', 'embeddings', 'claims', 'write_advisory'
            )),
            revision INTEGER NOT NULL CHECK(revision >= 1),
            state TEXT NOT NULL CHECK(state IN (
                'prepared', 'ready', 'claimed', 'retryable', 'completed',
                'not_required', 'aborted', 'superseded', 'reconcile_required',
                'failed'
            )),
            lease_revision INTEGER NOT NULL DEFAULT 0 CHECK(lease_revision >= 0),
            claim_owner TEXT CHECK(
                claim_owner IS NULL OR length(claim_owner) BETWEEN 1 AND 128
            ),
            claim_expires_at REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            failure_code TEXT CHECK(
                failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64
            ),
            PRIMARY KEY(batch_id, component),
            FOREIGN KEY(batch_id) REFERENCES derived_batches(batch_id)
        );
        CREATE TABLE IF NOT EXISTS pending_recall_rows (
            batch_id TEXT NOT NULL,
            rel_path TEXT NOT NULL CHECK(length(rel_path) BETWEEN 1 AND 1024),
            component_revision INTEGER NOT NULL CHECK(component_revision >= 1),
            canonical_generation TEXT NOT NULL
                CHECK(length(canonical_generation) BETWEEN 1 AND 128),
            state TEXT NOT NULL CHECK(state IN ('prepared', 'live', 'retired')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(batch_id, rel_path),
            FOREIGN KEY(batch_id, rel_path)
                REFERENCES derived_batch_paths(batch_id, rel_path)
        );
        CREATE TABLE IF NOT EXISTS write_advisory_results (
            result_id TEXT PRIMARY KEY CHECK(length(result_id) BETWEEN 1 AND 64),
            batch_id TEXT NOT NULL,
            component_revision INTEGER NOT NULL CHECK(component_revision >= 1),
            target_fingerprint TEXT NOT NULL
                CHECK(length(target_fingerprint) BETWEEN 1 AND 128),
            counterpart_fingerprint TEXT CHECK(
                counterpart_fingerprint IS NULL
                OR length(counterpart_fingerprint) BETWEEN 1 AND 128
            ),
            state TEXT NOT NULL CHECK(state IN (
                'pending', 'ready', 'failed', 'superseded'
            )),
            failure_code TEXT CHECK(
                failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64
            ),
            advisory_ref TEXT CHECK(
                advisory_ref IS NULL OR length(advisory_ref) BETWEEN 1 AND 256
            ),
            review_ref TEXT CHECK(
                review_ref IS NULL OR length(review_ref) BETWEEN 1 AND 256
            ),
            retention_deadline REAL NOT NULL,
            terminal_replay_until REAL NOT NULL,
            publication_revision INTEGER NOT NULL DEFAULT 1
                CHECK(publication_revision >= 1),
            published_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(batch_id, component_revision)
        );
        CREATE INDEX IF NOT EXISTS derived_components_schedule
            ON derived_batch_components(state, next_attempt_at, updated_at);
        CREATE INDEX IF NOT EXISTS derived_paths_lookup
            ON derived_batch_paths(rel_path, batch_id);
        CREATE INDEX IF NOT EXISTS pending_recall_visibility
            ON pending_recall_rows(state, canonical_generation);
        CREATE INDEX IF NOT EXISTS advisory_result_retention
            ON write_advisory_results(retention_deadline, terminal_replay_until);
        """
    )


def test_accepted_parent_schema_migrates_without_losing_any_custody(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    rel = "Knowledge Base/Notes/accepted-parent.md"
    result_id = "a" * 32
    digest = hashlib.sha256(b"accepted-parent").hexdigest()
    with monkeypatch.context() as patch:
        patch.setattr(
            deferred_index,
            "_ensure_derived_batch_schema",
            _ensure_accepted_parent_derived_schema,
        )
        connection = deferred_index._connect(vault, create=True)
        with connection:
            for table, queued_path in (
                ("semantic_upserts", "Knowledge Base/Notes/legacy-semantic.md"),
                ("graph_upserts", "Knowledge Base/Notes/legacy-graph.md"),
                ("full_upserts", "Knowledge Base/Notes/legacy-full.md"),
            ):
                connection.execute(
                    f"INSERT INTO {table}(rel_path, created_at, updated_at, revision) "
                    "VALUES (?, 1, 2, 3)",
                    (queued_path,),
                )
            connection.execute(
                "INSERT INTO derived_batches VALUES "
                "(1, 'accepted-parent', ?, 'generation-parent', "
                "'checkpoint-parent', 'ready', 1, 2, NULL)",
                (digest,),
            )
            connection.execute(
                "INSERT INTO derived_batch_paths VALUES "
                "('accepted-parent', ?, NULL, ?, 'exomem://memory/example')",
                (rel, digest),
            )
            connection.executemany(
                "INSERT INTO derived_batch_components(batch_id, component, revision, "
                "state, lease_revision, claim_owner, claim_expires_at, attempt_count, "
                "next_attempt_at, created_at, updated_at, failure_code) VALUES "
                "('accepted-parent', ?, 1, ?, 7, NULL, NULL, 4, 3, 1, 2, NULL)",
                (
                    (
                        component.value,
                        (
                            "ready"
                            if component is protocol.DerivedComponent.WRITE_ADVISORY
                            else "not_required"
                        ),
                    )
                    for component in protocol.DerivedComponent
                ),
            )
            connection.execute(
                "INSERT INTO pending_recall_rows VALUES "
                "('accepted-parent', ?, 1, 'generation-parent', 'live', 1, 2)",
                (rel,),
            )
            connection.execute(
                "INSERT INTO write_advisory_results VALUES "
                "(?, 'accepted-parent', 1, ?, NULL, 'pending', NULL, NULL, NULL, "
                "80, 90, 5, NULL, 1, 2)",
                (result_id, digest),
            )
        connection.close()

    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert "target_rel_path" not in {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(write_advisory_results)")
        }
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='write_advisory_result_candidates'"
        ).fetchone() is None

    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)
    result = protocol.read_advisory_result(
        vault,
        f"exomem://write-advisory-result/{result_id}",
        now=10.0,
    )

    assert snapshot.outcome == "complete"
    assert snapshot.batches[0].rows[0].state == "live"
    assert result is not None
    assert result.state == "failed"
    assert result.failure_code == "legacy_result_unverifiable"
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT rel_path, revision FROM semantic_upserts"
        ).fetchall() == [("Knowledge Base/Notes/legacy-semantic.md", 3)]
        assert connection.execute(
            "SELECT rel_path, revision FROM graph_upserts"
        ).fetchall() == [("Knowledge Base/Notes/legacy-graph.md", 3)]
        assert connection.execute(
            "SELECT rel_path, revision FROM full_upserts"
        ).fetchall() == [("Knowledge Base/Notes/legacy-full.md", 3)]
        assert connection.execute(
            "SELECT revision, lease_revision, attempt_count "
            "FROM derived_batch_components WHERE batch_id='accepted-parent' "
            "AND component='write_advisory'"
        ).fetchone() == (1, 7, 4)
        assert connection.execute(
            "SELECT component_revision, canonical_generation, state "
            "FROM pending_recall_rows WHERE batch_id='accepted-parent'"
        ).fetchone() == (1, "generation-parent", "live")
        assert connection.execute(
            "SELECT result_id, retention_deadline, terminal_replay_until, "
            "publication_revision, target_rel_path FROM write_advisory_results"
        ).fetchone() == (result_id, 80.0, 90.0, 5, None)


def test_frozen_protocol_fake_drives_the_store_consumer_lifecycle() -> None:
    protocol = _protocol()
    fake = DerivedReceiptProtocolFake()
    rel = "Knowledge Base/Notes/fake-consumer.md"
    path = _path(protocol, rel, before=None, after=b"consumer")
    fingerprint = _hash_bytes(b"consumer")
    receipt = fake.prepare_batch(
        Path("vault"),
        batch_id="fake-consumer",
        mutation_attempt_digest=hashlib.sha256(b"fake-consumer").hexdigest(),
        canonical_generation="generation-fake",
        checkpoint_id="checkpoint-fake",
        paths=(path,),
        required_components={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_rel_path=rel,
        advisory_target_fingerprint=fingerprint,
        terminal_replay_until=100.0,
        now=1.0,
    )

    prepared = fake.snapshot_pending_visibility(Path("vault"), limit=8)
    assert isinstance(prepared, protocol.PendingVisibilitySnapshot)
    assert prepared.outcome == "complete"
    assert tuple(row.state for row in prepared.batches[0].rows) == ("prepared",)
    assert isinstance(prepared.batches[0], protocol.PendingVisibilityBatch)
    assert isinstance(prepared.batches[0].rows[0], protocol.PendingVisibilityRow)

    assert fake.publish_pending_visibility(
        Path("vault"),
        receipt,
        publisher=lambda _root, _receipt: True,
    ) is True
    # Publishing a live overlay invalidates every fence taken before it.
    assert not fake.pending_visibility_snapshot_is_current(
        Path("vault"), prepared.snapshot_generation
    )

    live = fake.snapshot_pending_visibility(Path("vault"), limit=8)
    assert tuple(row.state for row in live.batches[0].rows) == ("live",)
    assert fake.pending_visibility_snapshot_is_current(
        Path("vault"), live.snapshot_generation
    )
    with pytest.raises(ValueError, match="positive"):
        fake.snapshot_pending_visibility(Path("vault"), limit=0)

    result_ref = fake.advisory_result_ref(Path("vault"), receipt)
    pending = fake.read_advisory_result(Path("vault"), result_ref, now=2.0)
    assert isinstance(pending, protocol.DerivedAdvisoryResult)
    assert pending.state == "pending" and pending.target_rel_path == rel
    claimed = replace(
        fake.component_status(
            Path("vault"), receipt, protocol.DerivedComponent.WRITE_ADVISORY
        ),
        state="claimed",
        claim_owner="fake-worker",
        claim_expires_at=60.0,
    )
    candidate = _advisory_candidate(protocol)
    publication = fake.publish_advisory_result(
        Path("vault"),
        claimed,
        state="ready",
        candidates=(candidate,),
        observed_target_fingerprint=fingerprint,
        now=3.0,
    )
    assert isinstance(publication, protocol.DerivedAdvisoryPublication)
    assert publication.outcome == "published"
    assert fake.publish_advisory_result(
        Path("vault"),
        claimed,
        state="ready",
        candidates=(candidate,),
        observed_target_fingerprint=fingerprint,
        now=3.0,
    ).outcome == "already_published"
    ready = fake.read_advisory_result(Path("vault"), result_ref, now=4.0)
    assert ready.state == "ready" and ready.candidates == (candidate,)

    retirement = fake.retire_pending_visibility(Path("vault"), live.batches[0])
    assert isinstance(retirement, protocol.PendingVisibilityRetirement)
    assert retirement.outcome == "retired"
    assert fake.snapshot_pending_visibility(Path("vault"), limit=8).batches == ()
    # Production retires idempotently once custody already converged.
    assert fake.retire_pending_visibility(
        Path("vault"), live.batches[0]
    ).outcome == "retired"

    assert fake.call_order == (
        "prepare_batch",
        "snapshot_pending_visibility",
        "publish_pending_visibility",
        "pending_visibility_snapshot_is_current",
        "snapshot_pending_visibility",
        "pending_visibility_snapshot_is_current",
        "snapshot_pending_visibility",
        "advisory_result_ref",
        "read_advisory_result",
        "component_status",
        "publish_advisory_result",
        "publish_advisory_result",
        "read_advisory_result",
        "retire_pending_visibility",
        "snapshot_pending_visibility",
        "retire_pending_visibility",
    )
    fake.inject(
        "snapshot_pending_visibility",
        protocol.PendingVisibilitySnapshot(
            outcome="unprovable",
            snapshot_generation=0,
            batches=(),
            failure_code="pending_visibility_unprovable",
        ),
    )
    assert fake.snapshot_pending_visibility(
        Path("vault"), limit=8
    ).outcome == "unprovable"


# --- Correction round 1 -------------------------------------------------------


_OVERLONG_REL = "Knowledge Base/Notes/" + ("a" * 1100) + ".md"


def test_retired_pending_rows_do_not_strand_unfinished_components(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="retire-strand",
        required={
            protocol.DerivedComponent.LEXSTORE,
            protocol.DerivedComponent.GRAPH,
        },
    )
    _commit_and_prove(vault, receipt, target, after)
    [lexstore] = protocol.claim_ready_components(
        vault, owner="worker-a", limit=1, lease_seconds=30.0, now=20.0
    )
    assert lexstore.component is protocol.DerivedComponent.LEXSTORE
    assert protocol.complete_component(
        vault, lexstore, current_generation=receipt.canonical_generation, now=21.0
    )

    batch = _snapshot_batch(protocol, vault, receipt.batch_id)
    assert protocol.retire_pending_visibility(
        vault, batch, now=22.0
    ).outcome == "retired"

    # `retired` is terminal beyond `live`: only never-published rows may block.
    assert protocol.due_component_count(vault, now=23.0) == 1
    assert protocol.recoverable_batch_count(vault) == 0
    [graph] = protocol.claim_ready_components(
        vault, owner="worker-b", limit=1, lease_seconds=30.0, now=23.0
    )
    assert graph.component is protocol.DerivedComponent.GRAPH
    assert protocol.complete_component(
        vault, graph, current_generation=receipt.canonical_generation, now=24.0
    )
    assert protocol.recover_prepared_batches(
        vault,
        observe_current_generation=lambda _root: receipt.canonical_generation,
        visibility_publisher=lambda _root, _receipt: True,
        limit=8,
        now=25.0,
    ) == 0
    assert protocol.component_status(
        vault, receipt, protocol.DerivedComponent.GRAPH
    ).failure_code is None


def test_retiring_an_unpublished_prepared_row_is_refused(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(vault, batch_id="prepared-retire")
    batch = _snapshot_batch(protocol, vault, receipt.batch_id)
    assert tuple(row.state for row in batch.rows) == ("prepared",)

    assert protocol.retire_pending_visibility(
        vault, batch, now=12.0
    ).outcome == "stale"
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT state FROM pending_recall_rows WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone() == ("prepared",)

    # The unpublished row survives, so ordinary publication still succeeds
    # instead of raising "pending visibility publication was incomplete".
    _commit_and_prove(vault, receipt, target, after)


def test_supersession_retires_the_shadowed_pending_rows(vault: Path) -> None:
    protocol = _protocol()
    old, target, _before, middle = _prepare_one(
        vault,
        batch_id="superseded-rows",
        generation="generation-1",
        after=b"middle",
        now=1.0,
    )
    _commit_and_prove(vault, old, target, middle)
    rel = old.paths[0].rel_path
    newer = _prepare(
        vault,
        batch_id="newer-rows",
        generation="generation-2",
        paths=(_path(protocol, rel, before=middle, after=b"after"),),
        required={protocol.DerivedComponent.LEXSTORE},
        now=3.0,
    )
    _write(target, b"after")
    assert protocol.prove_committed(
        vault, newer, current_generation="generation-2", now=4.0
    ).outcome == "ready"
    assert protocol.publish_pending_visibility(
        vault, newer, publisher=lambda _root, _receipt: True, now=4.0
    )

    assert protocol.prove_committed(
        vault, old, current_generation="generation-2", now=5.0
    ).outcome == "superseded"

    # A dead batch stops consuming the bounded snapshot limit.
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT state FROM pending_recall_rows WHERE batch_id = ?",
            (old.batch_id,),
        ).fetchone() == ("retired",)
    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)
    assert snapshot.outcome == "complete"
    assert {batch.receipt.batch_id for batch in snapshot.batches} == {newer.batch_id}


def test_read_seams_do_not_block_behind_a_held_write_lock(vault: Path) -> None:
    protocol = _protocol()
    receipt, target, _before, after = _prepare_one(
        vault,
        batch_id="lock-free-reads",
        required={protocol.DerivedComponent.WRITE_ADVISORY},
    )
    _commit_and_prove(vault, receipt, target, after)
    result_ref = protocol.advisory_result_ref(vault, receipt)
    baseline = protocol.snapshot_pending_visibility(vault, limit=8)
    assert baseline.outcome == "complete"

    writer = sqlite3.connect(deferred_index.store_path(vault), timeout=30.0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO maintenance_state(key, value) VALUES "
            "('correction-round-probe', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        started = time.monotonic()
        snapshot = protocol.snapshot_pending_visibility(vault, limit=8)
        fenced = protocol.pending_visibility_snapshot_is_current(
            vault, snapshot.snapshot_generation
        )
        advisory = protocol.read_advisory_result(vault, result_ref, now=20.0)
        elapsed = time.monotonic() - started
    finally:
        writer.rollback()
        writer.close()

    assert snapshot.outcome == "complete"
    assert {batch.receipt.batch_id for batch in snapshot.batches} == {receipt.batch_id}
    assert fenced is True
    assert advisory is not None and advisory.state == "pending"
    assert elapsed < 1.0, f"read seams blocked behind the write lock for {elapsed:.3f}s"


def test_advisory_publication_refuses_an_expired_lease(vault: Path) -> None:
    protocol = _protocol()
    _receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault, batch_id="expired-lease"
    )
    assert claimed.claim_expires_at is not None

    assert protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(_advisory_candidate(protocol),),
        observed_target_fingerprint=fingerprint,
        now=claimed.claim_expires_at + 1.0,
    ).outcome == "stale_claim"
    assert protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(_advisory_candidate(protocol),),
        observed_target_fingerprint=fingerprint,
        now=claimed.claim_expires_at - 1.0,
    ).outcome == "published"


def test_pending_snapshot_at_exactly_the_limit_is_complete(vault: Path) -> None:
    protocol = _protocol()
    for index in range(2):
        _prepare_one(vault, batch_id=f"exact-limit-{index}")

    snapshot = protocol.snapshot_pending_visibility(vault, limit=2)

    assert snapshot.outcome == "complete"
    assert len(snapshot.batches) == 2
    assert snapshot.failure_code is None


def test_advisory_result_within_terminal_replay_survives_retention_expiry(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(
        vault,
        batch_id="replay-window",
        required={protocol.DerivedComponent.WRITE_ADVISORY},
    )
    result_ref = protocol.advisory_result_ref(vault, receipt)
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE write_advisory_results SET retention_deadline = 5, "
            "terminal_replay_until = 100 WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    # Retention has passed but the mutation terminal can still replay, so the
    # ref must still resolve. Only both deadlines together expire a result.
    still_live = protocol.read_advisory_result(vault, result_ref, now=50.0)
    assert still_live is not None
    assert still_live.state == "pending"
    assert protocol.read_advisory_result(vault, result_ref, now=101.0) is None


def test_deleting_a_pending_row_advances_the_visibility_generation(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(vault, batch_id="delete-fence")
    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)
    assert protocol.pending_visibility_snapshot_is_current(
        vault, snapshot.snapshot_generation
    )

    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "DELETE FROM pending_recall_rows WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    assert not protocol.pending_visibility_snapshot_is_current(
        vault, snapshot.snapshot_generation
    )


def test_pending_snapshot_lineage_mismatch_is_unprovable(vault: Path) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(vault, batch_id="lineage-drift")
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE pending_recall_rows SET canonical_generation = 'generation-other' "
            "WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    snapshot = protocol.snapshot_pending_visibility(vault, limit=8)

    assert snapshot.outcome == "unprovable"
    assert snapshot.batches == ()
    assert snapshot.failure_code == "pending_visibility_unprovable"


def test_partly_retired_custody_cannot_be_retired_again(vault: Path) -> None:
    protocol = _protocol()
    rel_a = "Knowledge Base/Notes/partly-a.md"
    rel_b = "Knowledge Base/Notes/partly-b.md"
    for rel, body in ((rel_a, b"a"), (rel_b, b"b")):
        _write(vault / rel, body)
    receipt = _prepare(
        vault,
        batch_id="partly-retired",
        paths=(
            _path(protocol, rel_a, before=b"a", after=b"a-after"),
            _path(protocol, rel_b, before=b"b", after=b"b-after"),
        ),
        required={protocol.DerivedComponent.LEXSTORE},
    )
    _write(vault / rel_a, b"a-after")
    _write(vault / rel_b, b"b-after")
    assert protocol.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    ).outcome == "ready"
    assert protocol.publish_pending_visibility(
        vault, receipt, publisher=lambda _root, _receipt: True
    )
    batch = _snapshot_batch(protocol, vault, receipt.batch_id)
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE pending_recall_rows SET state = 'retired' "
            "WHERE batch_id = ? AND rel_path = ?",
            (receipt.batch_id, rel_a),
        )

    assert protocol.retire_pending_visibility(vault, batch).outcome == "stale"
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert sorted(
            connection.execute(
                "SELECT rel_path, state FROM pending_recall_rows WHERE batch_id = ?",
                (receipt.batch_id,),
            ).fetchall()
        ) == [(rel_a, "retired"), (rel_b, "live")]


def test_advisory_target_fingerprint_must_equal_the_prepared_after_hash(
    vault: Path,
) -> None:
    protocol = _protocol()
    rel = "Knowledge Base/Notes/fingerprint-contract.md"
    paths = (_path(protocol, rel, before=None, after=b"body"),)
    exact = _hash_bytes(b"body")

    with pytest.raises(ValueError, match="after hash"):
        _prepare(
            vault,
            batch_id="fingerprint-mismatch",
            paths=paths,
            required={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_rel_path=rel,
            advisory_target_fingerprint=_hash_bytes(b"something-else"),
            terminal_replay_until=100.0,
        )
    receipt = _prepare(
        vault,
        batch_id="fingerprint-exact",
        paths=paths,
        required={protocol.DerivedComponent.WRITE_ADVISORY},
        advisory_target_rel_path=rel,
        advisory_target_fingerprint=exact,
        terminal_replay_until=100.0,
    )
    assert protocol.advisory_result_ref(vault, receipt) is not None
    with pytest.raises(ValueError, match="cannot carry target identity"):
        _prepare(
            vault,
            batch_id="fingerprint-not-required",
            paths=paths,
            required={protocol.DerivedComponent.LEXSTORE},
            advisory_target_fingerprint=exact,
        )


def test_superseded_result_projects_closed_even_with_orphan_candidates(
    vault: Path,
) -> None:
    protocol = _protocol()
    receipt, claimed, fingerprint = _prepare_advisory_claim(
        vault, batch_id="orphan-superseded"
    )
    result_ref = protocol.advisory_result_ref(vault, receipt)
    assert protocol.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(_advisory_candidate(protocol),),
        observed_target_fingerprint=fingerprint,
        now=3.0,
    ).outcome == "published"
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE write_advisory_results SET state = 'superseded', "
            "failure_code = NULL WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    superseded = protocol.read_advisory_result(vault, result_ref, now=4.0)

    assert superseded is not None
    assert superseded.state == "superseded"
    assert superseded.failure_code is None
    assert superseded.candidates == ()


def test_typed_values_bound_every_relative_path(vault: Path) -> None:
    protocol = _protocol()
    assert deferred_index._safe_markdown_rel_path(_OVERLONG_REL) == _OVERLONG_REL

    with pytest.raises(ValueError, match="rel_path"):
        protocol.DerivedBatchPath(
            rel_path=_OVERLONG_REL,
            before_hash=None,
            after_hash=_hash_bytes(b"x"),
        )
    with pytest.raises(ValueError, match="rel_path"):
        protocol.PendingVisibilityRow(
            rel_path=_OVERLONG_REL,
            component_revision=1,
            canonical_generation="generation-1",
            state="live",
        )
    with pytest.raises(ValueError, match="rel_path"):
        replace(_advisory_candidate(protocol), counterpart_rel_path=_OVERLONG_REL)


def test_cleanup_treats_a_nonpositive_limit_as_zero(vault: Path) -> None:
    protocol = _protocol()
    receipt, _target, _before, _after = _prepare_one(
        vault,
        batch_id="nonpositive-limit",
        required={protocol.DerivedComponent.WRITE_ADVISORY},
    )
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        connection.execute(
            "UPDATE write_advisory_results SET retention_deadline = 1, "
            "terminal_replay_until = 1 WHERE batch_id = ?",
            (receipt.batch_id,),
        )

    assert protocol.cleanup_advisory_results(vault, now=50.0, limit=-1) == 0
    assert protocol.cleanup_advisory_results(vault, now=50.0, limit=0) == 0
    with sqlite3.connect(deferred_index.store_path(vault)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM write_advisory_results WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone() == (1,)
    assert protocol.cleanup_advisory_results(vault, now=50.0, limit=8) == 1


def test_fake_refuses_every_input_production_refuses() -> None:
    protocol = _protocol()
    rel = "Knowledge Base/Notes/fake-parity.md"
    path = _path(protocol, rel, before=None, after=b"body")
    exact = _hash_bytes(b"body")

    def _fresh():
        fake = DerivedReceiptProtocolFake()
        return fake

    def _prepared(fake, batch_id="parity"):
        return fake.prepare_batch(
            Path("vault"),
            batch_id=batch_id,
            mutation_attempt_digest=hashlib.sha256(batch_id.encode()).hexdigest(),
            canonical_generation="generation-fake",
            checkpoint_id="checkpoint-fake",
            paths=(path,),
            required_components={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_rel_path=rel,
            advisory_target_fingerprint=exact,
            terminal_replay_until=100.0,
            now=1.0,
        )

    # prepare_batch: advisory required without a target path.
    with pytest.raises(ValueError, match="target path"):
        _fresh().prepare_batch(
            Path("vault"),
            batch_id="no-target",
            mutation_attempt_digest=hashlib.sha256(b"no-target").hexdigest(),
            canonical_generation="generation-fake",
            checkpoint_id="checkpoint-fake",
            paths=(path,),
            required_components={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_fingerprint=exact,
            terminal_replay_until=100.0,
        )
    # prepare_batch: target path absent from the prepared batch.
    with pytest.raises(ValueError, match="prepared batch"):
        _fresh().prepare_batch(
            Path("vault"),
            batch_id="foreign-target",
            mutation_attempt_digest=hashlib.sha256(b"foreign-target").hexdigest(),
            canonical_generation="generation-fake",
            checkpoint_id="checkpoint-fake",
            paths=(path,),
            required_components={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_rel_path="Knowledge Base/Notes/elsewhere.md",
            advisory_target_fingerprint=exact,
            terminal_replay_until=100.0,
        )
    # prepare_batch: fingerprint that is not the prepared after hash.
    with pytest.raises(ValueError, match="after hash"):
        _fresh().prepare_batch(
            Path("vault"),
            batch_id="wrong-fingerprint",
            mutation_attempt_digest=hashlib.sha256(b"wrong-fingerprint").hexdigest(),
            canonical_generation="generation-fake",
            checkpoint_id="checkpoint-fake",
            paths=(path,),
            required_components={protocol.DerivedComponent.WRITE_ADVISORY},
            advisory_target_rel_path=rel,
            advisory_target_fingerprint=_hash_bytes(b"other"),
            terminal_replay_until=100.0,
        )
    # prepare_batch: target identity supplied when advisory is not required.
    with pytest.raises(ValueError, match="cannot carry target identity"):
        _fresh().prepare_batch(
            Path("vault"),
            batch_id="unwanted-target",
            mutation_attempt_digest=hashlib.sha256(b"unwanted-target").hexdigest(),
            canonical_generation="generation-fake",
            checkpoint_id="checkpoint-fake",
            paths=(path,),
            required_components={protocol.DerivedComponent.LEXSTORE},
            advisory_target_rel_path=rel,
            advisory_target_fingerprint=exact,
        )

    candidate = _advisory_candidate(protocol)
    claimed_of = lambda fake, receipt: replace(  # noqa: E731
        fake.component_status(
            Path("vault"), receipt, protocol.DerivedComponent.WRITE_ADVISORY
        ),
        state="claimed",
        claim_owner="fake-worker",
        claim_expires_at=60.0,
    )

    # publish_advisory_result: state=pending is not a publishable outcome.
    fake = _fresh()
    receipt = _prepared(fake)
    with pytest.raises(ValueError, match="ready or failed"):
        fake.publish_advisory_result(
            Path("vault"),
            claimed_of(fake, receipt),
            state="pending",
            observed_target_fingerprint=exact,
        )
    # publish_advisory_result: duplicate candidates.
    with pytest.raises(ValueError, match="unique"):
        fake.publish_advisory_result(
            Path("vault"),
            claimed_of(fake, receipt),
            state="ready",
            candidates=(candidate, candidate),
            observed_target_fingerprint=exact,
        )
    # publish_advisory_result: unbounded observed fingerprints.
    for bad in ("", None, "f" * 129):
        with pytest.raises(ValueError, match="observed_target_fingerprint"):
            fake.publish_advisory_result(
                Path("vault"),
                claimed_of(fake, receipt),
                state="ready",
                candidates=(candidate,),
                observed_target_fingerprint=bad,
            )


def test_fake_retirement_replay_is_idempotent_like_production() -> None:
    protocol = _protocol()
    rel = "Knowledge Base/Notes/fake-idempotent.md"
    path = _path(protocol, rel, before=None, after=b"body")
    fake = DerivedReceiptProtocolFake()
    receipt = fake.prepare_batch(
        Path("vault"),
        batch_id="idempotent-retire",
        mutation_attempt_digest=hashlib.sha256(b"idempotent-retire").hexdigest(),
        canonical_generation="generation-fake",
        checkpoint_id="checkpoint-fake",
        paths=(path,),
        required_components={protocol.DerivedComponent.LEXSTORE},
        now=1.0,
    )
    assert fake.publish_pending_visibility(
        Path("vault"), receipt, publisher=lambda _root, _receipt: True
    )
    live = fake.snapshot_pending_visibility(Path("vault"), limit=8)
    [batch] = live.batches

    assert fake.retire_pending_visibility(Path("vault"), batch).outcome == "retired"
    # Production returns `retired` for the identical replay; the fake must too.
    assert fake.retire_pending_visibility(Path("vault"), batch).outcome == "retired"


def test_fake_refuses_retiring_an_unpublished_prepared_row() -> None:
    protocol = _protocol()
    rel = "Knowledge Base/Notes/fake-prepared.md"
    path = _path(protocol, rel, before=None, after=b"body")
    fake = DerivedReceiptProtocolFake()
    receipt = fake.prepare_batch(
        Path("vault"),
        batch_id="fake-prepared-retire",
        mutation_attempt_digest=hashlib.sha256(b"fake-prepared-retire").hexdigest(),
        canonical_generation="generation-fake",
        checkpoint_id="checkpoint-fake",
        paths=(path,),
        required_components={protocol.DerivedComponent.LEXSTORE},
        now=1.0,
    )
    assert receipt.batch_id == "fake-prepared-retire"
    [batch] = fake.snapshot_pending_visibility(Path("vault"), limit=8).batches
    assert tuple(row.state for row in batch.rows) == ("prepared",)

    assert fake.retire_pending_visibility(Path("vault"), batch).outcome == "stale"


# --------------------------------------------------------------------------- #
# Lane 5 additions to the foundation's own guards (integration packet item 7)
# --------------------------------------------------------------------------- #


def _pending_states(vault: Path, batch_id: str) -> dict[str, str]:
    connection = sqlite3.connect(deferred_index.store_path(vault))
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT rel_path, state FROM pending_recall_rows WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        }
    finally:
        connection.close()


def test_newer_visibility_covers_a_batch_whose_rows_already_retired(
    vault: Path,
) -> None:
    """Supersession accepts a newer batch that has already converged.

    ``_newer_visibility_covers`` requires the newer batch's rows to be `live`
    OR `retired`, and the `retired` half is the load-bearing one: a newer batch
    that published and then converged is the strongest possible cover, and
    demanding `live` would refuse exactly that case -- leaving the older batch
    unprovable for ever instead of superseded, and holding its custody against
    the bounded snapshot limit.
    """
    protocol = _protocol()
    rel = "Knowledge Base/Notes/supersede-retired.md"
    target = vault / rel
    before, middle, after = b"before", b"middle", b"after"
    _write(target, before)

    older = _prepare(
        vault,
        batch_id="older-batch",
        generation="generation-1",
        paths=(_path(protocol, rel, before=before, after=middle),),
        required=frozenset({protocol.DerivedComponent.LEXSTORE}),
        now=10.0,
    )
    newer = _prepare(
        vault,
        batch_id="newer-batch",
        generation="generation-1",
        paths=(_path(protocol, rel, before=middle, after=after),),
        required=frozenset({protocol.DerivedComponent.LEXSTORE}),
        now=11.0,
    )
    _write(target, after)
    newer_proof = protocol.prove_committed(
        vault, newer, current_generation="generation-1"
    )
    assert newer_proof.outcome == "ready"
    assert protocol.publish_pending_visibility(
        vault, newer, publisher=lambda _root, _receipt: True
    )

    # The newer batch converges: its rows retire through the exact CAS.
    snapshot = protocol.snapshot_pending_visibility(vault, limit=64)
    newer_batch = next(
        batch for batch in snapshot.batches if batch.receipt.batch_id == "newer-batch"
    )
    assert (
        protocol.retire_pending_visibility(vault, newer_batch).outcome == "retired"
    )
    assert set(_pending_states(vault, "newer-batch").values()) == {"retired"}

    older_proof = protocol.prove_committed(
        vault, older, current_generation="generation-1"
    )
    assert older_proof.outcome == "superseded", older_proof.outcome


def test_aborted_batch_stops_consuming_the_bounded_snapshot_limit(
    vault: Path,
) -> None:
    """An aborted batch's `prepared` rows must not accumulate for ever.

    The abort transition already closes every component, but its pending rows
    were left `prepared`, and exact retirement deliberately refuses a
    never-published row -- so nothing could ever clear them. They then counted
    against the bounded hydration snapshot, and enough aborted writes would
    overflow it and fail managed recall closed on custody for canonical bytes
    that were rolled back and no longer exist.
    """
    protocol = _protocol()
    receipt, target, before, _after = _prepare_one(
        vault, batch_id="aborted-batch", before=b"before", after=b"after"
    )
    assert set(_pending_states(vault, "aborted-batch").values()) == {"prepared"}

    # The canonical batch rolled back: the complete before-state is restored.
    _write(target, before)
    proof = protocol.prove_committed(
        vault,
        receipt,
        current_generation=receipt.canonical_generation,
        known_uncommitted=True,
    )
    assert proof.outcome == "aborted"

    assert set(_pending_states(vault, "aborted-batch").values()) == {"retired"}
    snapshot = protocol.snapshot_pending_visibility(vault, limit=64)
    assert snapshot.outcome == "complete"
    assert all(
        batch.receipt.batch_id != "aborted-batch" for batch in snapshot.batches
    ), [batch.receipt.batch_id for batch in snapshot.batches]
