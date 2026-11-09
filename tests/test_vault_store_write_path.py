"""A governed write to a store-mode vault commits in the store and writes no Markdown.

OpenSpec make-vault-store-canonical tasks 2.2 and 2.3. The vault is the migration suite's generated
vault in store mode, and the writer is bound as the store thread binds it (`require_servable` still
refuses a store-mode service start, so no service holds the store here). The projector is off:
nothing writes a stored page to disk, so every answer below comes from the store.
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
from test_vault_store_migration import ALPHA, vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401

from exomem import bm25, commands, lexstore
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection, preview, runtime
from exomem.vault_store import availability, projection, read, verify, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

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


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_create_note_is_searchable_and_linked_when_it_returns_and_writes_no_markdown(served):
    """A write acknowledged before its rows are: the note returns and a search misses it."""
    before = tree(served)
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


def test_a_batch_commits_the_pages_and_their_rows_at_one_revision(served):
    """A reader between a page and its rows: the revision moves once for the whole batch."""
    start = revision(served)
    vault_module.batch_atomic_write(
        [
            planned(served, GAMMA, GAMMA_TEXT, create_only=True),
            planned(served, ALPHA, LINKING_ALPHA),
        ],
        vault_root=served,
        post_commit_fanout=False,
    )
    assert revision(served) == start + 1
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


def test_a_failure_while_deriving_rolls_back_the_page_and_the_revision(served, writer, monkeypatch):  # noqa: F811
    """A page that is stored and has no rows, as the derivation failed after the page was put."""
    start = revision(served)

    def fail(*_arguments, **_keywords):
        raise RuntimeError("derivation failed")

    monkeypatch.setattr("exomem.lexstore.write_page_rows", fail)
    with pytest.raises(RuntimeError, match="derivation failed"):
        write.commit(
            writer,
            write.StoreChange(
                write.Origin.GOVERNED, "test", puts=(write.PagePut(GAMMA, GAMMA_TEXT.encode()),)
            ),
        )
    assert revision(served) == start
    assert not store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", GAMMA)


@pytest.mark.parametrize("failing_step", ["bindings", "derivation"])
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

    write_page_rows = lexstore.write_page_rows

    def derive(*arguments, **keywords):
        if failing_step == "derivation" and failing[0]:
            raise RuntimeError("derivation failed")
        return write_page_rows(*arguments, **keywords)

    monkeypatch.setattr("exomem.lexstore.write_page_rows", derive)

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
    assert revision(served) == start + 1
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
