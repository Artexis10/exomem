import hashlib
import sqlite3

import pytest


def _advance(provenance, *args, **kwargs):
    return provenance.advance(*args, snapshot_validator=lambda *_args: True, **kwargs)


def _page(path, *, url=None, body="A source body."):
    frontmatter = {}
    if url is not None:
        frontmatter["url"] = url
    return {
        "path": path,
        "ref": path,
        "content_hash": hashlib.sha256(body.encode()).hexdigest(),
        "frontmatter": frontmatter,
        "body": body,
    }


def test_discovery_withholds_until_the_ninth_source_is_checkpointed(tmp_path):
    from exomem import vocabulary_provenance

    first = [
        _page(f"Knowledge Base/Sources/{index}.md", url="https://example.test/copied")
        for index in range(8)
    ]
    first_result = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="a" * 64,
        graph_generation="generation-1",
        source_page=first,
        has_more=True,
    )
    assert first_result["status"] == "warming"
    assert first_result["continuation"]

    final = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="a" * 64,
        graph_generation="generation-1",
        source_page=[
            _page(
                "Knowledge Base/Sources/ninth.md",
                url="https://independent.test/report",
                body="An independent ninth account.",
            )
        ],
        has_more=False,
        continuation=first_result["continuation"],
    )
    assert final["status"] == "current"
    assert len(final["representatives"]) == 2
    assert final["context_continuation"]


@pytest.mark.parametrize(
    "pages",
    [
        [
            _page("Knowledge Base/Sources/a.md", url="https://same.test/a", body="left"),
            _page("Knowledge Base/Sources/b.md", url="https://same.test/a", body="right"),
        ],
        [
            _page("Knowledge Base/Sources/a.md", url="https://one.test/a", body="same"),
            _page("Knowledge Base/Sources/b.md", url="https://two.test/b", body="same"),
        ],
    ],
)
def test_copies_are_one_component_even_when_the_alias_arrives_on_another_page(tmp_path, pages):
    from exomem import vocabulary_provenance

    first = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="b" * 64,
        graph_generation="generation-1",
        source_page=[pages[0]],
        has_more=True,
    )
    final = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="b" * 64,
        graph_generation="generation-1",
        source_page=[pages[1]],
        has_more=False,
        continuation=first["continuation"],
    )
    assert final["status"] == "current"
    assert final["representatives"] == []
    assert final["context_continuation"] is None


def test_two_declared_components_remain_eligible_with_an_unknown_body_only_copy(tmp_path):
    from exomem import vocabulary_provenance

    result = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="c" * 64,
        graph_generation="generation-1",
        source_page=[
            _page("Knowledge Base/Sources/one.md", url="https://one.test/a", body="left"),
            _page("Knowledge Base/Sources/two.md", url="https://two.test/b", body="right"),
            _page("Knowledge Base/Sources/copy.md", body="left"),
        ],
        has_more=False,
    )
    assert result["status"] == "current"
    assert len(result["representatives"]) == 2
    assert result["context_continuation"]


def test_transitive_bridge_merges_components_across_three_pages(tmp_path):
    from exomem import vocabulary_provenance

    first = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="d" * 64,
        graph_generation="generation-1",
        source_page=[
            _page("Knowledge Base/Sources/one.md", url="https://one.test/a", body="bridge")
        ],
        has_more=True,
    )
    second = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="d" * 64,
        graph_generation="generation-1",
        source_page=[
            _page("Knowledge Base/Sources/two.md", url="https://two.test/b", body="bridge")
        ],
        has_more=True,
        continuation=first["continuation"],
    )
    final = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="d" * 64,
        graph_generation="generation-1",
        source_page=[
            _page("Knowledge Base/Sources/z-three.md", url="https://two.test/b", body="third")
        ],
        has_more=False,
        continuation=second["continuation"],
    )
    assert final["representatives"] == []


def test_old_or_wrong_generation_discovery_cursor_cannot_replace_a_checkpoint(tmp_path):
    from exomem import vocabulary_provenance

    first = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="e" * 64,
        graph_generation="generation-1",
        source_page=[_page("Knowledge Base/Sources/one.md", url="https://one.test/a")],
        has_more=True,
    )
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="e" * 64,
            graph_generation="generation-2",
            source_page=[
                _page("Knowledge Base/Sources/two.md", url="https://two.test/b", body="other")
            ],
            has_more=False,
            continuation=first["continuation"],
        )
    completed = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="e" * 64,
        graph_generation="generation-1",
        source_page=[
            _page("Knowledge Base/Sources/two.md", url="https://two.test/b", body="other")
        ],
        has_more=False,
        continuation=first["continuation"],
    )
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="e" * 64,
            graph_generation="generation-1",
            source_page=[
                _page("Knowledge Base/Sources/two.md", url="https://two.test/b", body="other")
            ],
            has_more=False,
            continuation=first["continuation"],
        )
    assert len(completed["representatives"]) == 2


