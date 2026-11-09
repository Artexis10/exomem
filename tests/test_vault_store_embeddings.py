"""A store-mode vault's vectors: computed after commit from the store's bytes, stamped, and gated.

OpenSpec make-vault-store-canonical, task 6.1. The vault is the migration suite's generated
vault, moved into store mode by ``store-mode migrate``; the writer is a real store writer on the
migrated store. The encoder is a deterministic hash of the words of a text, so nothing downloads a
model. The drain runs here by direct calls, as the semantic drain's turn calls it. The interpreter
that dies in the resume case is a real child process, killed by the operating system.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
import signal
import sqlite3
import sys
import time
import zlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import test_vault_store_migration as migration
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA

from exomem import (
    embeddings,
    find,
    find_candidates,
    index_paths,
    lexstore,
    readiness,
    recall_space,
    semantic_drain,
)
from exomem import vault as vault_module
from exomem.embedding_index import (
    STORE_CURSOR_REVISION,
    STORE_CURSOR_SEQ,
    EmbeddingIndex,
    StoreStamp,
)
from exomem.ranking_config import DEFAULT_RANKING
from exomem.vault_store import mode, read, vectors, write

# The store-mode vault and its writer are that suite's fixtures; a name here is how pytest finds
# them.
vault = migration.vault
stored = read_write.stored
writer = read_write.writer
put = read_write.put
served = write_path.served

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

MOVED = "Knowledge Base/Notes/Moved.md"
HARBOUR = (
    b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\n"
    b"The lighthouse keeper now answers the harbour pilot.\n"
)
POISON = "---\ntitle: Alpha\n---\n# Alpha\n\nPoisoned projection about a volcano observatory.\n"


class Encoder:
    """Hashes each word of a text into the vector, so texts that share words score alike.

    ``texts`` records every text that reached the model, which is how a case sees what was encoded
    and what was reused.
    """

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.refuse: str | None = None

    def __call__(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        self.texts.extend(texts)
        out = np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            if self.refuse is not None and self.refuse in text:
                raise RuntimeError("the encoder refused this text")
            for word in text.lower().split():
                out[row, zlib.crc32(word.encode()) % embeddings.VECTOR_DIM] += 1.0
            out[row] /= max(float(np.linalg.norm(out[row])), 1.0)
        return out


def install_encoder(monkeypatch) -> Encoder:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    model = SimpleNamespace(texts_fit=lambda _texts: True)
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", lambda: model)
    readiness.reset()
    encoder = Encoder()
    monkeypatch.setattr(embeddings, "embed_texts", encoder)
    return encoder


@pytest.fixture
def encoder(monkeypatch):
    yield install_encoder(monkeypatch)
    readiness.reset()


def catch_up(root, **limits):
    """Drain until the sidecar is level with the store, and say what the calls did together."""
    total = {"embedded": 0, "purged": 0, "owed": 0}
    for _ in range(50):
        drained = vectors.drain(root, **limits)
        assert drained.blocked is None
        for name in total:
            total[name] += getattr(drained, name)
        if not drained.more:
            return SimpleNamespace(**total)
    raise AssertionError("the drain never reached the end of the store")


def index_of(root) -> EmbeddingIndex:
    return embeddings.get_embedding_index(root)


def hit_paths(root, query: str) -> list[str]:
    """The pages whose chunks the vector lane returns for `query`, best first."""
    hits = index_of(root).search(Encoder()([query], is_query=True)[0], 50)
    return list(dict.fromkeys(path for path, _chunk, _text, _score in hits))


def chunk_texts(root, rel: str) -> list[str]:
    with closing(sqlite3.connect(index_of(root).path)) as database:
        return [
            text
            for (text,) in database.execute(
                "SELECT chunk_text FROM chunks WHERE file_path = ? ORDER BY chunk_idx", (rel,)
            )
        ]


def sidecar_state(root):
    """The chunk rows, the unit-row counts and the stamps: what a resumed drain must reproduce."""
    with closing(sqlite3.connect(index_of(root).path)) as database:
        return (
            database.execute(
                "SELECT file_path, chunk_idx, chunk_text, vector, file_mtime"
                " FROM chunks ORDER BY 1, 2"
            ).fetchall(),
            database.execute(
                "SELECT parent_path, COUNT(*) FROM semantic_unit_vectors GROUP BY 1 ORDER BY 1"
            ).fetchall(),
            database.execute(
                "SELECT file_path, sha256, revision, owed FROM store_page_vectors ORDER BY 1"
            ).fetchall(),
        )


def stored_sha(root, rel: str) -> str:
    with read.snapshot(root) as view:
        return view.page_meta(rel).sha256


def commit(writer_, body: bytes, rel: str = ALPHA) -> int:
    return write.commit(writer_, put(rel, body)).revision


def test_a_committed_edit_is_embedded_from_the_store_and_stamped_with_its_hash_and_revision(
    stored, writer, encoder
):
    """Vector rows that do not say which bytes they came from: a reader cannot tell a current page
    from a stale one, and the revision shows nothing about how far the drain has caught up."""
    catch_up(stored)
    revision = commit(writer, HARBOUR)

    drained = catch_up(stored)

    stamp = index_of(stored).store_stamps([ALPHA])[ALPHA]
    assert (stamp.sha256, stamp.revision, stamp.owed) == (
        hashlib.sha256(HARBOUR).hexdigest(),
        revision,
        False,
    )
    assert drained.embedded == 1, "only the edited page needed new rows"
    assert any("harbour pilot" in text for text in chunk_texts(stored, ALPHA))


def test_the_first_pass_stamps_every_embeddable_page_once_and_then_goes_quiet(stored, encoder):
    """A first pass that leaves pages without a stamp (never served) or that embeds the vault again
    on every turn."""
    first = catch_up(stored)
    encoded_by_first_pass = len(encoder.texts)
    second = catch_up(stored)

    stamps = index_of(stored).store_stamps()
    assert ALPHA in stamps and BETA in stamps
    assert all(
        stamp.sha256 == stored_sha(stored, rel) and not stamp.owed for rel, stamp in stamps.items()
    )
    assert index_of(stored).store_cursor() is not None
    assert first.embedded == len(stamps) and encoded_by_first_pass > 0
    assert (second.embedded, second.purged, len(encoder.texts)) == (0, 0, encoded_by_first_pass)


def test_before_the_drain_runs_the_vector_lane_leaves_out_the_edited_page_and_lexical_find_keeps_it(
    stored, writer, encoder
):
    """A stale vector served for bytes that the page no longer holds, or a page that disappears from
    find altogether because its vectors lag."""
    catch_up(stored)
    assert ALPHA in hit_paths(stored, "lighthouse keeper tide")
    commit(writer, HARBOUR)

    assert ALPHA not in hit_paths(stored, "lighthouse keeper tide")
    assert BETA in hit_paths(stored, "lighthouse keeper tide"), "the pages that did not change stay"
    lexical = lexstore.search_bm25(stored, "lighthouse", 5, scope="kb", repair=False)
    assert ALPHA in [path for path, _score in lexical]

    catch_up(stored)
    assert ALPHA in hit_paths(stored, "lighthouse keeper harbour pilot")


def test_the_drain_embeds_the_stored_bytes_when_the_projected_markdown_is_poisoned(
    stored, writer, encoder
):
    """A drain that reads the projection, which lags the store and which an outside edit can change,
    so the vectors describe text that the store does not hold."""
    catch_up(stored)
    commit(writer, HARBOUR)
    (stored / ALPHA).write_text(POISON)

    catch_up(stored)

    assert any("harbour pilot" in text for text in chunk_texts(stored, ALPHA))
    assert not any("volcano" in text for text in encoder.texts)
    assert (
        index_of(stored).store_stamps([ALPHA])[ALPHA].sha256 == hashlib.sha256(HARBOUR).hexdigest()
    )


def test_a_deleted_page_loses_its_vectors_and_its_stamp(stored, writer, encoder):
    """A deleted page that stays in the vector lane, so find returns a page that is not there."""
    catch_up(stored)
    assert chunk_texts(stored, BETA)
    write.commit(
        writer, write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(BETA),))
    )

    drained = catch_up(stored)

    assert drained.purged == 1
    assert chunk_texts(stored, BETA) == []
    assert BETA not in index_of(stored).store_stamps()
    assert BETA not in hit_paths(stored, "lighthouse keeper tide")


def test_a_moved_page_takes_its_vectors_to_the_new_path_without_a_model_call(
    stored, writer, encoder
):
    """A move that leaves the old path's vectors behind or encodes the page again, so each rename
    costs the model the whole page and find returns a path that no longer exists."""
    catch_up(stored)
    old_texts = chunk_texts(stored, BETA)
    seen = len(encoder.texts)
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "tidy", moves=(write.PageMove(BETA, MOVED),)),
    )

    catch_up(stored)

    assert chunk_texts(stored, BETA) == []
    assert chunk_texts(stored, MOVED) == old_texts
    stamps = index_of(stored).store_stamps()
    assert BETA not in stamps and stamps[MOVED].sha256 == stored_sha(stored, MOVED)
    assert len(encoder.texts) == seen, "the new path reused the old path's vectors by text"


def test_a_page_whose_encode_fails_is_withheld_then_retried_once_the_encoder_recovers(
    stored, writer, encoder
):
    """A failed page that the cursor skips for good (never searchable by meaning), or one that the
    reader serves from rows that no longer match its bytes."""
    catch_up(stored)
    commit(writer, HARBOUR)
    encoder.refuse = "harbour"

    first = catch_up(stored)

    assert first.owed == 1
    assert index_of(stored).store_stamps([ALPHA])[ALPHA].owed
    assert ALPHA not in hit_paths(stored, "lighthouse keeper tide"), "the old rows are withheld"
    assert index_of(stored).store_cursor() is not None, (
        "one failed page does not hold the rest back"
    )

    encoder.refuse = None
    catch_up(stored)
    assert not index_of(stored).store_stamps([ALPHA])[ALPHA].owed
    assert ALPHA in hit_paths(stored, "lighthouse keeper harbour pilot")


def test_rows_that_a_foreign_writer_rewrote_are_withheld_even_though_the_cursor_passed_them(
    stored, encoder
):
    """A file-mode writer that rebuilds rows from a lagging projection, after the cursor has
    passed the page: its rows would be served as proven because the page looks caught up."""
    catch_up(stored)
    index = index_of(stored)
    identity = index.identity
    index.upsert_batch(
        [(ALPHA, ["a chunk of the projection"], encoder(["a chunk of the projection"]), 1.0)],
        identity=identity,
    )

    assert index.store_stamps([ALPHA])[ALPHA].owed
    assert ALPHA not in hit_paths(stored, "a chunk of the projection")

    catch_up(stored)
    assert chunk_texts(stored, ALPHA) != ["a chunk of the projection"]
    assert ALPHA in hit_paths(stored, "lighthouse keeper tide")


def test_rows_from_before_the_first_pass_are_not_served_until_the_pass_stamps_them(stored, encoder):
    """A sidecar that a file-mode vault filled before it migrated: its rows carry no stamp, and a
    reader that serves what it cannot prove would serve them from the first query."""
    catch_up(stored)
    with closing(sqlite3.connect(index_of(stored).path)) as database, database:
        database.execute("DELETE FROM store_page_vectors")
        database.execute(
            "DELETE FROM meta WHERE key IN (?, ?)", (STORE_CURSOR_REVISION, STORE_CURSOR_SEQ)
        )
    assert index_of(stored).store_cursor() is None
    assert ALPHA not in hit_paths(stored, "lighthouse keeper tide")

    catch_up(stored)

    assert ALPHA in hit_paths(stored, "lighthouse keeper tide")


def test_a_bounded_turn_leaves_the_cursor_behind_the_pages_it_did_not_reach(stored, encoder):
    """A cursor that jumps over the pages a turn's budget cut off, so they are never embedded."""
    first = vectors.drain(stored, pages=1, publication=1)

    assert first.more and first.embedded == 1
    assert index_of(stored).store_cursor() is None

    catch_up(stored, pages=1, publication=1)
    assert index_of(stored).store_cursor() is not None


