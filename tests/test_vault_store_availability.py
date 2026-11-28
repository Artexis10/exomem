"""A vault in store mode refuses every path that would serve stale Markdown or drop its pages."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest

from exomem import __main__ as cli
from exomem import commands, doctor, server_runtime, state_migration, state_paths
from exomem.cli_ops import OpError
from exomem.collection_store import (
    admission,
    capability,
    connection,
    export,
    migration,
    preflight,
    schema,
    takeover,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope
from exomem.vault_store import availability, cli_fence, migrate, ownership, write

AT = "2026-11-08T00:00:00Z"
LEASE_ENV = ("EXOMEM_WRITER_LEASE_URL", "EXOMEM_WRITER_LEASE_TOPOLOGY", "EXOMEM_CLOUD_CELL")


@pytest.fixture
def store_mode_vault(vault, monkeypatch):
    """The fixture vault with both store-mode records and its pages in the store, as activation leaves them."""
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    return _enter_store_mode(vault)


def _enter_store_mode(vault, stored_only=None):
    """Move ``vault`` into store mode; ``stored_only`` maps a page to bytes that only its store copy holds."""
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            # A store-mode store holds the owners' tables, and every commit derives rows into them.
            migrate.create_owner_schema(tx)
            tx.execute("INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                       "VALUES (1, 'store', 1, ?, 'test')", (AT,))
            # A release from before the derivation queue derived each page's rows inside the page's
            # commit. This release's commit queues them, so the queue stands in until they are derived.
            queueless = tx.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'store_derivation_queue'"
            ).fetchone() is None
            if queueless:
                schema._migrate_to_20(tx)
        # In store mode a page read answers from the store, so the pages must be there.
        pages = tuple(
            write.PagePut(owned.rel, (stored_only or {}).get(owned.rel, (vault / owned.rel).read_bytes()),
                          owned.stat.st_mtime_ns)
            for owned in ownership.iter_owned(vault)
            if owned.ownership is ownership.Ownership.PAGE
        )
        write.commit(
            CollectionWriter(vault, handle),
            write.StoreChange(write.Origin.MIGRATION, "test fixture", puts=pages),
        )
        if queueless:
            # The earlier release left every row derived and no queue behind.
            with handle.transaction() as tx:
                assert tx.execute("SELECT COUNT(*) FROM store_derivation_queue").fetchone() == (0,)
                tx.execute("DROP TABLE store_derivation_queue")
    state_migration.record_vault_store_compatibility(vault, authority_check=lambda: True, active=True)
    return vault


def _coordinator(monkeypatch, url, topology=None):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", url)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "test-vault")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "test-replica")
    if topology is not None:
        monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOPOLOGY", topology)


def _record_divergence(vault, key, value):
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as writer:
        with writer.transaction() as tx:
            tx.execute("INSERT INTO store_meta(key, value) VALUES (?, ?)", (key, value))


def _record_id_only(vault):
    state_migration.record_vault_store_compatibility(vault, authority_check=lambda: True, active=True)


def _create_store_without_mode_row(vault):
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True):
        pass


def _vault_bytes(vault):
    return {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}


def test_a_store_mode_vault_with_no_coordinator_refuses_to_start(store_mode_vault):
    """Defect: store mode starts with no coordinator, so no fence or head exchange protects the store."""
    with pytest.raises(CollectionStoreError) as refused:
        availability.require_servable(store_mode_vault)
    assert refused.value.code == admission.COORDINATION_REQUIRED


def test_a_store_mode_vault_under_the_default_multi_host_topology_refuses_to_start(
        store_mode_vault, monkeypatch):
    """Defect: a coordinator with no topology lets writer authority move to a host that holds no pages."""
    _coordinator(monkeypatch, "https://lease.example.test")
    with pytest.raises(CollectionStoreError) as refused:
        availability.require_servable(store_mode_vault)
    assert refused.value.code == availability.MULTI_HOST
    assert "EXOMEM_WRITER_LEASE_TOPOLOGY=single-host" in refused.value.reason


def test_startup_on_a_build_with_store_mode_disabled_names_the_rollback(store_mode_vault, monkeypatch):
    """Defect: a build that cannot serve the store's pages starts and answers from stale Markdown."""
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED - {capability.VAULT_STORE_V1})
    _coordinator(monkeypatch, "http://127.0.0.1:8799", topology="single-host")
    with pytest.raises(CollectionStoreError) as refused:
        server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)
    assert refused.value.code == availability.NOT_SERVED
    assert "evermatter store-mode rollback" in refused.value.reason


