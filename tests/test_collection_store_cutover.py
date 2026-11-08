"""`exomem collections cutover`: every structured collection to SQLite-only storage (OpenSpec
move-structured-collections-to-sqlite C3.1 and C4.1, design §17).

The vault is invented and holds the shapes a production vault holds before the cutover: Records
and Planning collections that the legacy importer moved into the store in items mode, a Planning
hierarchy, a Records collection still in files with one item an ID backfill stamped, a derived
summary collection whose manifest still says ``markdown-items``, and a dataset that stays in files.
The owner previews through the agent's maintenance call or the CLI and applies through the CLI with
the service stopped, as the runbook does. Each expected value comes from the public tools before
the cutover, never from the cutover's own report.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from contextlib import closing
from pathlib import Path

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH, copy_x3_fixture, ledger_item
from record_presentation_fixtures import COLLECTION_PATH as OBSERVED
from record_presentation_fixtures import ITEM_KEY as OBSERVATION
from record_presentation_fixtures import manifest_text as presentation_manifest
from record_presentation_fixtures import values
from test_collection_field_admission import GUEST
from test_collection_store_derived import DAYS, DERIVED, _archive, _preserve
from test_collection_store_derived import PATH as SAMPLES_PATH
from test_collection_store_derived import TEXT as DERIVED_TEXT
from test_collection_store_import_exports import SAMPLES
from test_collection_store_migration import (  # noqa: F401 - the env fixture
    LEDGER_ID,
    MIGRATE,
    OPERATOR,
    PLANNING,
    REPRESENTATION,
    WORK,
    X3,
    env,
    legacy_vault,
    migrate,
    printed,
    start_service,
)
from test_collection_store_preflight import EVIDENCE
from test_collection_store_s1_gate import _until
from test_collection_store_service import OWNER, call, imported, refused
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text

from exomem import __main__ as cli
from exomem import commands, record_formats, records, state_migration
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import (
    admission,
    capability,
    connection,
    conversion,
    edit_back,
    references,
    runtime,
    summary,
    typed_storage,
    types,
)
from exomem.collection_store import cutover as collections_cutover
from exomem.governance.principal import request_scope

FLOOR = "collections-sqlite-only-v1"
#: The release that ships the cutover serves summary collections, publishes views and runs edit-back.
RELEASED = MIGRATE | frozenset({capability.RECORDS_SUMMARY_V1, capability.COLLECTION_VIEWS_V1})
KEPT = "4e4e4e4e-4e4e-4e4e-8e4e-4e4e4e4e4e4e"
KEPT_PATH = manifest_path().replace("Work", "Kept")
DATASET = "Knowledge Base/Records/dataset/_collection.md"
OUTCOME = "c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1"
INITIATIVE = "d2d2d2d2-d2d2-4d2d-8d2d-d2d2d2d2d2d2"
#: A derived collection as a release before C2.1 created it: rows in the store, manifest still markdown-items.
OLD_DERIVED_TEXT = DERIVED_TEXT.replace("strategy: sqlite\n", "strategy: markdown-items\n")
CONVERTED = {WORK: CID, PLANNING: OTHER, LEDGER_COLLECTION_PATH: LEDGER_ID, KEPT_PATH: KEPT}
#: The ledger names its item folder in ``storage.source``; the others keep the default.
SOURCES = {LEDGER_COLLECTION_PATH: "Entries"}


def item_files(root, path):
    """The item files of a collection's item folder, by vault path and digest."""
    folder = root / Path(path).parent / SOURCES.get(path, "Items")
    return {file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in sorted(folder.rglob("*.md")) if not file.name.startswith("_")}


def contents(root, path):
    """A collection's rows as its public query answers them, without the fields migration re-represents."""
    tool = "plan_memory" if path == PLANNING else "record_memory"
    # A derived collection's rows answer only a collection query.
    query = {"query": {"version": 1, "page": {"limit": 50}}} if path == SAMPLES_PATH else {}
    found = call(root, tool, action="query", collection=path, **query)
    return sorted(json.dumps({name: value for name, value in row.items() if name not in REPRESENTATION},
                              sort_keys=True) for row in found["rows"])


