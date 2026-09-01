from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from exomem import (
    activation,
    corpus_aware,
    epistemic_graph,
    find,
    freshness,
    graph_sync,
    memory_refs,
    relation_queue,
    review_state,
    semantic_contract,
    vault,
    writer_lease,
)

KB = "Knowledge Base/Notes/Insights"


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\nstatus: active\n---\n"
        f"# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _build_current(root: Path) -> epistemic_graph.EpistemicGraphIndex:
    find.clear_cache()
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    freshness.rebaseline(root)
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()
    return index


def _warm_reference_authority(root: Path) -> None:
    """Publish cached resolver maps for the graph's current identity snapshot."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
        semantic_contract.build_corpus_context(root)
    assert semantic_contract.current_reference_identity_snapshot(root) is not None


def _items(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for group in queue["groups"] for item in group["items"]]


class _CountingConnection:
    def __init__(self, connection: Any):
        self.connection = connection
        self.queries = 0

    def execute(self, *args: Any, **kwargs: Any):
        self.queries += 1
        return self.connection.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


def test_queue_uses_one_fixed_cost_graph_batch_for_3600_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    target = f"{KB}/zzzz-target.md"
    _write(root, target, "A shared target.")
    for number in range(3_600):
        _write(
            root,
            f"{KB}/source-{number:04d}.md",
            f"See [[{target.removesuffix('.md')}]].",
        )
    _build_current(root)

    opens = 0
    counted: list[_CountingConnection] = []
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

    def open_counted(self: Any, *args: Any, **kwargs: Any):
        nonlocal opens
        opens += 1
        connection = original_open(self, *args, **kwargs)
        if connection is None:
            return None
        wrapped = _CountingConnection(connection)
        counted.append(wrapped)
        return wrapped

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", open_counted
    )
    monkeypatch.setattr(
        activation,
        "scan",
        lambda *_args, **_kwargs: pytest.fail("queue ran activation census"),
    )
    monkeypatch.setattr(
        find,
        "_walk_md",
        lambda *_args, **_kwargs: pytest.fail("queue walked Markdown"),
    )
    monkeypatch.setattr(
        find,
        "_parse_page",
        lambda *_args, **_kwargs: pytest.fail("queue parsed Markdown"),
    )
    monkeypatch.setattr(
        epistemic_graph,
        "suggest_relations",
        lambda *_args, **_kwargs: pytest.fail("queue used per-page suggestions"),
    )
    monkeypatch.setattr(
        corpus_aware,
        "_best_cosine_per_file",
        lambda *_args, **_kwargs: pytest.fail("queue invoked embeddings"),
    )
    result = relation_queue.build_queue(root, limit_pages=5, limit_per_page=5)

    assert result["status"] == "available"
    assert result["pages_shown"] == 5
    assert opens == 1
    assert len(counted) == 1
    assert counted[0].queries <= 8
    assert result["coverage"]["eligible_pages"] == 3_601


def test_queue_returns_typed_warming_without_any_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    _write(root, f"{KB}/source.md", f"See [[{KB}/target]].")
    _write(root, f"{KB}/target.md", "A target.")

    monkeypatch.setattr(
        activation,
        "scan",
        lambda *_args, **_kwargs: pytest.fail("warming queue ran activation census"),
    )
    monkeypatch.setattr(
        find,
        "_walk_md",
        lambda *_args, **_kwargs: pytest.fail("warming queue walked Markdown"),
    )
    monkeypatch.setattr(
        epistemic_graph,
        "suggest_relations",
        lambda *_args, **_kwargs: pytest.fail("warming queue generated candidates"),
    )

    result = relation_queue.build_queue(root)

    assert result["status"] == "warming"
    assert result["groups"] == []
    assert result["retryable"] is True
    assert result["next_action"] == "retry-relation-queue"


@pytest.mark.parametrize(
    ("batch_status", "graph_enabled", "sync_state", "expected_status"),
    [
        ("warming", True, "recovery_required", "pending"),
        ("warming", False, "current", "unavailable"),
        ("unavailable", True, "current", "unavailable"),
    ],
)
def test_unready_graph_statuses_remain_typed_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    batch_status: str,
    graph_enabled: bool,
    sync_state: str,
    expected_status: str,
) -> None:
    root = tmp_path / "vault"
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "relation_review_batch",
        lambda *_args, **_kwargs: {
            "status": batch_status,
            "groups": [],
            "shown": 0,
            "pages_shown": 0,
        },
    )
    monkeypatch.setattr(epistemic_graph, "graph_enabled", lambda: graph_enabled)
    monkeypatch.setattr(graph_sync, "status", lambda _root: {"state": sync_state})
    monkeypatch.setattr(
        activation,
        "scan",
        lambda *_args, **_kwargs: pytest.fail("unready queue ran activation census"),
    )

    result = relation_queue.build_queue(root)

    assert result["status"] == expected_status
    assert result["groups"] == []
    assert result["shown"] == 0
    assert result["pages_shown"] == 0
    assert result["retryable"] is True


def test_hinted_accept_revalidates_one_source_and_queue_items_carry_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(root, target, "A target.")
    _build_current(root)

    queue = relation_queue.build_queue(root)
    group = next(group for group in queue["groups"] if group["path"] == source)
    item = next(item for item in group["items"] if item["to"] == target)
    assert group["source_path"] == source
    assert group["source_content_hash"] == group["content_hash"]
    assert item["source_path"] == source
    assert item["source_content_hash"] == group["content_hash"]

    page_reads: list[str] = []
    original_parse = find._parse_page

    def parse_counted(path: Path, *args: Any, **kwargs: Any):
        page_reads.append(path.relative_to(root).as_posix())
        return original_parse(path, *args, **kwargs)

    monkeypatch.setattr(find, "_parse_page", parse_counted)
    monkeypatch.setattr(
        corpus_aware,
        "_best_cosine_per_file",
        lambda *_args, **_kwargs: pytest.fail("hinted accept invoked embeddings"),
    )
    edits: list[dict[str, Any]] = []

    def record_edit(_root: Path, **kwargs: Any) -> dict[str, Any]:
        edits.append(kwargs)
        return {"mutated": True}

    accepted = relation_queue.accept(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        expected_hash=item["source_content_hash"],
        expected_fingerprint=item["fingerprint"],
        why="Accepted synthetic reviewed relation.",
        edit_memory=record_edit,
    )

    assert accepted["accepted"] is True
    assert page_reads == [source]
    assert edits[0]["path"] == source


def test_hinted_triage_revalidates_only_one_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(root, target, "A target.")
    _build_current(root)
    item = _items(relation_queue.build_queue(root))[0]

    page_reads: list[str] = []
    original_parse = find._parse_page

    def parse_counted(path: Path, *args: Any, **kwargs: Any):
        page_reads.append(path.relative_to(root).as_posix())
        return original_parse(path, *args, **kwargs)

    monkeypatch.setattr(find, "_parse_page", parse_counted)
    result = relation_queue.triage(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        action="dismiss",
        expected_fingerprint=item["fingerprint"],
        why="Synthetic one-source triage proof.",
    )

    assert result["state"] == "dismissed"
    assert result["path"] == source
    assert page_reads == [source]


def test_hinted_decisions_do_not_rebuild_cold_reference_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    source_file = root / source
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "---\ntype: insight\nstatus: active\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n"
        "---\n# Source\n\n"
        f"See [[{target.removesuffix('.md')}]].\n",
        encoding="utf-8",
    )
    target_file = root / target
    target_file.write_text(
        "---\ntype: insight\nstatus: active\n"
        "exomem_id: 22222222-2222-4222-8222-222222222222\n"
        "---\n# Target\n\nA target.\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _build_current(root)
    semantic_contract.build_corpus_context(root)
    assert semantic_contract.current_reference_identity_snapshot(root) is not None
    assert not memory_refs.sidecar_path(root).exists()
    item = _items(relation_queue.build_queue(root))[0]

    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "rebuild_all",
        lambda *_args, **_kwargs: pytest.fail("hinted decision rebuilt refs corpus"),
    )
    monkeypatch.setattr(
        memory_refs,
        "_scan_pages",
        lambda *_args, **_kwargs: pytest.fail("hinted decision scanned refs corpus"),
    )
    monkeypatch.setattr(
        find,
        "_walk_md",
        lambda *_args, **_kwargs: pytest.fail("hinted decision walked Markdown"),
    )

    resolved = relation_queue.resolve_candidate(
        root, item["ref"], source_path=item["source_path"]
    )
    assert resolved.fingerprint == item["fingerprint"]
    accepted = relation_queue.accept(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        expected_hash=item["source_content_hash"],
        expected_fingerprint=item["fingerprint"],
        why="Accept with cached identity authority.",
        edit_memory=lambda *_args, **_kwargs: {"mutated": True},
    )
    triaged = relation_queue.triage(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        action="dismiss",
        expected_fingerprint=item["fingerprint"],
        why="Triage with cached identity authority.",
    )
    assert accepted["fingerprint"] == item["fingerprint"]
    assert triaged["state"] == "dismissed"


def test_no_id_fallback_refreshes_if_identity_authority_appears_during_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    target_file = _write(root, target, "A target without identity.")
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _build_current(root)
    item = next(
        queued
        for queued in _items(relation_queue.build_queue(root))
        if queued["from"] == source and queued["to"] == target
    )
    semantic_contract.evict_corpus_context(root)
    assert semantic_contract.current_reference_identity_snapshot(root) is None
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot
    replacement_refs: list[str] = []
    replaced = False

    class MaterializedRows:
        def __init__(self, rows: list[tuple[Any, ...]]):
            self.rows = rows

        def fetchall(self) -> list[tuple[Any, ...]]:
            return self.rows

    class ReplacingConnection:
        def __init__(self, connection: Any):
            self.connection = connection

        def execute(self, sql: str, *args: Any, **kwargs: Any):
            nonlocal replaced
            cursor = self.connection.execute(sql, *args, **kwargs)
            if "SELECT path, exomem_id FROM graph_nodes" not in sql or replaced:
                return cursor
            rows = cursor.fetchall()
            replaced = True
            target_file.write_text(
                "---\ntype: insight\nstatus: active\n"
                "exomem_id: 33333333-3333-4333-8333-333333333333\n"
                "---\n# target\n\nA target with current identity.\n",
                encoding="utf-8",
            )
            freshness.rebaseline(root)
            semantic_contract.build_corpus_context(root)
            snapshot = semantic_contract.current_reference_identity_snapshot(root)
            assert snapshot is not None
            replacement = snapshot.canonical_refs_by_path[target]
            assert replacement is not None
            replacement_refs.append(replacement)
            return MaterializedRows(rows)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    def open_replacing(self: Any, *args: Any, **kwargs: Any):
        connection = original_open(self, *args, **kwargs)
        return ReplacingConnection(connection) if connection is not None else None

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_open_read_snapshot",
        open_replacing,
    )
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "rebuild_all",
        lambda *_args, **_kwargs: pytest.fail("triage rebuilt the refs corpus"),
    )
    monkeypatch.setattr(
        memory_refs,
        "_scan_pages",
        lambda *_args, **_kwargs: pytest.fail("triage scanned the refs corpus"),
    )

    with pytest.raises(ValueError, match="REVIEW_REFRESH_REQUIRED"):
        relation_queue.triage(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            action="dismiss",
            expected_fingerprint=item["fingerprint"],
            why="Refuse a dismissal under replaced identity authority.",
        )

    assert replacement_refs and replacement_refs[0] != item["target_ref"]
    assert review_state.ReviewStateStore(root).load()["records"] == {}


def test_no_id_fallback_refreshes_if_cached_identity_context_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(root, target, "A target without identity.")
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _build_current(root)
    item = next(
        queued
        for queued in _items(relation_queue.build_queue(root))
        if queued["from"] == source and queued["to"] == target
    )
    semantic_contract.build_corpus_context(root)
    original_snapshot = semantic_contract.current_reference_identity_snapshot(root)
    assert original_snapshot is not None
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot
    replaced = False

    class MaterializedRows:
        def __init__(self, rows: list[tuple[Any, ...]]):
            self.rows = rows

        def fetchall(self) -> list[tuple[Any, ...]]:
            return self.rows

    class ReplacingConnection:
        def __init__(self, connection: Any):
            self.connection = connection

        def execute(self, sql: str, *args: Any, **kwargs: Any):
            nonlocal replaced
            cursor = self.connection.execute(sql, *args, **kwargs)
            if "SELECT path, exomem_id FROM graph_nodes" not in sql or replaced:
                return cursor
            rows = cursor.fetchall()
            replaced = True
            cache_key = semantic_contract._corpus_cache_key(root)
            with semantic_contract._CORPUS_CONTEXT_UPDATE_LOCK:
                with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
                    entry = semantic_contract._CORPUS_CONTEXT_CACHE[cache_key]
                    semantic_contract._CORPUS_CONTEXT_CACHE[cache_key] = (
                        entry[0],
                        replace(entry[1]),
                    )
            replacement = semantic_contract.current_reference_identity_snapshot(root)
            assert replacement is not None
            assert replacement._cache_context_identity is not (
                original_snapshot._cache_context_identity
            )
            return MaterializedRows(rows)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    def open_replacing(self: Any, *args: Any, **kwargs: Any):
        connection = original_open(self, *args, **kwargs)
        return ReplacingConnection(connection) if connection is not None else None

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_open_read_snapshot",
        open_replacing,
    )

    with pytest.raises(ValueError, match="REVIEW_REFRESH_REQUIRED"):
        relation_queue.triage(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            action="dismiss",
            expected_fingerprint=item["fingerprint"],
            why="Refuse a dismissal under a replaced cached context.",
        )

    assert review_state.ReviewStateStore(root).load()["records"] == {}


def _current_title_authored_case(
    root: Path,
    *,
    title: str = "Readable Target",
    authored_target: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    source = f"{KB}/source.md"
    target = f"{KB}/opaque-target.md"
    source_file = _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    target_file = root / target
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(
        f"---\ntype: insight\nstatus: active\ntitle: {title}\n---\n"
        "# Opaque target\n\nA target.\n",
        encoding="utf-8",
    )
    _build_current(root)
    item = next(
        queued
        for queued in _items(relation_queue.build_queue(root))
        if queued["from"] == source and queued["to"] == target
    )
    _warm_reference_authority(root)
    source_file.write_text(
        source_file.read_text(encoding="utf-8")
        + f"\n## Relations\n\n- links_to [[{authored_target or title}]]\n",
        encoding="utf-8",
    )
    current = relation_queue.resolve_candidate(
        root, item["ref"], source_path=item["source_path"]
    )
    return source_file, {**item, "fingerprint": current.fingerprint}


def test_hinted_accept_refreshes_when_cached_authored_authority_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    source_file, item = _current_title_authored_case(root)
    original_guard = relation_queue._current_candidate_is_authored
    original_snapshot = semantic_contract.current_reference_identity_snapshot
    inside_guard = False

    def guarded(*args: Any, **kwargs: Any):
        nonlocal inside_guard
        inside_guard = True
        try:
            return original_guard(*args, **kwargs)
        finally:
            inside_guard = False

    def unavailable_in_guard(*args: Any, **kwargs: Any):
        if inside_guard:
            return None
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(relation_queue, "_current_candidate_is_authored", guarded)
    monkeypatch.setattr(
        semantic_contract,
        "current_reference_identity_snapshot",
        unavailable_in_guard,
    )
    edits: list[dict[str, Any]] = []
    with pytest.raises(ValueError, match="REVIEW_REFRESH_REQUIRED"):
        relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
            expected_fingerprint=item["fingerprint"],
            why="Fail closed when title authority errors.",
            edit_memory=lambda *_args, **kwargs: edits.append(kwargs)
            or {"mutated": True},
        )
    assert edits == []


def test_hinted_accept_refreshes_when_cached_authority_is_lost_during_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    source_file, item = _current_title_authored_case(root)
    original_resolve = semantic_contract._resolve_reference_wikilink_from_context

    def lose_authority(*args: Any, **kwargs: Any):
        result = original_resolve(*args, **kwargs)
        semantic_contract.evict_corpus_context(root)
        return result

    monkeypatch.setattr(
        semantic_contract,
        "_resolve_reference_wikilink_from_context",
        lose_authority,
    )
    edits: list[dict[str, Any]] = []
    with pytest.raises(ValueError, match="REVIEW_REFRESH_REQUIRED"):
        relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
            expected_fingerprint=item["fingerprint"],
            why="Fail closed when title authority is unavailable.",
            edit_memory=lambda *_args, **kwargs: edits.append(kwargs)
            or {"mutated": True},
        )
    assert edits == []


def test_live_authored_guard_blocks_unicode_title_without_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source_file, item = _current_title_authored_case(
        root,
        title="Älpha Target",
        authored_target="älpha target",
    )
    edits: list[dict[str, Any]] = []

    with pytest.raises(ValueError, match=r"REVIEW_ITEM_CHANGED.*authored_edge"):
        relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
            expected_fingerprint=item["fingerprint"],
            why="Block a Unicode-case-equivalent authored relation.",
            edit_memory=lambda *_args, **kwargs: edits.append(kwargs)
            or {"mutated": True},
        )
    assert edits == []


def test_live_authored_guard_uses_no_unindexed_graph_title_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    source_file, item = _current_title_authored_case(root)
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot
    statements: list[str] = []

    class ScanRejectingConnection:
        def __init__(self, connection: Any):
            self.connection = connection

        def execute(self, sql: str, *args: Any, **kwargs: Any):
            statements.append(sql)
            normalized = " ".join(sql.casefold().split())
            assert "substr(path" not in normalized
            assert "lower(title)" not in normalized
            assert "select title from graph_nodes" not in normalized
            return self.connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    def open_rejecting(self: Any, *args: Any, **kwargs: Any):
        connection = original_open(self, *args, **kwargs)
        return ScanRejectingConnection(connection) if connection is not None else None

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_open_read_snapshot",
        open_rejecting,
    )

    with pytest.raises(ValueError, match=r"REVIEW_ITEM_CHANGED.*authored_edge"):
        relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
            expected_fingerprint=item["fingerprint"],
            why="Reject any old graph title scan or temp-sort path.",
            edit_memory=lambda *_args, **_kwargs: pytest.fail(
                "authored candidate reached edit"
            ),
        )
    assert statements


def test_live_authored_guard_preserves_ambiguous_same_stem_relation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target_a = f"{KB}/Area-A/same.md"
    target_b = f"{KB}/Area-B/same.md"
    source_file = _write(root, source, f"See [[{target_a.removesuffix('.md')}]].")
    _write(root, target_a, "Area A target.")
    _write(root, target_b, "Area B target.")
    _build_current(root)
    item = next(
        queued
        for queued in _items(relation_queue.build_queue(root))
        if queued["from"] == source and queued["to"] == target_a
    )
    _warm_reference_authority(root)
    source_file.write_text(
        source_file.read_text(encoding="utf-8")
        + "\n## Relations\n\n- links_to [[same]]\n",
        encoding="utf-8",
    )
    current = relation_queue.resolve_candidate(
        root, item["ref"], source_path=item["source_path"]
    )
    edits: list[dict[str, Any]] = []
    accepted = relation_queue.accept(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
        expected_fingerprint=current.fingerprint,
        why="Do not retarget an ambiguous bare relation.",
        edit_memory=lambda *_args, **kwargs: edits.append(kwargs) or {"mutated": True},
    )
    assert accepted["accepted"] is True
    assert len(edits) == 1


def test_live_authored_guard_preserves_stem_title_collision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/Area-A/same.md"
    titled_other = f"{KB}/Area-B/opaque.md"
    source_file = _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(root, target, "The stem match.")
    titled_file = root / titled_other
    titled_file.parent.mkdir(parents=True, exist_ok=True)
    titled_file.write_text(
        "---\ntype: insight\nstatus: active\ntitle: same\n---\n"
        "# Opaque\n\nThe title match.\n",
        encoding="utf-8",
    )
    _build_current(root)
    item = next(
        queued
        for queued in _items(relation_queue.build_queue(root))
        if queued["from"] == source and queued["to"] == target
    )
    _warm_reference_authority(root)
    source_file.write_text(
        source_file.read_text(encoding="utf-8")
        + "\n## Relations\n\n- links_to [[same]]\n",
        encoding="utf-8",
    )
    current = relation_queue.resolve_candidate(
        root, item["ref"], source_path=item["source_path"]
    )
    edits: list[dict[str, Any]] = []
    accepted = relation_queue.accept(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
        expected_fingerprint=current.fingerprint,
        why="Do not choose between a stem and title collision.",
        edit_memory=lambda *_args, **kwargs: edits.append(kwargs) or {"mutated": True},
    )
    assert accepted["accepted"] is True
    assert len(edits) == 1


@pytest.mark.parametrize(
    ("case", "target_title", "other_title", "authored_target"),
    (
        ("same-title", "Twin Title", "Twin Title", "Twin Title"),
        (
            "unicode-fold-collision",
            "Älpha Collision",
            "älpha collision",
            "ÄLPHA COLLISION",
        ),
    ),
)
def test_live_authored_guard_preserves_title_ambiguity(
    tmp_path: Path,
    case: str,
    target_title: str,
    other_title: str,
    authored_target: str,
) -> None:
    root = tmp_path / case
    source = f"{KB}/source.md"
    target = f"{KB}/Area-A/target.md"
    other = f"{KB}/Area-B/other.md"
    source_file = _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    for rel_path, title in ((target, target_title), (other, other_title)):
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: insight\nstatus: active\ntitle: {title}\n---\n"
            f"# {path.stem}\n\nA target.\n",
            encoding="utf-8",
        )
    _build_current(root)
    item = next(
        queued
        for queued in _items(relation_queue.build_queue(root))
        if queued["from"] == source and queued["to"] == target
    )
    _warm_reference_authority(root)
    source_file.write_text(
        source_file.read_text(encoding="utf-8")
        + f"\n## Relations\n\n- links_to [[{authored_target}]]\n",
        encoding="utf-8",
    )
    current = relation_queue.resolve_candidate(
        root, item["ref"], source_path=item["source_path"]
    )
    edits: list[dict[str, Any]] = []

    accepted = relation_queue.accept(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
        expected_fingerprint=current.fingerprint,
        why="Do not retarget an ambiguous title relation.",
        edit_memory=lambda *_args, **kwargs: edits.append(kwargs) or {"mutated": True},
    )

    assert accepted["accepted"] is True
    assert len(edits) == 1


@pytest.mark.parametrize(
    ("case", "target", "target_text", "authored_target"),
    [
        (
            "exact-path",
            f"{KB}/Area-A/same.md",
            "A target.",
            f"{KB}/Area-A/same",
        ),
        ("unique-stem", f"{KB}/Area-A/same.md", "A target.", "same"),
        (
            "unique-title",
            f"{KB}/Area-A/opaque.md",
            "---\ntype: insight\nstatus: active\ntitle: Readable Target\n---\n"
            "# Opaque\n\nA target.\n",
            "Readable Target",
        ),
    ],
)
def test_live_authored_guard_blocks_exact_and_unique_targets(
    tmp_path: Path,
    case: str,
    target: str,
    target_text: str,
    authored_target: str,
) -> None:
    root = tmp_path / case
    source = f"{KB}/source.md"
    source_file = _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    if case == "unique-title":
        target_file = root / target
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(target_text, encoding="utf-8")
    else:
        _write(root, target, target_text)
    _build_current(root)
    item = next(
        queued
        for queued in _items(relation_queue.build_queue(root))
        if queued["from"] == source and queued["to"] == target
    )
    _warm_reference_authority(root)
    source_file.write_text(
        source_file.read_text(encoding="utf-8")
        + f"\n## Relations\n\n- links_to [[{authored_target}]]\n",
        encoding="utf-8",
    )
    current = relation_queue.resolve_candidate(
        root, item["ref"], source_path=item["source_path"]
    )
    with pytest.raises(ValueError, match=r"REVIEW_ITEM_CHANGED.*authored_edge"):
        relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
            expected_fingerprint=current.fingerprint,
            why="Block an exactly resolved current relation.",
            edit_memory=lambda *_args, **_kwargs: pytest.fail(
                "authored candidate reached edit"
            ),
        )


@pytest.mark.parametrize("decision", ("accept", "triage"))
def test_newly_returned_item_after_filter_headroom_is_hinted_actionable(
    tmp_path: Path,
    decision: str,
) -> None:
    root = tmp_path / decision
    source = f"{KB}/source.md"
    filtered_targets = [f"{KB}/filtered-{number:02d}.md" for number in range(40)]
    returned_target = f"{KB}/returned-source.md"
    for target in [*filtered_targets, returned_target]:
        _write(root, target, "A target.")
    source_file = root / source
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "---\ntype: insight\nstatus: active\n"
        f"sources:\n  - {returned_target}\n---\n# source\n\n"
        "## Relations\n\n"
        + "\n".join(
            f"- links_to [[{target.removesuffix('.md')}]]"
            for target in filtered_targets
        )
        + "\n\n## Context\n\n"
        + "\n".join(
            f"See [[{target.removesuffix('.md')}]]." for target in filtered_targets
        )
        + "\n",
        encoding="utf-8",
    )
    _build_current(root)
    queue = relation_queue.build_queue(root)
    item = next(
        item
        for item in _items(queue)
        if item["from"] == source and item["to"] == returned_target
    )
    assert item["method"] == "frontmatter_sources"

    if decision == "accept":
        edits: list[dict[str, Any]] = []
        result = relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash=item["source_content_hash"],
            expected_fingerprint=item["fingerprint"],
            why="Accept candidate beyond filtered method prefix.",
            edit_memory=lambda _root, **kwargs: edits.append(kwargs)
            or {"mutated": True},
        )
        assert result["accepted"] is True
        assert edits[0]["path"] == source
    else:
        result = relation_queue.triage(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            action="dismiss",
            expected_fingerprint=item["fingerprint"],
            why="Dismiss candidate beyond filtered method prefix.",
        )
        assert result["state"] == "dismissed"


def test_every_item_returned_at_supported_64_cap_is_hinted_actionable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/000-source.md"
    shared = f"{KB}/shared-evidence.md"
    _write(root, shared, "Shared evidence.")

    def write_sourced(rel_path: str) -> None:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: insight\nstatus: active\nsources:\n"
            f"  - {shared}\n---\n# {path.stem}\n\nA sourced claim.\n",
            encoding="utf-8",
        )

    write_sourced(source)
    for number in range(64):
        write_sourced(f"{KB}/peer-{number:02d}.md")
    _build_current(root)
    queue = relation_queue.build_queue(root, limit_per_page=64)
    items = next(group["items"] for group in queue["groups"] if group["path"] == source)
    assert len(items) == 64
    assert items[0]["method"] == "frontmatter_sources"
    assert items[-1]["method"] == "shared_sources"

    for item in items:
        resolved = relation_queue.resolve_candidate(
            root, item["ref"], source_path=item["source_path"]
        )
        assert resolved.ref == item["ref"]
        assert resolved.fingerprint == item["fingerprint"]
        assert resolved.candidate["evidence"] == item["evidence"]

    accepted = relation_queue.accept(
        root,
        ref=items[-1]["ref"],
        source_path=items[-1]["source_path"],
        expected_hash=items[-1]["source_content_hash"],
        expected_fingerprint=items[-1]["fingerprint"],
        why="Accept the final supported-cap item.",
        edit_memory=lambda *_args, **_kwargs: {"mutated": True},
    )
    triaged = relation_queue.triage(
        root,
        ref=items[-2]["ref"],
        source_path=items[-2]["source_path"],
        action="dismiss",
        expected_fingerprint=items[-2]["fingerprint"],
        why="Dismiss the penultimate supported-cap item.",
    )
    assert accepted["accepted"] is True
    assert triaged["state"] == "dismissed"


def test_hinted_wikilink_regeneration_preserves_graph_indexed_body_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/TargetCase.md"
    _write(root, target, "A target.")
    source_file = root / source
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "---\ntype: insight\nstatus: active\n---\n# source\n\n"
        "## Relations\n\n"
        f"- supports [[{target.removesuffix('.md')}]]\n\n"
        "## Context\n\n"
        f"The exact body spelling is [[{target}|display spelling]].\n",
        encoding="utf-8",
    )
    _build_current(root)
    item = next(
        item
        for item in _items(relation_queue.build_queue(root))
        if item["from"] == source and item["method"] == "wikilink"
    )
    assert item["evidence"] == {"source_path": source, "target": target}
    find.evict_resolver_caches(root)
    monkeypatch.setattr(
        vault,
        "walk_vault_md",
        lambda *_args, **_kwargs: pytest.fail("hinted regeneration walked corpus"),
    )

    resolved = relation_queue.resolve_candidate(
        root, item["ref"], source_path=item["source_path"]
    )
    assert resolved.candidate["evidence"] == item["evidence"]
    assert resolved.fingerprint == item["fingerprint"]
    accepted = relation_queue.accept(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        expected_hash=item["source_content_hash"],
        expected_fingerprint=item["fingerprint"],
        why="Accept the exact graph-indexed body signal.",
        edit_memory=lambda *_args, **_kwargs: {"mutated": True},
    )
    triaged = relation_queue.triage(
        root,
        ref=item["ref"],
        source_path=item["source_path"],
        action="dismiss",
        expected_fingerprint=item["fingerprint"],
        why="Dismiss the exact graph-indexed body signal.",
    )
    assert accepted["accepted"] is True
    assert triaged["state"] == "dismissed"
    assert item["ref"] not in {
        queued["ref"] for queued in _items(relation_queue.build_queue(root))
    }


def test_hinted_accept_refuses_fingerprint_drift_before_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    source_file = _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(root, target, "A target.")
    _build_current(root)
    item = _items(relation_queue.build_queue(root))[0]
    source_file.write_text(
        source_file.read_text(encoding="utf-8") + "\nA changed source signal.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash=item["source_content_hash"],
            expected_fingerprint=item["fingerprint"],
            why="Synthetic stale fingerprint proof.",
            edit_memory=lambda *_args, **_kwargs: pytest.fail(
                "drifted candidate reached edit"
            ),
        )


def test_hinted_accept_forwards_source_hash_guard_to_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(root, target, "A target.")
    _build_current(root)
    item = _items(relation_queue.build_queue(root))[0]

    def stale_edit(_root: Path, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["path"] == source
        assert kwargs["expected_hash"] == "0" * 64
        raise ValueError("STALE_EDIT: synthetic hash mismatch")

    with pytest.raises(ValueError, match="STALE_EDIT"):
        relation_queue.accept(
            root,
            ref=item["ref"],
            source_path=item["source_path"],
            expected_hash="0" * 64,
            expected_fingerprint=item["fingerprint"],
            why="Synthetic hash guard proof.",
            edit_memory=stale_edit,
        )


def test_hinted_accept_refuses_newly_authored_and_dismissed_candidates(
    tmp_path: Path,
) -> None:
    authored_root = tmp_path / "authored"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    source_file = _write(
        authored_root, source, f"See [[{target.removesuffix('.md')}]]."
    )
    _write(authored_root, target, "A target.")
    _build_current(authored_root)
    authored_item = _items(relation_queue.build_queue(authored_root))[0]
    _warm_reference_authority(authored_root)
    source_file.write_text(
        source_file.read_text(encoding="utf-8")
        + f"\n## Relations\n\n- links_to [[{target.removesuffix('.md')}]]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        relation_queue.accept(
            authored_root,
            ref=authored_item["ref"],
            source_path=authored_item["source_path"],
            expected_hash=authored_item["source_content_hash"],
            expected_fingerprint=authored_item["fingerprint"],
            why="Synthetic authored guard proof.",
            edit_memory=lambda *_args, **_kwargs: pytest.fail(
                "authored candidate reached edit"
            ),
        )
    current = relation_queue.resolve_candidate(
        authored_root,
        authored_item["ref"],
        source_path=authored_item["source_path"],
    )
    with pytest.raises(ValueError, match=r"REVIEW_ITEM_CHANGED.*authored_edge"):
        relation_queue.accept(
            authored_root,
            ref=authored_item["ref"],
            source_path=authored_item["source_path"],
            expected_hash=vault.content_hash(source_file.read_text(encoding="utf-8")),
            expected_fingerprint=current.fingerprint,
            why="Synthetic live authored guard proof.",
            edit_memory=lambda *_args, **_kwargs: pytest.fail(
                "live authored candidate reached edit"
            ),
        )

    placeholder_root = tmp_path / "placeholder"
    placeholder_source = _write(placeholder_root, source, "A sourced claim.")
    placeholder_source.write_text(
        "---\ntype: insight\nstatus: active\n"
        f"sources:\n  - {target}\n---\n# source\n\nA sourced claim.\n",
        encoding="utf-8",
    )
    target_file = _write(placeholder_root, target, "A target.")
    _build_current(placeholder_root)
    placeholder_item = _items(relation_queue.build_queue(placeholder_root))[0]
    target_file.unlink()
    with pytest.raises(ValueError, match=r"REVIEW_ITEM_CHANGED.*placeholder_target"):
        relation_queue.accept(
            placeholder_root,
            ref=placeholder_item["ref"],
            source_path=placeholder_item["source_path"],
            expected_hash=placeholder_item["source_content_hash"],
            expected_fingerprint=placeholder_item["fingerprint"],
            why="Synthetic placeholder guard proof.",
            edit_memory=lambda *_args, **_kwargs: pytest.fail(
                "placeholder candidate reached edit"
            ),
        )

    dismissed_root = tmp_path / "dismissed"
    _write(dismissed_root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(dismissed_root, target, "A target.")
    _build_current(dismissed_root)
    dismissed_item = _items(relation_queue.build_queue(dismissed_root))[0]
    relation_queue.triage(
        dismissed_root,
        ref=dismissed_item["ref"],
        source_path=dismissed_item["source_path"],
        action="dismiss",
    )
    with pytest.raises(ValueError, match=r"REVIEW_ITEM_CHANGED.*decided"):
        relation_queue.accept(
            dismissed_root,
            ref=dismissed_item["ref"],
            source_path=dismissed_item["source_path"],
            expected_hash=dismissed_item["source_content_hash"],
            expected_fingerprint=dismissed_item["fingerprint"],
            why="Synthetic dismissal guard proof.",
            edit_memory=lambda *_args, **_kwargs: pytest.fail(
                "dismissed candidate reached edit"
            ),
        )


def test_hintless_compatibility_is_limited_to_the_ordinary_queue_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    target = f"{KB}/zzzz-target.md"
    _write(root, target, "A target.")
    sources: list[str] = []
    for number in range(51):
        source = f"{KB}/source-{number:02d}.md"
        sources.append(source)
        _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _build_current(root)

    prefix = relation_queue.build_queue(root)
    assert prefix["pages_shown"] == relation_queue._DEFAULT_LIMIT_PAGES
    in_prefix = _items(prefix)[0]
    resolved = relation_queue.resolve_candidate(root, in_prefix["ref"])
    assert resolved.ref == in_prefix["ref"]

    outside_source = sources[-1]
    outside_candidate = {
        "from": outside_source,
        "to": target,
        "relation_type": "links_to",
        "method": "wikilink",
        "evidence": {
            "source_path": outside_source,
            "target": target.removesuffix(".md"),
        },
    }
    outside_ref = relation_queue.relation_review_ref(
        relation_queue._candidate_identity(outside_candidate)
    )
    with pytest.raises(ValueError, match="REVIEW_REFRESH_REQUIRED"):
        relation_queue.resolve_candidate(root, outside_ref)
    assert (
        relation_queue.resolve_candidate(
            root, outside_ref, source_path=outside_source
        ).ref
        == outside_ref
    )

    embedding_ref = relation_queue.relation_review_ref(
        relation_queue._candidate_identity(
            {
                "from": outside_source,
                "to": target,
                "relation_type": "relates_to",
                "method": "embedding_proximity",
            }
        )
    )
    with pytest.raises(ValueError, match="REVIEW_REFRESH_REQUIRED"):
        relation_queue.resolve_candidate(root, embedding_ref)


def test_queue_read_uses_published_snapshot_while_writer_boundary_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source = f"{KB}/source.md"
    target = f"{KB}/target.md"
    _write(root, source, f"See [[{target.removesuffix('.md')}]].")
    _write(root, target, "A target.")
    _build_current(root)

    held = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()
    reader_done = threading.Event()
    batch_while_held = threading.Event()
    errors: list[BaseException] = []
    result: dict[str, Any] = {}
    manager = writer_lease.active_manager()
    original_batch = epistemic_graph.EpistemicGraphIndex.relation_review_batch

    def observed_batch(self: Any, *args: Any, **kwargs: Any):
        if held.is_set() and not release.is_set():
            batch_while_held.set()
        return original_batch(self, *args, **kwargs)

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "relation_review_batch",
        observed_batch,
    )

    def writer() -> None:
        try:
            with manager.mutation_guard(root, operation="relation-queue-test-holder"):
                held.set()
                if not release.wait(10):
                    raise AssertionError("test did not release held writer boundary")
        except BaseException as exc:  # noqa: BLE001 - transported to test thread
            errors.append(exc)

    def reader() -> None:
        try:
            reader_started.set()
            result.update(relation_queue.build_queue(root))
        except BaseException as exc:  # noqa: BLE001 - transported to test thread
            errors.append(exc)
        finally:
            reader_done.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    assert held.wait(10)
    reader_thread.start()
    assert reader_started.wait(10)
    completed_before_release = reader_done.wait(10)
    release.set()
    writer_thread.join(10)
    reader_thread.join(10)

    assert errors == []
    assert completed_before_release is True
    assert batch_while_held.is_set()
    assert result["status"] == "available"
    assert result["groups"][0]["source_path"] == source
