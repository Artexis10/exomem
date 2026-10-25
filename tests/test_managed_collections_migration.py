"""A managed upgrade that moves a vault's file collections into the store at the worker handoff.

OpenSpec move-structured-collections-to-sqlite P1b.3, design §10 and §16 A11: the managed path.
Each case drives the supervisor through one upgrade. The launcher's candidate probe, its
migration declaration and the migrator child it spawns are real, on a real interpreter, against
a real coordinator on a loopback port. Only the two worker trees are simulated: the serving
worker is a flag, and the standby is the pre-import its warm runs, called in this process. A
wrapper interpreter turns the candidate's migration release fence on, as the GA release would.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn
from record_fixtures import LEDGER_COLLECTION_PATH, ledger_item
from test_collection_store_migration import (
    MIGRATE,
    MIGRATED,
    OPERATOR,
    PLANNING,
    WORK,
    X3,
    edit_by_hand,
    legacy_vault,
    marker,
    rows,
    staging_directories,
    start_service,
    stop_writer,
    stored_body,
    vault_bytes,
)
from test_collection_store_service import DAILY, DAILY_PATH, DAILY_TEXT, Service, call
from test_collection_store_writer import CID, KEY, OTHER
from test_service_manager import _Ingress, _release_of

from exomem import __main__ as cli
from exomem import (
    record_formats,
    records,
    service_manager,
    service_standby,
    state_migration,
    writer_lease,
)
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import authority, capability, chain, connection, custody, legacy_import
from exomem.governance.principal import library_scope
from exomem.lease_coordinator import create_app

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Managed lifecycle is Linux-only")

#: The coordinator's operator credential; only the operator file in the service config holds it here.
OPERATOR_SECRET = "operator-credential-5c1e"
S1_RELEASED = MIGRATE | {capability.RECORDS_SUMMARY_V1}


@pytest.fixture
def coordinator_url(tmp_path):
    """The shipped coordinator app on a loopback port, reachable from child processes."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(
        create_app(database=tmp_path / "coordinator.sqlite", bearer_token="lease", operator_token=OPERATOR_SECRET),
        log_level="error", ws="none", lifespan="off", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the coordinator did not start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()


@pytest.fixture
def service(tmp_path, monkeypatch, coordinator_url):
    """A managed service's environment: its vault, state root and writer lease, and no operator credential."""
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    for name, value in {
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"), "EXOMEM_WRITER_LEASE_URL": coordinator_url,
        "EXOMEM_WRITER_LEASE_VAULT_ID": "managed", "EXOMEM_WRITER_LEASE_REPLICA_ID": "service",
        "EXOMEM_WRITER_LEASE_TOKEN": "lease", "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-service"),
        "EXOMEM_WRITER_LEASE_PREFERRED": "1",
    }.items():
        monkeypatch.setenv(name, value)
    # This process stands in for the standby, which runs the candidate release.
    monkeypatch.setattr(capability, "RELEASED", MIGRATE)
    # Generous, so a loaded host never turns a migration into an abandon; the budget case shrinks it.
    monkeypatch.setattr(service_manager, "MIGRATION_SECONDS", 300.0)
    config = tmp_path / "config"
    config.mkdir()
    (config / "service.env").write_text("EXOMEM_PRELOAD_MODELS=0\n")
    with library_scope():
        root = legacy_vault(tmp_path / "vault")
        state_migration.require_vault_state_ready(root)
        yield root
    stop_writer()


def operator_file(tmp_path, mode=0o600):
    """The protected operator file beside the service environment, holding the operator credential."""
    path = tmp_path / "config" / service_manager.OPERATOR_ENV_FILE
    path.write_text(f"{OPERATOR}={OPERATOR_SECRET}\n")
    path.chmod(mode)
    return path


def candidate(tmp_path, released, fault="", name=""):
    """This interpreter, posing as a release whose fence enables ``released``.

    The launcher runs the candidate with ``-I``, so the release rides in a wrapper, not on PYTHONPATH.
    ``fault`` is code the candidate runs first, in every child: the probe and the migrator.
    """
    prelude = (
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
        "import exomem.collection_store.capability as capability\n"
        "from pathlib import Path\n"
        f"assert Path(capability.__file__).resolve().is_relative_to({str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
        f"capability.RELEASED = frozenset({sorted(released)!r})\n"
        + fault +
        "arguments = [value for value in sys.argv[1:] if value != '-I']\n"
        "if arguments[0] == '-c':\n"
        "    sys.argv = ['-c', *arguments[2:]]\n"
        "    exec(compile(arguments[1], '<candidate>', 'exec'), {'__name__': '__main__'})\n"
        "else:\n"
        "    sys.argv = [arguments[1], *arguments[2:]]\n"
        "    runpy.run_module(arguments[1], run_name='__main__', alter_sys=True)\n"
    )
    wrapper = tmp_path / f"python-{'-'.join(sorted(released)) or 'dark'}{name}"
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -c {shlex.quote(prelude)} "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


class Launcher(service_manager.WorkerRuntime):
    """The launcher's runtime in a test process that it shares with other tests.

    A launcher proves that every child it owns has exited. This process may already have
    children that an earlier test started, such as a multiprocessing resource tracker,
    which are not this runtime's, so its proofs leave out exactly those.
    """

    def __init__(self, tmp_path):
        super().__init__(tmp_path / "worker.sock", host="127.0.0.1", port=1,
                         environment_files=[tmp_path / "config" / "service.env"])
        self.unrelated = frozenset(service_manager._descendants())

    def _standby_session(self):
        return super()._standby_session() | self.unrelated


