"""A revised page stays nameable (close-memory-loop, activation quality).

The carry admits a page on a phrase of words DISTINCTIVE in the corpus, and
counts distinctiveness as the number of pages a word occurs on. A page revised
three times is the same subject written four times: its retired revisions carry
its name words too, so the words stop counting as distinctive and the current
page can no longer be named by them, however plainly the turn says them.
Retired revisions, by a retiring status or by a `superseded_by` pointer, are
not counted, exactly as the carry already never serves them
(`working_set._is_current_page`). Invented names only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_carry import _write, seed_ordinary_notes
from test_working_set_index import _seed_planning, _seed_structure

from exomem import lexstore, working_set, working_set_index, working_set_runtime

RESEARCH = "Knowledge Base/Notes/Research"
CURRENT = f"{RESEARCH}/brask-ferry-timetable.md"


def _revision(name: str, *, status: str, body: str, successor: str | None) -> str:
    successor_line = f"superseded_by: [{successor}]\n" if successor else ""
    return (
        f"---\ntype: research-note\nstatus: {status}\nupdated: 2026-09-01\n"
        f"{successor_line}---\n\n# Brask ferry timetable ({name})\n\n## Summary\n\n"
        f"- [decision] The brask ferry timetable {body}\n"
    )


def _seed(vault: Path, *, retired: int, retired_status: str = "superseded") -> None:
    _seed_structure(vault)
    _seed_planning(vault)
    kb = vault / "Knowledge Base"
    _write(
        vault / CURRENT,
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-20\n---\n\n"
        "# Brask ferry timetable\n\n## Summary\n\n"
        "- [decision] The brask ferry timetable now runs hourly from the north pier.\n",
    )
    for index in range(retired):
        _write(
            kb / "Notes" / "Research" / f"brask-ferry-timetable-v{index + 1}.md",
            _revision(
                f"v{index + 1}",
                status=retired_status,
                body=f"ran every {index + 2} hours from the south pier.",
                successor=CURRENT,
            ),
        )
    seed_ordinary_notes(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


TURN = "What is the current brask ferry timetable?"


def test_a_page_revised_three_times_is_still_carried_by_its_name(vault: Path) -> None:
    _seed(vault, retired=3)

    hits, state = working_set_runtime.carry_candidates(vault, TURN)

    assert state == "available"
    assert [path for path, _score in hits] == [CURRENT]
    assert working_set.dominant_carry(hits) is not None


def test_revisions_retired_only_by_superseded_by_do_not_block_the_carry(vault: Path) -> None:
    """Review probe: three `status: active` revisions that each name their
    successor in `superseded_by` are as retired as a `superseded` status."""

    _seed(vault, retired=3, retired_status="active")

    hits, state = working_set_runtime.carry_candidates(vault, TURN)

    assert state == "available"
    assert [path for path, _score in hits] == [CURRENT]


def test_retired_revisions_do_not_count_toward_a_words_rarity(vault: Path) -> None:
    _seed(vault, retired=3)
    stems = working_set_runtime.pairable_stems(TURN)

    rare, _pages, state = working_set_runtime.rare_turn_terms(vault, stems)

    assert state == "available"
    assert set(working_set_runtime.pairable_stems("brask ferry timetable")) <= set(rare)


def test_current_pages_sharing_the_name_still_make_it_ordinary(vault: Path) -> None:
    """Only retirement is discounted. Four CURRENT pages sharing the words keep
    them ordinary, and the turn names no single page."""

    _seed(vault, retired=0)
    for index in range(3):
        _write(
            vault / RESEARCH / f"brask-ferry-timetable-copy-{index}.md",
            _revision(f"copy {index}", status="active", body="is posted at the pier.", successor=None),
        )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()

    hits, _state = working_set_runtime.carry_candidates(vault, TURN)

    assert working_set.dominant_carry(hits) is None


def test_warm_carry_rarity_reuses_counts_until_the_catalogue_changes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault, retired=3)
    store = lexstore.get_store(vault)
    counted: list[tuple[str, ...]] = []
    original = store._document_frequency_query

    def count(conn, tokens, scope, **kwargs):
        counted.append(tuple(tokens))
        return original(conn, tokens, scope, **kwargs)

    monkeypatch.setattr(store, "_document_frequency_query", count)
    stems = working_set_runtime.pairable_stems(TURN)
    first = working_set_runtime.rare_turn_terms(vault, stems)
    working_set_runtime.reset_caches_for_tests()
    assert working_set_runtime.rare_turn_terms(vault, stems) == first
    assert len(counted) == 1, "an unchanged carry recounted the whole corpus"

    for index in range(3):
        _write(
            vault / RESEARCH / f"brask-ferry-timetable-copy-{index}.md",
            _revision(f"copy {index}", status="active", body="is posted at the pier.", successor=None),
        )
    lexstore.ensure_fresh(vault)
    rare, pages, state = working_set_runtime.rare_turn_terms(vault, stems)
    assert state == "available"
    assert pages == first[1] + 3
    assert "brask" not in rare
    assert len(counted) == 2


def test_carry_rarity_cache_keeps_filtered_counts_separate(vault: Path) -> None:
    _seed(vault, retired=3)
    stems = ["brask"]
    unfiltered = lexstore.term_document_frequencies(vault, stems, scope="kb").value
    filtered = lexstore.term_document_frequencies(
        vault, stems, scope="kb", exclude_navigation=True,
        exclude_raw_material=True, exclude_statuses=working_set.RETIRED_PAGE_STATUSES,
    ).value
    assert unfiltered is not None and filtered is not None
    assert unfiltered[0]["brask"] == 4
    assert filtered[0]["brask"] == 1
    assert lexstore.term_document_frequencies(vault, stems, scope="kb").value == unfiltered


def test_carry_rarity_cache_eviction_keeps_counts_needed_by_this_request(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault, retired=3)
    monkeypatch.setattr(lexstore, "_TERM_FREQUENCY_CACHE_MAX", 2)
    kwargs = {"scope": "kb", "exclude_statuses": working_set.RETIRED_PAGE_STATUSES}
    first = lexstore.term_document_frequencies(vault, ["brask", "oldword"], **kwargs).value
    assert first is not None and first[0]["brask"] == 1

    second = lexstore.term_document_frequencies(vault, ["brask", "unseenword"], **kwargs).value
    assert second is not None
    assert second[0] == {"brask": 1, "unseenword": 0}
    cached = lexstore.get_store(vault)._filtered_term_frequency_cache
    assert cached is not None and len(cached[1]) <= 2


def test_new_carry_terms_reuse_the_catalogue_page_total(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault, retired=3)
    store = lexstore.get_store(vault)
    queries: list[str] = []
    original = store._document_frequency_query

    def trace(conn, tokens, scope, **kwargs):
        conn.set_trace_callback(queries.append)
        try:
            return original(conn, tokens, scope, **kwargs)
        finally:
            conn.set_trace_callback(None)

    monkeypatch.setattr(store, "_document_frequency_query", trace)
    kwargs = {"scope": "kb", "exclude_raw_material": True}
    first = lexstore.term_document_frequencies(vault, ["brask"], **kwargs).value
    second = lexstore.term_document_frequencies(vault, ["unseenword"], **kwargs).value
    assert first is not None and second is not None
    assert second == ({"unseenword": 0}, first[1])
    totals = [query for query in queries if query.startswith("SELECT COUNT(*) FROM pages p WHERE")]
    assert len(totals) == 1, "a new term recounted an unchanged corpus"


_FREQUENCY_FILTERS = {
    "scope": "kb",
    "exclude_navigation": True,
    "exclude_raw_material": True,
    "exclude_statuses": working_set.RETIRED_PAGE_STATUSES,
}


def _filtered_counts(vault: Path, terms=("brask",)):
    result = lexstore.term_document_frequencies(vault, terms, **_FREQUENCY_FILTERS)
    assert result.readiness.complete
    assert result.value is not None
    return result.value


def _remove_index_rows(store, paths):
    conn = store._connect()
    try:
        with conn:
            for path in paths:
                row = conn.execute("SELECT rowid FROM pages WHERE path = ?", (path,)).fetchone()
                assert row is not None
                store._delete_rowid(conn, row[0])
    finally:
        conn.close()


def test_filtered_counts_follow_an_incremental_repair(vault: Path) -> None:
    _seed(vault, retired=0)
    paths = [CURRENT]
    for index in range(3):
        path = f"{RESEARCH}/copy-{index}.md"
        _write(vault / path, _revision(str(index), status="active", body="runs hourly.", successor=None))
        paths.append(path)
    lexstore.ensure_fresh(vault)
    store = lexstore.get_store(vault)
    _remove_index_rows(store, paths)
    before = _filtered_counts(vault)
    assert before[0] == {"brask": 0}

    lexstore.ensure_fresh(vault)

    assert _filtered_counts(vault) == ({"brask": 4}, before[1] + 4)
    rare, pages, state = working_set_runtime.rare_turn_terms(vault, ["brask"])
    assert state == "available" and pages == before[1] + 4
    assert "brask" not in rare


@pytest.mark.parametrize("second_process", [False, True])
def test_filtered_counts_follow_an_exact_row_purge(vault: Path, second_process: bool) -> None:
    import subprocess
    import sys

    _seed(vault, retired=0)
    before = _filtered_counts(vault)
    assert before[0] == {"brask": 1}
    if second_process:
        script = (
            "from pathlib import Path; from exomem import lexstore; import sys; "
            "assert lexstore.get_store(Path(sys.argv[1])).purge_exact_persisted_rows([sys.argv[2]]) == 1"
        )
        subprocess.run(
            [sys.executable, "-c", script, str(vault), CURRENT],
            check=True, capture_output=True, text=True,
        )
    else:
        assert lexstore.get_store(vault).purge_exact_persisted_rows([CURRENT]) == 1

    assert _filtered_counts(vault) == ({"brask": 0}, before[1] - 1)


def test_an_old_snapshot_finishes_but_cannot_serve_counts_after_a_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault, retired=0)
    store = lexstore.get_store(vault)
    _remove_index_rows(store, [CURRENT])
    original = store._document_frequency_query

    def rebuild_after_compute(conn, tokens, scope, **kwargs):
        result = original(conn, tokens, scope, **kwargs)
        with store._publication_lock():
            writer = store._connect_setup()
            try:
                store._rebuild(writer)
            finally:
                writer.close()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "_document_frequency_query", rebuild_after_compute)
        before = _filtered_counts(vault)
    assert before[0] == {"brask": 0}
    assert _filtered_counts(vault) == ({"brask": 1}, before[1] + 1)


def test_frequency_cache_miss_does_not_block_a_committing_writer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One blocked read can escape the burst probe's p95 when it has many samples."""
    import sqlite3
    import threading
    import time

    _seed(vault, retired=0)
    store = lexstore.get_store(vault)
    connection = store._connect()
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        connection.close()
    commit_started = threading.Event()
    writer_done = threading.Event()
    errors: list[sqlite3.Error] = []
    resumed_at: list[float] = []

    def write() -> None:
        connection = sqlite3.connect(store.path, timeout=5)
        try:
            rowid = connection.execute("SELECT rowid FROM pages WHERE path = ?", (CURRENT,)).fetchone()[0]
            connection.execute("UPDATE fts SET stemmed = 'unseenword' WHERE rowid = ?", (rowid,))
            connection.set_trace_callback(lambda sql: commit_started.set() if sql == "COMMIT" else None)
            connection.commit()
        except sqlite3.Error as error:
            errors.append(error)
        finally:
            connection.close()
            writer_done.set()

    writer = threading.Thread(target=write)
    count = store._document_frequency_query

    def count_then_commit(connection, *args, **kwargs):
        result = count(connection, *args, **kwargs)
        writer.start()
        assert commit_started.wait(1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not writer_done.is_set():
            observer = sqlite3.connect(store.path, timeout=0)
            try:
                observer.execute("SELECT value FROM meta LIMIT 1").fetchone()
            except sqlite3.OperationalError as error:
                assert "locked" in str(error)
                resumed_at.append(time.monotonic())
                return result
            finally:
                observer.close()
            writer_done.wait(0.001)
        pytest.fail("the writer never reached commit while the count snapshot was held")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, "_document_frequency_query", count_then_commit)
            before = _filtered_counts(vault)
        assert time.monotonic() - resumed_at[0] < 2
    finally:
        if writer.ident is not None:
            writer.join(timeout=6)
    assert not writer.is_alive() and writer_done.is_set()
    assert not errors
    assert before[0] == {"brask": 1}
    assert _filtered_counts(vault) == ({"brask": 0}, before[1])


