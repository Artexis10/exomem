"""A governed write to a store-mode vault commits in the store and writes no Markdown.

OpenSpec make-vault-store-canonical tasks 2.2 and 2.3. The vault is the migration suite's generated
vault in store mode. The fixture binds the writer as the store thread does. The projector is off,
so every answer comes from the store.
"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import sys
from contextlib import closing

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_media_store import selected_store as selected_store
from test_vault_store_migration import ALPHA, BETA, DIAGRAM, DIAGRAM_BYTES, vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_workflow_contracts import _proposal as workflow_contract_proposal

from exomem import bm25, commands, deferred_index, mode, note, workflow_contracts
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection, preview, runtime
from exomem.governance import artifact_census
from exomem.vault_store import availability, derivation, projection, read, verify, write
from exomem.vault_store import batch as store_batch
from exomem.vault_store import mode as store_mode

GAMMA = "Knowledge Base/Notes/Gamma.md"
OUTSIDE = "Outside/Loose.md"
GAMMA_TEXT = (
    "---\ntitle: Gamma\ntype: note\n---\n# Gamma\n\nThe harbour master keeps the tide ledger.\n"
)
LINKING_ALPHA = (
    "---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\n"
    "The lighthouse keeper logs the tide in [[Gamma]].\n"
)


@pytest.fixture
def served(stored, writer):  # noqa: F811
    """The vault with the store thread's writer bound: where a store-mode batch may commit."""
    token = preview._BOUND.set((stored.resolve(), writer, False))
    try:
        yield stored
    finally:
        preview._BOUND.reset(token)


def tree(root):
    """Every file under the vault with its bytes, to prove that a write changed none."""
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def store_rows(root, sql, *arguments):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, arguments).fetchall()


def revision(root):
    return store_rows(root, "SELECT revision FROM store_mode")[0][0]


def commits_since(root, start):
    """The canonical commits after revision ``start``. Each writes audit rows; a derivation that
    moves the revision after one writes none."""
    return store_rows(root, "SELECT COUNT(DISTINCT revision) FROM store_audit WHERE revision > ?", start)[0][0]


def bm25_paths(root, word):
    """The pages that the store's own full-text rows match for ``word``, best first."""
    stems = " OR ".join(f'"{token}"' for token in bm25.tokenize(word, query=True))
    return [
        path
        for (path,) in store_rows(
            root,
            "SELECT p.path FROM fts JOIN pages p ON p.rowid = fts.rowid "
            "WHERE fts MATCH ? ORDER BY bm25(fts), p.path",
            stems,
        )
    ]


def edge_targets(root, source):
    """The destinations of every graph edge that starts in ``source``."""
    return {
        target
        for (target,) in store_rows(
            root, "SELECT dst_page_key FROM graph_edges WHERE source_path = ?", source
        )
    }


def planned(root, rel, text, **keywords):
    return vault_module.PlannedWrite(root / rel, text, **keywords)


def test_source_metadata_between_request_checkouts_uses_canonical_store_without_a_projection(served, writer):  # noqa: F811
    from exomem import memory_refs, source_custody, writer_lease

    reference = memory_refs.memory_ref(memory_refs.new_id())
    manager = writer_lease.active_manager()
    with preview.preview_store(served, writer.handle) as access:
        saved_connection = access.connection
        with manager.mutation_guard(served, operation="enroll_test_source"):
            with preview.source_metadata_phase(served).source_metadata():
                assert manager._store_borrowers == {}
                assert not writer.connection.in_transaction
                source_custody.publish(served, source_custody.prepare_enrollment(
                    served, reference=reference, source_hash="1" * 64))
                admissions = source_custody.reserve_dependencies(served, [reference],
                    producer=source_custody.OwnerLocator(owner="collection_records", locator="test-store", version="2"),
                    operation_id="test-operation", checkpoint="before:test-operation")
                source_custody.begin_dependency_effect(served, admissions)
            manifest, _ = source_custody.read_manifest(served, reference)
            assert manifest.dependency_epochs["collection_records"].revision == 2
        with pytest.raises(connection.CollectionStoreError, match="CHECKOUT_CLOSED"):
            saved_connection.execute("SELECT 1")
        assert access.connection.execute("SELECT 1").fetchone() == (1,)
    assert not source_custody.manifest_path(served, reference).exists()