def tree(root):
    """The Planning hierarchy under the outcome, as the public query answers it."""
    return call(root, "plan_memory", action="query", collection=PLANNING, hierarchy_mode="descendants",
                filters=[{"column": "plan_id", "op": "eq", "value": OUTCOME}])["hierarchy"]["edges"]


def settled(root, cid):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return not summary.publication_outstanding(reader, cid)


def cutover(root, *arguments, operator=True):
    """`exomem collections cutover`, the operator credential set only in the apply shell."""
    with pytest.MonkeyPatch.context() as shell:
        if operator:
            shell.setenv(OPERATOR, "operator")
        return cli._collections_main(["cutover", "--vault", str(root), *arguments])


def apply(root, plan_id, backup):
    return cutover(root, "--plan-id", plan_id, "--why", "move every collection to SQLite-only storage",
                   "--backup-to", str(backup))


def vault_with_every_shape(folder, monkeypatch):
    """The production shapes before the cutover, with the service stopped as the runbook leaves it.

    A release before the reference indexes built its query projections: the hierarchy rule followed
    no reference then, so no projection indexes the fields a Planning write follows.
    """
    real = types.named_validator
    with monkeypatch.context() as release:
        release.setattr(types, "named_validator", lambda name: dataclasses.replace(real(name), references=tuple))
        root = legacy_vault(folder)
        # The cutover refuses a log collection (its own test); this vault holds none.
        shutil.rmtree((root / X3).parent)
        assert migrate(root, "apply") == 0
        # A Records collection that a release before the cutover still created in files.
        records.create_collection(root, KEPT_PATH, manifest_text().replace(CID, KEPT).replace("title: Work", "title: Kept"),
                                  why="kept in files", scaffold=True)
        monkeypatch.setattr(capability, "RELEASED", RELEASED)
        monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)
        # A full census pass every few tenths of a second, so a new item view need not wait the production 60 s.
        monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
        service = start_service(root)
        try:
            call(root, "plan_memory", action="add", collection=PLANNING, plan_id=OUTCOME,
                 item={"title": "Publish the guide", "kind": "outcome", "status": "planned",
                       "commitment": "committed", "horizon": "quarter"}, why="a parent")
            call(root, "plan_memory", action="add", collection=PLANNING, plan_id=INITIATIVE,
                 item={"title": "Draft the outline", "kind": "initiative", "status": "planned",
                       "commitment": "committed", "horizon": "quarter", "parent": f"exomem://plan/{OTHER}/{OUTCOME}"}, why="a child")
            call(root, "record_memory", action="append", collection=KEPT_PATH, item={"title": "Stamped", "count": 1},
                 why="one kept row")
            [item] = (root / KEPT_PATH).parent.glob("Items/*.md")
            # The stamp an ID backfill writes: the vault's note identity, outside the item schema.
            item.write_text(item.read_text().replace("---\n", "---\nexomem_id: 0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e\n", 1))
            with monkeypatch.context() as older:
                older.setattr(records, "require_store_storage", lambda manifest: None)
                call(root, "record_memory", action="create", manifest_path=SAMPLES_PATH, manifest_text=OLD_DERIVED_TEXT,
                     why="derived samples")
            job = imported(root, collection=DERIVED, mode="start", source_ref=_preserve(root, _archive(DAYS))["path"],
                           format="json-document", members="samples/*", mapping=SAMPLES)
            _until(lambda: imported(root, collection=DERIVED, mode="status", continuation=job["continuation"])["state"]
                   == "complete", 30)
            _until(lambda: all(settled(root, cid) for cid in (CID, OTHER, LEDGER_ID, DERIVED))
                   and len(item_files(root, PLANNING)) == 2, 30)
        finally:
            service.stop()
        return root