@pytest.mark.parametrize("filtered", [False, True])
def test_retained_cache_stays_within_its_limit_for_an_oversized_request(
    vault: Path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    _seed(vault, retired=0)
    monkeypatch.setattr(lexstore, "_TERM_FREQUENCY_CACHE_MAX", 2)
    kwargs = _FREQUENCY_FILTERS if filtered else {"scope": "kb"}
    # Warm one positive count, then request more positive counts than fit.
    lexstore.term_document_frequencies(vault, ["brask"], **kwargs)
    terms = ["brask", "ferri", "timet", "unseenword"]
    result = lexstore.term_document_frequencies(vault, terms, **kwargs)
    assert result.readiness.complete and result.value is not None
    store = lexstore.get_store(vault)
    conn = store._connect()
    try:
        expected = store._document_frequency_query(conn, terms, **kwargs)
    finally:
        conn.close()
    assert result.value == expected
    assert all(expected[0][term] > 0 for term in terms[:3])
    cached = store._filtered_term_frequency_cache if filtered else store._term_frequency_cache
    assert cached is not None and len(cached[1]) <= 2
    assert lexstore.term_document_frequencies(vault, terms, **kwargs).value == expected


def test_frequency_revision_follows_fts_only_mutations(vault: Path) -> None:
    _seed(vault, retired=0)
    before = _filtered_counts(vault)
    store = lexstore.get_store(vault)
    conn = store._connect()
    try:
        with conn:
            rowid = conn.execute("SELECT rowid FROM pages WHERE path = ?", (CURRENT,)).fetchone()[0]
            conn.execute("UPDATE fts SET stemmed = 'unseenword' WHERE rowid = ?", (rowid,))
    finally:
        conn.close()
    assert _filtered_counts(vault) == ({"brask": 0}, before[1])