@pytest.mark.parametrize("entry", ["cli", "terminal_ui"])
def test_the_cli_and_the_terminal_ui_refuse_the_vault_that_the_service_refuses(
        store_mode_vault, monkeypatch, capsys, entry):
    """Defect: the service refuses a store-mode vault that this build cannot serve, while a CLI command
    or a terminal UI session starts on the same vault and serves it."""
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED - {capability.VAULT_STORE_V1})
    _coordinator(monkeypatch, "http://127.0.0.1:8799", topology="single-host")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(store_mode_vault))
    if entry == "cli":
        assert cli.main(["bootstrap", "--json"]) == 1
        refusal = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert refusal["error"]["code"] == availability.NOT_SERVED
        return
    from exomem.tui.backend import ExomemBackend

    monkeypatch.setattr(ExomemBackend, "_apply_lean_fallback", staticmethod(lambda: None))
    backend = ExomemBackend(str(store_mode_vault))
    assert backend.resolve_vault().initialized
    with pytest.raises(CollectionStoreError) as refused:
        backend.start_runtime()
    assert refused.value.code == availability.NOT_SERVED


@pytest.mark.parametrize("earlier", [schema.SCHEMA_VERSION - 1, schema.VAULT_STORE_READ_SCHEMA_VERSION],
                         ids=["previous schema", "oldest schema the page reader admits"])
def test_a_store_mode_vault_that_an_earlier_store_schema_wrote_starts_and_serves_its_stored_pages(
        vault, monkeypatch, earlier):
    """Defect: a release that changes the store schema cannot start on a store-mode vault an earlier release wrote.

    The start reads the schema pages from the store before the writer takes the lease and migrates the store,
    and the page reader refused every store below the current schema with COLLECTION_STORE_SCHEMA_PENDING.
    """
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    note = "Knowledge Base/Notes/Patterns/retry-with-fixed-interval.md"
    stored = b"---\ntitle: Retry\ntype: note\n---\n# Retry\n\nAn edit that only the store holds.\n"
    with monkeypatch.context() as earlier_release:
        # The earlier release built its store through the same migrations, up to its own schema.
        earlier_release.setattr(schema, "SCHEMA_VERSION", earlier)
        _enter_store_mode(vault, stored_only={note: stored})
    store = connection.store_path(vault)
    _coordinator(monkeypatch, "http://127.0.0.1:8799", topology="single-host")

    started = server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)

    assert "session" in started.source_schema.source_types
    assert "An edit that only the store holds." in commands.op_read_memory(vault, path=note)["body"]
    # Only the writer migrates, under the lease: a start leaves the store for a rollback to the earlier release.
    with closing(sqlite3.connect(store)) as raw:
        assert schema.schema_version(raw) == earlier
    # The store thread opens this writer once it holds the lease; its migration leaves the pages as they read.
    with connection.open_writer(store, lease_check=lambda: True):
        pass
    with closing(sqlite3.connect(store)) as raw:
        assert schema.schema_version(raw) == schema.SCHEMA_VERSION
    assert "An edit that only the store holds." in commands.op_read_memory(vault, path=note)["body"]


def test_a_file_mode_vault_starts_without_a_lease(vault, monkeypatch):
    """Defect: the store-mode checks reach file-mode users, who run with no coordinator."""
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    availability.require_servable(vault)
    assert availability.page_writer(vault) is None
    assert availability.marker_inconsistency(vault) is None
    assert doctor._check_vault_store_records(vault) is None