def test_a_cutover_moves_every_collection_to_sqlite_only_storage_and_keeps_every_row(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: the cutover loses or changes a row, skips a collection in files or a derived summary
    manifest, leaves item files in the vault, removes one before its archived copy verifies, takes no
    verified backup first, or does any step twice when the owner runs it again."""
    root = vault_with_every_shape(env, monkeypatch)
    service = start_service(root)
    try:
        before = {path: contents(root, path) for path in (*CONVERTED, SAMPLES_PATH)}
        files = {path: item_files(root, path) for path in CONVERTED}
        hierarchy = tree(root)
        _until(lambda: all(settled(root, cid) for cid in (*CONVERTED.values(), DERIVED)), 30)
        # The agent's one call previews; it changes nothing and names the CLI for the apply.
        agent = call(root, "maintain_memory", mode="collections-cutover")
    finally:
        service.stop()
    assert all(files.values()) and hierarchy == [{"parent": OUTCOME, "child": INITIATIVE}]

    assert cutover(root, "--dry-run") == 0
    preview = printed(capsys)
    assert preview["plan_id"] == agent["plan_id"] and preview["blockers"] == []
    assert {row["path"]: row["steps"] for row in preview["collections"]} == {
        WORK: ["convert"], PLANNING: ["convert"], LEDGER_COLLECTION_PATH: ["convert"],
        KEPT_PATH: ["migrate", "convert"], SAMPLES_PATH: ["rewrite_strategy"], DATASET: []}

    backup = backup_file(tmp_path)
    assert apply(root, preview["plan_id"], backup) == 0, capsys.readouterr().err
    receipt = printed(capsys)
    assert receipt["phase"] == "complete"
    # The backup was taken before any step and still verifies.
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == receipt["backup"]["sha256"]
    done = {row["path"]: row for row in receipt["collections"]}
    assert set(done) == {*CONVERTED, SAMPLES_PATH}
    assert all(row["parity"]["status"] == "held" and row["projection"] == "ready" for row in done.values()), done
    for path in CONVERTED:
        assert item_files(root, path) == {}
        archive = Path(done[path]["archive"])
        assert not archive.is_relative_to(root)
        # Every file that left the vault is in its archive with the same bytes.
        assert {f"{entry}": hashlib.sha256((archive / "files" / entry).read_bytes()).hexdigest()
                for entry in files[path]} == files[path]
    assert done[SAMPLES_PATH]["archive"] is None
    assert FLOOR in state_migration.recorded_descriptor_ids(root)
    # Before the service starts, a Planning write finds the items that name a parent by index, not by
    # reading every row.
    followed = types.named_validator("planning.hierarchy.v1").references()
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        [(_, parent)] = typed_storage.keyed_values(reader, OTHER, [OUTCOME])
        assert set(references.neighbourhood(reader, OTHER, followed, OUTCOME, parent) or ()) == {OUTCOME, INITIATIVE}

    # A second run of the same plan repeats nothing.
    store_bytes = connection.store_path(root).read_bytes()
    assert apply(root, preview["plan_id"], backup) == 0
    assert printed(capsys) == receipt
    assert connection.store_path(root).read_bytes() == store_bytes

    service = start_service(root)
    try:
        assert {path: contents(root, path) for path in before} == before
        assert tree(root) == hierarchy
        rows = []
        cursor = None
        while True:
            report = call(root, "maintain_memory", mode="collections-store", **({"cursor": cursor} if cursor else {}))
            rows.extend(report["inventory"]["collections"])
            if (cursor := report["inventory"]["next_cursor"]) is None:
                break
        shown = {row["path"]: (row["authority"], row["storage_strategy"], row["view_mode"], row["item_views"])
                 for row in rows}
        assert shown == {**{path: ("store", "sqlite", "summary", 0) for path in (*CONVERTED, SAMPLES_PATH)},
                         DATASET: ("file", "dataset", "items", None)}
    finally:
        service.stop()

    # A release from before the floor refuses the state instead of rendering item views again.
    monkeypatch.setattr(state_migration, "_OPTIONAL_COMPATIBILITY_IDS",
                        state_migration._OPTIONAL_COMPATIBILITY_IDS - {FLOOR})
    with pytest.raises(state_migration.StateMigrationManifestError):
        state_migration.require_vault_state_ready(root)


def crash(*args, **kwargs):
    raise SystemExit("injected crash")


def view_mode(root, cid):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute("SELECT view_mode FROM collections WHERE collection_id=?", (cid,)).fetchone()[0]


def backup_file(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir(exist_ok=True)
    return folder / "before-cutover.sqlite"


def test_a_records_collection_with_a_presentation_table_converts_and_keeps_its_child_rows(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: the cutover refuses a Records collection whose presentation renders a table field, or
    drops the table's child rows, which summary mode serves only through the store query."""
    root = legacy_vault(env)
    shutil.rmtree((root / X3).parent)
    # A file collection as an older release wrote it, so the migration imports it in items mode.
    observed = root / OBSERVED
    observed.parent.mkdir(parents=True)
    observed.write_text(presentation_manifest())
    (observed.parent / "Items").mkdir()
    (observed.parent / "Items/Observation.md").write_text(record_formats.render_markdown_item(
        collections.load_manifest(root, observed), values(), OBSERVATION))
    assert migrate(root, "apply") == 0
    monkeypatch.setattr(capability, "RELEASED", RELEASED)
    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)
    service = start_service(root)
    try:
        before = call(root, "record_memory", action="query", collection=OBSERVED)["rows"]
        _until(lambda: all(settled(root, cid) for cid in (CID, OTHER, LEDGER_ID)), 30)
    finally:
        service.stop()
    assert [len(row["measurements"]) for row in before] == [2]

    assert cutover(root, "--dry-run") == 0
    preview = printed(capsys)
    assert {row["path"]: row["steps"] for row in preview["collections"]}[OBSERVED] == ["convert"]
    assert apply(root, preview["plan_id"], backup_file(tmp_path)) == 0, capsys.readouterr().err
    service = start_service(root)
    try:
        assert call(root, "record_memory", action="query", collection=OBSERVED)["rows"] == before
    finally:
        service.stop()