def test_source_capture_stores_sealed_original_declarations_without_manifest_files(
        served, writer, source_schema, selected_store, monkeypatch):  # noqa: F811
    from test_media_store import _offload

    from exomem import add, media_store, source_custody, writer_lease

    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    original = b"Sealed transcript retained by the canonical store.\n"
    manager = writer_lease.active_manager()
    with manager.mutation_guard(served):
        saved = add.add(served, source_schema, title="Stored attachment", content="", source_type="session",
            artifact=add.SourceArtifact(io.BytesIO(original), "transcript.txt", "text/plain"),
            source_identity={"system": "archive", "record_id": "stored-attachment", "revision": "one"})
        manifest, _ = source_custody.read_manifest(served, saved.ref)
        assert manifest.publication == "published"
        assert manifest.originals == (source_custody.OriginalBinding(
            sha256=hashlib.sha256(original).hexdigest(), size=len(original)),)
    assert (served / saved.artifact_path).read_bytes() == original
    assert not source_custody.manifest_path(served, saved.ref).exists()
    with read.snapshot(served) as view:
        assert view.page(saved.path).body
    retained, _ = source_custody.read_manifest(served, saved.ref)
    assert retained.originals == manifest.originals
    path = served / saved.artifact_path
    _offload(served, path)
    assert media_store.evict(served, path)
    writer.connection.execute("BEGIN")
    try:
        with pytest.raises(OpError, match="SOURCE_CUSTODY_BOUNDARY_REQUIRED"):
            with media_store.open_original(served, path):
                pytest.fail("a caller-owned SQL transaction cannot hold original acquisition")
        assert writer.connection.in_transaction
        assert not path.exists()
    finally:
        writer.connection.rollback()
    with media_store.open_original(served, path) as hydrated:
        assert hydrated.stream.read() == original
    assert not source_custody.manifest_path(served, saved.ref).exists()


def test_unenrolled_original_hydration_preserves_a_callers_sql_transaction(
        served, writer, source_schema, selected_store, monkeypatch):  # noqa: F811
    from test_media_store import _offload

    from exomem import add, media_store, source_custody, writer_lease

    monkeypatch.delenv("EXOMEM_SOURCE_ERASURE_POLICY", raising=False)
    original = b"Ordinary original with Source custody disabled.\n"
    with writer_lease.active_manager().mutation_guard(served):
        saved = add.add(served, source_schema, title="Ordinary attachment", content="", source_type="session",
            artifact=add.SourceArtifact(io.BytesIO(original), "ordinary.txt", "text/plain"))
    assert source_custody.read_manifest(served, saved.ref)[0] is None
    path = served / saved.artifact_path
    _offload(served, path)
    assert media_store.evict(served, path)
    writer.connection.execute("BEGIN")
    try:
        with media_store.open_original(served, path) as hydrated:
            assert hydrated.stream.read() == original
        assert writer.connection.in_transaction
    finally:
        writer.connection.rollback()
    assert source_custody.read_manifest(served, saved.ref)[0] is None


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_create_note_is_searchable_and_linked_when_it_returns_and_writes_no_markdown(
    served, monkeypatch
):
    """A write acknowledged before its rows are: the note returns and a search misses it."""
    # Under the service profile a write once queued a semantic receipt that no turn reads.
    monkeypatch.setattr(mode, "service_profile_enabled", lambda: True)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    before = tree(served)
    receipts = deferred_index.snapshot(served)
    result = commands.op_remember(
        served,
        title="Quokka census",
        status="draft",
        content="## Observations\n\n- [finding] The quokka population grew near [[Alpha]].\n",
        summary="A census finding for the test rig.",
    )
    created = result["path"]
    # The write also files the page in the Knowledge Base index and log, which hold its title.
    assert created in bm25_paths(served, "quokka")
    assert f"file:{ALPHA}" in edge_targets(served, created)
    assert store_rows(served, "SELECT 1 FROM identities WHERE path = ?", created)
    assert tree(served) == before, "a store-mode write must change no Markdown byte on disk"
    assert not (served / created).exists()
    assert deferred_index.snapshot(served) == receipts, "the drain follows audit rows, not receipts"