def test_the_service_turn_drains_a_store_mode_vault_without_a_receipt(stored, writer, encoder):
    """A semantic drain that waits for a receipt, which a store-mode commit never queues, so no
    vector row is ever written."""
    drain = semantic_drain.SemanticDrain(stored)

    for _ in range(50):
        progressed, _interval = drain._turn()
        if not progressed:
            break
    commit(writer, HARBOUR)
    for _ in range(50):
        progressed, _interval = drain._turn()
        if not progressed:
            break

    assert (
        index_of(stored).store_stamps([ALPHA])[ALPHA].sha256 == hashlib.sha256(HARBOUR).hexdigest()
    )


def drain_and_die(environment, publications):
    """`vectors.drain` in a fresh interpreter that the operating system kills after a publish."""
    os.environ.clear()
    os.environ.update(environment)
    patch = pytest.MonkeyPatch()
    install_encoder(patch)
    published = []
    upsert = EmbeddingIndex.upsert_batch

    def upsert_then_die(self, *arguments, **keywords):
        token = upsert(self, *arguments, **keywords)
        published.append(token)
        if len(published) == publications:
            os.kill(os.getpid(), signal.SIGKILL)
        return token

    EmbeddingIndex.upsert_batch = upsert_then_die
    vectors.drain(Path(environment["EXOMEM_VAULT_PATH"]), publication=1)
    os._exit(0)


