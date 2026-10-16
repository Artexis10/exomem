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
import shlex
import socket
import sqlite3
import sys
import threading
import time
from contextlib import closing

import pytest
import uvicorn
from test_collection_store_migration import (
    MIGRATE,
    MIGRATED,
    OPERATOR,
    WORK,
    legacy_vault,
    marker,
    rows,
    start_service,
    staging_directories,
    stop_writer,
    vault_bytes,
)
from test_collection_store_service import DAILY, DAILY_PATH, DAILY_TEXT, call
from test_collection_store_writer import KEY, OTHER
from test_service_manager import _Ingress, _release_of

from exomem import __main__ as cli
from exomem import record_formats, records, service_manager, service_standby, state_migration, writer_lease
from exomem import structured_collections as collections
from exomem.collection_store import authority, capability, chain, connection, custody
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
    monkeypatch.delenv("EXOMEM_COLLECTION_STORE_PREVIEW", raising=False)
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


def candidate(tmp_path, released):
    """This interpreter, posing as a release whose fence enables ``released``.

    The launcher runs the candidate with ``-I``, so the release rides in a wrapper, not on PYTHONPATH.
    """
    prelude = (
        "import runpy, sys\n"
        "import exomem.collection_store.capability as capability\n"
        f"capability.RELEASED = frozenset({sorted(released)!r})\n"
        "arguments = [value for value in sys.argv[1:] if value != '-I']\n"
        "if arguments[0] == '-c':\n"
        "    sys.argv = ['-c', *arguments[2:]]\n"
        "    exec(compile(arguments[1], '<candidate>', 'exec'), {'__name__': '__main__'})\n"
        "else:\n"
        "    sys.argv = [arguments[1], *arguments[2:]]\n"
        "    runpy.run_module(arguments[1], run_name='__main__', alter_sys=True)\n"
    )
    wrapper = tmp_path / f"python-{'-'.join(sorted(released)) or 'dark'}"
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
        # What the standby process runs as the last step of its warm, beside the serving worker.
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
        "state": "ran", "reason": "collections migration staged", "collections": {"state": "ran", "outcome": "migrated"}}
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


def test_a_release_that_keeps_the_migration_dark_upgrades_as_before(service, tmp_path):
    """A public release, whose fence keeps the migration off, migrating a vault at an ordinary
    upgrade because a staging directory and the operator credential happen to be there."""
    root = service
    operator_file(tmp_path)
    result = upgrade(tmp_path, Handoff(tmp_path, root), candidate(tmp_path, frozenset()))
    assert result["ok"], result
    assert result["handoff"]["migration"] == {"state": "skipped", "reason": "declared_none"}
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert len(staging_directories(root)) == 1


@pytest.mark.parametrize("credential", ["missing", "readable-by-group"])
def test_without_a_usable_operator_credential_the_upgrade_completes_in_files(service, tmp_path, credential):
    """An upgrade that fails, or migrates with a credential another local user could read or replace,
    when the operator file is absent or not private to its owner."""
    root = service
    if credential == "readable-by-group":
        operator_file(tmp_path, mode=0o640)
    result = upgrade(tmp_path, Handoff(tmp_path, root), candidate(tmp_path, MIGRATE))
    assert result["ok"], result
    reason = {"missing": "operator credential file missing",
              "readable-by-group": "operator credential file is not private to its owner"}[credential]
    assert result["handoff"]["migration"] == {
        "state": "skipped", "reason": "declared_none", "collections": {"state": "skipped", "reason": reason}}
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 0)


def test_the_standby_warm_pre_imports_the_collections_it_reports(service):
    """A standby warm that never pre-imports, so no handoff ever has staging to migrate, or that
    reports its collections component ready before the pre-import ran."""
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
    assert result["handoff"]["migration"]["collections"] == {"state": "ran", "outcome": "abandoned"}
    assert migrator_report(capsys)["status"] == "abandoned"
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 0)
    assert written[0]["item_key"] in rows(root, WORK)


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


def test_the_operator_credential_reaches_only_the_migrator(service, tmp_path):
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
    service_standby.prepare_collections_migration(root)
    runtime = Launcher(tmp_path)

    async def scenario():
        target = await runtime.inspect({"python": str(stub), "version": _release_of(str(stub))})
        with pytest.raises(TimeoutError):
            await runtime.start(target, timeout=1)
        with pytest.raises(RuntimeError, match="exited before cutover readiness"):
            await runtime.start_standby(target, timeout=10)
        await runtime.discard_standby()
        await runtime.stop(timeout=10)
        assert runtime.migration_required(target) == (True, "collections migration staged")
        await runtime.migrate(target, timeout=30)

    asyncio.run(scenario())
    seen = {kind: (dumps / kind).read_text() for kind in ("worker", "standby", "migrator")}
    assert "--migrate-collections" in seen["migrator"].splitlines()[0]
    assert f"{OPERATOR}={OPERATOR_SECRET}" in seen["migrator"].splitlines()[1:]
    for kind in ("worker", "standby"):
        assert OPERATOR not in seen[kind]
    # Never on a command line, which any local user can read.
    assert all(OPERATOR_SECRET not in text.splitlines()[0] for text in seen.values())
