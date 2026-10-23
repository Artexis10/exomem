"""One installed receipt journey; invoked only inside a selected fixture worker.

Graph adoption belongs to the managed transport fixture's simulation. This
journey proves receipt custody and persistent lexical/reference integration.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

# These names fix this test procedure, not a content vocabulary or classifier.
PHASES = ("prelude", "capable", "compatibility", "restart", "return")


def _save(root, state):
    path = root / "receipt-journey.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def _identity():
    import exomem
    from exomem import derived_receipts

    return {
        "pid": os.getpid(), "interpreter": sys.executable, "version": version("exomem"),
        "module_path": exomem.__file__, "receipts_module_path": derived_receipts.__file__,
        "isolated": bool(sys.flags.isolated),
    }


def _snapshot(vault, state):
    from exomem import deferred_index

    with closing(sqlite3.connect(deferred_index.store_path(vault))) as connection:
        connection.row_factory = sqlite3.Row
        receipts = {}
        hashes = {}
        for name, arguments in state["batches"].items():
            batch_id = arguments["batch_id"]
            receipts[name] = {
                "batch": dict(connection.execute(
                    "SELECT * FROM derived_batches WHERE batch_id = ?", (batch_id,),
                ).fetchone()),
                "paths": [dict(row) for row in connection.execute(
                    "SELECT * FROM derived_batch_paths WHERE batch_id = ? ORDER BY rel_path",
                    (batch_id,),
                )],
                "components": [dict(row) for row in connection.execute(
                    "SELECT * FROM derived_batch_components WHERE batch_id = ? ORDER BY component",
                    (batch_id,),
                )],
                "visibility": [dict(row) for row in connection.execute(
                    "SELECT * FROM pending_recall_rows WHERE batch_id = ? ORDER BY rel_path",
                    (batch_id,),
                )],
            }
            rel = arguments["paths"][0]["rel_path"]
            hashes[rel] = hashlib.sha256((vault / rel).read_bytes()).hexdigest()
            assert hashes[rel] == arguments["paths"][0]["after_hash"]
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'derived_batch_components'"
        ).fetchone()[0]
    return {"receipts": receipts, "canonical_hashes": hashes,
            "hash_scope": "receipt paths", "component_table_ddl": ddl}


def _prepare(vault, arguments, *, optional=True):
    from exomem import derived_receipts as receipts

    kwargs = dict(arguments)
    kwargs["paths"] = tuple(receipts.DerivedBatchPath(**path) for path in kwargs["paths"])
    kwargs["required_components"] = {
        receipts.DerivedComponent(component) for component in kwargs["required_components"]
        if optional or component != receipts.DerivedComponent.CONTEXT_CATALOGUE.value
    }
    return receipts.prepare_batch(vault, **kwargs)


def _write(vault, state, name, *, optional, now):
    from exomem import derived_receipts as receipts, index_sync, writer_lease
    from exomem import vault as vault_module

    rel = f"Knowledge Base/Notes/{name}.md"
    source = (
        f"---\ntitle: {name.title()}\ntype: insight\nstatus: active\n"
        f"updated: 2026-09-01\n---\n\n# {name.title()}\n\n{name}receiptmarker\n"
    )
    # This fixed scenario owes only the storage protocol projections exercised below.
    required = {
        receipts.DerivedComponent.MEMORY_REFS, receipts.DerivedComponent.RESOLVER,
        receipts.DerivedComponent.LEXSTORE,
    }
    if optional:
        required.add(receipts.DerivedComponent.CONTEXT_CATALOGUE)
    arguments = {
        "batch_id": f"journey-{name}",
        "mutation_attempt_digest": hashlib.sha256(name.encode()).hexdigest(),
        "canonical_generation": "fixture-generation", "checkpoint_id": "fixture-checkpoint",
        "paths": [{"rel_path": rel, "before_hash": None,
                   "after_hash": hashlib.sha256(source.encode()).hexdigest()}],
        "required_components": sorted(required), "now": now,
    }
    with writer_lease.active_manager().consistency_guard(
        vault, operation="fixture_receipt_write", holder_kind="derived-worker",
    ):
        receipt = _prepare(vault, arguments)
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, source, create_only=True)],
            vault_root=vault, post_commit_fanout=False,
        )
        proof = receipts.prove_committed(
            vault, receipt, current_generation="fixture-generation", now=now,
        )
        assert proof.outcome == "ready" and not proof.canonical_replay_authorized
    assert index_sync.publish_corpus_delta(vault, changed=[target])
    from exomem import pending_recall

    assert receipts.publish_pending_visibility(vault, receipt, publisher=pending_recall.publish, now=now)
    state["batches"][name] = arguments
    return receipt


def _publish(vault, status):
    """Publish this scenario's declared projections through their native seams."""
    from exomem import derived_receipts as receipts, find, index_sync, lexstore, memory_refs

    receipt = receipts._load_receipt(vault, status.batch_id)
    paths = [vault / path.rel_path for path in receipt.paths]
    expected = {path.rel_path: path.after_hash for path in receipt.paths}
    if status.component is receipts.DerivedComponent.MEMORY_REFS:
        return all(memory_refs.holds_content_identities(vault, expected).values()) or memory_refs.upsert_after_write(vault, paths)
    if status.component is receipts.DerivedComponent.RESOLVER:
        find.on_resolver_files_changed(vault, list(expected), [])
        return True
    if status.component is receipts.DerivedComponent.LEXSTORE:
        return all(lexstore.holds_content_identities(vault, expected).values()) or lexstore.get_store(vault).upsert_paths(paths)
    if status.component is receipts.DerivedComponent.CONTEXT_CATALOGUE:
        return index_sync.converge_derived_component(vault, receipt, status.component)
    raise AssertionError(f"component outside this fixture's declared obligations: {status.component}")