def test_no_item_checkpoints_do_not_leave_history(tmp_path):
    from exomem import deferred_index, vocabulary_provenance

    for index in range(100):
        target_hash = f"{index:064x}"
        generation = f"generation-{index}"

        def is_current(_path, digest, current_generation, expected=(target_hash, generation)):
            return (digest, current_generation) == expected

        result = vocabulary_provenance.advance(
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash=target_hash,
            graph_generation=generation,
            source_page=[_page("Knowledge Base/Sources/copy.md", body="body only")],
            has_more=False,
            snapshot_validator=is_current,
        )
        assert result["context_continuation"] is None
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as conn:
        assert (
            conn.execute("SELECT count(*) FROM vocabulary_provenance_checkpoints").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM vocabulary_provenance_sources").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM vocabulary_provenance_components").fetchone()[0] == 0
        )
        assert conn.execute("SELECT count(*) FROM vocabulary_provenance_retired").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM vocabulary_provenance_active").fetchone()[0] == 1


def test_corrupt_checkpoint_store_is_typed_unavailable(tmp_path):
    from exomem import deferred_index, vocabulary_provenance

    store = deferred_index.store_path(tmp_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("not a sqlite database")
    with pytest.raises(ValueError, match="VOCABULARY_EVIDENCE_UNAVAILABLE"):
        _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="f" * 64,
            graph_generation="generation-1",
            source_page=[],
            has_more=False,
        )


def test_completed_checkpoint_returns_only_bounded_component_summaries(tmp_path, monkeypatch):
    from exomem import deferred_index, vocabulary_provenance

    sources = [
        _page(
            f"Knowledge Base/Sources/{index:03}.md",
            url=f"https://example.test/{index}",
            body=f"Independent source {index}.",
        )
        for index in range(104)
    ]
    continuation = None
    for start in range(
        0, len(sources) - vocabulary_provenance.SOURCE_PAGE, vocabulary_provenance.SOURCE_PAGE
    ):
        page = sources[start : start + vocabulary_provenance.SOURCE_PAGE]
        result = _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="0" * 64,
            graph_generation="generation-1",
            source_page=page,
            has_more=True,
            continuation=continuation,
        )
        continuation = result["continuation"]

    with sqlite3.connect(deferred_index.store_path(tmp_path)) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT declared_path FROM vocabulary_provenance_components "
            "WHERE target_path = ? AND is_root = 1 AND declared_path IS NOT NULL "
            "ORDER BY first_path LIMIT 2",
            ("Knowledge Base/Notes/target.md",),
        ).fetchall()
    assert any("vocabulary_provenance_roots" in row[-1] for row in plan)

    original_connect = deferred_index._connect
    materialized = []

    class Cursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchall(self):
            rows = self._cursor.fetchall()
            materialized.append(len(rows))
            return rows

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class Connection:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def execute(self, *args):
            return Cursor(self._connection.execute(*args))

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        deferred_index,
        "_connect",
        lambda *args, **kwargs: Connection(original_connect(*args, **kwargs)),
    )
    final = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="0" * 64,
        graph_generation="generation-1",
        source_page=sources[-vocabulary_provenance.SOURCE_PAGE :],
        has_more=False,
        continuation=continuation,
    )
    cached = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="0" * 64,
        graph_generation="generation-1",
        source_page=[],
        has_more=False,
    )
    assert final["status"] == "current"
    assert cached["status"] == "current"
    assert max(materialized) <= 3


def test_alias_union_does_not_rewrite_every_existing_source_component(tmp_path, monkeypatch):
    from exomem import deferred_index, vocabulary_provenance

    original_connect = deferred_index._connect
    rewritten = []
    transaction_changes = []

    class Cursor:
        def __init__(self, cursor, statement):
            self._cursor = cursor
            if statement.startswith("UPDATE vocabulary_provenance_sources"):
                rewritten.append(cursor.rowcount)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class Connection:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            result = self._connection.__exit__(*args)
            transaction_changes.append(self._connection.total_changes)
            return result

        def execute(self, statement, *args):
            return Cursor(self._connection.execute(statement, *args), statement)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        deferred_index,
        "_connect",
        lambda *args, **kwargs: Connection(original_connect(*args, **kwargs)),
    )
    continuation = None
    for start in range(0, 64, vocabulary_provenance.SOURCE_PAGE):
        page = [
            _page(
                f"Knowledge Base/Sources/{index:03}.md",
                url="https://example.test/copied",
                body=f"Copied source {index}.",
            )
            for index in range(start, start + vocabulary_provenance.SOURCE_PAGE)
        ]
        result = _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="1" * 64,
            graph_generation="generation-1",
            source_page=page,
            has_more=True,
            continuation=continuation,
        )
        continuation = result["continuation"]
    assert rewritten == []
    assert transaction_changes[-1] <= transaction_changes[0] + vocabulary_provenance.SOURCE_PAGE