def test_a_drain_killed_between_publications_resumes_with_the_rows_of_an_uninterrupted_one(
    stored, encoder
):
    """A restart that begins again (re-embedding what the dead drain finished) or that skips a page
    whose rows the dead drain never wrote: the rows must equal those of a drain that never died."""
    catch_up(stored)
    reference = sidecar_state(stored)
    pages = len(index_of(stored).store_stamps())
    for suffix in ("", "-wal", "-shm"):
        path = index_of(stored).path
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    embeddings._INDEX_CACHE.clear()

    child = multiprocessing.get_context("spawn").Process(
        target=drain_and_die, args=(dict(os.environ), 2)
    )
    child.start()
    child.join(180)
    assert child.exitcode == -signal.SIGKILL
    done_before_the_kill = set(index_of(stored).store_stamps())
    assert len(done_before_the_kill) == 2 and len(done_before_the_kill) < pages
    assert index_of(stored).store_cursor() is None, "the census was not finished"

    encoder.texts.clear()
    resumed = catch_up(stored)

    assert resumed.embedded == pages - 2
    assert sidecar_state(stored) == reference
    reembedded_by_the_dead_drain = [
        rel
        for rel in done_before_the_kill
        if any(text in encoder.texts for text in chunk_texts(stored, rel))
    ]
    assert reembedded_by_the_dead_drain == [], "the pages that the dead drain finished were kept"


