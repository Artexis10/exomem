from __future__ import annotations

import threading
from pathlib import Path

import pytest

# Module scope because the parametrized remediation expectations below are built
# at collection time. They reference the shared strings rather than re-pinning
# copies: the wording is allowed to change, the semantics are not.
from exomem import graph_sync


def _receipt(root: Path, *, digest: str, attempt: object) -> None:
    from exomem import graph_sync

    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest=digest,
        attempt_id=attempt.attempt_id,
        commit_token=attempt.commit_token,
        canonical_disposition="success",
        terminal_projection={"status": "committed", "mutated": True},
        commit_secret=attempt.commit_secret,
    )
    graph_sync.write_graph_commit_receipt(root, receipt)


def _exact_receipt(
    root: Path, digest: str, attempt_id: str, token: str, secret: bytes | None = None
) -> bool:
    from exomem import graph_sync

    receipt = graph_sync.read_graph_commit_receipt(root, token)
    return bool(
        receipt is not None
        and (
            receipt.verify(
                secret,
                idempotency_key_digest="a" * 64,
                command_digest=digest,
                attempt_id=attempt_id,
                commit_token=token,
            )
            if secret is not None
            else receipt.command_digest == digest
            and receipt.attempt_id == attempt_id
            and receipt.commit_token == token
        )
    )


def _legacy_graph_pending_checkpoint(
    vault: Path, *, generation: int = 1, mutation_id: str = "1" * 24
):
    from exomem import graph_sync

    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=generation,
        mutation_id=mutation_id,
        paths=(),
        created_paths=(),
        scope="full",
    )
    graph_sync._write_floor(vault, graph_sync.GraphSyncGenerationFloor.create(generation))
    graph_sync._write_checkpoint(vault, checkpoint)
    return checkpoint


def _legacy_graph_pending_proof(vault: Path):
    from exomem import graph_sync

    def proof(candidate):  # noqa: ANN001
        return (
            graph_sync.read_checkpoint(vault) == candidate
            and graph_sync.classify_epoch(vault).kind == "coherent"
        )

    return proof


def _assert_retained_outcome_unknown(store: object) -> None:
    import pickle

    with store._connect() as conn:
        state, payload = conn.execute("SELECT state, result FROM mutations").fetchone()
    assert state == "completed"
    assert pickle.loads(payload) == ("exomem.outcome-unknown", 1)


def test_canonical_result_is_handed_off_before_derived_wait(tmp_path: Path) -> None:
    """The graph wait cannot retain execution ownership of the leaf."""
    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    canonical_guard_released = threading.Event()
    allow_graph_completion = threading.Event()
    results: list[object] = []

    class Guard:
        def __enter__(self) -> Guard:
            return self

        def __exit__(self, *_args: object) -> None:
            canonical_guard_released.set()

    def invoke() -> None:
        results.append(
            store.run(
                "handoff",
                "digest",
                lambda: {"canonical": "committed"},
                operation_guard=Guard,
                after_canonical_persisted=lambda result: result,
                after_operation_guard=lambda result: (
                    allow_graph_completion.wait(2) and {**result, "graph_sync": "completed"}
                ),
            )
        )

    worker = threading.Thread(target=invoke)
    worker.start()
    assert canonical_guard_released.wait(1)
    with store._connect() as conn:
        assert conn.execute("SELECT state, owner FROM mutations").fetchone() == (
            "canonically_committed",
            None,
        )
    allow_graph_completion.set()
    worker.join(2)
    assert results == [{"canonical": "committed", "graph_sync": "completed"}]