def _drain(vault, *, now, retire=True):
    from exomem import derived_drain, pending_recall

    return derived_drain.drain_once(
        vault, dispatch=_publish, observe_current_generation=lambda _root: "fixture-generation",
        visibility_publisher=pending_recall.publish,
        retire_visibility=pending_recall.note_components_completed if retire else None,
        limit=16, now=now,
    )


def _optional(snapshot):
    from exomem.derived_receipts import DerivedComponent

    return next(row for row in snapshot["receipts"]["restart"]["components"]
                if row["component"] == DerivedComponent.CONTEXT_CATALOGUE.value)


def _recall(vault, arguments, name):
    from exomem import find, lexstore

    query = f"{name}receiptmarker"
    persistent = lexstore.search_bm25_result(vault, query, 5)
    assert persistent.readiness.complete, asdict(persistent.readiness)
    stored = [path for path, _score in persistent.value]
    visible = [hit.path for hit in find.find(vault, query=query, scope="kb-only", mode="keyword")]
    assert stored == visible == [arguments["paths"][0]["rel_path"]]
    return {"persistent_catalogue": stored, "find": visible}


def run(root):
    from exomem import derived_receipts as receipts, freshness, lexstore, memory_refs, pending_recall

    root = Path(root)
    path = root / "receipt-journey.json"
    state = json.loads(path.read_text()) if path.exists() else {
        "phases": {}, "batches": {}, "clock": time.time(),
    }
    phase = (root / "receipt-journey-phase").read_text().strip()
    if phase in state["phases"]:
        raise RuntimeError(f"receipt phase already attempted; inspect retained evidence: {phase}")
    assert phase == PHASES[len(state["phases"])], phase
    report = {"phase": phase, "identity": _identity(), "state": "started"}
    state["phases"][phase] = report
    _save(root, state)
    vault = root / "vault"
    now = state["clock"] + 10
    try:
        if phase != "prelude":
            assert all(freshness.rebaseline(vault).values())
            # Prove inherited rows before adopting lineage; never rebuild after rollback.
            assert lexstore.runtime_retrieval_catalog_proof(
                vault, require_live_projection=True, schedule_repair=False,
            ) is not None
            lexstore.rebase_inherited_catalog_lineage(vault)
        if phase == "prelude":
            from exomem.init import init_vault

            init_vault(vault)
            assert all(freshness.rebaseline(vault).values())
            memory_refs.ReferenceIndex(vault).rebuild_all()
            assert lexstore.get_store(vault).rebuild_atomic()
            _write(vault, state, "ordinary", optional=False, now=now)
            assert _drain(vault, now=now + 1) == 3
            prelude = _snapshot(vault, state)
            rows = prelude["receipts"]["ordinary"]["components"]
            assert len(rows) == 9
            assert all(row["component"] != receipts.DerivedComponent.CONTEXT_CATALOGUE.value for row in rows)
            report["narrow_schema_proof"] = "actual DDL retained; CHECK rejection covered by source regression"
        elif phase == "capable":
            _write(vault, state, "history", optional=True, now=now)
            assert _drain(vault, now=now + 1) == 4
            restart = _write(vault, state, "restart", optional=True, now=now + 2)
            # Carry current catalogue rows across rollback while receipt work stays pending.
            target = vault / state["batches"]["restart"]["paths"][0]["rel_path"]
            assert lexstore.get_store(vault).upsert_paths([target])
            claimed = receipts.claim_ready_components(
                vault, owner="capable-fixture", limit=2, lease_seconds=3600, now=now + 3,
            )
            assert {row.component for row in claimed} == {
                receipts.DerivedComponent.MEMORY_REFS, receipts.DerivedComponent.CONTEXT_CATALOGUE,
            }
            for row in claimed:
                if row.component is receipts.DerivedComponent.CONTEXT_CATALOGUE:
                    receipts.retry_component(vault, row, failure_code="dispatch_failed", now=now + 3)
                else:
                    receipts.defer_component(vault, row, now=now + 3)
            reclaimed = receipts.claim_ready_components(
                vault, owner="capable-fixture", limit=2, lease_seconds=3600, now=now + 9,
            )
            assert len(reclaimed) == 2
            for row in reclaimed:
                if row.component is receipts.DerivedComponent.MEMORY_REFS:
                    receipts.defer_component(vault, row, now=now + 9)
            snapshot = _snapshot(vault, state)
            state["optional"] = _optional(snapshot)
            assert state["optional"]["state"] == "claimed"
            assert state["optional"]["attempt_count"] == 1
            assert state["optional"]["claim_expires_at"] > now + 10
            assert snapshot["receipts"]["history"]["batch"]["state"] == "completed"
            assert receipts._load_receipt(vault, restart.batch_id).state == "ready"
            now += 10
        elif phase == "compatibility":
            history = _prepare(vault, state["batches"]["history"], optional=False)
            proof = receipts.prove_committed(
                vault, history, current_generation="fixture-generation", now=now,
            )
            assert history.state == "completed" and proof.outcome == "ready"
            assert proof.canonical_replay_authorized is False
            report["historical_proof"] = asdict(proof)

            def unavailable(_root):
                raise RuntimeError("fixture checkpoint observer unavailable")

            assert receipts.recover_prepared_batches(
                vault, observe_current_generation=unavailable,
                visibility_publisher=pending_recall.publish, limit=1, now=now,
            ) == 0
            snapshot = _snapshot(vault, state)
            assert _optional(snapshot) == state["optional"]
            refs = next(row for row in snapshot["receipts"]["restart"]["components"]
                        if row["component"] == receipts.DerivedComponent.MEMORY_REFS.value)
            assert refs["attempt_count"] == 1 and refs["failure_code"] == "handler_unavailable"
            # Reconcile inherited rows while resolver completion still blocks retirement.
            target = vault / state["batches"]["restart"]["paths"][0]["rel_path"]
            assert memory_refs.upsert_after_write(vault, [target])
            assert lexstore.get_store(vault).upsert_paths([target])
            # The final supported commit deliberately loses its retirement callback.
            assert _drain(vault, now=now + 6, retire=False) == 3
            snapshot = _snapshot(vault, state)
            assert _optional(snapshot) == state["optional"]
            assert snapshot["receipts"]["restart"]["batch"]["state"] == "ready"
            assert {row["state"] for row in snapshot["receipts"]["restart"]["visibility"]} == {"live"}
            now += 6
        elif phase == "restart":
            assert receipts.recoverable_batch_count(vault) == 1
            assert _drain(vault, now=now) == 0
            snapshot = _snapshot(vault, state)
            assert _optional(snapshot) == state["optional"]
            assert snapshot["receipts"]["restart"]["batch"]["state"] == "ready"
            assert {row["state"] for row in snapshot["receipts"]["restart"]["visibility"]} == {"retired"}
            report["keyword_paths"] = _recall(vault, state["batches"]["restart"], "restart")
        else:
            now = state["optional"]["claim_expires_at"] + 1
            assert _drain(vault, now=now) == 1
            assert _optional(_snapshot(vault, state))["state"] == "completed"
            report["keyword_paths"] = {}
            for name in ("ordinary", "history", "restart"):
                replay = _prepare(vault, state["batches"][name])
                assert replay.state == "completed"
                report["keyword_paths"][name] = _recall(vault, state["batches"][name], name)
        state["clock"] = now
        report.update(_snapshot(vault, state))
        report.update(state="completed", fixture_clock=now, graph_adoption="simulated")
        _save(root, state)
        return report
    except BaseException as error:
        report.update(state="failed", error_type=type(error).__name__, error=str(error))
        if state["batches"]:
            try:
                report.update(_snapshot(vault, state))
            except Exception as snapshot_error:  # Retain the original failed phase as the primary evidence.
                report["snapshot_error"] = str(snapshot_error)
        _save(root, state)
        raise