def note(title: str, text: str) -> bytes:
    return f"---\ntitle: {title}\ntype: note\n---\n# {title}\n\n{text}\n".encode()


def foreign_rows(index: EmbeddingIndex, rel: str, text: str) -> None:
    """Rows for `rel` as a writer other than the store drain leaves them: no stamp."""
    vector = Encoder()([text])
    index.upsert_batch([(rel, [text], vector, 1.0)], identity=index.identity)


def sidecar_execute(root, sql: str, *arguments) -> None:
    with closing(sqlite3.connect(index_of(root).path)) as database, database:
        database.execute(sql, arguments)


def unreadable_mode(_root):
    raise OSError("the mode record is unreadable")


def test_a_census_keeps_the_rows_of_files_the_store_does_not_own_and_drops_a_gone_pages(
    stored, encoder
):
    """A census that deletes the vectors of notes in a dot-folder or outside the Knowledge Base:
    the file-mode walk embedded them and store mode never embeds them again, so find loses them."""
    unowned = ("Knowledge Base/.drafts/Outside.md", "Elsewhere/Outside.md")
    gone = "Knowledge Base/Notes/Gone.md"
    catch_up(stored)
    index = index_of(stored)
    for rel in (*unowned, gone):
        foreign_rows(index, rel, "outside lantern text")
    sidecar_execute(
        stored, "DELETE FROM meta WHERE key IN (?, ?)", STORE_CURSOR_REVISION, STORE_CURSOR_SEQ
    )

    served = hit_paths(stored, "outside lantern text")
    assert all(rel in served for rel in unowned), "the census gate does not withhold them"
    assert gone not in served, "a page the store does not hold has no proof"

    drained = catch_up(stored)

    assert drained.purged == 1
    assert chunk_texts(stored, gone) == []
    assert all(chunk_texts(stored, rel) == ["outside lantern text"] for rel in unowned)