def test_exact_receipt_recovers_same_process_canonical_persistence_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leaf ran once even when its canonical SQLite CAS failed."""
    from exomem.writer_lease import IdempotencyStore, OpError

    root = tmp_path / "vault"
    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    digest = "d" * 64
    calls = 0
    original = store._persist_canonically_committed
    failed = False

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise __import__("sqlite3").OperationalError("injected canonical CAS failure")
        original(*args, **kwargs)

    monkeypatch.setattr(store, "_persist_canonically_committed", fail_once)

    def leaf() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"canonical": "committed"}

    def persist(result: dict[str, str], attempt: object) -> dict[str, str]:
        _receipt(root, digest=digest, attempt=attempt)
        return result

    with pytest.raises(OpError, match="exact terminal result could not be persisted"):
        store.run(
            "exact-canonical-cas",
            digest,
            leaf,
            after_canonical_persisted=persist,
            commit_evidence=lambda expected, attempt_id, token: _exact_receipt(
                root, expected, attempt_id, token
            ),
        )

    recovered = store.run(
        "exact-canonical-cas",
        digest,
        lambda: pytest.fail("canonical leaf replayed"),
        resume_canonically_committed=lambda _stored: {"canonical": "resumed"},
        commit_evidence=lambda expected, attempt_id, token: _exact_receipt(
            root, expected, attempt_id, token
        ),
    )

    assert recovered == {"canonical": "resumed"}
    assert calls == 1


def test_lease_manager_recovers_terminal_from_exact_receipt_after_canonical_cas_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact retry rebuilds only graph work when SQLite lost the terminal handoff."""
    import sqlite3
    from types import SimpleNamespace

    from exomem.writer_lease import LeaseConfig, LeaseManager, OpError
    from exomem.vault import PlannedWrite, batch_atomic_write

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/recovered.md"
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    original = manager.idempotency._persist_canonically_committed
    failed = False

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("injected canonical CAS failure")
        original(*args, **kwargs)

    monkeypatch.setattr(manager.idempotency, "_persist_canonically_committed", fail_once)

    def leaf(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# recovered\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/recovered.md", "warnings": []}

    command = SimpleNamespace(name="receipt-cas-recovery", read_only=False, leaf=leaf)
    with pytest.raises(OpError) as uncertain:
        manager.invoke(command, (vault,), {}, idempotency_key="receipt-cas-recovery")
    assert uncertain.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"

    recovered = manager.invoke(command, (vault,), {}, idempotency_key="receipt-cas-recovery")

    assert recovered["status"] == "committed"
    assert recovered["mutated"] is True
    assert recovered["terminal"] is True
    assert recovered["graph_sync"] == "completed"
    assert calls == 1


def test_canonical_resume_rebuild_keeps_the_invoking_manager_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt recovery must not fall back to ambient runtime state after the leaf."""
    import sqlite3
    from types import SimpleNamespace

    from exomem import epistemic_graph
    from exomem.writer_lease import LeaseConfig, LeaseManager, OpError
    from exomem.vault import PlannedWrite, batch_atomic_write

    ambient_state = tmp_path / "ambient-state"
    custom_state = tmp_path / "custom-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(ambient_state))
    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/resume-boundary.md"
    manager = LeaseManager(LeaseConfig(state_dir=custom_state))
    original_persist = manager.idempotency._persist_canonically_committed
    failed = False
    observed_roots: list[Path] = []

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("injected canonical CAS failure")
        original_persist(*args, **kwargs)

    def capture_rebuild(self):  # noqa: ANN001
        observed_roots.append(self._canonical_mutation_coordinator().state_root)
        return {"indexed_files": 0, "nodes": 0, "edges": 0}

    monkeypatch.setattr(manager.idempotency, "_persist_canonically_committed", fail_once)
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "rebuild_all", capture_rebuild)

    def leaf(root: Path) -> dict[str, object]:
        batch_atomic_write([PlannedWrite(note, "# resume boundary\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/resume-boundary.md", "warnings": []}

    command = SimpleNamespace(name="resume-boundary", read_only=False, leaf=leaf)
    with pytest.raises(OpError, match="exact terminal result"):
        manager.invoke(command, (vault,), {}, idempotency_key="resume-boundary")

    result = manager.invoke(command, (vault,), {}, idempotency_key="resume-boundary")

    assert result["graph_sync"] == "failed"
    assert observed_roots == [custom_state]


def test_receipt_recovery_preserves_public_terminal_schemas_without_leaf_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt recovery rebuilds a terminal envelope, not a bare projection."""
    import json
    import sqlite3
    from types import SimpleNamespace

    from exomem.writer_lease import LeaseConfig, LeaseManager, OpError
    from exomem.vault import PlannedWrite, batch_atomic_write

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/schema-recovery.md"
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    original = manager.idempotency._persist_canonically_committed
    failed = False

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("injected canonical CAS failure")
        original(*args, **kwargs)

    monkeypatch.setattr(manager.idempotency, "_persist_canonically_committed", fail_once)

    def leaf(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# schema recovery\n")], vault_root=root)
        return {
            "path": "Knowledge Base/Notes/schema-recovery.md",
            "warnings": ["private warning"],
            "private_leaf_value": "must not enter the graph receipt",
        }

    command = SimpleNamespace(name="receipt-schema-recovery", read_only=False, leaf=leaf)
    with pytest.raises(OpError) as uncertain:
        manager.invoke(
            command,
            (vault,),
            {"response_detail": "compact"},
            idempotency_key="receipt-schema-recovery",
        )
    assert uncertain.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"

    compact = manager.invoke(
        command,
        (vault,),
        {"response_detail": "compact"},
        idempotency_key="receipt-schema-recovery",
    )
    full = manager.invoke(
        command,
        (vault,),
        {"response_detail": "full"},
        idempotency_key="receipt-schema-recovery",
    )
    legacy = manager.invoke(
        command,
        (vault,),
        {"response_detail": "legacy"},
        idempotency_key="receipt-schema-recovery",
    )
    compact_replay = manager.invoke(
        command,
        (vault,),
        {"response_detail": "compact"},
        idempotency_key="receipt-schema-recovery",
    )

    assert {
        "ok",
        "state",
        "terminal",
        "status",
        "mutated",
        "request_id",
        "receipt_id",
        "idempotency_key",
        "warnings_count",
    } <= compact.keys()
    assert full == {**compact, "diagnostics": legacy}
    assert isinstance(legacy, dict)
    assert compact_replay == compact
    assert "private_leaf_value" not in json.dumps(full)
    assert "private warning" not in json.dumps(full)
    assert calls == 1


def test_receipt_result_digest_commits_non_retained_leaf_summary_without_leaking_it(
    tmp_path: Path,
) -> None:
    """Receipt digests bind hidden leaf output without retaining its content."""
    import json
    from types import SimpleNamespace

    from exomem.writer_lease import LeaseConfig, LeaseManager
    from exomem.vault import PlannedWrite, batch_atomic_write

    def receipt_for(result: dict[str, object], name: str) -> tuple[dict[str, object], bytes]:
        vault = tmp_path / name
        note = vault / "Knowledge Base/Notes/receipt-digest.md"
        manager = LeaseManager(
            LeaseConfig(state_dir=tmp_path / f"{name}-state", vault_id="receipt-digest-test")
        )

        def leaf(root: Path) -> dict[str, object]:
            batch_atomic_write([PlannedWrite(note, "# receipt digest\n")], vault_root=root)
            return result

        command = SimpleNamespace(name="receipt-result-digest", read_only=False, leaf=leaf)
        manager.invoke(
            command,
            (vault,),
            {},
            idempotency_key="receipt-result-digest",
            mutation_request_id="11111111-1111-4111-8111-111111111111",
        )
        raw = next((vault / "Knowledge Base/.graph-commit-receipts").glob("*.json")).read_bytes()
        return json.loads(raw)["terminal_projection"], raw

    first, first_bytes = receipt_for(
        {
            "path": "Knowledge Base/Notes/private-first.md",
            "warnings": ["private first warning"],
            "private_result": {"content": "first private content", "count": 1},
        },
        "first",
    )
    second, second_bytes = receipt_for(
        {
            "path": "Knowledge Base/Notes/private-second.md",
            "warnings": ["private second warning"],
            "private_result": {"content": "second private content", "count": 1},
        },
        "second",
    )
    reordered, reordered_bytes = receipt_for(
        {
            "private_result": {"count": 1, "content": "first private content"},
            "warnings": ["private first warning"],
            "path": "Knowledge Base/Notes/private-first.md",
        },
        "reordered",
    )

    def without_digest(projection: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in projection.items() if key != "result_sha256"}

    assert without_digest(first) == without_digest(second) == without_digest(reordered)
    assert first["result_sha256"] != second["result_sha256"]
    assert first["result_sha256"] == reordered["result_sha256"]
    for receipt_bytes in (first_bytes, second_bytes, reordered_bytes):
        assert b"private-first.md" not in receipt_bytes
        assert b"private-second.md" not in receipt_bytes
        assert b"private first warning" not in receipt_bytes
        assert b"private second warning" not in receipt_bytes
        assert b"first private content" not in receipt_bytes
        assert b"second private content" not in receipt_bytes


def test_receipt_result_digest_uses_closed_summary_for_unserializable_values() -> None:
    """Opaque values must not leak through a fallback string representation."""
    from exomem import writer_lease

    class UnserializableLeafValue:
        def __str__(self) -> str:
            return "private __str__ output must not reach the receipt"

    first_value = UnserializableLeafValue()
    first = writer_lease._receipt_result_sha256({"opaque": first_value})
    second = writer_lease._receipt_result_sha256({"opaque": UnserializableLeafValue()})
    summary = writer_lease._receipt_result_summary({"opaque": first_value})

    assert first == second
    assert "private __str__ output" not in first
    assert summary["items"][0][1] == {"type": "opaque"}


def test_lease_manager_receipt_recovery_returns_graph_failure_after_rebuild_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt-backed recovery remains terminal when the derived rebuild itself fails."""
    import sqlite3
    from types import SimpleNamespace

    from exomem.epistemic_graph import EpistemicGraphIndex
    from exomem.writer_lease import LeaseConfig, LeaseManager, OpError
    from exomem.vault import PlannedWrite, batch_atomic_write

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/rebuild-error.md"
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    original = manager.idempotency._persist_canonically_committed
    failed = False

    def fail_canonical_cas(*args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("injected canonical CAS failure")
        original(*args, **kwargs)

    monkeypatch.setattr(
        manager.idempotency, "_persist_canonically_committed", fail_canonical_cas
    )

    def leaf(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# rebuild error\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/rebuild-error.md", "warnings": []}

    command = SimpleNamespace(name="receipt-rebuild-error", read_only=False, leaf=leaf)
    with pytest.raises(OpError) as uncertain:
        manager.invoke(command, (vault,), {}, idempotency_key="receipt-rebuild-error")
    assert uncertain.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"

    def fail_rebuild(_self: EpistemicGraphIndex) -> dict[str, int]:
        raise RuntimeError("injected graph rebuild failure")

    monkeypatch.setattr(EpistemicGraphIndex, "rebuild_all", fail_rebuild)
    recovered = manager.invoke(command, (vault,), {}, idempotency_key="receipt-rebuild-error")
    replay = manager.invoke(command, (vault,), {}, idempotency_key="receipt-rebuild-error")

    assert recovered["status"] == "committed"
    assert recovered["graph_sync"] == "failed"
    assert recovered["graph_sync_code"] == "GRAPH_SYNC_STABILIZATION_EXHAUSTED"
    assert recovered == replay
    assert calls == 1


def test_pre_receipt_cut_is_retained_as_a_completed_outcome_unknown_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead post-write attempt without a receipt remains fail-closed without a fifth state."""
    import sqlite3
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.writer_lease import LeaseConfig, LeaseManager, OpError
    from exomem.vault import PlannedWrite, batch_atomic_write

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/unknown.md"
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    original = graph_sync.write_graph_commit_receipt

    def fail_receipt(*args: object, **kwargs: object) -> Path:
        raise OSError("injected receipt failure after caller files")

    monkeypatch.setattr(graph_sync, "write_graph_commit_receipt", fail_receipt)

    def leaf(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# unknown\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/unknown.md", "warnings": []}

    command = SimpleNamespace(name="receipt-cut", read_only=False, leaf=leaf)
    with pytest.raises(OpError) as first:
        manager.invoke(command, (vault,), {}, idempotency_key="receipt-cut")
    assert first.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    monkeypatch.setattr(graph_sync, "write_graph_commit_receipt", original)

    with pytest.raises(OpError) as retry:
        manager.invoke(command, (vault,), {}, idempotency_key="receipt-cut")
    assert retry.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert "will remain fail-closed" in retry.value.remediation
    assert calls == 1
    with sqlite3.connect(manager.idempotency.path) as connection:
        assert connection.execute("SELECT state FROM mutations").fetchone() == ("completed",)


def test_committed_cleanup_failure_replays_from_completed_state_without_leaf_rerun(
    tmp_path: Path,
) -> None:
    """New committed failures use the canonical terminal state, not a fifth lifecycle state."""
    import sqlite3
    from types import SimpleNamespace

    from exomem.vault import BatchTargetSummary, BatchWriteError, PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/cleanup.md"
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    def leaf(root: Path) -> None:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# cleanup\n")], vault_root=root)
        raise BatchWriteError(
            "BATCH_CLEANUP_INCOMPLETE",
            BatchTargetSummary(1, ("Knowledge Base/Notes/cleanup.md",), 0),
            committed=True,
        )

    command = SimpleNamespace(name="committed-cleanup", read_only=False, leaf=leaf)
    with pytest.raises(BatchWriteError) as first:
        manager.invoke(command, (vault,), {}, idempotency_key="committed-cleanup")
    with pytest.raises(ValueError) as replay:
        manager.invoke(command, (vault,), {}, idempotency_key="committed-cleanup")

    assert replay.value.as_public_dict() == first.value.as_public_dict()
    assert calls == 1
    with sqlite3.connect(manager.idempotency.path) as connection:
        assert connection.execute("SELECT state FROM mutations").fetchone() == ("completed",)


def test_post_fanout_cleanup_failure_finishes_graph_before_exact_failure_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real post-fanout cleanup error cannot strand its registered graph work."""
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.vault import BatchWriteError, PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager
    from exomem import vault as vault_module

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/cleanup-after-fanout.md"
    calls = 0
    original_cleanup = vault_module._cleanup_batch_workspaces
    injected = False

    def retain_first_cleanup(*args: object, **kwargs: object) -> bool:
        nonlocal injected
        cleaned = original_cleanup(*args, **kwargs)
        if not injected:
            injected = True
            return True
        return cleaned

    monkeypatch.setattr(vault_module, "_cleanup_batch_workspaces", retain_first_cleanup)

    def leaf(root: Path) -> None:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# cleanup after fanout\n")], vault_root=root)

    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    command = SimpleNamespace(name="cleanup-after-fanout", read_only=False, leaf=leaf)
    with pytest.raises(BatchWriteError) as first:
        manager.invoke(command, (vault,), {}, idempotency_key="cleanup-after-fanout")
    with pytest.raises(ValueError) as replay:
        manager.invoke(command, (vault,), {}, idempotency_key="cleanup-after-fanout")

    # The registered work is no longer joined by the write (#576/#588). Joining
    # it explicitly is what actually proves this test's subject -- that a
    # post-fanout cleanup error cannot *strand* the work -- instead of relying
    # on a blocking side effect of the write path to have finished it.
    graph_sync.await_active_rebuild(vault, state_root=tmp_path / "state")
    assert graph_sync.status(vault)["state"] == "current"
    assert replay.value.as_public_dict() == first.value.as_public_dict()
    assert calls == 1


def test_cleanup_receipt_before_canonical_cas_never_recovers_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signed receipt retains failure classification across the SQLite cut."""
    import sqlite3
    from types import SimpleNamespace

    from exomem import vault as vault_module
    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager, OpError

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/cleanup-cas.md"
    calls = 0
    original_cleanup = vault_module._cleanup_batch_workspaces
    cleanup_failed = False

    def retain_first_cleanup(*args: object, **kwargs: object) -> bool:
        nonlocal cleanup_failed
        cleaned = original_cleanup(*args, **kwargs)
        if not cleanup_failed:
            cleanup_failed = True
            return True
        return cleaned

    monkeypatch.setattr(vault_module, "_cleanup_batch_workspaces", retain_first_cleanup)
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    persist = manager.idempotency._persist_canonically_committed
    failed = False

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("injected canonical CAS failure")
        persist(*args, **kwargs)

    monkeypatch.setattr(manager.idempotency, "_persist_canonically_committed", fail_once)

    def leaf(root: Path) -> None:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# cleanup cas\n")], vault_root=root)

    command = SimpleNamespace(name="cleanup-cas", read_only=False, leaf=leaf)
    with pytest.raises(OpError) as first:
        manager.invoke(command, (vault,), {}, idempotency_key="cleanup-cas")
    assert first.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    with pytest.raises(OpError) as replay:
        manager.invoke(command, (vault,), {}, idempotency_key="cleanup-cas")
    assert replay.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert calls == 1
    receipts = list((vault / "Knowledge Base/.graph-commit-receipts").glob("*.json"))
    assert len(receipts) == 1
    from exomem import graph_sync

    assert graph_sync.GraphCommitReceipt.parse(receipts[0].read_bytes()).canonical_disposition == (
        "committed_failure"
    )
    with sqlite3.connect(manager.idempotency.path) as connection:
        assert connection.execute("SELECT state FROM mutations").fetchone() == ("completed",)


def test_compact_terminal_retains_deferred_graph_failure_from_real_lease_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred graph handoff remains visible through compact terminal projection."""
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/deferred.md"

    def leaf(root: Path) -> dict[str, object]:
        batch_atomic_write([PlannedWrite(note, "# deferred\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/deferred.md", "warnings": []}

    result = LeaseManager(LeaseConfig(state_dir=tmp_path / "state")).invoke(
        SimpleNamespace(name="deferred-graph", read_only=False, leaf=leaf),
        (vault,),
        {},
        idempotency_key="deferred-graph",
    )

    assert graph_sync.status(vault)["state"] == "recovery_required"
    assert result["status"] == "committed"
    assert result["graph_sync"] == "failed"
    assert result["graph_sync_code"] == "GRAPH_SYNC_SCHEDULING_DISABLED"
    assert result["graph_sync_checkpoint"]
    assert result["graph_sync_remediation"] == (
        f"Enable graph scheduling, or {graph_sync._RECONCILE_HINT}"
    )


def test_lease_manager_outer_upsert_failure_registers_graph_failure_before_guard_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caught batch fanout error keeps its exact graph epoch through replay."""
    from types import SimpleNamespace

    from exomem import graph_sync, index_sync
    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/outer-upsert.md"
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    calls = 0
    registered: list[graph_sync.GraphSyncCheckpoint | None] = []
    original_register_failure = graph_sync.register_outer_fanout_failure

    def fail_outer_upsert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected outer upsert failure")

    def register_failure_while_guarded(root: Path, **kwargs: object) -> graph_sync.GraphSyncCheckpoint | None:
        assert manager.status(root)["mutation_boundary"]["state"] == "held"
        checkpoint = original_register_failure(root, **kwargs)
        registered.append(graph_sync.registered_checkpoint(root))
        return checkpoint

    monkeypatch.setattr(index_sync, "upsert_after_write", fail_outer_upsert)
    monkeypatch.setattr(graph_sync, "register_outer_fanout_failure", register_failure_while_guarded)

    def leaf(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# outer upsert\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/outer-upsert.md", "warnings": []}

    command = SimpleNamespace(name="outer-upsert", read_only=False, leaf=leaf)
    result = manager.invoke(command, (vault,), {}, idempotency_key="outer-upsert")
    replay = manager.invoke(command, (vault,), {}, idempotency_key="outer-upsert")

    checkpoint = graph_sync.read_checkpoint(vault)
    assert checkpoint is not None
    assert registered == [checkpoint]
    assert graph_sync.registered_checkpoint(vault, state_root=manager.config.state_dir) is None
    assert graph_sync.status(vault)["state"] == "recovery_required"
    assert result["status"] == "committed"
    assert result["graph_sync"] == "failed"
    assert result["graph_sync_code"] == "GRAPH_SYNC_FANOUT_FAILED"
    assert result["graph_sync_checkpoint"] == checkpoint.checkpoint_sha256
    assert replay == result
    assert calls == 1


def test_compact_terminal_retains_typed_platform_graph_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An immediate platform failure handle survives production terminal projection."""
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/platform.md"

    def platform_failure(
        root: Path, checkpoint: graph_sync.GraphSyncCheckpoint, **_kwargs: object
    ):
        return graph_sync.register_failure(
            root,
            checkpoint,
            code="GRAPH_SYNC_PLATFORM_UNAVAILABLE",
            remediation="Restore the graph platform, then run reconcile.",
        )

    monkeypatch.setattr(graph_sync, "register_deferred", platform_failure)

    def leaf(root: Path) -> dict[str, object]:
        batch_atomic_write([PlannedWrite(note, "# platform\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/platform.md", "warnings": []}

    result = LeaseManager(LeaseConfig(state_dir=tmp_path / "state")).invoke(
        SimpleNamespace(name="platform-graph", read_only=False, leaf=leaf),
        (vault,),
        {},
        idempotency_key="platform-graph",
    )

    assert result["status"] == "committed"
    assert result["graph_sync"] == "failed"
    assert result["graph_sync_code"] == "GRAPH_SYNC_PLATFORM_UNAVAILABLE"
    assert result["graph_sync_remediation"] == "Restore the graph platform, then run reconcile."


@pytest.mark.parametrize(
    ("failure_kind", "expected_code", "expected_remediation"),
    [
        (
            "GraphEpochIncoherent",
            "GRAPH_SYNC_LINEAGE_CONFLICT",
            "Reconcile the graph epoch before retrying this mutation.",
        ),
        (
            "GraphRebuildLockUnavailable",
            "GRAPH_SYNC_REBUILD_LOCK_UNAVAILABLE",
            "Retry the same mutation identity after graph rebuild locking recovers, or run reconcile.",
        ),
        (
            "GraphSidecarReplaceUnavailable",
            "GRAPH_SYNC_PLATFORM_SHARING_REFUSED",
            f"Release graph sidecar readers, then {graph_sync._RECONCILE_HINT}",
        ),
        (
            "RuntimeError",
            "GRAPH_SYNC_REBUILD_STOPPED",
            graph_sync._RETRY_OR_RECONCILE,
        ),
    ],
)
def test_lease_manager_preserves_real_wait_path_graph_failure_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_code: str,
    expected_remediation: str,
) -> None:
    """A builder failure keeps its exact handle through terminal persistence and replay."""
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.epistemic_graph import EpistemicGraphIndex
    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/wait-failure.md"
    calls = 0
    failure_type = RuntimeError if failure_kind == "RuntimeError" else getattr(graph_sync, failure_kind)
    sentinel = "private builder path <vault>/Knowledge Base/secret.md"
    failure = failure_type(sentinel)

    def fail_builder(_self: EpistemicGraphIndex) -> dict[str, int]:
        raise failure

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_off_boundary", fail_builder)

    def leaf(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# wait failure\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/wait-failure.md", "warnings": []}

    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    command = SimpleNamespace(name="wait-path-failure", read_only=False, leaf=leaf)
    result = manager.invoke(command, (vault,), {}, idempotency_key=f"wait-{failure_kind}")
    replay = manager.invoke(command, (vault,), {}, idempotency_key=f"wait-{failure_kind}")

    assert result["status"] == "committed"
    # The write no longer waits for the builder (#576/#588): the rebuild runs on
    # its own daemon thread, so at response time the honest outcome is `pending`.
    assert result["graph_sync"] == "pending"
    assert replay == result
    assert calls == 1

    # The three properties this test defends are unchanged; they are now
    # asserted where they are produced rather than after two layers of
    # projection. `committed_graph_failure` reads `.code`/`.remediation`
    # straight off this exception, and the private builder path must not reach
    # a caller through either.
    with pytest.raises(graph_sync.GraphRebuildRegistrationError) as raised:
        graph_sync.await_active_rebuild(vault, state_root=tmp_path / "state")
    assert raised.value.code == expected_code
    assert raised.value.remediation == expected_remediation
    # The *exception* legitimately carries the builder's own message, private
    # path and all -- that is what makes it diagnosable in a log. The no-leak
    # contract is about what reaches a caller, which is the projection below,
    # and it holds because `committed_graph_failure` reads only `.code` and
    # `.remediation` and never the message.
    assert sentinel not in str(
        graph_sync.committed_graph_failure(
            graph_sync.read_checkpoint(vault),
            code=raised.value.code,
            remediation=raised.value.remediation,
        )
    )


def test_lease_manager_preserves_graph_thread_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler-thread start failure is a stable terminal graph handle."""
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    class BrokenThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start refused")

    monkeypatch.setattr(graph_sync.threading, "Thread", BrokenThread)
    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/thread-start.md"
    calls = 0

    def leaf(root: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(note, "# thread start\n")], vault_root=root)
        return {"path": "Knowledge Base/Notes/thread-start.md", "warnings": []}

    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    command = SimpleNamespace(name="thread-start-failure", read_only=False, leaf=leaf)
    result = manager.invoke(command, (vault,), {}, idempotency_key="thread-start-failure")
    replay = manager.invoke(command, (vault,), {}, idempotency_key="thread-start-failure")

    assert result["graph_sync"] == "failed"
    assert result["graph_sync_code"] == "GRAPH_SYNC_START_FAILED"
    assert result["graph_sync_remediation"] == (
        graph_sync._RETRY_OR_RECONCILE
    )
    assert replay == result
    assert calls == 1


def test_live_attempt_wins_over_exact_receipt_until_it_exits(tmp_path: Path) -> None:
    """A receipt is evidence after liveness, never a way to steal a live leaf."""
    from exomem.writer_lease import IdempotencyStore, OpError

    root = tmp_path / "vault"
    digest = "e" * 64
    owner = IdempotencyStore(tmp_path / "idempotency.sqlite")
    observer = IdempotencyStore(tmp_path / "idempotency.sqlite", wait_seconds=0)
    assert owner._claim_or_inspect("live", digest, None) == ("owner", None)
    attempt = owner._attempts["live"]
    _receipt(root, digest=digest, attempt=attempt)

    with pytest.raises(OpError) as pending:
        observer.run(
            "live",
            digest,
            lambda: pytest.fail("live canonical leaf was stolen"),
            commit_evidence=lambda expected, attempt_id, token: _exact_receipt(
                root, expected, attempt_id, token
            ),
        )
    assert pending.value.code == "MUTATION_ACKNOWLEDGEMENT_PENDING"
    owner._release_attempt("live", attempt)


def test_legacy_graph_pending_migrates_only_with_an_exact_coherent_checkpoint(
    tmp_path: Path,
) -> None:
    import pickle

    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    vault = tmp_path / "vault"
    checkpoint = _legacy_graph_pending_checkpoint(vault)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO mutations(key, digest, state, result, updated_at, owner) "
            "VALUES (?, ?, 'graph_pending', ?, 0, 'legacy-owner')",
            (
                "legacy",
                "f" * 64,
                pickle.dumps(
                    {
                        "canonical": "committed",
                        "_graph_sync_checkpoint": checkpoint.as_dict(),
                    }
                ),
            ),
        )

    assert store.run(
        "legacy",
        "f" * 64,
        lambda: pytest.fail("legacy graph_pending replayed the leaf"),
        resume_canonically_committed=lambda result: {
            key: value
            for key, value in {**result, "graph_sync": "completed"}.items()
            if key != "_graph_sync_checkpoint"
        },
        legacy_graph_pending_proof=_legacy_graph_pending_proof(vault),
    ) == {"canonical": "committed", "graph_sync": "completed"}


@pytest.mark.parametrize(
    ("payload", "generation", "mutation_id"),
    [
        ({"canonical": "committed"}, 1, "1" * 24),
        ({"canonical": "committed", "_graph_sync_checkpoint": {}}, 1, "1" * 24),
        (
            {
                "canonical": "committed",
                "_graph_sync_checkpoint": "stale-placeholder",
            },
            2,
            "2" * 24,
        ),
    ],
    ids=("missing-binding", "malformed-binding", "wrong-payload-type"),
)
def test_legacy_graph_pending_without_a_strict_checkpoint_binding_fails_closed(
    tmp_path: Path, payload: dict[str, object], generation: int, mutation_id: str
) -> None:
    import pickle

    from exomem.writer_lease import IdempotencyStore, OpError

    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    vault = tmp_path / "vault"
    _legacy_graph_pending_checkpoint(vault, generation=generation, mutation_id=mutation_id)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO mutations(key, digest, state, result, updated_at, owner) "
            "VALUES (?, ?, 'graph_pending', ?, 0, 'legacy-owner')",
            ("legacy", "f" * 64, pickle.dumps(payload)),
        )

    with pytest.raises(OpError) as error:
        store.run(
            "legacy",
            "f" * 64,
            lambda: pytest.fail("invalid legacy graph_pending replayed the leaf"),
            resume_canonically_committed=lambda result: {**result, "graph_sync": "completed"},
            legacy_graph_pending_proof=_legacy_graph_pending_proof(vault),
        )

    assert error.value.code == "MUTATION_OUTCOME_UNKNOWN"
    _assert_retained_outcome_unknown(store)


@pytest.mark.parametrize("kind", ("stale", "same-generation-wrong-digest"))
def test_legacy_graph_pending_checkpoint_mismatch_never_replays_the_leaf(
    tmp_path: Path, kind: str
) -> None:
    import pickle

    from exomem.writer_lease import IdempotencyStore, OpError

    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    vault = tmp_path / "vault"
    current = _legacy_graph_pending_checkpoint(vault, generation=2, mutation_id="2" * 24)
    if kind == "stale":
        stored = _legacy_graph_pending_checkpoint(
            tmp_path / "stale", generation=1, mutation_id="1" * 24
        )
    else:
        stored = _legacy_graph_pending_checkpoint(
            tmp_path / "conflict", generation=current.generation, mutation_id="3" * 24
        )
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO mutations(key, digest, state, result, updated_at, owner) "
            "VALUES (?, ?, 'graph_pending', ?, 0, 'legacy-owner')",
            (
                "legacy",
                "f" * 64,
                pickle.dumps(
                    {
                        "canonical": "committed",
                        "_graph_sync_checkpoint": stored.as_dict(),
                    }
                ),
            ),
        )

    with pytest.raises(OpError) as error:
        store.run(
            "legacy",
            "f" * 64,
            lambda: pytest.fail("mismatched legacy graph_pending replayed the leaf"),
            resume_canonically_committed=lambda result: {**result, "graph_sync": "completed"},
            legacy_graph_pending_proof=_legacy_graph_pending_proof(vault),
        )

    assert error.value.code == "MUTATION_OUTCOME_UNKNOWN"
    _assert_retained_outcome_unknown(store)