class Handoff(Launcher):
    """The launcher's runtime, with both worker trees simulated and the migrator child real.

    ``served`` is what the serving worker does after the standby pre-imported, before its stop.
    """

    def __init__(self, tmp_path, root, served=None):
        super().__init__(tmp_path)
        self.root = root
        self.served = served
        self.serving = True

    @property
    def pid(self):
        return 100 if self.serving else super().pid

    async def stop(self, timeout):
        self.serving = False
        await super().stop(timeout)

    async def start_standby(self, target, timeout):
        # What the standby process runs as the last step of its warm, beside the serving worker,
        # in the environment the launcher gives it.
        with pytest.MonkeyPatch.context() as standby:
            for name, value in self.standby_environment(target).items():
                standby.setenv(name, value)
            service_standby.prepare_collections_migration(self.root)
        if self.served is not None:
            self.served()
        return "standby"

    async def discard_standby(self, timeout=10):
        pass

    async def promote_standby(self, *, migrated, timeout):
        self.serving = True
        return "promoted", {"migrated": migrated}


def upgrade(tmp_path, runtime, python):
    supervisor = service_manager.Supervisor(
        service_manager.private_directory(tmp_path / "managed"),
        initial_target={"python": sys.executable, "version": _release_of(sys.executable)},
        ingress=_Ingress(), runtime=runtime, identity={"unit": "sample.service"}, transition_timeout=600)
    return asyncio.run(supervisor.upgrade({"python": python, "version": _release_of(python)}))


def pre_import(root, monkeypatch):
    """The standby's pre-import, in a warm whose launcher says the handoff can run the collections step."""
    monkeypatch.setenv(service_manager.STANDBY_COLLECTIONS_ENV, "1")
    service_standby.prepare_collections_migration(root)
    monkeypatch.delenv(service_manager.STANDBY_COLLECTIONS_ENV)


def migrator_report(capsys):
    """What the migrator child printed, which the supervisor passes through to the service journal."""
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith('{"success"')]
    return json.loads(lines[-1])["data"]["collections"]


def append_to_work(root, title):
    """A file-mode write the serving worker makes, through the writer lease."""
    manifest = collections.load_manifest(root, root / WORK)
    written = records.append_record(root, WORK, item={"title": title}, why="a write while the standby pre-imports",
                                    expected_container_hash=record_formats.load_adapter(root, manifest).read().snapshot)
    stop_writer()
    return written


def fence():
    return writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env()).collection_store_fence()


def lease():
    return writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env()).status()


def test_a_managed_upgrade_routes_every_file_collection_to_the_store(service, tmp_path):
    """A managed upgrade to a release with the migration that leaves the vault in files, or routes it
    without the coordinator's fence, the compatibility descriptor or the rows its files hold."""
    root = service
    expected = {cid: rows(root, path) for cid, path in MIGRATED.items()}
    before = vault_bytes(root)
    operator_file(tmp_path)
    result = upgrade(tmp_path, Handoff(tmp_path, root), candidate(tmp_path, MIGRATE))
    assert result["ok"], result
    assert result["handoff"]["migration"] == {
        "state": "ran", "reason": "collections step requested", "collections": {"state": "ran", "outcome": "migrated"}}
    # The step rewrote no source a graph snapshot proves, so promotion does not re-run that proof.
    assert result["handoff"]["promotion"] == {"migrated": False}
    routed = marker(root)
    assert {entry["collection_id"]: entry["manifest_path"] for entry in routed["collections"]} == MIGRATED
    found = fence()
    assert (found.enrolled, found.generation, found.store_id) == (True, 1, routed["store_id"])
    assert "collections-store-v1" in state_migration.recorded_descriptor_ids(root)
    assert vault_bytes(root, outside="Knowledge Base/_Collections") == before
    assert staging_directories(root) == []
    serving = start_service(root)
    try:
        assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == expected
    finally:
        serving.stop()


def test_a_release_that_keeps_the_migration_dark_upgrades_as_before(service, tmp_path, monkeypatch):
    """A public release, whose fence keeps the migration off, migrating a vault at an ordinary
    upgrade because a staging directory and the operator credential happen to be there."""
    root = service
    operator_file(tmp_path)
    pre_import(root, monkeypatch)
    # The standby runs the candidate, a public release: summary collections on, the migration off.
    public = frozenset({capability.RECORDS_SUMMARY_V1})
    monkeypatch.setattr(capability, "RELEASED", public)
    result = upgrade(tmp_path, Handoff(tmp_path, root), candidate(tmp_path, public))
    assert result["ok"], result
    assert result["handoff"]["migration"] == {"state": "skipped", "reason": "declared_none"}
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert len(staging_directories(root)) == 1