def test_find_reports_the_unit_lane_unavailable_when_the_store_cannot_say_what_to_trust(
    stored, encoder, monkeypatch
):
    """A store that cannot be read withholds every page, and the unit lane then answers with
    nothing: `find` must call that unproved coverage, never a proved miss."""
    catch_up(stored)
    monkeypatch.setattr(mode, "store_mode_for_reads", unreadable_mode)

    hits, profile, _scope = find._vector_unit_candidates(
        stored,
        query="lighthouse keeper",
        candidate_limit=5,
        allowed_unit_refs=None,
        allowed_parent_paths={ALPHA, BETA},
        degraded_out=[],
        failed_out=[],
        timings=None,
    )

    assert hits == []
    assert (profile["status"], profile["reason"]) == ("unavailable", "coverage_incomplete")


def test_an_unreadable_store_logs_once_until_the_state_changes(stored, encoder, caplog):
    """A warning per vector query, which fills the log for as long as the store stays unreadable."""
    catch_up(stored)
    index = index_of(stored)

    def withheld_everything_while_unreadable(queries: int) -> int:
        caplog.clear()
        with pytest.MonkeyPatch.context() as broken, caplog.at_level(logging.WARNING):
            broken.setattr(mode, "store_mode_for_reads", unreadable_mode)
            for _ in range(queries):
                assert vectors.withheld(stored, index).everything
        return sum("withheld" in record.getMessage() for record in caplog.records)

    assert withheld_everything_while_unreadable(3) == 1
    assert not vectors.withheld(stored, index), "readable again"
    assert withheld_everything_while_unreadable(2) == 1, "a new outage is logged again"


def test_a_file_mode_vault_runs_its_drain_turn_when_its_mode_record_is_unreadable(
    vault, monkeypatch
):
    """A file-mode vault whose drain turn dies with its mode probe: nothing embeds it again."""
    monkeypatch.setattr(mode, "store_mode_for_reads", unreadable_mode)

    def the_store_turn(*_arguments, **_keywords):
        raise AssertionError("a vault outside store mode took the store turn")

    monkeypatch.setattr(vectors, "drain", the_store_turn)

    assert vectors.in_store_mode(vault) is False
    semantic_drain.SemanticDrain(vault)._turn()