def test_corrupt_legacy_graph_pending_pickle_fails_closed_before_resume(tmp_path: Path) -> None:
    from exomem.writer_lease import IdempotencyStore, OpError

    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    vault = tmp_path / "vault"
    _legacy_graph_pending_checkpoint(vault)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO mutations(key, digest, state, result, updated_at, owner) "
            "VALUES (?, ?, 'graph_pending', ?, 0, 'legacy-owner')",
            ("legacy", "f" * 64, b"not a pickle"),
        )

    with pytest.raises(OpError) as error:
        store.run(
            "legacy",
            "f" * 64,
            lambda: pytest.fail("corrupt legacy graph_pending replayed the leaf"),
            resume_canonically_committed=lambda result: {**result, "graph_sync": "completed"},
            legacy_graph_pending_proof=_legacy_graph_pending_proof(vault),
        )

    assert error.value.code == "MUTATION_OUTCOME_UNKNOWN"
    _assert_retained_outcome_unknown(store)


def test_lease_manager_never_completes_an_unbound_legacy_graph_pending_result(
    tmp_path: Path,
) -> None:
    import pickle
    from types import SimpleNamespace

    from exomem import writer_lease
    from exomem.writer_lease import LeaseConfig, LeaseManager, OpError

    vault = tmp_path / "vault"
    _legacy_graph_pending_checkpoint(vault)
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    command = SimpleNamespace(
        name="legacy-graph-pending",
        read_only=False,
        leaf=lambda _root: pytest.fail("unbound legacy graph_pending replayed the leaf"),
    )
    digest = writer_lease._command_digest(command, {})
    key, _expires_after, _on_replay = writer_lease._effective_idempotency_key(
        manager,
        command=command,
        mutation_subject=vault,
        digest=digest,
        idempotency_key="legacy",
        principal_scope=None,
    )
    assert key is not None
    with manager.idempotency._connect() as conn:
        conn.execute(
            "INSERT INTO mutations(key, digest, state, result, updated_at, owner) "
            "VALUES (?, ?, 'graph_pending', ?, 0, 'legacy-owner')",
            (key, digest, pickle.dumps({"canonical": "committed"})),
        )

    with pytest.raises(OpError) as error:
        manager.invoke(command, (vault,), {}, idempotency_key="legacy")

    assert error.value.code == "MUTATION_OUTCOME_UNKNOWN"
    _assert_retained_outcome_unknown(manager.idempotency)