@pytest.mark.parametrize("credential", ["missing", "readable-by-group", "a-fifo"])
def test_without_a_usable_operator_credential_the_upgrade_completes_in_files(service, tmp_path, credential):
    """An upgrade that fails, hangs, or migrates with a credential another local user could read or
    replace, when the operator file is absent, not private to its owner, or a FIFO no one writes."""
    root = service
    if credential == "readable-by-group":
        operator_file(tmp_path, mode=0o640)
    elif credential == "a-fifo":
        os.mkfifo(tmp_path / "config" / service_manager.OPERATOR_ENV_FILE, 0o600)
    result = upgrade(tmp_path, Handoff(tmp_path, root), candidate(tmp_path, MIGRATE))
    assert result["ok"], result
    reason = {"missing": "operator credential file missing",
              "readable-by-group": "operator credential file is not private to its owner",
              "a-fifo": "operator credential file is not a regular file"}[credential]
    assert result["handoff"]["migration"] == {
        "state": "skipped", "reason": "declared_none", "collections": {"state": "skipped", "reason": reason}}
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 0)
    # A standby that no handoff could finish pre-imports nothing.
    assert staging_directories(root) == []


def test_the_standby_warm_pre_imports_the_collections_it_reports(service, monkeypatch):
    """A standby warm that never pre-imports, so no handoff ever has staging to migrate, or that
    reports its collections component ready before the pre-import ran."""
    monkeypatch.setenv(service_manager.STANDBY_COLLECTIONS_ENV, "1")
    service_standby.reset_for_tests()
    try:
        service_standby.enter_standby()
        assert service_standby.cutover_components()["collections_migration"] == "waiting"
        service_standby.warm(service)
        assert service_standby.cutover_components()["collections_migration"] == "ready"
        assert len(staging_directories(service)) == 1
    finally:
        service_standby.reset_for_tests()


def test_a_standby_whose_pre_import_failed_still_reaches_cutover_readiness(service, monkeypatch):
    """A standby whose collections pre-import failed holding its cutover component in waiting until
    the warm budget discards it, which refuses every upgrade of the vault while the failure lasts."""
    service_standby.reset_for_tests()
    try:
        assert service_standby.cutover_components()["collections_migration"] == "waiting"
        monkeypatch.setenv(service_manager.STANDBY_COLLECTIONS_ENV, "1")
        # Nothing listens there: the coordinator is down while the standby warms.
        monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "http://127.0.0.1:9")
        service_standby.prepare_collections_migration(service)
        assert service_standby.cutover_components()["collections_migration"] == "ready"
        assert staging_directories(service) == []
    finally:
        service_standby.reset_for_tests()


def test_a_write_while_the_standby_pre_imports_reaches_the_store(service, tmp_path, capsys):
    """Carrying the standby's pre-import forward although the serving worker wrote one of its
    collections afterwards, which loses that write from the store that becomes canonical."""
    root = service
    operator_file(tmp_path)
    written = []
    runtime = Handoff(tmp_path, root, served=lambda: written.append(append_to_work(root, "Written after pre-import")))
    capsys.readouterr()
    result = upgrade(tmp_path, runtime, candidate(tmp_path, MIGRATE))
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {"state": "ran", "outcome": "migrated"}
    report = migrator_report(capsys)
    # Only the written collection changed: the activity-log line the write added leaves the others' basis.
    assert (report["rebuilt_because"], report["changed"]) == ("basis_changed", [WORK])
    serving = start_service(root)
    try:
        assert written[0]["item_key"] in rows(root, WORK)
    finally:
        serving.stop()


def test_a_migration_that_cannot_finish_in_its_budget_abandons_and_the_release_completes(
        service, tmp_path, monkeypatch, capsys):
    """A migrator over its budget that fails the upgrade into recovery, or publishes a store, a marker
    or a fence cut it cannot finish, instead of leaving the vault in files for the next handoff."""
    root = service
    operator_file(tmp_path)
    # The migrator's own deadline is what the supervisor leaves after the finishing margin.
    monkeypatch.setattr(service_manager, "MIGRATION_FINISH_SECONDS", service_manager.MIGRATION_SECONDS - 0.01)
    written = []
    runtime = Handoff(tmp_path, root, served=lambda: written.append(append_to_work(root, "Written after pre-import")))
    result = upgrade(tmp_path, runtime, candidate(tmp_path, MIGRATE))
    assert result["ok"], result
    assert result["phase"] == "ready"
    assert result["handoff"]["migration"]["collections"] == {
        "state": "ran", "outcome": "abandoned", "code": "TimeoutError"}
    assert migrator_report(capsys)["status"] == "abandoned"
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 0)
    assert written[0]["item_key"] in rows(root, WORK)


def test_a_file_changed_after_the_refresh_under_lease_abandons_before_seal(service, tmp_path, capsys):
    """An editor outside the lease changes the fresh import before its final comparison; publication must stop."""
    root = service
    operator_file(tmp_path)
    fault = (
        "from pathlib import Path\n"
        "from exomem.collection_store import migration\n"
        "stage = migration._stage\n"
        "def refresh_then_edit(root, *args, **kwargs):\n"
        "    result = stage(root, *args, **kwargs)\n"
        "    if result[1] is not None:\n"
        f"        path = Path(root) / {str(Path(WORK).parent / 'Items/Original.md')!r}\n"
        "        path.write_text(path.read_text() + '\\nExternal edit after refresh\\n')\n"
        "    return result\n"
        "migration._stage = refresh_then_edit\n"
    )
    result = upgrade(tmp_path, Handoff(tmp_path, root, served=lambda: append_to_work(root, "After pre-import")),
                     candidate(tmp_path, MIGRATE, fault))
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {"state": "ran", "outcome": "abandoned"}
    report = migrator_report(capsys)
    assert (report["abandoned_because"], report["changed"]) == ("basis_changed", [WORK])
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 0)
    assert (root / WORK).parent.joinpath("Items/Original.md").read_text().count("External edit after refresh") == 2