def test_changed_generation_retires_large_checkpoint_in_fixed_batches(tmp_path, monkeypatch):
    from exomem import deferred_index, vocabulary_provenance

    continuation = None
    for start in range(0, 1000, vocabulary_provenance.SOURCE_PAGE):
        page = [
            _page(
                f"Knowledge Base/Sources/{index:04}.md",
                url="https://example.test/copied",
                body=f"Copied source {index}.",
            )
            for index in range(start, start + vocabulary_provenance.SOURCE_PAGE)
        ]
        old = _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="2" * 64,
            graph_generation="generation-1",
            source_page=page,
            has_more=True,
            continuation=continuation,
        )
        continuation = old["continuation"]

    original_connect = deferred_index._connect
    deleted = []

    class Connection:
        def __init__(self, connection):
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):
            return self._connection.__exit__(*args)

        def execute(self, statement, *args):
            cursor = self._connection.execute(statement, *args)
            if statement.startswith("DELETE FROM vocabulary_provenance_sources"):
                deleted.append(cursor.rowcount)
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        deferred_index,
        "_connect",
        lambda *args, **kwargs: Connection(original_connect(*args, **kwargs)),
    )
    fresh = _advance(
        vocabulary_provenance,
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="3" * 64,
        graph_generation="generation-2",
        source_page=[],
        has_more=True,
    )
    assert max(deleted, default=0) <= vocabulary_provenance.SOURCE_PAGE
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="2" * 64,
            graph_generation="generation-1",
            source_page=[],
            has_more=True,
            continuation=continuation,
        )
    for _ in range(130):
        fresh = _advance(
            vocabulary_provenance,
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="3" * 64,
            graph_generation="generation-2",
            source_page=[],
            has_more=True,
            continuation=fresh["continuation"],
        )
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM vocabulary_provenance_sources").fetchone()[0] == 0


def test_retired_positive_snapshot_cannot_displace_active_generation(tmp_path):
    from exomem import vocabulary_provenance

    live = ("a" * 64, "generation-a")

    def advance(*args, **kwargs):
        return vocabulary_provenance.advance(
            *args,
            snapshot_validator=lambda _path, digest, generation: (digest, generation) == live,
            **kwargs,
        )

    first = advance(
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="a" * 64,
        graph_generation="generation-a",
        source_page=[
            _page(
                f"Knowledge Base/Sources/{index}.md",
                url=f"https://example.test/{index}",
                body=f"Source {index}.",
            )
            for index in range(8)
        ],
        has_more=True,
    )
    positive = advance(
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="a" * 64,
        graph_generation="generation-a",
        source_page=[
            _page(
                "Knowledge Base/Sources/ninth.md", url="https://example.test/ninth", body="Ninth."
            )
        ],
        has_more=False,
        continuation=first["continuation"],
    )
    assert len(positive["representatives"]) == 2
    live = ("b" * 64, "generation-b")
    active = advance(
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="b" * 64,
        graph_generation="generation-b",
        source_page=[],
        has_more=True,
    )
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        advance(
            tmp_path,
            path="Knowledge Base/Notes/target.md",
            target_hash="a" * 64,
            graph_generation="generation-a",
            source_page=[],
            has_more=True,
        )
    advanced = advance(
        tmp_path,
        path="Knowledge Base/Notes/target.md",
        target_hash="b" * 64,
        graph_generation="generation-b",
        source_page=[],
        has_more=True,
        continuation=active["continuation"],
    )
    assert advanced["status"] == "warming"


@pytest.mark.parametrize("result_kind", ["warming", "ineligible", "eligible", "cached"])
def test_final_snapshot_refusal_rolls_back_provenance_mutation(tmp_path, result_kind):
    from exomem import deferred_index, vocabulary_provenance

    pages = [
        _page(
            f"Knowledge Base/Sources/{index}.md",
            url=f"https://source.test/{index}",
            body=str(index),
        )
        for index in range(3)
    ]
    arguments = {
        "path": "Knowledge Base/Notes/target.md",
        "target_hash": "d" * 64,
        "graph_generation": "generation-d",
        "source_page": pages,
        "has_more": result_kind == "warming",
    }
    tables = (
        "vocabulary_provenance_checkpoints",
        "vocabulary_provenance_sources",
        "vocabulary_provenance_components",
        "vocabulary_provenance_active",
        "vocabulary_provenance_retired",
    )

    def snapshot():
        with deferred_index._connect(tmp_path, create=True) as conn:
            return {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in tables
            }

    if result_kind == "cached":
        original = _advance(vocabulary_provenance, tmp_path, **arguments)
        assert original["context_continuation"]
        arguments["source_page"] = []
    elif result_kind == "ineligible":
        arguments["source_page"] = [_page("Knowledge Base/Sources/one.md", body="body only")]
    before = snapshot()
    calls = 0

    def validator(*_args):
        nonlocal calls
        calls += 1
        return calls == 1

    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        vocabulary_provenance.advance(
            tmp_path,
            **arguments,
            snapshot_validator=validator,
        )
    assert calls == 2
    assert snapshot() == before