def test_v1_receipt_is_advisory_and_a_copied_v2_receipt_cannot_authorize_without_local_secret(
    tmp_path: Path,
) -> None:
    """Only the local attempt row can turn a v2 receipt into exact evidence."""
    from exomem import graph_sync
    from exomem.writer_lease import IdempotencyStore, OpError

    root = tmp_path / "vault"
    digest = "a" * 64
    store = IdempotencyStore(tmp_path / "idempotency.sqlite")
    assert store._claim_or_inspect("copied", digest, None) == ("owner", None)
    local_attempt = store._attempts["copied"]
    copied = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest=digest,
        attempt_id=local_attempt.attempt_id,
        commit_token=local_attempt.commit_token,
        canonical_disposition="success",
        terminal_projection={"status": "committed", "mutated": True},
        commit_secret=b"c" * 32,
    )
    graph_sync.write_graph_commit_receipt(root, copied)
    legacy = copied.as_dict()
    legacy["version"] = 1
    legacy.pop("receipt_hmac_sha256")
    legacy.pop("canonical_disposition")
    legacy["commit_point"] = True
    parsed_legacy = graph_sync.GraphCommitReceipt.parse(__import__("json").dumps(legacy))
    assert parsed_legacy is not None
    assert parsed_legacy.verify(
        local_attempt.commit_secret,
        idempotency_key_digest="a" * 64,
        command_digest=digest,
        attempt_id=local_attempt.attempt_id,
        commit_token=local_attempt.commit_token,
    ) is False

    store._release_attempt("copied", local_attempt)
    observer = IdempotencyStore(tmp_path / "idempotency.sqlite", wait_seconds=0)
    with pytest.raises(OpError) as blocked:
        observer.run(
            "copied",
            digest,
            lambda: pytest.fail("copied receipt authorized a leaf replay"),
            commit_evidence=lambda expected, attempt_id, token, secret: (
                graph_sync.read_graph_commit_receipt(root, token).verify(
                    secret,
                    idempotency_key_digest="a" * 64,
                    command_digest=expected,
                    attempt_id=attempt_id,
                    commit_token=token,
                )
            ),
        )
    assert blocked.value.code == "MUTATION_OUTCOME_UNKNOWN"

    empty_store = IdempotencyStore(tmp_path / "fresh-idempotency.sqlite")
    leaf_calls: list[str] = []
    evidence_calls: list[str] = []
    assert empty_store.run(
        "copied",
        digest,
        lambda: leaf_calls.append("ran") or {"canonical": "new"},
        commit_evidence=lambda *_args: evidence_calls.append("consulted") or True,
    ) == {"canonical": "new"}
    assert leaf_calls == ["ran"]
    assert evidence_calls == []