@pytest.mark.parametrize("record", [
    (schema.META_REPLICA_DIVERGENCE, json.dumps({"reason": "the vault replica records unresolved divergence"})),
    (schema.META_VIEW_DIVERGED, "1"),
], ids=["replica divergence", "view stamp"])
def test_a_write_to_a_diverged_store_mode_vault_refuses_with_the_stores_code_and_changes_no_markdown(
        store_mode_vault, record):
    """Defect: a page write reaches disk while the store that owns the pages awaits reconciliation."""
    _record_divergence(store_mode_vault, *record)
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(OpError) as refused:
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert refused.value.code == "COLLECTION_STORE_DIVERGED"
    assert _vault_bytes(store_mode_vault) == before


def test_a_page_write_off_the_store_thread_refuses_and_changes_no_markdown(store_mode_vault):
    """Defect: a page write off the store thread lands in Markdown that the store ignores."""
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(OpError) as refused:
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert refused.value.code == availability.OFF_THREAD
    assert _vault_bytes(store_mode_vault) == before


class _Boundary:
    """A manager that offers only the mutation boundary, so any later use of the manager raises AttributeError."""

    def __init__(self):
        self.entered = []

    @contextmanager
    def consistency_guard(self, _root, *, operation):
        self.entered.append(operation)
        yield


@pytest.mark.parametrize("step, options, boundary", [
    (export.apply, {"plan_id": "any-plan", "why": "return to files"}, "collection_store_export"),
    (export.retire_local, {}, "collection_store_export_retire_local"),
], ids=["apply", "retire_local"])
def test_the_reverse_export_refuses_under_the_mutation_boundary_before_any_step(
        store_mode_vault, monkeypatch, step, options, boundary):
    """Defect: exporting the collections to files drops the store that holds the pages, or checks outside the boundary."""
    monkeypatch.setattr(admission._ProducerSession, "bind", lambda *_args: None)
    monkeypatch.setattr(admission, "_bind_fence_client", lambda *_args: None)
    manager = _Boundary()
    session = SimpleNamespace(root=store_mode_vault, verify_custody=lambda: None)
    with pytest.raises(CollectionStoreError) as refused:
        step(session, manager, None, **options)
    assert refused.value.code == availability.PAGES_AT_RISK
    assert manager.entered == [boundary]


def test_abandoning_an_export_still_runs_in_store_mode(store_mode_vault, monkeypatch):
    """Defect: the page-loss refusal blocks the step that gives up an unfinished export, which drops no store."""
    ran = []
    monkeypatch.setattr(admission, "_offline_owner",
                        lambda root, operation, work, **_options: work(SimpleNamespace(root=root), None, None))
    monkeypatch.setattr(export, "abandon", lambda *_args, **_kwargs: ran.append("abandon"))
    with library_scope():
        admission.export_route(store_mode_vault, plan_id="any-plan", why="stop", abandon=True)
    assert ran == ["abandon"]


@pytest.mark.parametrize("primitive", [
    lambda session: takeover.replace_live(session, "leaf", lambda: None),
    lambda session: export._tombstone_store(session, AT),
], ids=["replace_live", "tombstone_store"])
def test_the_steps_that_swap_or_retire_the_live_store_refuse_for_any_caller_in_store_mode(store_mode_vault, primitive):
    """Defect: a caller with no guard of its own swaps or retires the store that holds the pages."""
    with pytest.raises(CollectionStoreError) as refused:
        primitive(SimpleNamespace(root=store_mode_vault))  # the refusal comes before any use of the session
    assert refused.value.code == availability.PAGES_AT_RISK


def test_a_takeover_adopt_refuses_when_the_local_store_is_in_store_mode(store_mode_vault):
    """Defect: adopting the published replica replaces the live store with a copy that has no pages."""
    session = SimpleNamespace(root=store_mode_vault)  # the refusal comes before any use of the session
    with pytest.raises(CollectionStoreError) as refused:
        admission._settle(session, None, 1, {}, None, "adopt", "the replica is newer", None)
    assert refused.value.code == availability.PAGES_AT_RISK


def test_a_staged_migration_with_pending_collections_refuses_in_store_mode(store_mode_vault, monkeypatch):
    """Defect: staging a replacement store from collection files installs a store that holds no pages."""
    monkeypatch.setattr(preflight, "candidates", lambda root, ownership_proof=None: ([], ["pending"], []))
    with pytest.raises(CollectionStoreError) as refused:
        migration._stage(store_mode_vault)
    assert refused.value.code == availability.PAGES_AT_RISK