def test_a_batch_that_fails_after_its_commit_point_reports_each_stored_note_as_written(
    served, monkeypatch
):
    """The commit holds the pages and no file does yet. A failure after it that reads as a refusal
    sends the caller to create each note again, which finds its page taken."""

    def prepared(title, finding):
        arguments = {
            "title": title,
            "note_type": "insight",
            "status": "draft",
            "content": f"## Observations\n\n- [finding] {finding}\n",
            "summary": "A census finding for the test rig.",
        }
        draft = note.note(served, validate_only=True, **arguments)
        return note.note(
            served,
            draft_id=draft.draft_id,
            draft_hash=draft.draft_hash,
            draft_token=draft.draft_token,
            _return_prepared=True,
            _defer_navigation=True,
            **arguments,
        )

    notes = [
        prepared("Reef census", "The reef census counted more terns."),
        prepared("Marsh census", "The marsh census counted fewer herons."),
    ]

    def census_fails(_root, _replaced):
        raise RuntimeError("injected failure after the commit point")

    monkeypatch.setattr(artifact_census, "note_created", census_fails)

    outcomes = note.commit_prepared_notes(served, notes)

    assert [str(outcome) for outcome in outcomes] == ["injected failure after the commit point"] * 2
    for item in notes:
        assert store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", item.destination)
        assert not (served / item.destination).exists()


def test_a_saved_workflow_contract_is_inventoried_while_its_file_is_unwritten(served):
    """A contract page that the commit stores has no file until the projector writes it. An inventory
    that lists the contract folder's files reports no contract, and a save of the same title would
    miss the page it replaces."""
    saved = workflow_contracts.save_contract(
        served, workflow_contracts.parse_proposal(workflow_contract_proposal()), why="reviewed"
    )
    assert not (served / saved["path"]).exists()

    inventory = workflow_contracts.inventory_contracts(served)

    assert inventory["valid"] is True
    assert [item["key"] for item in inventory["summaries"]] == ["software-delivery"]
    inspected = workflow_contracts.inspect_contract(served, "software-delivery")
    assert inspected["content_hash"] == saved["content_hash"]


def test_a_new_page_resolves_the_links_that_wait_for_its_title_and_a_delete_unresolves_them(
    served,
    writer,  # noqa: F811
):
    """Links to a title that stay unresolved once the page exists, or resolved after it goes."""
    vault_module.batch_atomic_write(
        [planned(served, ALPHA, LINKING_ALPHA)], vault_root=served, post_commit_fanout=False
    )
    waiting = edge_targets(served, ALPHA)
    assert f"file:{GAMMA}" not in waiting

    vault_module.batch_atomic_write(
        [planned(served, GAMMA, GAMMA_TEXT, create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )
    assert f"file:{GAMMA}" in edge_targets(served, ALPHA)

    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(GAMMA),)),
    )
    assert edge_targets(served, ALPHA) == waiting
    assert not store_rows(served, "SELECT 1 FROM pages WHERE path = ?", GAMMA)


def test_a_batch_commits_its_pages_at_one_revision(served):
    """A reader between two pages of one batch: the pages commit together, at one revision."""
    start = revision(served)
    vault_module.batch_atomic_write(
        [
            planned(served, GAMMA, GAMMA_TEXT, create_only=True),
            planned(served, ALPHA, LINKING_ALPHA),
        ],
        vault_root=served,
        post_commit_fanout=False,
    )
    assert store_rows(
        served,
        "SELECT DISTINCT revision FROM store_audit WHERE path IN (?, ?) AND revision > ?",
        GAMMA,
        ALPHA,
        start,
    ) == [(start + 1,)]
    with read.snapshot(served) as view:
        assert view.page(GAMMA).body == GAMMA_TEXT.encode()
        assert view.page(ALPHA).body == LINKING_ALPHA.encode()