def test_a_cutover_cut_after_any_step_resumes_from_its_record_and_repeats_nothing(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: a cutover cut after its migration, after one collection's conversion committed, or
    while that collection's query index builds, migrates or converts again, breaks parity on the
    resumed collection, leaves item files that nothing removes, or forgets where they went."""
    root = vault_with_every_shape(env, monkeypatch)
    files = {path: item_files(root, path) for path in CONVERTED}
    assert cutover(root, "--dry-run") == 0
    plan_id = printed(capsys)["plan_id"]
    backup = backup_file(tmp_path)

    # Cut 1: the migration finished, and no conversion started.
    with monkeypatch.context() as cut:
        cut.setattr(collections_cutover, "_convert_all", crash)
        with pytest.raises(SystemExit):
            apply(root, plan_id, backup)
    assert view_mode(root, KEPT) == "items" and item_files(root, KEPT_PATH) == files[KEPT_PATH]
    # The preview names the unfinished run, so the owner finishes it with the same plan_id.
    assert cutover(root, "--dry-run") == 0
    unfinished = printed(capsys)
    assert (unfinished["step"], unfinished["plan_id"], unfinished["phase"]) == ("resume", plan_id, "migrated")

    # Cut 2: the first conversion (Planning, in path order) committed and archived nothing.
    with monkeypatch.context() as cut:
        cut.setattr(admission, "migrate_route", lambda *a, **k: pytest.fail("the migration ran again"))
        cut.setattr(conversion, "_archive", crash)
        with pytest.raises(SystemExit):
            apply(root, plan_id, backup)
    assert view_mode(root, OTHER) == "summary" and item_files(root, PLANNING) == files[PLANNING]

    # Cut 3: Planning's files left the vault, and its query index had not published.
    with monkeypatch.context() as cut:
        cut.setattr(admission, "migrate_route", lambda *a, **k: pytest.fail("the migration ran again"))
        cut.setattr(collections_cutover, "_publish_projection", crash)
        with pytest.raises(SystemExit):
            apply(root, plan_id, backup)
    assert item_files(root, PLANNING) == {}

    with monkeypatch.context() as cut:
        cut.setattr(admission, "migrate_route", lambda *a, **k: pytest.fail("the migration ran again"))
        assert apply(root, plan_id, backup) == 0, capsys.readouterr().err
    receipt = printed(capsys)
    done = {row["path"]: row for row in receipt["collections"]}
    assert all(row["parity"]["status"] == "held" and row["projection"] == "ready" for row in done.values()), done
    archive = Path(done[PLANNING]["archive"]) / "files"
    assert {entry: hashlib.sha256((archive / entry).read_bytes()).hexdigest()
            for entry in files[PLANNING]} == files[PLANNING]
    # The resumed conversion added its one revise event; no step ran twice.
    assert done[PLANNING]["parity"]["audit_events"]["after"] == done[PLANNING]["parity"]["audit_events"]["before"] + 1
    assert {path: item_files(root, path) for path in CONVERTED} == {path: {} for path in CONVERTED}


def test_a_conversion_cut_after_its_commit_shows_in_the_inventory_and_the_cutover_finishes_it(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: a conversion cut after its store commit is visible only to the census, so the inventory
    and the cutover show the collection as done and its item files stay in the vault for good."""
    root = vault_with_every_shape(env, monkeypatch)
    files = item_files(root, WORK)
    service = start_service(root)
    try:
        with monkeypatch.context() as cut:
            cut.setattr(conversion, "_archive", crash)
            with request_scope(OWNER), pytest.raises(SystemExit):
                conversion.convert(root, WORK, why="cut short")
        shown = {}

        def listed():
            rows = call(root, "maintain_memory", mode="collections-store")["inventory"]["collections"]
            shown.update({row["path"]: row["conversion"] for row in rows})
            return WORK in shown

        # The conversion republishes the manifest; the egress withholds its path until the service catches up.
        _until(listed, 30)
        assert shown[WORK] == "committed"
    finally:
        service.stop()

    assert cutover(root, "--dry-run") == 0
    preview = printed(capsys)
    row = {row["path"]: row for row in preview["collections"]}[WORK]
    assert (row["steps"], row["conversion"]) == (["convert"], "committed")
    assert apply(root, preview["plan_id"], backup_file(tmp_path)) == 0, capsys.readouterr().err
    done = {row["path"]: row for row in printed(capsys)["collections"]}[WORK]
    # The conversion's revise event predates the backup, so the backup already counts it.
    assert done["parity"]["status"] == "held" and done["parity"]["audit_events"]["after"] == (
        done["parity"]["audit_events"]["before"])
    assert item_files(root, WORK) == {}
    assert {entry: hashlib.sha256((Path(done["archive"]) / "files" / entry).read_bytes()).hexdigest()
            for entry in files} == files


def test_only_the_owner_applies_a_cutover_and_only_with_the_service_stopped(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: the cutover runs beside a second writer (a running service, or an agent's call through
    it), or a caller without the owner's access reads the plan."""
    root = vault_with_every_shape(env, monkeypatch)
    files = item_files(root, WORK)
    assert cutover(root, "--dry-run") == 0
    plan_id = printed(capsys)["plan_id"]
    backup = backup_file(tmp_path)
    service = start_service(root)
    try:
        with pytest.raises((ValueError, OpError), match="INVALID_ARGUMENTS"):
            call(root, "maintain_memory", mode="collections-cutover", apply=True, plan_id=plan_id,
                 why="from an agent")
        with request_scope(GUEST):
            refused(lambda: commands.op_maintain_memory(root, mode="collections-cutover"),
                    "COLLECTION_STORE_OWNER_REQUIRED")
        assert apply(root, plan_id, backup) == 1
        assert "COLLECTION_STORE_SERVICE_ACTIVE" in capsys.readouterr().err
    finally:
        service.stop()
    assert not backup.exists() and view_mode(root, CID) == "items" and item_files(root, WORK) == files


def test_a_log_collection_blocks_the_cutover_and_a_plan_previewed_before_it_is_stale(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: the cutover applies a plan the owner did not review, or converts a markdown-log
    collection it has no SQLite-only form for, instead of naming it."""
    root = vault_with_every_shape(env, monkeypatch)
    files = item_files(root, WORK)
    assert cutover(root, "--dry-run") == 0
    reviewed = printed(capsys)["plan_id"]
    copy_x3_fixture(root)
    backup = backup_file(tmp_path)

    assert apply(root, reviewed, backup) == 1
    assert "STALE_MAINTENANCE_PLAN" in capsys.readouterr().err
    assert cutover(root, "--dry-run") == 1
    blocked = printed(capsys)
    assert [(row["path"], row["code"]) for row in blocked["blockers"]] == [(X3, collections_cutover.LOG_UNSUPPORTED)]
    assert apply(root, blocked["plan_id"], backup) == 1
    assert collections_cutover.BLOCKED in capsys.readouterr().err
    assert not backup.exists() and view_mode(root, CID) == "items" and item_files(root, WORK) == files


def test_a_parity_failure_stops_the_cutover_at_that_collection_and_leaves_later_ones_untouched(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: a conversion that changed a row commits anyway, or the run goes on to later
    collections; or the run cannot finish once the cause is fixed."""
    root = vault_with_every_shape(env, monkeypatch)
    files = {path: item_files(root, path) for path in CONVERTED}
    assert cutover(root, "--dry-run") == 0
    plan_id = printed(capsys)["plan_id"]
    backup = backup_file(tmp_path)
    real = collections_cutover.measure

    def one_row_more(conn, cid):
        found = real(conn, cid)
        # Only inside the ledger's conversion transaction: the conversion added a row.
        return {**found, "rows": found["rows"] + 1} if cid == LEDGER_ID and conn.in_transaction else found

    with monkeypatch.context() as broken:
        broken.setattr(collections_cutover, "measure", one_row_more)
        assert apply(root, plan_id, backup) == 1
    error = capsys.readouterr().err
    assert collections_cutover.PARITY in error and LEDGER_COLLECTION_PATH in error and "rows" in error
    # Path order: Planning and Kept converted before the ledger; the ledger and Work are as they were.
    assert (view_mode(root, OTHER), view_mode(root, KEPT)) == ("summary", "summary")
    assert (view_mode(root, LEDGER_ID), view_mode(root, CID)) == ("items", "items")
    assert {path: item_files(root, path) for path in (LEDGER_COLLECTION_PATH, WORK)} == {
        path: files[path] for path in (LEDGER_COLLECTION_PATH, WORK)}

    assert apply(root, plan_id, backup) == 0, capsys.readouterr().err
    assert all(row["parity"]["status"] == "held" for row in printed(capsys)["collections"])
    assert {path: item_files(root, path) for path in CONVERTED} == {path: {} for path in CONVERTED}


def test_after_the_cutover_writes_on_both_types_write_no_item_file(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: after the cutover an update, upsert, transition, archive or restore on a converted
    Records or Planning collection writes a file per row again, or refuses the multi-line text that
    its rows held in their item files."""
    root = vault_with_every_shape(env, monkeypatch)
    assert cutover(root, "--dry-run") == 0
    assert apply(root, printed(capsys)["plan_id"], backup_file(tmp_path)) == 0, capsys.readouterr().err
    service = start_service(root)
    try:
        work = call(root, "record_memory", action="query", collection=WORK)
        updated = call(root, "record_memory", action="update", collection=WORK, item_key=KEY, changes={"count": 7},
                       why="after the cutover", expected_container_hash=work["snapshot"],
                       expected_item_version=work["rows"][0]["item_version"])
        assert updated["outcome"] == "committed", updated
        ledger = call(root, "record_memory", action="query", collection=LEDGER_COLLECTION_PATH)
        upserted = call(root, "record_memory", action="bulk_upsert", collection=LEDGER_COLLECTION_PATH,
                        source=EVIDENCE, why="a newer export", expected_container_hash=ledger["snapshot"],
                        rows=[{"item": ledger_item(slug="entry-0-0", word_count=50)},
                              {"item": ledger_item(slug="entry-9-9")}])
        assert upserted.get("state") == "committed", upserted.get("rows")
        # The existing rows carry a multi-line text, which the converted collection keeps accepting.
        counts = {row["slug"]: row["word_count"] for row in call(
            root, "record_memory", action="query", collection=LEDGER_COLLECTION_PATH)["rows"]}
        assert (counts["entry-0-0"], counts["entry-9-9"]) == (50, 42)
        plans = call(root, "plan_memory", action="query", collection=PLANNING,
                     filters=[{"column": "plan_id", "op": "eq", "value": INITIATIVE}])
        guards = {"expected_container_hash": plans["snapshot"],
                  "expected_item_version": plans["rows"][0]["item_version"]}
        for action, change in (("triage", {"transition": {"status": "cancelled"}}),
                               ("update", {"changes": {"lifecycle": "archived"}}),
                               ("update", {"changes": {"lifecycle": "active"}})):
            done = call(root, "plan_memory", action=action, collection=PLANNING, plan_id=INITIATIVE, why=action,
                        **change, **guards)
            assert done["outcome"] == "committed", done
            guards = {"expected_container_hash": done["after_container_hash"],
                      "expected_item_version": done["after_item_hash"]}
        _until(lambda: all(settled(root, cid) for cid in CONVERTED.values()), 30)
    finally:
        service.stop()
    assert {path: item_files(root, path) for path in CONVERTED} == {path: {} for path in CONVERTED}


def test_a_host_that_loses_its_state_after_the_cutover_restores_every_row_from_the_vault(
        env, monkeypatch, capsys, tmp_path):  # noqa: F811 - the imported fixture, by name
    """Defect: after the cutover the vault holds no item files, so a host whose state root is lost
    restores no row of a converted collection, or restores one with item files."""
    root = vault_with_every_shape(env, monkeypatch)
    assert cutover(root, "--dry-run") == 0
    assert apply(root, printed(capsys)["plan_id"], backup_file(tmp_path)) == 0, capsys.readouterr().err
    service = start_service(root)
    try:
        before = {path: contents(root, path) for path in (*CONVERTED, SAMPLES_PATH)}
    finally:
        service.stop()
    # A replaced disk: an empty state root and a new lease identity for the same vault.
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "new-host-state"))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "new-host")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-new-host"))
    service = start_service(root)
    restored = {}

    def rebuilt():
        # The derived rows come back from their preserved sources, which the new host imports again.
        try:
            restored[SAMPLES_PATH] = contents(root, SAMPLES_PATH)
        except OpError as error:
            if error.code != "QUERY_REBUILDING":
                raise
            return False
        return True

    try:
        restored.update({path: contents(root, path) for path in CONVERTED})
        _until(rebuilt, 30)
        assert restored == before
    finally:
        service.stop()
    assert {path: item_files(root, path) for path in CONVERTED} == {path: {} for path in CONVERTED}