def test_a_store_mode_vault_with_an_unreadable_mode_record_fails_its_turn(
    stored, encoder, monkeypatch
):
    """A vault the store says is in store mode, run as a file-mode vault on a mode record it
    cannot read: the file turn would embed the lagging projection."""
    monkeypatch.setattr(mode, "store_mode_for_reads", unreadable_mode)

    with pytest.raises(OSError, match="mode record"):
        semantic_drain.SemanticDrain(stored)._turn()


def test_a_cursor_beyond_the_audit_trail_makes_the_drain_compare_every_page(stored, encoder):
    """A sidecar that another store left behind keeps a cursor that this store's audit trail never
    reaches: followed, it would show the drain no page and every row would stay unproven."""
    catch_up(stored)
    index = index_of(stored)
    cursor = index.store_cursor()
    sidecar_execute(stored, "DELETE FROM store_page_vectors WHERE file_path = ?", ALPHA)
    index.set_store_cursor(cursor[0], cursor[1] + 1000)
    assert ALPHA not in hit_paths(stored, "lighthouse keeper tide")

    drained = catch_up(stored)

    assert drained.embedded == 1
    assert ALPHA in hit_paths(stored, "lighthouse keeper tide")
    assert index.store_cursor() == cursor


def test_a_page_that_keeps_failing_does_not_keep_the_pages_behind_it_owed(stored, writer, encoder):
    """A retry that takes the same four owed pages each turn: pages that fail for good, ahead of a
    page that would succeed, keep it owed (and withheld) for ever."""
    stubborn = [f"Knowledge Base/Notes/Aa{number}.md" for number in range(4)]
    fine = "Knowledge Base/Notes/Zz.md"
    catch_up(stored)
    encoder.refuse = "kettle"
    for rel in stubborn:
        commit(writer, note(rel, "stubborn kettle"), rel)
    commit(writer, note("Zz", "fine kettle"), fine)
    catch_up(stored)
    assert index_of(stored).store_stamps([fine])[fine].owed
    encoder.refuse = "stubborn"

    first = vectors.drain(stored)
    second = vectors.drain(stored)

    assert not index_of(stored).store_stamps([fine])[fine].owed
    assert (first.more, second.more) == (False, True), "more owed pages wait after a gain"


def test_a_sidecar_gets_its_stamp_table_with_its_first_stamp_and_not_before(vault, encoder):
    """A file-mode sidecar that carries stamp triggers: every row insert and delete of its rebuilds
    runs a lookup that can never match."""
    index = index_of(vault)
    identity = recall_space.current_identity(embeddings.VECTOR_DIM)
    index.upsert_batch([(ALPHA, ["a chunk"], encoder(["a chunk"]), 1.0)], identity=identity)

    with closing(sqlite3.connect(index.path)) as database:
        objects = database.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'store_page_vectors%'"
        ).fetchall()
    assert objects == []
    assert index.store_stamps() == {} and index.store_owed_paths(4) == []
    assert ALPHA in index.row_paths()
    assert index.purge_paths_if_present([ALPHA]) == 1

    index.upsert_batch([(ALPHA, ["a chunk"], encoder(["a chunk"]), 1.0)], identity=identity)
    index.record_store_stamps({ALPHA: StoreStamp("a" * 64, 7)})
    assert index.store_stamps([ALPHA])[ALPHA] == StoreStamp("a" * 64, 7)
    index.upsert_batch([(ALPHA, ["another"], encoder(["another"]), 1.0)], identity=identity)
    assert index.store_stamps([ALPHA])[ALPHA].owed, "the stamp table now watches the rows"