def test_a_stale_expected_hash_raises_the_file_mode_error_and_commits_nothing(served):
    """A caller that branches on the file-mode error and meets the store's own conflict class."""
    start = revision(served)
    with pytest.raises(vault_module.ContentHashMismatchError):
        vault_module.batch_atomic_write(
            [planned(served, ALPHA, LINKING_ALPHA, expected_hash=sha("not the stored bytes"))],
            vault_root=served,
            post_commit_fanout=False,
        )
    assert revision(served) == start
    assert bm25_paths(served, "Gamma") == []


def test_a_create_only_page_that_the_store_holds_raises_the_file_mode_error(served):
    """A create that overwrites a page that only the store holds, as its file does not exist."""
    vault_module.batch_atomic_write(
        [planned(served, GAMMA, GAMMA_TEXT, create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )
    start = revision(served)
    with pytest.raises(vault_module.CreateOnlyConflict):
        vault_module.batch_atomic_write(
            [planned(served, GAMMA, "# Another Gamma\n", create_only=True)],
            vault_root=served,
            post_commit_fanout=False,
        )
    assert revision(served) == start


def test_a_stale_guard_raises_the_guard_error_that_callers_retry_on(served, writer):  # noqa: F811
    """A guard error that changes its code in store mode, so a caller's retry branch never runs."""
    guard = vault_module.PathGuard.capture_store(served, ALPHA)
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "test", puts=(write.PagePut(ALPHA, b"# Alpha, changed\n"),)
        ),
    )
    start = revision(served)
    with pytest.raises(vault_module.PathGuardError) as refused:
        vault_module.batch_atomic_write(
            [planned(served, ALPHA, LINKING_ALPHA, guard=guard)],
            vault_root=served,
            post_commit_fanout=False,
        )
    assert refused.value.code == "PATH_GUARD_CONTENT"
    assert revision(served) == start


def test_a_page_changed_between_the_check_and_the_commit_commits_nothing(served, writer):  # noqa: F811
    """A stale batch that wins the race against a competing commit, as only the early check ran."""
    stored_alpha = read.owned_bytes(served, ALPHA)
    raced = []

    def competing_commit():
        # Runs after the batch's own check and before its commit, as another request's commit would.
        if not raced:
            raced.append(1)
            write.commit(
                writer,
                write.StoreChange(
                    write.Origin.GOVERNED, "race", puts=(write.PagePut(ALPHA, b"# Alpha, raced\n"),)
                ),
            )

    with pytest.raises(vault_module.PathGuardError) as refused:
        vault_module.batch_atomic_write(
            [
                planned(
                    served,
                    ALPHA,
                    LINKING_ALPHA,
                    expected_hash=hashlib.sha256(stored_alpha).hexdigest(),
                )
            ],
            vault_root=served,
            post_commit_fanout=False,
            _validate_prepared_bindings=competing_commit,
        )
    assert refused.value.code == "PATH_GUARD_CHANGED"
    assert read.owned_bytes(served, ALPHA) == b"# Alpha, raced\n"


def test_a_failure_while_deriving_keeps_the_page_and_reports_it_failed(served, writer, monkeypatch):  # noqa: F811
    """A page whose rows cannot be derived: the page stays, status names the failure, and a retry
    that succeeds makes the rows current."""

    def fail(*_arguments, **_keywords):
        raise RuntimeError("derivation failed")

    with monkeypatch.context() as patch:
        patch.setattr("exomem.lexstore.write_page_rows", fail)
        write.commit(
            writer,
            write.StoreChange(
                write.Origin.GOVERNED, "test", puts=(write.PagePut(GAMMA, GAMMA_TEXT.encode()),)
            ),
        )
        assert read.owned_bytes(served, GAMMA) == GAMMA_TEXT.encode()
        assert store_batch.row_report("upsert", [GAMMA], root=served).components[0].outcome == "deferred"
        for _retry in range(derivation.ATTEMPTS - 1):
            derivation.started(served.resolve(), writer)
            derivation.drain(served.resolve(), writer)
        figures = store_mode.status(served)
        assert (figures["derived"], figures["derived_failed"]) == ("failed", 1)
        assert figures["derived_failed_by_code"] == {derivation.FAILED: 1}
        report = store_batch.row_report("upsert", [GAMMA], root=served)
        assert {component.outcome for component in report.components} == {"failed"}
        assert bm25_paths(served, "harbour") == []

    derivation.started(served.resolve(), writer)
    derivation.drain(served.resolve(), writer)
    assert store_mode.status(served)["derived"] == "current"
    assert bm25_paths(served, "harbour") == [GAMMA]


