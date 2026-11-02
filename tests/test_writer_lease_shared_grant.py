"""Processes sharing a replica keep its grant until every command has finished."""

from __future__ import annotations

import io
import json
import multiprocessing
import shutil
import time
import traceback
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

import pytest
from test_collection_store_s1_gate import _until, coordinator
from test_collection_store_service import ON, Service, call, offline_create
from test_collection_store_summary import summary_text
from test_collection_store_writer import manifest_path
from test_curation_execution import _create_step, _seed_note
from test_writer_lease import StoreClient

from exomem import curation, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import capability, runtime
from exomem.lease_coordinator import SQLiteLeaseStore


def test_file_writer_close_and_idle_release_preserve_another_managers_command(tmp_path: Path) -> None:
    store = SQLiteLeaseStore(tmp_path / "coordinator.sqlite")
    config = writer_lease.LeaseConfig(
        url="https://lease.example", vault_id="main", replica_id="local-replica",
        state_dir=tmp_path / "lease", idle_release_seconds=1,
    )
    owner = writer_lease.LeaseManager(config, client=StoreClient(store, "local-replica"))
    peer = writer_lease.LeaseManager(config, client=StoreClient(store, "local-replica"))
    original_token = owner.ensure_writer().fencing_token
    try:
        with peer.writer_authority_guard():
            owner._last_activity_monotonic = time.monotonic() - 10
            assert owner._maybe_idle_release(original_token) is False
            owner.close()
            assert owner._fencing_token == original_token
            assert store.status("main")["holder"] == "local-replica"
            peer.validate_fencing_token(original_token)
        owner.close()
        assert store.status("main")["holder"] is None
        assert store.status("main")["fencing_token"] == original_token
    finally:
        peer.close()
        owner.close()


def test_a_preview_borrower_can_finish_its_command_while_close_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from exomem.collection_store import connection
    from exomem.collection_store.preview import preview_store

    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    store = SQLiteLeaseStore(tmp_path / "coordinator.sqlite")
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(
            url="https://lease.example", vault_id="main", replica_id="local-replica",
            state_dir=tmp_path / "lease", mutation_timeout_seconds=0.25,
        ), client=StoreClient(store, "local-replica"),
    )
    token = manager.ensure_writer().fencing_token
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with preview_store(root, handle):
                closing = pool.submit(manager.close, deadline=time.monotonic() + 2)
                _until(lambda: manager._store_handoff)
                with manager.writer_authority_guard():
                    manager.validate_fencing_token(token)
            closing.result(timeout=3)
    assert store.status("main")["holder"] is None


def test_repeated_file_writer_close_needs_no_retired_runtime_state(tmp_path: Path) -> None:
    store = SQLiteLeaseStore(tmp_path / "coordinator.sqlite")
    config = writer_lease.LeaseConfig(
        url="https://lease.example", vault_id="main", replica_id="local-replica",
        state_dir=tmp_path / "lease",
    )
    manager = writer_lease.LeaseManager(config, client=StoreClient(store, "local-replica"))
    manager.ensure_writer()
    manager.close()
    manager.idempotency._owner_lock_handle.close()
    shutil.rmtree(config.state_dir)

    manager.close()

    assert not config.state_dir.exists()
    assert store.status("main")["holder"] is None


def _prepare_in_another_process(channel, database: Path, plan: dict) -> None:
    """Run the real CLI, parking its read-only preparation before its seal."""
    from exomem import __main__ as cli

    original = curation._prepare_step
    manager = writer_lease.get_manager()

    def prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        channel.send(("prepared", manager._fencing_token))
        assert channel.poll(45), "parent did not finish the release attempts"
        assert channel.recv() == "seal"
        return prepared

    curation._prepare_step = prepare
    with coordinator(database):
        try:
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main([
                    "maintain_memory", "--mode", "curation", "--curation-action", "propose",
                    "--plan", json.dumps(plan), "--json",
                ])
            channel.send(("result", code, json.loads(output.getvalue().strip().splitlines()[-1])))
            assert channel.poll(45), "parent did not finish checking the sealed proposal"
            channel.recv()
        except BaseException:  # noqa: BLE001 - report child failures to the parent assertion
            channel.send(("error", traceback.format_exc()))
        finally:
            manager.close()
            channel.close()