@pytest.fixture
def s1(service, tmp_path, monkeypatch):
    """The legacy vault after S1 enrolled a summary collection, with a row the service wrote."""
    monkeypatch.setattr(capability, "RELEASED", S1_RELEASED)
    root = service
    (tmp_path / "daily.md").write_text(DAILY_TEXT)
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, OPERATOR_SECRET)
        assert cli._collections_main(["create", "--vault", str(root), "--manifest-path", DAILY_PATH,
                                      "--manifest-file", str(tmp_path / "daily.md"), "--why", "summary"]) == 0
    summary_write(root, "Summary row", KEY)
    return root


def summary_write(root, title, key):
    """A store write the service makes, with its own start and orderly stop."""
    serving = start_service(root)
    try:
        assert call(root, "record_memory", action="append", collection=DAILY, item={"title": title},
                    item_key=key, why="a summary row")["outcome"] == "committed"
    finally:
        serving.stop()


def live_head(root):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return chain.verify_store_chain(reader)


def test_a_vault_with_a_live_s1_store_migrates_by_copy_then_swap(s1, tmp_path):
    """Swapping a store over the vault's live S1 store that drops its summary collection, forks its
    head, moves its fence, or loses what the serving worker recorded after the standby copied it."""
    root = s1
    head = live_head(root)
    enrolled = fence()
    operator_file(tmp_path)

    def record_a_view():
        # What the serving worker's projector writes when it checks a view: bookkeeping that
        # moves no head, which the copy the standby took before it does not hold.
        with closing(sqlite3.connect(connection.store_path(root))) as live, live:
            live.execute("UPDATE projection_state SET stat_identity=? WHERE path=?", ("checked-after-copy", DAILY_PATH))

    result = upgrade(tmp_path, Handoff(tmp_path, root, served=record_a_view), candidate(tmp_path, S1_RELEASED))
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {"state": "ran", "outcome": "migrated"}
    routed = marker(root)
    assert {entry["collection_id"] for entry in routed["collections"]} == {DAILY, *MIGRATED}
    assert (fence().generation, fence().store_id) == (enrolled.generation, enrolled.store_id) == (
        routed["collection_store_fence"]["generation"], routed["store_id"])
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert chain.head_relation(reader, *head) == "ancestor"
        assert reader.execute("SELECT stat_identity FROM projection_state WHERE path=?", (DAILY_PATH,)).fetchone() == (
            "checked-after-copy",)
    serving = start_service(root)
    try:
        assert [row["title"] for row in call(root, "record_memory", action="query", collection=DAILY)["rows"]] == [
            "Summary row"]
    finally:
        serving.stop()


def test_changed_only_handoff_preserves_proved_import_identity_and_live_bookkeeping(s1, tmp_path, capsys):
    """Refreshing the first import must not mint later imports again or lose their held governance."""
    root = s1
    assert Path(connection.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1] / "src")
    for path in (PLANNING, X3):
        shutil.rmtree((root / path).parent)
    head = live_head(root)
    operator_file(tmp_path)
    before = {}

    def write_after_pre_import():
        with closing(connection.open_reader(staging_directories(root)[0] / "staging.sqlite")) as donor:
            before["history"] = donor.execute(
                "SELECT receipt_json,legacy_event_json,transition_id,event_hash FROM txns "
                "WHERE collection_id=? ORDER BY txn_id", (CID,)).fetchall()
            before["row_id"] = donor.execute("SELECT row_id FROM items WHERE collection_id=?", (CID,)).fetchone()
            before["held"] = donor.execute(
                "SELECT held_id,held_bytes,governance_json,governance_hash FROM held_candidates "
                "WHERE collection_id=?", (CID,)).fetchall()
            basis = json.loads(donor.execute("SELECT value FROM store_meta WHERE key='migration_basis'").fetchone()[0])
            before["basis"] = basis["collections"][CID]
        manifest = collections.load_manifest(root, root / LEDGER_COLLECTION_PATH)
        records.append_record(root, LEDGER_COLLECTION_PATH, item=ledger_item(slug="after-pre-import"),
                              why="a write after pre-import", expected_container_hash=
                              record_formats.load_adapter(root, manifest).read().snapshot)
        stop_writer()
        with closing(sqlite3.connect(connection.store_path(root))) as live, live:
            live.execute("UPDATE projection_state SET stat_identity=? WHERE path=?", ("checked-after-copy", DAILY_PATH))
        before["files"] = vault_bytes(root, outside="Knowledge Base/_Collections")

    result = upgrade(tmp_path, Handoff(tmp_path, root, served=write_after_pre_import), candidate(tmp_path, S1_RELEASED))
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {"state": "ran", "outcome": "migrated"}
    with closing(connection.open_reader(connection.store_path(root))) as live:
        assert live.execute("SELECT receipt_json,legacy_event_json,transition_id,event_hash FROM txns "
                            "WHERE collection_id=? ORDER BY txn_id", (CID,)).fetchall() == before["history"]
        assert live.execute("SELECT row_id FROM items WHERE collection_id=?", (CID,)).fetchone() != before["row_id"]
        assert live.execute("SELECT held_id,held_bytes,governance_json,governance_hash FROM held_candidates "
                            "WHERE collection_id=?", (CID,)).fetchall() == before["held"]
        assert live.execute("SELECT stat_identity FROM projection_state WHERE path=?", (DAILY_PATH,)).fetchone() == (
            "checked-after-copy",)
        assert chain.head_relation(live, *head) == "ancestor"
        chain.verify_store_chain(live)
        assert live.execute("PRAGMA foreign_key_check").fetchall() == []
        (checkpoint,) = live.execute("SELECT txn_id FROM txns WHERE collection_id=? AND legacy_event_json IS NULL",
                                     (CID,)).fetchone()
        entry = {**before["basis"], "txn_id": checkpoint}
        retained = legacy_import.retained_material(live, entry, check=lambda: None)
        assert retained is not None and retained.digest() == entry["material_digest"]
    assert vault_bytes(root, outside="Knowledge Base/_Collections") == before["files"]
    serving = start_service(root)
    try:
        assert any(row["slug"] == "after-pre-import" for row in rows(root, LEDGER_COLLECTION_PATH).values())
    finally:
        serving.stop()
    capsys.readouterr()