@pytest.mark.parametrize("failing_step", ["bindings", "commit"])
def test_a_mixed_batch_that_fails_after_its_files_are_published_restores_them_and_can_retry(
    served, monkeypatch, failing_step
):
    """A retry that meets the files a failed batch left, and refuses them as already created."""
    payload = bytes(range(64))
    diagram = "Knowledge Base/Notes/second.bin"
    probe = "Outside/Probe.md"
    failing = [True]
    bindings_calls = []

    def bindings():
        bindings_calls.append(1)
        # The first call is the check before any file is published, the second the check after.
        if failing_step == "bindings" and failing[0] and len(bindings_calls) == 2:
            raise RuntimeError("a binding changed after publication")

    enqueue = derivation.enqueue

    def commit_step(*arguments, **keywords):
        if failing_step == "commit" and failing[0]:
            raise RuntimeError("the commit failed")
        return enqueue(*arguments, **keywords)

    monkeypatch.setattr(derivation, "enqueue", commit_step)

    def mixed_batch():
        return vault_module.batch_atomic_write(
            [
                planned(served, GAMMA, GAMMA_TEXT, create_only=True),
                vault_module.PlannedWrite(
                    served / diagram,
                    vault_module.PreparedBinaryContent(
                        io.BytesIO(payload), len(payload), hashlib.sha256(payload).hexdigest()
                    ),
                    create_only=True,
                    expected_hash=vault_module.MISSING_CONTENT_HASH,
                ),
                planned(served, probe, "# Probe\n\nA page outside the Knowledge Base folder.\n"),
            ],
            vault_root=served,
            post_commit_fanout=False,
            _validate_prepared_bindings=bindings,
        )

    before = tree(served)
    start = revision(served)
    with pytest.raises(RuntimeError):
        mixed_batch()
    assert tree(served) == before, "the files that the failed batch published must be restored"
    assert revision(served) == start
    assert not store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", GAMMA)

    failing[0] = False
    bindings_calls.clear()
    mixed_batch()
    assert (served / diagram).read_bytes() == payload
    assert (served / probe).exists()
    assert revision(served) > start
    assert bm25_paths(served, "harbour") == [GAMMA]


def test_a_write_with_no_service_refuses_and_a_read_does_not_need_one(stored):  # noqa: F811
    """A command line write that falls back to Markdown, because no thread holds the store."""
    with pytest.raises(OpError) as refused:
        runtime.route("create_note", stored, {}, write=True)
    assert refused.value.code == availability.SERVICE_REQUIRED
    assert runtime.route("find", stored, {}) is None


def test_a_batch_off_the_store_thread_refuses_and_changes_no_byte(stored):  # noqa: F811
    """A batch from a thread that does not hold the store, which would write Markdown it ignores."""
    before = tree(stored)
    with pytest.raises(OpError) as refused:
        vault_module.batch_atomic_write(
            [planned(stored, GAMMA, GAMMA_TEXT, create_only=True)], vault_root=stored
        )
    assert refused.value.code == availability.OFF_THREAD
    assert tree(stored) == before