@pytest.mark.parametrize("superseded", [False, True], ids=["shared-grant", "external-supersession"])
def test_service_release_cannot_revoke_a_cli_curation_grant(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, superseded: bool,
) -> None:
    """Idle/shutdown release must spare a peer; a real takeover must still fence its seal."""
    # The grant is the subject; `always` would journal the curation instead of committing it in-process.
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "off")
    note_path = _seed_note(vault, "independent-edit", "Keep the original observation.")
    for name, value in {
        "EXOMEM_WRITER_LEASE_URL": "http://localhost",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "shared-vault",
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "shared-replica",
        "EXOMEM_WRITER_LEASE_TOKEN": "lease",
        "EXOMEM_WRITER_LEASE_PREFERRED": "0",
        "EXOMEM_WRITER_LEASE_IDLE_SECONDS": "0",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capability, "RELEASED", ON)
    manifest = tmp_path / "collection.md"
    manifest.write_text(summary_text())
    database = tmp_path / "coordinator.sqlite"
    plan = {"version": 1, "title": "Shared grant proposal",
            "steps": [_create_step("conclusion", "shared-grant-conclusion")]}
    process = None
    with coordinator(database):
        assert offline_create(vault, manifest, manifest_path(), why="new collection") == 0
        service = Service(vault, writer_lease.start_server_lifecycle())
        manager = service.manager
        server = runtime._SERVERS.get(vault)
        channel, child = multiprocessing.get_context("spawn").Pipe()
        try:
            _until(lambda: manager.status().get("collection_store", {}).get("status") == "admitted")
            assert server is not None
            original_token = manager._fencing_token
            assert original_token is not None
            process = multiprocessing.get_context("spawn").Process(
                target=_prepare_in_another_process, args=(child, database, plan),
            )
            process.start()
            child.close()
            assert channel.poll(15), "CLI did not reach preparation"
            assert channel.recv() == ("prepared", original_token)

            manager.config = replace(manager.config, idle_release_seconds=1)
            manager._last_activity_monotonic = time.monotonic() - 10
            assert manager._maybe_idle_release(original_token) is False
            # Preparation holds authority, not the vault's canonical mutation boundary.
            operation = {"kind": "replace_string", "old_string": "original observation",
                         "new_string": "updated observation"}
            preview = call(vault, "edit_memory", path=note_path, why="independent correction",
                           operation={**operation, "validate_only": True})["semantic"]
            call(vault, "edit_memory", path=note_path, why="independent correction",
                 operation={**operation, "transition_token": preview["transition_token"],
                            "relation_disposition": "reviewed_none",
                            "relation_review_hash": preview["relation_review_hash"],
                            "relation_review_reason": "This isolated correction has no supported relation."})
            assert "updated observation" in (vault / note_path).read_text()

            with pytest.raises(OpError, match="COLLECTION_STORE_FLUSH_PENDING"):
                manager.close(deadline=time.monotonic() + 2)
            assert manager._fencing_token == original_token
            assert not manager._store_closed
            assert not manager._collection_store._handed_off
            server.thread.join(10)
            assert not server.thread.is_alive()
            held = manager.client.status()
            assert (held.holder, held.fencing_token) == ("shared-replica", original_token)

            if superseded:
                store = SQLiteLeaseStore(database)
                store.release("shared-vault", "shared-replica", original_token)
                replacement = store.acquire(
                    "shared-vault", "other-replica", 30,
                    collection_store_capability=writer_lease.COLLECTION_STORE_CAPABILITY,
                )
                assert replacement["fencing_token"] == original_token + 1
            channel.send("seal")
            assert channel.poll(15), "CLI did not finish sealing"
            message = channel.recv()
            assert message[0] == "result", message
            _, code, terminal = message
            runs = curation.CurationStore(vault).root
            if superseded:
                assert code == 1, terminal
                assert terminal["error"]["code"] == "WRITER_FENCED"
                assert not runs.exists() or not any(runs.iterdir())
            else:
                assert code == 0, terminal
                proposed = terminal["data"]
                assert curation.status(vault, run_id=proposed["run_id"])["phase"] == "proposed"
                assert len(list(runs.iterdir())) == 1
                assert not (vault / "Knowledge Base/Notes/Insights/shared-grant-conclusion.md").exists()
                assert manager.client.status().fencing_token == original_token
            channel.send("finished")
            process.join(15)
            assert process.exitcode == 0
            if not superseded:
                released = manager.client.status()
                assert (released.holder, released.fencing_token) == (None, original_token)
        finally:
            if process is not None and process.is_alive():
                process.terminate()
                process.join(5)
            channel.close()
            manager._stop.set()
            if server is not None:
                server.thread.join(10)
                assert not server.thread.is_alive()
            manager._collection_store.retire_idle_handle()
            manager._store_closed = True