def test_opaque_mutation_subject_never_becomes_a_relative_receipt_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cell/role identity is not filesystem authority for a receipt."""
    from types import SimpleNamespace

    from exomem.writer_lease import (
        LeaseConfig,
        LeaseManager,
        OpError,
        mark_active_mutation_committed,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)
    opaque_cell = "opaque-cell-not-a-vault"
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state", vault_id=opaque_cell))
    command = SimpleNamespace(
        name="opaque-mutation",
        read_only=False,
        leaf=lambda _subject: mark_active_mutation_committed() or {"committed": True},
    )

    with pytest.raises(OpError) as error:
        manager.invoke(command, (opaque_cell,), {}, idempotency_key="opaque-receipt")

    assert error.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    assert not (tmp_path / opaque_cell).exists()


def test_standalone_path_subject_persists_exact_receipt_under_its_vault(tmp_path: Path) -> None:
    """A real standalone vault, unlike a coordination identity, owns its receipt."""
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.vault import PlannedWrite, batch_atomic_write
    from exomem.writer_lease import LeaseConfig, LeaseManager

    vault = tmp_path / "vault"
    note = vault / "Knowledge Base/Notes/receipt.md"

    def leaf(root: Path) -> dict[str, bool]:
        batch_atomic_write([PlannedWrite(note, "# receipt\n")], vault_root=root)
        return {"committed": True}

    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    manager.invoke(
        SimpleNamespace(name="standalone-receipt", read_only=False, leaf=leaf),
        (vault,),
        {},
        idempotency_key="standalone-receipt",
    )

    receipt_dir = vault / "Knowledge Base/.graph-commit-receipts"
    receipts = list(receipt_dir.glob("*.json"))
    assert len(receipts) == 1
    assert graph_sync.GraphCommitReceipt.parse(receipts[0].read_bytes()).version == 2


def test_hosted_cell_uses_its_configured_vault_for_exact_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hosted cell identity resolves receipts through its configured mount."""
    from types import SimpleNamespace

    from exomem import graph_sync
    from exomem.writer_lease import LeaseConfig, LeaseManager, mark_active_mutation_committed

    vault = tmp_path / "hosted-vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state", vault_id="cell:rita"))
    command = SimpleNamespace(
        name="hosted-receipt",
        read_only=False,
        leaf=lambda _cell: mark_active_mutation_committed() or {"committed": True},
    )

    manager.invoke(command, ("cell:rita",), {}, idempotency_key="hosted-receipt")

    receipts = list((vault / "Knowledge Base/.graph-commit-receipts").glob("*.json"))
    assert len(receipts) == 1
    assert graph_sync.GraphCommitReceipt.parse(receipts[0].read_bytes()).version == 2