@pytest.mark.parametrize(
    ("folder", "page", "refused"),
    [
        ("Knowledge Base/Notes", GAMMA, False),
        ("Outside", "Outside/Probe.md", True),
        ("Outside", GAMMA, True),
    ],
    ids=[
        "a folder that the store owns",
        "a folder that holds files on disk",
        "a folder that holds files on disk, with only a page in the batch",
    ],
)
def test_a_folder_census_binds_a_batch_only_where_the_batch_writes_on_disk(
    served, folder, page, refused
):
    """A census that stops binding a folder where files live on disk, or refuses a stored page."""
    (served / folder).mkdir(parents=True, exist_ok=True)
    census = vault_module.DirectoryCensusGuard.capture(served, folder, max_entries=100)
    (served / folder / "stray.txt").write_text("a file that appeared after the census\n")
    batch = [planned(served, page, GAMMA_TEXT, create_only=True)]
    if refused:
        with pytest.raises(vault_module.PathGuardError):
            vault_module.batch_atomic_write(
                batch, vault_root=served, required_guards=[census], post_commit_fanout=False
            )
        assert not (served / page).exists()
        with read.snapshot(served) as view:
            assert view.page(page) is None
    else:
        vault_module.batch_atomic_write(
            batch, vault_root=served, required_guards=[census], post_commit_fanout=False
        )
        with read.snapshot(served) as view:
            assert view.page(page) is not None


def test_a_fan_out_that_a_caller_runs_after_its_own_batch_refreshes_no_sidecar_in_store_mode(
    served, monkeypatch
):
    """File-mode rows written beside the store's, by a caller that fans out after its own batch."""
    refreshed = []
    monkeypatch.setattr(
        "exomem.index_sync.upsert_after_write", lambda *arguments, **keywords: refreshed.append(1)
    )
    assert vault_module.post_commit_batch_fanout(served, [served / ALPHA], None, None) is True
    assert refreshed == []


def test_a_batch_that_mixes_a_page_a_binary_and_a_file_the_store_does_not_own(served):
    """A mixed batch that loses a part: a binary that is not durable, or a file with no rows."""
    payload = bytes(range(64))
    diagram = "Knowledge Base/Notes/second.bin"
    content = vault_module.PreparedBinaryContent(
        io.BytesIO(payload), len(payload), hashlib.sha256(payload).hexdigest()
    )
    vault_module.batch_atomic_write(
        [
            planned(served, GAMMA, GAMMA_TEXT, create_only=True),
            vault_module.PlannedWrite(
                served / diagram,
                content,
                create_only=True,
                expected_hash=vault_module.MISSING_CONTENT_HASH,
            ),
            planned(served, OUTSIDE, "# Loose\n\nA page outside the Knowledge Base folder.\n"),
        ],
        vault_root=served,
        post_commit_fanout=False,
    )
    assert (served / diagram).read_bytes() == payload
    assert not (served / GAMMA).exists()
    assert (served / OUTSIDE).exists()
    with read.snapshot(served) as view:
        assert view.binary(diagram).sha256 == hashlib.sha256(payload).hexdigest()
        assert view.page(GAMMA) is not None
        assert view.page(OUTSIDE) is None
    assert store_rows(served, "SELECT 1 FROM pages WHERE path = ?", OUTSIDE)
    assert bm25_paths(served, "harbour") == [GAMMA]