def test_a_live_s1_head_that_moved_after_the_copy_abandons_the_handoff(s1, tmp_path, capsys):
    """Swapping in the standby's copy of the live S1 store although the serving worker committed to
    it afterwards, which loses that commit; the managed path abandons and keeps the store as it is."""
    root = s1
    marked = authority.read_marker(root)
    operator_file(tmp_path)
    runtime = Handoff(tmp_path, root, served=lambda: summary_write(root, "After the copy", OTHER))
    capsys.readouterr()
    result = upgrade(tmp_path, runtime, candidate(tmp_path, S1_RELEASED))
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {"state": "ran", "outcome": "abandoned"}
    assert migrator_report(capsys)["abandoned_because"] == "live_store_changed"
    assert authority.read_marker(root) == marked
    serving = start_service(root)
    try:
        assert sorted(row["title"] for row in call(root, "record_memory", action="query", collection=DAILY)["rows"]) == [
            "After the copy", "Summary row"]
    finally:
        serving.stop()


def test_the_operator_credential_reaches_only_the_migrator(service, tmp_path, monkeypatch):
    """The serving worker or the standby holding the operator credential, which lets any of its
    code paths cut or retire the coordinator's store fence; only the migrator child may hold it."""
    root = service
    operator_file(tmp_path)
    dumps = tmp_path / "environments"
    dumps.mkdir()
    released = candidate(tmp_path, MIGRATE)
    # Each child records its arguments and environment; the probe still runs the candidate.
    stub = tmp_path / "python-stub"
    stub.write_text(
        "#!/bin/sh\n"
        f'if [ "$2" = "-c" ]; then exec {shlex.quote(released)} "$@"; fi\n'
        'case " $* " in *" --standby "*) kind=standby ;; *" maintain "*) kind=migrator ;; *) kind=worker ;; esac\n'
        f'{{ echo "$*"; env; }} > {shlex.quote(str(dumps))}/"$kind"\n'
        'if [ "$kind" = worker ]; then exec sleep 60; fi\n'
        'exit 0\n')
    stub.chmod(0o755)
    pre_import(root, monkeypatch)
    runtime = Launcher(tmp_path)

    async def scenario():
        target = await runtime.inspect({"python": str(stub), "version": _release_of(str(stub))})
        with pytest.raises(TimeoutError):
            await runtime.start(target, timeout=1)
        with pytest.raises(RuntimeError, match="exited before cutover readiness"):
            await runtime.start_standby(target, timeout=10)
        await runtime.discard_standby()
        await runtime.stop(timeout=10)
        assert runtime.migration_required(target) == (False, "declared_none")
        assert runtime.collections_migration == {"state": "requested"}
        await runtime.migrate(target, timeout=30)

    asyncio.run(scenario())
    seen = {kind: (dumps / kind).read_text() for kind in ("worker", "standby", "migrator")}
    assert "--migrate-collections" in seen["migrator"].splitlines()[0]
    assert f"{OPERATOR}={OPERATOR_SECRET}" in seen["migrator"].splitlines()[1:]
    for kind in ("worker", "standby"):
        assert OPERATOR not in seen[kind]
    # The standby learns only that this handoff could run the collections step.
    assert f"{service_manager.STANDBY_COLLECTIONS_ENV}=1" in seen["standby"].splitlines()[1:]
    # Never on a command line, which any local user can read.
    assert all(OPERATOR_SECRET not in text.splitlines()[0] for text in seen.values())


def test_a_standby_whose_handoff_cannot_run_the_step_removes_the_staging_it_would_leave(service, monkeypatch):
    """A standby that pre-imports, a whole live-store copy and import per warm, for a handoff that cannot
    run the collections step, or leaves an earlier warm's staging for a later handoff to migrate."""
    root = service
    pre_import(root, monkeypatch)
    assert len(staging_directories(root)) == 1
    monkeypatch.setenv(service_manager.STANDBY_COLLECTIONS_ENV, "0")
    service_standby.reset_for_tests()
    try:
        service_standby.prepare_collections_migration(root)
        assert service_standby.cutover_components()["collections_migration"] == "ready"
    finally:
        service_standby.reset_for_tests()
    assert staging_directories(root) == []