def _corrupt_manifest(vault):
    (state_paths.vault_state_dir(vault) / state_migration.MANIFEST_NAME).write_bytes(b"{not json")


def test_a_corrupt_manifest_refuses_a_write_and_a_start_and_changes_no_markdown(store_mode_vault):
    """Defect: a damaged manifest passes for file mode, so a write lands in Markdown the store owns."""
    _corrupt_manifest(store_mode_vault)
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(state_migration.StateMigrationManifestError):
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert _vault_bytes(store_mode_vault) == before
    with pytest.raises(state_migration.StateMigrationManifestError):
        availability.require_servable(store_mode_vault)


def test_the_doctor_reports_a_corrupt_manifest_as_the_manifest_error_not_as_a_marker_inconsistency(
        store_mode_vault):
    """Defect: a damaged manifest crashes the doctor, or reads as the crash state that a rollback settles."""
    _corrupt_manifest(store_mode_vault)
    check = doctor._check_vault_store_records(store_mode_vault)
    assert (check.status, check.details) == (
        "fail", {"migration_manifest": str(state_paths.vault_state_dir(store_mode_vault) / state_migration.MANIFEST_NAME)})
    assert "unreadable or invalid" in check.message
    assert "store-mode rollback" not in check.message + check.remediation


@pytest.mark.parametrize("store_exists", [False, True], ids=["no store file", "store without a store_mode row"])
def test_a_recorded_id_with_no_store_mode_warns_and_names_the_rollback_without_refusing(
        vault, monkeypatch, caplog, store_exists):
    """Defect: a crash between the two records leaves a vault that nothing names, or one that refuses to serve."""
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    _record_id_only(vault)
    if store_exists:
        _create_store_without_mode_row(vault)
    with caplog.at_level(logging.WARNING, logger=availability.log.name):
        availability.require_servable(vault)
    # Creating the store runs Alembic, which logs at INFO whenever an earlier test left the root logger there.
    warnings = [record.getMessage() for record in caplog.records if record.name == availability.log.name]
    assert warnings == [availability.marker_inconsistency(vault)]
    assert "evermatter store-mode rollback" in caplog.text
    assert availability.page_writer(vault) is None
    check = doctor._check_vault_store_records(vault)
    assert (check.status, check.message) == ("warn", availability.marker_inconsistency(vault))
    assert "evermatter store-mode rollback" in check.remediation


def test_every_dispatched_subcommand_declares_what_it_touches_in_the_vault():
    """Defect: a subcommand added to the dispatch table with no declaration reads or writes pages of a
    store-mode vault that its handler never checks."""
    declared = {name.split(" ")[0] for name in cli_fence.ACCESS}
    assert set(cli._COMMANDS) == declared
    assert cli._CLI_ONLY_SUBCOMMANDS <= set(cli._COMMANDS)
    assert all(isinstance(entry.access, cli_fence.VaultAccess) for entry in cli_fence.ACCESS.values())
    assert all(entry.remedy for entry in cli_fence.ACCESS.values() if entry.access is cli_fence.VaultAccess.PAGES)


