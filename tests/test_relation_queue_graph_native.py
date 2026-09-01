from __future__ import annotations

import threading
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
    relation_queue,
    semantic_contract,
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