def test_a_target_whose_migration_probe_raises_is_recorded_apart_from_one_that_declares_none(service, tmp_path):
    """A target whose own declaration raised recorded exactly as a release without the migration,
    which hides a broken candidate from the operator reading the handoff record."""
    root = service
    operator_file(tmp_path)
    broken = candidate(tmp_path, MIGRATE, name="-broken-probe", fault=(
        "def offline_migrations():\n"
        "    raise RuntimeError('a broken declaration')\n"
        "capability.offline_migrations = offline_migrations\n"))
    result = upgrade(tmp_path, Handoff(tmp_path, root), broken)
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {
        "state": "skipped", "reason": "target migration probe failed"}
    assert authority.read_marker(root) is None


def test_a_slow_comparison_with_the_staging_abandons_inside_the_budget(service, tmp_path, monkeypatch, capsys):
    """A migrator whose first comparison with the staging runs past its own deadline, so the
    supervisor's wait ends first and the upgrade fails into recovery with nothing published."""
    root = service
    operator_file(tmp_path)
    monkeypatch.setattr(service_manager, "MIGRATION_SECONDS", 12.0)
    monkeypatch.setattr(service_manager, "MIGRATION_FINISH_SECONDS", 6.0)
    # Every capture takes 3.5 s and checks its deadline as a real one does, before and after.
    slow = candidate(tmp_path, MIGRATE, name="-slow-capture", fault=(
        "import time\n"
        "from exomem.collection_store import legacy_import\n"
        "captured = legacy_import.capture_legacy_collection\n"
        "def capture(root, path, *, audit, **options):\n"
        "    audit._check()\n"
        "    time.sleep(3.5)\n"
        "    audit._check()\n"
        "    return captured(root, path, audit=audit, **options)\n"
        "legacy_import.capture_legacy_collection = capture\n"))
    capsys.readouterr()
    result = upgrade(tmp_path, Handoff(tmp_path, root), slow)
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {
        "state": "ran", "outcome": "abandoned", "code": "TimeoutError"}
    assert migrator_report(capsys)["abandoned_because"] == "deadline"
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 0)


def test_a_lease_handback_that_raises_after_admission_reports_the_migration(service, tmp_path):
    """A migrator that reports a vault the marker now routes to the store as abandoned, because the
    lease handback raised after the marker was admitted; the record then misstates what serves."""
    root = service
    operator_file(tmp_path)
    handback = candidate(tmp_path, MIGRATE, name="-failed-handback", fault=(
        "from exomem import writer_lease\n"
        "from exomem.cli_ops import OpError\n"
        "closed = writer_lease.LeaseManager.close\n"
        "def close(self):\n"
        "    closed(self)\n"
        "    raise OpError('WRITER_LEASE_UNAVAILABLE', 'the coordinator did not answer the handback')\n"
        "writer_lease.LeaseManager.close = close\n"))
    result = upgrade(tmp_path, Handoff(tmp_path, root), handback)
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {
        "state": "ran", "outcome": "migrated", "code": "WRITER_LEASE_UNAVAILABLE"}
    routed = marker(root)
    assert {entry["collection_id"]: entry["manifest_path"] for entry in routed["collections"]} == MIGRATED
    assert (fence().enrolled, fence().generation) == (True, 1)


@pytest.mark.parametrize("boundary", ["before_aside", "after_aside"])
def test_a_swap_failure_keeps_a_safe_store_serving_or_stops_the_handoff(s1, tmp_path, boundary):
    """A failure before moving the live store can abandon; a failure after moving it must stop."""
    root = s1
    head, original_marker = live_head(root), authority.read_marker(root)
    operator_file(tmp_path)
    if boundary == "before_aside":
        fault = (
            "import os, types\n"
            "from exomem.collection_store import takeover\n"
            "replaced = takeover.replace_live\n"
            "def failing(descriptor):\n"
            "    raise OSError(5, 'an I/O error before moving the live store')\n"
            "def replace_live(*arguments, **options):\n"
            "    takeover.os = types.SimpleNamespace(**{**vars(os), 'fsync': failing})\n"
            "    try:\n"
            "        return replaced(*arguments, **options)\n"
            "    finally:\n"
            "        takeover.os = os\n"
            "takeover.replace_live = replace_live\n")
    else:
        fault = (
            "from exomem.collection_store import takeover\n"
            "replaced = takeover.replace_live\n"
            "def replace_live(session, *arguments, **options):\n"
            "    filesystem = session.state_fs\n"
            "    class FailingInstall:\n"
            "        def __getattr__(self, name):\n"
            "            return getattr(filesystem, name)\n"
            "        def rename(self, source, parent, leaf, **kwargs):\n"
            "            if leaf == session.path.name:\n"
            "                raise OSError(5, 'an I/O error after moving the live store')\n"
            "            return filesystem.rename(source, parent, leaf, **kwargs)\n"
            "    session.state_fs = FailingInstall()\n"
            "    try:\n"
            "        return replaced(session, *arguments, **options)\n"
            "    finally:\n"
            "        session.state_fs = filesystem\n"
            "takeover.replace_live = replace_live\n")
    swap = candidate(tmp_path, S1_RELEASED, name="-failed-swap", fault=fault)
    result = upgrade(tmp_path, Handoff(tmp_path, root), swap)

    if boundary == "before_aside":
        assert result["ok"], result
        assert result["handoff"]["migration"]["collections"] == {
            "state": "ran", "outcome": "abandoned", "code": "OSError"}
        assert live_head(root) == head and authority.read_marker(root) == original_marker
    else:
        assert not result["ok"]
        assert result["error"] == "upgrade failed after shutdown; worker is stopped; resume required"
        assert result["handoff"]["migration"]["collections"] == {
            "state": "ran", "outcome": "unsafe", "code": "OSError"}
        assert not connection.store_path(root).exists()