def _entry_arguments(name, vault, scratch):
    """The command line that makes each entry resolve `vault` and reach its first page access."""
    where = str(vault)
    manifest = scratch / "manifest.json"
    manifest.write_text("{}")
    folder = scratch / "folder"
    folder.mkdir()
    termination_log = str(scratch / "termination.log")
    digest = "0" * 64
    return {
        "setup": ["setup", "--vault", where, "--yes"],
        "init": ["init", "--vault", where],
        "reclaim-schema": ["reclaim-schema", "--vault", where],
        "package-skills": ["package-skills", "--vault", where, "--out", str(scratch / "skills")],
        "personalize": ["personalize", "--vault", where, "--dry-run"],
        "demo": ["demo", "--vault", where],
        "warm": ["warm", "--vault", where],
        "backfill-media": ["backfill-media", "--vault", where],
        "index": ["index", "--vault", where],
        "governance-schema plan-migration": ["governance-schema", "plan-migration", "--vault", where],
        "governance-schema stage-migration": [
            "governance-schema", "stage-migration", "--vault", where, "--expected-plan-digest", digest, "--yes"],
        "governance-schema commit-migration": [
            "governance-schema", "commit-migration", "--vault", where, "--expected-plan-digest", digest, "--yes"],
        "relations": ["relations", "census", "--vault", where],
        "collections adopt-local": ["collections", "adopt-local", "--why", "check", "--dry-run", "--vault", where],
        "collections create": [
            "collections", "create", "--manifest-path", "Planning/x.json", "--manifest-file", str(manifest),
            "--why", "check", "--vault", where],
        "collections rollback": ["collections", "rollback", "--vault", where],
        "collections migrate": ["collections", "migrate", "--dry-run", "--vault", where],
        "collections cutover": ["collections", "cutover", "--dry-run", "--vault", where],
        "import": ["import", str(folder), "--vault", where],
        "cell-import": ["cell-import", "prepare", "--import-id", "check", "--vault", where,
                        "--termination-log", termination_log],
        "cell-export": ["cell-export", "--export-id", str(uuid.uuid4()), "--max-bytes", "1", "--vault", where,
                        "--termination-log", termination_log],
        "cell-init": ["cell-init", "--vault", where],
        "media-replicas": ["media-replicas", "list", "--vault", where],
        "cell-media-census": ["cell-media-census", "--vault", where],
        "cell-media-storage": ["cell-media-storage", "--vault", where],
        "cell-media-manifest": ["cell-media-manifest", "--vault", where],
    }[name]


#: A cell job's termination log carries only the refusal's code: a reason never leaves the Job.
CODE_ONLY_ROUTES = frozenset({"cell-import", "cell-export", "cell-init"})


@pytest.mark.parametrize("name", [
    name for name, declared in cli_fence.ACCESS.items() if declared.access is cli_fence.VaultAccess.PAGES
])
def test_a_subcommand_that_touches_pages_refuses_a_store_mode_vault_even_where_the_service_serves_it(
        store_mode_vault, monkeypatch, tmp_path, capsys, name):
    """Defect: a CLI subcommand that the dispatcher routes before the registry path reads or rewrites the
    Markdown, projections or file-mode indexes of a vault whose pages the released build serves."""
    _coordinator(monkeypatch, "http://127.0.0.1:8799", topology="single-host")
    availability.require_servable(store_mode_vault)
    if name == "warm":
        monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)  # `warm` skips the vault it has no model for
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "cell-check")  # a cell job's pod always sets it
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    argv = _entry_arguments(name, store_mode_vault, scratch)
    before = _vault_bytes(store_mode_vault)

    status = cli._dispatch_main(argv)

    heard = capsys.readouterr()
    reported = "".join(path.read_text() for path in scratch.glob("*.log"))
    assert status != 0
    assert cli_fence.REFUSED in heard.out + heard.err + reported
    if name not in CODE_ONLY_ROUTES:
        assert cli_fence.ACCESS[name].remedy in heard.out + heard.err + reported
    assert _vault_bytes(store_mode_vault) == before


def test_a_subcommand_that_touches_pages_refuses_a_vault_with_a_corrupt_manifest_in_one_line(
        store_mode_vault, tmp_path, capsys):
    """Defect: an unreadable manifest crashes a PAGES command with a traceback instead of a refusal."""
    _corrupt_manifest(store_mode_vault)

    status = cli._dispatch_main(["reclaim-schema", "--vault", str(store_mode_vault)])

    heard = capsys.readouterr()
    assert status != 0
    assert "unreadable or invalid" in heard.out + heard.err
    assert "Traceback" not in heard.out + heard.err


def test_the_store_route_and_a_fresh_vault_still_run_beside_the_fence(
        store_mode_vault, monkeypatch, tmp_path, capsys):
    """Defect: a fence that is too wide leaves the owner no command to read or leave store mode, or refuses a
    command that creates its vault, which a missing vault does not put in store mode."""
    _coordinator(monkeypatch, "http://127.0.0.1:8799", topology="single-host")

    assert cli._dispatch_main(["store-mode", "status", "--vault", str(store_mode_vault)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "store"

    created = tmp_path / "created"
    assert cli._dispatch_main(["init", "--vault", str(created)]) == 0
    assert (created / "Knowledge Base").is_dir()