def test_the_store_drain_embeds_the_pages_that_the_file_mode_walk_embeds(vault):
    """A path rule that drifts from the walk: store mode would embed pages that search never
    reaches, or leave out pages that it does."""
    names = [
        "Notes/Plain.md",
        "Notes/Deep/Deeper/Plain.md",
        ".drafts/Hidden.md",
        "_trash/Gone.md",
        "_archive/Old.md",
        "_attachments/Note.md",
        "_Governance/Policy.md",
        ".exomem-batch-1/Part.md",
        "Notes/Plain.sync-conflict-20260101-ab.md",
        "Notes/index.md",
        "Notes/readme.txt",
    ]
    knowledge_base = vault / "Knowledge Base"
    for name in names:
        (knowledge_base / name).parent.mkdir(parents=True, exist_ok=True)
        (knowledge_base / name).write_text("---\ntitle: T\n---\n# T\n\nA page.\n")
    # The walk's callers apply `is_embeddable_path` to what it yields, as the file-mode drain does.
    walked = {
        Path(path).relative_to(vault).as_posix()
        for path in index_paths.iter_index_markdown(vault)
        if index_paths.is_embeddable_path(Path(path))
    }

    disagreements = {
        name: (f"Knowledge Base/{name}" in walked, vectors.embeddable(f"Knowledge Base/{name}"))
        for name in names
        if (f"Knowledge Base/{name}" in walked) != vectors.embeddable(f"Knowledge Base/{name}")
    }

    assert disagreements == {}


def test_find_reports_the_chunk_lane_degraded_when_the_store_cannot_say_what_to_trust(
    stored, encoder, monkeypatch
):
    """A store that cannot be read withholds every page's vectors, and the chunk lane then answers
    with nothing: `find` must not report that as the lane searching and finding no match."""
    catch_up(stored)

    def lane(degraded: list[str]):
        return find_candidates.collect_candidates(
            stored, query="lighthouse keeper", query_norm="lighthouse keeper", limit=10,
            scope="kb", mode="vector", graph=False, temporal=False, intent="conceptual",
            prefer_compiled=False, prefer_active=False, prefer_used=False,
            config=DEFAULT_RANKING, timings=None, snapshot=find.FreshnessSnapshot(stored),
            page_of=lambda _path: None,
            keyword_match_paths=lambda *_a, **_k: [],
            outbound_wikilink_paths=lambda *_a, **_k: [],
            get_query_resolver=lambda *_a, **_k: None, record_degradation=lambda _lane: None,
            degraded_out=degraded, failed_out=[], recall_paths={ALPHA, BETA}, capture_trace=True,
        )

    readable = lane([])
    assert readable.lane_statuses["vector"]["status"] == "participated"

    monkeypatch.setattr(vectors, "in_store_mode", unreadable_mode)
    degraded: list[str] = []
    unproved = lane(degraded)

    assert unproved.vector_ranking == []
    assert unproved.lane_statuses["vector"]["status"] == "unavailable"
    assert unproved.lane_statuses["vector"]["reason"] == "coverage_incomplete"
    assert degraded == ["embeddings"]


def wait_until(condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.05)


def test_a_committed_write_wakes_the_idle_drain_instead_of_waiting_for_its_poll(
    served, encoder, monkeypatch
):
    """A write whose vectors wait out the drain's idle poll: until then the vector lane leaves the
    page out. The poll is far longer than this test, so only a wake can bring the drain in."""
    monkeypatch.setattr(semantic_drain, "IDLE_POLL_SECONDS", 600.0)
    turns = []
    drain_once = vectors.drain

    def counted(*arguments, **keywords):
        turns.append(drain_once(*arguments, **keywords))
        return turns[-1]

    monkeypatch.setattr(vectors, "drain", counted)
    drain = semantic_drain.SemanticDrain(served).start()
    try:
        wait_until(lambda: turns and not turns[-1].more)  # the first pass is done: it sleeps now
        # No fan-out, so the wake is the commit's own and not a receipt's.
        vault_module.batch_atomic_write(
            [write_path.planned(served, ALPHA, HARBOUR.decode())],
            vault_root=served,
            post_commit_fanout=False,
        )
        wait_until(
            lambda: (stamp := index_of(served).store_stamps([ALPHA]).get(ALPHA)) is not None
            and stamp.sha256 == hashlib.sha256(HARBOUR).hexdigest()
        )
    finally:
        drain.stop()