def test_a_failure_after_publication_that_persists_serves_the_vault_and_the_next_handoff_finishes_it(
        service, tmp_path, monkeypatch, capsys):
    """A failure after the store was published that a rerun does not clear and that stops the whole
    service until an operator resumes, a writer inside the lease that changes the files the published
    store imported, or a next handoff that admits that store although a file changed outside the
    lease meanwhile, losing the change."""
    root = service
    operator_file(tmp_path)
    # Short, so the lease the failed migrator keeps expires within the test.
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TTL", "3")
    failing = candidate(tmp_path, MIGRATE, name="-failed-marker", fault=(
        "from exomem.collection_store import admission\n"
        "def install_marker(*arguments, **options):\n"
        "    raise OSError(5, 'an I/O error before the marker was written')\n"
        "admission._install_marker = install_marker\n"))
    result = upgrade(tmp_path, Handoff(tmp_path, root), failing)
    assert result["ok"], result
    assert result["phase"] == "ready"
    # The supervisor's one rerun met the same fault.
    assert result["handoff"]["migration"]["collections"] == {
        "state": "ran", "outcome": "pending", "code": "OSError",
        "first_attempt": {"outcome": "pending", "code": "OSError"}}
    assert authority.read_marker(root) is None and (fence().enrolled, fence().generation) == (True, 1)
    # The migrator that raised keeps its lease until it expires.
    held = lease()
    assert held.holder.endswith(writer_lease.OFFLINE_OWNER_SUFFIX) and held.expires_at > time.time()
    while (lease().expires_at or 0) > time.time():
        time.sleep(0.2)
    # Then the cut fence refuses every writer inside the lease: the vault reads, and no write
    # can change the files the published store imported.
    serving = Service(root, writer_lease.start_server_lifecycle())
    try:
        with pytest.raises(OpError) as refused:
            call(root, "record_memory", action="append", collection=CID, item={"title": "While pending"},
                 why="a write while the store is pending")
        assert refused.value.code == "COLLECTION_STORE_UNAVAILABLE"
        with pytest.raises(OpError, match="COLLECTION_STORE_UNAVAILABLE"):
            rows(root, WORK)
    finally:
        serving.stop()
    edit_by_hand(root)
    # The published store, not whatever staging its cutover left behind, asks the next handoff to finish it.
    for directory in staging_directories(root):
        shutil.rmtree(directory)
    capsys.readouterr()
    result = upgrade(tmp_path, Handoff(tmp_path, root), candidate(tmp_path, MIGRATE))
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {"state": "ran", "outcome": "migrated"}
    report = migrator_report(capsys)
    assert (report["rebuilt_because"], report["changed"]) == ("basis_changed", [WORK])
    routed = marker(root)
    assert {entry["collection_id"] for entry in routed["collections"]} == set(MIGRATED)
    assert (fence().generation, fence().store_id) == (2, routed["store_id"])
    assert stored_body(root, KEY) == "Edited by hand\n"


def test_a_failure_after_publication_that_a_rerun_clears_finishes_in_the_same_handoff(service, tmp_path, monkeypatch):
    """A failure after the store was published that a second attempt would clear, left for the next
    upgrade, so the vault stays read-only until then although nothing stands in the way."""
    root = service
    operator_file(tmp_path)
    # Short, so the lease the failed migrator keeps expires within the test.
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TTL", "3")
    fired = tmp_path / "fault-fired"
    transient = candidate(tmp_path, MIGRATE, name="-failed-marker-once", fault=(
        "import pathlib\n"
        "from exomem.collection_store import admission\n"
        "installed = admission._install_marker\n"
        "def install_marker(*arguments, **options):\n"
        f"    fired = pathlib.Path({str(fired)!r})\n"
        "    if not fired.exists():\n"
        "        fired.touch()\n"
        "        raise OSError(5, 'an I/O error before the marker was written')\n"
        "    return installed(*arguments, **options)\n"
        "admission._install_marker = install_marker\n"))
    result = upgrade(tmp_path, Handoff(tmp_path, root), transient)
    assert result["ok"], result
    assert result["phase"] == "ready"
    assert result["handoff"]["migration"]["collections"] == {
        "state": "ran", "outcome": "migrated", "first_attempt": {"outcome": "pending", "code": "OSError"}}
    routed = marker(root)
    assert {entry["collection_id"]: entry["manifest_path"] for entry in routed["collections"]} == MIGRATED
    assert (fence().enrolled, fence().generation, fence().store_id) == (True, 1, routed["store_id"])
    # The replacement writes: the vault is not left read-only behind the cut fence.
    serving = start_service(root)
    try:
        assert call(root, "record_memory", action="append", collection=CID, item={"title": "After the rerun"},
                    why="a write after the handoff")["outcome"] == "committed"
    finally:
        serving.stop()