def test_a_batch_of_files_the_store_does_not_hold_commits_through_the_file_writer(served):
    """A curation run's JSON file is no page, binary or Markdown file, so the store has no change
    for it and refuses an empty one: every curation run failed to seal in a store-mode vault."""
    run_file = "Knowledge Base/_Governance/curation/runs/run-1/plan.json"
    vault_module.batch_atomic_write(
        [planned(served, run_file, '{"version": 1}\n', create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )
    assert (served / run_file).read_text() == '{"version": 1}\n'
    assert not store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", run_file)


def apply_curation_run(root, proposed):
    """Approve a sealed run and call again until it is no longer executing."""
    result = commands.op_maintain_memory(
        root,
        mode="curation",
        curation_action="apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved in the test.",
    )
    while result["phase"] == "executing":
        result = commands.op_maintain_memory(
            root,
            mode="curation",
            curation_action="resume",
            run_id=proposed["run_id"],
            plan_id=proposed["plan_id"],
        )
    return result


def test_a_curation_plan_seals_and_completes_in_a_store_mode_vault(served):
    """Every sealing write of a run failed in store mode, so no plan could be proposed."""
    plan = {
        "version": 1,
        "title": "A plain note",
        "steps": [
            {
                "step_id": "note",
                "kind": "create-note",
                "args": {
                    "title": "Plain note",
                    "slug": "plain-note",
                    "content": "## Observations\n\n- [finding] A plain note. ^plain\n",
                    "relation_disposition": "reviewed_none",
                    "relation_review_reason": "No honest relation exists for this fixture.",
                },
            }
        ],
    }
    proposed = commands.op_maintain_memory(
        served, mode="curation", curation_action="propose", plan=plan
    )
    done = apply_curation_run(served, proposed)
    assert done["phase"] == "completed"
    with read.snapshot(served) as view:
        assert view.page(done["step"]["path"]) is not None


def test_a_relocation_of_two_pages_seals_and_completes_in_a_store_mode_vault(served):
    proposed = commands.op_maintain_memory(
        served,
        mode="curation",
        curation_action="relocate",
        pages=[ALPHA, BETA],
        to_project="meadow",
    )
    assert proposed["sealed"] is True
    assert apply_curation_run(served, proposed)["phase"] == "completed"
    with read.snapshot(served) as view:
        for rel in (ALPHA, BETA):
            assert "project: meadow" in view.page(rel).body.decode()


def test_a_binary_delete_that_the_disk_already_made_queues_no_removal(stored, writer):  # noqa: F811
    """A delete of a binary's record that also queues the removal of a file that is already gone,
    so that a file restored before the projector runs is taken away."""
    found = "Knowledge Base/Notes/found.bin"
    digest = hashlib.sha256(DIAGRAM_BYTES).hexdigest()
    change = write.StoreChange(
        write.Origin.GOVERNED, "test", binaries=(write.BinaryRecord(found, digest, 256),)
    )
    write.commit(writer, change)

    def drop(rel, **how):
        gone = write.StoreChange(
            write.Origin.GOVERNED, "test", binary_deletes=(write.BinaryDelete(rel, **how),)
        )
        write.commit(writer, gone)
        return store_rows(
            stored, "SELECT operation FROM store_projection_queue WHERE path = ?", rel
        )

    assert drop(found, on_disk=True) == [], "the file is gone, so nothing is left to remove"
    assert drop(DIAGRAM) == [("delete",)], "a governed delete still queues the file's removal"
    assert not store_rows(
        stored, "SELECT path FROM store_binaries WHERE path IN (?, ?)", found, DIAGRAM
    )
    assert store_rows(
        stored, "SELECT operation FROM store_audit WHERE path = ? ORDER BY seq", found
    ) == [("write",), ("delete",)]


def test_a_change_to_a_binary_whose_move_is_queued_refuses_and_keeps_its_only_copy(
    stored, writer  # noqa: F811
):
    """A change that touches a binary's queued move before its file moved, and so splits the move
    into a write that no projector can make and a delete that removes the only copy of the bytes."""
    moved = "Knowledge Base/Notes/moved.bin"
    digest = hashlib.sha256(DIAGRAM_BYTES).hexdigest()
    move = write.BinaryMove(DIAGRAM, moved, digest, len(DIAGRAM_BYTES))
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", binary_moves=(move,)))

    with pytest.raises(write.StoreConflict):
        write.commit(
            writer,
            write.StoreChange(
                write.Origin.GOVERNED, "test", binary_deletes=(write.BinaryDelete(moved),)
            ),
        )

    queued = "SELECT operation, from_path FROM store_projection_queue WHERE path = ?"
    assert store_rows(stored, queued, moved) == [("move", DIAGRAM)]
    assert (stored / DIAGRAM).read_bytes() == DIAGRAM_BYTES


def test_a_folder_and_a_name_that_only_the_store_holds_are_taken_not_new(served):
    """A page named over a stored page whose file is not written, or a second folder spelling."""
    stored_only = "Knowledge Base/Stored Only/Gamma.md"
    vault_module.batch_atomic_write(
        [planned(served, stored_only, GAMMA_TEXT, create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )
    assert not (served / "Knowledge Base/Stored Only").exists()
    folder = served / "Knowledge Base/Stored Only"
    assert vault_module.unique_path(folder, "Gamma", ".md", vault_root=served).name == "Gamma-2.md"
    assert (
        vault_module.respell_new_path(served, "Knowledge Base/stored only/Delta.md")
        == "Knowledge Base/Stored Only/Delta.md"
    )


@pytest.mark.skipif(sys.platform in {"win32", "darwin"}, reason="needs a case-sensitive volume")
def test_a_page_written_into_a_split_lands_in_the_fuller_stored_spelling(served):
    """Defect: the store walk accepts an exact folder and keeps filling the smaller side of a split."""
    for stem in ("Gamma", "Delta"):
        vault_module.batch_atomic_write(
            [planned(served, f"Knowledge Base/Notes/Stored Only/{stem}.md", GAMMA_TEXT, create_only=True)],
            vault_root=served,
            post_commit_fanout=False,
        )
    loose = served / "Knowledge Base/Notes/stored only"
    loose.mkdir(parents=True)
    (loose / "chart.png").write_bytes(b"png")

    assert (
        vault_module.respell_new_path(served, "Knowledge Base/Notes/stored only/notes/Epsilon.md")
        == "Knowledge Base/Notes/Stored Only/notes/Epsilon.md"
    )


@pytest.mark.skipif(sys.platform in {"win32", "darwin"}, reason="needs a case-sensitive volume")
def test_a_stored_split_is_merged_into_its_canonical_spelling(served):
    """Defect: the merge reads only the disk and misses pages that only the store holds."""
    from exomem import path_variants

    for stem in ("Gamma", "Delta"):
        vault_module.batch_atomic_write(
            [planned(served, f"Knowledge Base/Notes/Stored Only/{stem}.md", GAMMA_TEXT, create_only=True)],
            vault_root=served,
            post_commit_fanout=False,
        )
    vault_module.batch_atomic_write(
        [planned(served, "Knowledge Base/Notes/stored only/Epsilon.md", GAMMA_TEXT, create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )

    preview = path_variants.preview(served)
    assert [unit["from"] for unit in preview["moves"]] == ["Knowledge Base/Notes/stored only/Epsilon.md"]
    result = path_variants.apply(served, plan_id=preview["plan_id"], why="merge the stored split")

    assert result["moved"] == [
        {"from": "Knowledge Base/Notes/stored only/Epsilon.md", "to": "Knowledge Base/Notes/Stored Only/Epsilon.md"}
    ]
    rows = store_rows(served, "SELECT path FROM store_pages WHERE path LIKE ?", "Knowledge Base/Notes/%Epsilon.md")
    assert rows == [("Knowledge Base/Notes/Stored Only/Epsilon.md",)]


def test_the_rows_that_governed_commands_commit_equal_a_cold_derivation_from_the_files(
    served, tmp_path
):
    """Rows that a commit derives from its own bytes and that differ from a cold derivation:
    a link that resolves to another page, a stale title, a missing identity."""
    first = commands.op_remember(
        served,
        title="Heron survey",
        status="draft",
        content="## Observations\n\n- [finding] Herons nest beside [[Alpha]].\n",
        summary="A survey finding for the test rig.",
    )["path"]
    commands.op_remember(
        served,
        title="Wader census",
        status="draft",
        content="## Observations\n\n- [finding] Waders follow the [[Heron survey]].\n",
        summary="A census finding for the test rig.",
    )
    # The edit commands read a page from disk, which the readers' task changes: edit by batch.
    with read.snapshot(served) as view:
        body = view.page(first).body.decode()
    vault_module.batch_atomic_write(
        [planned(served, first, body.replace("nest beside", "nest near"), expected_hash=sha(body))],
        vault_root=served,
        post_commit_fanout=False,
    )
    # The projector's step, run by hand: each stored page becomes its file, with its stored mtime.
    with read.snapshot(served) as view:
        for meta in view.pages():
            target = projection.write_file(
                served, meta.path, view.page(meta.path).body, durable=False
            )
            os.utime(target, ns=(meta.mtime_ns, meta.mtime_ns))
    expected = tmp_path / "expected.sqlite"
    verify.derive_expected(served, expected)
    with closing(sqlite3.connect(connection.store_path(served))) as store:
        comparison = verify.compare_tables(store, expected)
    assert comparison.tables, "the comparison must cover tables for this check to mean anything"
    assert comparison.differences == ()