def test_a_migrator_the_supervisor_stopped_before_it_published_completes_in_files_with_its_output(
        service, tmp_path, capsys):
    """A migrator stopped before it published anything that fails the upgrade into recovery, is
    recorded as an unrecognized outcome, or whose last output never reaches the service journal
    because the supervisor closed its output before the migrator stopped."""
    root = service
    operator_file(tmp_path)
    handler_ready = tmp_path / "handler-ready"
    hung = candidate(tmp_path, MIGRATE, name="-hung", fault=(
        "import pathlib, signal, time\n"
        "from exomem.collection_store import migration\n"
        "def stopped(number, frame):\n"
        "    print('the migrator stopped mid-step', flush=True)\n"
        "    raise SystemExit(1)\n"
        "def settle_handoff(vault_root, *, deadline):\n"
        "    signal.signal(signal.SIGTERM, stopped)\n"
        f"    pathlib.Path({str(handler_ready)!r}).touch()\n"
        "    time.sleep(60)\n"
        "migration.settle_handoff = settle_handoff\n"))

    class ReadyMigrator(Handoff):
        async def _spawn(self, command, **options):
            await super()._spawn(command, **options)
            # Expire only after handler setup; this case proves stop-time output,
            # while the budget cases cover the supervisor's elapsed-time limit.
            while not handler_ready.exists():
                assert self.child.returncode is None, "the migrator exited before handler setup"
                await asyncio.sleep(0.01)
            raise TimeoutError("simulated timeout after handler setup")

    capsys.readouterr()
    result = upgrade(tmp_path, ReadyMigrator(tmp_path, root), hung)
    assert result["ok"], result
    assert result["handoff"]["migration"]["collections"] == {
        "state": "ran", "outcome": "abandoned", "code": "timed_out"}
    assert "the migrator stopped mid-step" in capsys.readouterr().out
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()


def test_a_migrator_the_supervisor_stopped_after_it_published_leaves_the_vault_serving(
        service, tmp_path, monkeypatch):
    """A migrator the supervisor's wait stopped while its published store finished, as on a store
    larger than the finishing reserve, that fails the upgrade into recovery; every resume then meets
    the same wall until the owner applies offline."""
    root = service
    note = root / "Knowledge Base/Notes/Independent.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntitle: Independent\n---\nIndependent knowledge\n")
    operator_file(tmp_path)
    # Short, so the lease each stopped migrator keeps expires within the test.
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TTL", "3")
    marker_install_ready = tmp_path / "marker-install-ready"
    # Every finish outlasts the wait: the marker install never returns before the stop.
    slow = candidate(tmp_path, MIGRATE, name="-slow-finish", fault=(
        "import pathlib, time\n"
        "from exomem.collection_store import admission\n"
        "def install_marker(*arguments, **options):\n"
        f"    pathlib.Path({str(marker_install_ready)!r}).touch()\n"
        "    time.sleep(60)\n"
        "admission._install_marker = install_marker\n"))

    class PublishedMigrator(Handoff):
        async def _spawn(self, command, **options):
            marker_install_ready.unlink(missing_ok=True)
            await super()._spawn(command, **options)
            # Stop after publication; the budget cases cover the elapsed-time limit.
            while not marker_install_ready.exists():
                assert self.child.returncode is None, "the migrator exited before publication"
                await asyncio.sleep(0.01)
            raise TimeoutError("simulated timeout after publication")

    result = upgrade(tmp_path, PublishedMigrator(tmp_path, root), slow)
    assert result["ok"], result
    assert result["phase"] == "ready"
    assert result["handoff"]["migration"]["collections"] == {
        "state": "ran", "outcome": "pending", "code": "timed_out",
        "first_attempt": {"outcome": "pending", "code": "timed_out"}}
    assert authority.read_marker(root) is None and (fence().enrolled, fence().generation) == (True, 1)
    # The stopped migrator renewed until its exit; its last grant must expire before service admission.
    deadline = time.monotonic() + 10
    while lease().holder is not None:
        assert time.monotonic() < deadline, "the stopped migrator's lease did not expire"
        time.sleep(0.05)
    serving = Service(root, writer_lease.start_server_lifecycle())
    try:
        with pytest.raises(OpError, match="COLLECTION_STORE_UNAVAILABLE"):
            rows(root, WORK)
        assert serving.manager.status()["collection_store"]["status"] == "sync_pending"
        with pytest.raises(OpError, match="COLLECTION_STORE_UNAVAILABLE"):
            call(root, "record_memory", action="append", collection=CID,
                 item={"title": "While pending"}, why="an unadmitted write")
        assert "Independent knowledge" in call(root, "read_memory", path="Knowledge Base/Notes/Independent.md")["body"]
    finally:
        serving.stop()
    assert lease().holder is None
    result = upgrade(tmp_path, Handoff(tmp_path, root), candidate(tmp_path, MIGRATE))
    assert result["ok"], result
    serving = start_service(root)
    try:
        assert call(root, "record_memory", action="append", collection=CID, item_key=OTHER,
                    item={"title": "After recovery"}, why="a recovered write")["outcome"] == "committed"
        assert OTHER in rows(root, WORK)
    finally:
        serving.stop()
