from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from exomem import (
    corpus_aware,
    epistemic_graph,
    freshness,
    memory_refs,
    relation_queue,
    review_state,
    semantic_contract,
)

KB = "Knowledge Base/Notes/Insights"


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _legacy_relation_queue(
    root: Path,
    *,
    limit_pages: int,
    limit_per_page: int,
) -> dict[str, Any]:
    """Test-local pre-C Markdown oracle for graph-batch parity assertions.

    Production must return typed warming when the graph/identity snapshot is
    unavailable.  These Lane B tests still need the retired implementation as
    an independent oracle, so they compose its surviving private generation,
    classification, and enrichment leaves here without restoring a fallback.
    """
    scan = relation_queue.activation_module.scan(root)
    store = review_state.ReviewStateStore(root)
    state_payload = store.load()
    cap = max(0, int(limit_pages))
    filtered = {"authored_edge": 0, "placeholder_target": 0, "decided": 0}
    groups: list[dict[str, Any]] = []
    pages_scanned = 0
    scan_complete = True

    for page in relation_queue._ordered_pages(root, scan):
        if len(groups) >= cap:
            scan_complete = False
            break
        pages_scanned += 1
        authored = relation_queue._authored_targets(page, root)
        items: list[dict[str, Any]] = []
        for candidate in relation_queue._page_candidates(
            root, page, limit_per_page=limit_per_page
        ):
            reason, enriched = relation_queue._classify_candidate(
                root,
                page,
                candidate,
                store=store,
                state_payload=state_payload,
                authored=authored,
            )
            if reason is not None:
                filtered[reason] += 1
                continue
            assert enriched is not None
            items.append(enriched)
            if len(items) >= max(0, int(limit_per_page)):
                break
        if items:
            groups.append(
                {
                    "path": page.rel_path,
                    "title": page.title,
                    "content_hash": relation_queue._page_content_hash(page),
                    "items": items,
                }
            )

    eligible_pages_total = int(scan.coverage.get("eligible_pages", 0))
    shown_items = sum(len(group["items"]) for group in groups)
    coverage = dict(scan.coverage)
    coverage["relation_pages_scanned"] = pages_scanned
    coverage["relation_candidate_pages_found"] = len(groups)
    coverage["relation_candidates_found"] = shown_items
    coverage["relation_scan_complete"] = scan_complete
    return {
        "mode": "relation-queue",
        "mutated": False,
        "groups": groups,
        "shown": shown_items,
        "pages_shown": len(groups),
        "pages_scanned": pages_scanned,
        "pages_truncated": not scan_complete,
        "pages_unscanned": max(0, eligible_pages_total - pages_scanned),
        "filtered": filtered,
        "coverage": coverage,
    }


def _warm_identity_authority(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> semantic_contract.ReferenceIdentitySnapshot:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    freshness.rebaseline(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    semantic_contract.build_corpus_context(root)
    snapshot = semantic_contract.current_reference_identity_snapshot(root)
    assert snapshot is not None
    return snapshot


def _wikilink_item(queue: dict[str, Any], source_rel: str) -> dict[str, Any]:
    return next(
        item
        for group in queue["groups"]
        if group["path"] == source_rel
        for item in group["items"]
        if item["method"] == "wikilink"
    )


def _excluded_duplicate_case(
    root: Path, *, build_refs: bool = True
) -> tuple[str, str]:
    source_rel = f"{KB}/source.md"
    target_rel = f"{KB}/target.md"
    target_id = "22222222-2222-4222-8222-222222222222"
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n"
        "---\n# Source\n\n"
        f"See [[{target_rel.removesuffix('.md')}]].\n",
    )
    _write(
        root,
        target_rel,
        "---\ntype: insight\nstatus: active\n"
        f"exomem_id: {target_id}\n---\n# Target\n",
    )
    _write(
        root,
        "Knowledge Base/Records/private.md",
        f"---\nexomem_id: {target_id}\n---\n# Structured-only owner\n",
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    if build_refs:
        memory_refs.ReferenceIndex(root).rebuild_all()
    return source_rel, target_rel


class _CountingConnection:
    def __init__(self, connection: Any):
        self.connection = connection
        self.queries = 0

    def execute(self, *args, **kwargs):
        self.queries += 1
        return self.connection.execute(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self.connection, name)


def test_relation_review_batch_uses_one_snapshot_fixed_queries_and_exact_wikilink_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/source.md"
    target_rel = f"{KB}/TargetCase.md"
    _write(
        root,
        source_rel,
        f"""---
type: insight
status: active
exomem_id: 11111111-1111-4111-8111-111111111111
created: 2026-02-01
---
# Source

The first authored spelling is [[{KB}/TargetCase|display one]].
The duplicate is [[{KB}/targetcase|display two]].
""",
    )
    _write(
        root,
        target_rel,
        """---
type: insight
status: active
exomem_id: 22222222-2222-4222-8222-222222222222
---
# Target
""",
    )
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()
    page_path = root / source_rel
    page = epistemic_graph.find_module._parse_page(
        page_path, page_path.stat().st_mtime, root
    )
    legacy = epistemic_graph._wikilink_candidates(root, page.body, source_rel)[0]
    expected = relation_queue._enrich(root, page, legacy)
    _warm_identity_authority(root, monkeypatch)

    opens = 0
    counted: list[_CountingConnection] = []
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

    def open_counted(self, *args, **kwargs):
        nonlocal opens
        opens += 1
        connection = original_open(self, *args, **kwargs)
        if connection is None:
            return None
        wrapper = _CountingConnection(connection)
        counted.append(wrapper)
        return wrapper

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", open_counted
    )
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "_current_readonly_connection",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch opened refs SQL"),
    )
    monkeypatch.setattr(
        semantic_contract,
        "build_corpus_context",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch built corpus"),
    )
    monkeypatch.setattr(
        epistemic_graph.find_module,
        "_parse_page",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch parsed Markdown"),
    )
    monkeypatch.setattr(
        corpus_aware,
        "_best_cosine_per_file",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch invoked embeddings"),
    )

    result = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=5, limit_per_page=5
    )

    assert result["status"] == "available"
    assert opens == 1
    assert len(counted) == 1
    assert counted[0].queries <= 8
    wikilinks = [
        item
        for group in result["groups"]
        for item in group["items"]
        if item["method"] == "wikilink"
    ]
    assert len(wikilinks) == 1
    item = wikilinks[0]
    assert item["evidence"] == {
        "source_path": source_rel,
        "target": f"{KB}/TargetCase",
    }
    assert item["evidence"] == legacy["evidence"]
    assert item["review_id"] == expected["review_id"]
    assert item["ref"] == expected["ref"]
    assert item["fingerprint"] == expected["fingerprint"]
    assert item["signal_version"] == relation_queue._evidence_signal_version(page, legacy)
    assert item["internal_evidence"]["occurrence"] == 0
    assert item["internal_evidence"]["start"] < item["internal_evidence"]["end"]
    assert "occurrence" not in item["evidence"]
    assert result["pages_truncated"] is False
    assert result["coverage"]["eligible_pages"] == 2

    review_state.ReviewStateStore(root).apply(
        item["review_id"],
        item["fingerprint"],
        action="dismiss",
        why="Synthetic parity decision.",
    )
    dismissed = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=5, limit_per_page=5
    )
    assert not any(
        candidate["ref"] == item["ref"]
        for group in dismissed["groups"]
        for candidate in group["items"]
    )


def test_relation_review_batch_preserves_authored_wikilink_order_and_display_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/source.md"
    first_target = f"{KB}/z-target.md"
    second_target = f"{KB}/a-target.md"
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n---\n# Source\n\n"
        f"First [[{first_target.removesuffix('.md')}]].\n"
        f"Second [[{second_target.removesuffix('.md')}]].\n",
    )
    for target in (first_target, second_target):
        _write(
            root,
            target,
            f"---\ntype: insight\nstatus: active\n---\n# {Path(target).stem}\n",
        )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=10, limit_per_page=2)
    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=2
    )
    legacy_items = next(group for group in legacy["groups"] if group["path"] == source_rel)[
        "items"
    ]
    native_items = next(group for group in native["groups"] if group["path"] == source_rel)[
        "items"
    ]

    assert [item["to"] for item in native_items] == [
        item["to"] for item in legacy_items
    ] == [first_target, second_target]

    legacy_prefix = _legacy_relation_queue(root, limit_pages=10, limit_per_page=1)
    native_prefix = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=1
    )
    legacy_prefix_items = next(
        group for group in legacy_prefix["groups"] if group["path"] == source_rel
    )["items"]
    native_prefix_items = next(
        group for group in native_prefix["groups"] if group["path"] == source_rel
    )["items"]
    assert [item["to"] for item in native_prefix_items] == [
        item["to"] for item in legacy_prefix_items
    ] == [first_target]


def test_relation_review_batch_preserves_frontmatter_source_order_and_display_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/source.md"
    first_target = "Knowledge Base/Sources/z-source.md"
    second_target = "Knowledge Base/Sources/a-source.md"
    for target in (first_target, second_target):
        _write(
            root,
            target,
            f"---\ntype: source\nsource_type: article\n---\n# {Path(target).stem}\n",
        )
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\nsources:\n"
        f'  - "[[{first_target.removesuffix(".md")}]]"\n'
        f'  - "[[{second_target.removesuffix(".md")}]]"\n'
        "---\n# Source\n",
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=10, limit_per_page=2)
    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=2
    )
    legacy_items = next(group for group in legacy["groups"] if group["path"] == source_rel)[
        "items"
    ]
    native_items = next(group for group in native["groups"] if group["path"] == source_rel)[
        "items"
    ]

    assert [item["to"] for item in native_items] == [
        item["to"] for item in legacy_items
    ] == [first_target, second_target]
    assert [item["evidence"] for item in native_items] == [
        item["evidence"] for item in legacy_items
    ] == [{"source_path": source_rel, "field": "sources"}] * 2

    legacy_prefix = _legacy_relation_queue(root, limit_pages=10, limit_per_page=1)
    native_prefix = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=1
    )
    legacy_prefix_items = next(
        group for group in legacy_prefix["groups"] if group["path"] == source_rel
    )["items"]
    native_prefix_items = next(
        group for group in native_prefix["groups"] if group["path"] == source_rel
    )["items"]
    assert [item["to"] for item in native_prefix_items] == [
        item["to"] for item in legacy_prefix_items
    ] == [first_target]

    with sqlite3.connect(epistemic_graph.sidecar_path(root)) as connection:
        rows = connection.execute(
            "SELECT dst_key, review_evidence FROM graph_edges "
            "WHERE source_path = ? AND origin = 'frontmatter' "
            "AND source_anchor = 'sources' ORDER BY dst_key",
            (source_rel,),
        ).fetchall()
    occurrences = {
        str(dst_key).removeprefix("file:"): json.loads(str(evidence))["internal"][
            "occurrence"
        ]
        for dst_key, evidence in rows
    }
    assert occurrences == {first_target: 0, second_target: 1}


def test_relation_review_batch_uses_current_identity_census_for_excluded_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_rel, _target_rel = _excluded_duplicate_case(root)
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=10, limit_per_page=10)
    _warm_identity_authority(root, monkeypatch)
    legacy_item = next(
        item
        for group in legacy["groups"]
        if group["path"] == source_rel
        for item in group["items"]
        if item["method"] == "wikilink"
    )

    monkeypatch.setattr(
        memory_refs,
        "_scan_pages",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch scanned Markdown"),
    )
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "refs_for_paths",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch used repairing refs API"),
    )
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "_current_readonly_connection",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch opened refs SQL"),
    )
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "refresh_paths",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch refreshed refs"),
    )
    monkeypatch.setattr(
        epistemic_graph.find_module,
        "_parse_page",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch parsed Markdown"),
    )

    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=10
    )
    native_item = next(
        item
        for group in native["groups"]
        if group["path"] == source_rel
        for item in group["items"]
        if item["method"] == "wikilink"
    )

    assert native_item["review_id"] == legacy_item["review_id"]
    assert native_item["target_ref"] == legacy_item["target_ref"]
    assert native_item["fingerprint"] == legacy_item["fingerprint"]


def test_relation_review_batch_honors_dismissal_with_excluded_duplicate_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_rel, _target_rel = _excluded_duplicate_case(root)
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=10, limit_per_page=10)
    _warm_identity_authority(root, monkeypatch)
    legacy_item = next(
        item
        for group in legacy["groups"]
        if group["path"] == source_rel
        for item in group["items"]
        if item["method"] == "wikilink"
    )
    review_state.ReviewStateStore(root).apply(
        legacy_item["review_id"],
        legacy_item["fingerprint"],
        action="dismiss",
        why="Excluded duplicate identity must not reopen this signal.",
    )

    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=10
    )

    assert not any(
        item["review_id"] == legacy_item["review_id"]
        for group in native["groups"]
        for item in group["items"]
    )


def test_relation_review_batch_declines_identity_repair_when_census_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    _excluded_duplicate_case(root, build_refs=False)
    assert not memory_refs.sidecar_path(root).exists()
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "rebuild_all",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch rebuilt refs"),
    )
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "refresh_paths",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch refreshed refs"),
    )
    monkeypatch.setattr(
        memory_refs,
        "_scan_pages",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch scanned Markdown"),
    )
    monkeypatch.setattr(
        semantic_contract,
        "build_corpus_context",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch built corpus"),
    )

    result = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=10
    )

    assert result["status"] == "warming"
    assert result["groups"] == []


@pytest.mark.parametrize("operation", ("add", "delete", "change"))
def test_relation_review_batch_ignores_stale_refs_for_excluded_duplicate_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/source.md"
    target_rel = f"{KB}/target.md"
    record_rel = "Knowledge Base/Records/private.md"
    target_id = "22222222-2222-4222-8222-222222222222"
    other_id = "33333333-3333-4333-8333-333333333333"
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n"
        "---\n# Source\n\n"
        f"See [[{target_rel.removesuffix('.md')}]].\n",
    )
    _write(
        root,
        target_rel,
        "---\ntype: insight\nstatus: active\n"
        f"exomem_id: {target_id}\n---\n# Target\n",
    )
    record = root / record_rel
    if operation in {"delete", "change"}:
        _write(
            root,
            record_rel,
            f"---\nexomem_id: {target_id}\n---\n# Initial duplicate\n",
        )
    refs = memory_refs.ReferenceIndex(root)
    refs.rebuild_all()
    stale_sidecar = memory_refs.sidecar_path(root).read_bytes()

    if operation == "add":
        _write(
            root,
            record_rel,
            f"---\nexomem_id: {target_id}\n---\n# Added duplicate\n",
        )
    elif operation == "delete":
        record.unlink()
    else:
        record.write_text(
            f"---\nexomem_id: {other_id}\n---\n# Changed owner\n",
            encoding="utf-8",
        )

    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    refs.rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})
    legacy = _legacy_relation_queue(root, limit_pages=10, limit_per_page=10)
    legacy_item = _wikilink_item(legacy, source_rel)
    memory_refs.sidecar_path(root).write_bytes(stale_sidecar)
    _warm_identity_authority(root, monkeypatch)

    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "_current_readonly_connection",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch opened stale refs SQL"),
    )
    monkeypatch.setattr(
        memory_refs,
        "_scan_pages",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch scanned Markdown"),
    )
    monkeypatch.setattr(
        semantic_contract,
        "build_corpus_context",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch built corpus"),
    )
    monkeypatch.setattr(
        epistemic_graph.find_module,
        "_parse_page",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch parsed Markdown"),
    )

    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=10
    )

    assert native["status"] == "available"
    native_item = _wikilink_item(native, source_rel)
    assert native_item["target_ref"] == legacy_item["target_ref"]
    assert native_item["fingerprint"] == legacy_item["fingerprint"]


def test_relation_review_batch_keeps_canonical_dismissal_when_refs_are_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/source.md"
    target_rel = f"{KB}/target.md"
    target_id = "22222222-2222-4222-8222-222222222222"
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n---\n# Source\n\n"
        f"See [[{target_rel.removesuffix('.md')}]].\n",
    )
    _write(
        root,
        target_rel,
        "---\ntype: insight\nstatus: active\n"
        f"exomem_id: {target_id}\n---\n# Target\n",
    )
    refs = memory_refs.ReferenceIndex(root)
    refs.rebuild_all()
    stale_sidecar = memory_refs.sidecar_path(root).read_bytes()
    _write(
        root,
        "Knowledge Base/Records/private.md",
        f"---\nexomem_id: {target_id}\n---\n# Added duplicate\n",
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    refs.rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})
    legacy = _legacy_relation_queue(root, limit_pages=10, limit_per_page=10)
    legacy_item = _wikilink_item(legacy, source_rel)
    review_state.ReviewStateStore(root).apply(
        legacy_item["review_id"],
        legacy_item["fingerprint"],
        action="dismiss",
        why="Canonical duplicate-aware dismissal remains authoritative.",
    )
    memory_refs.sidecar_path(root).write_bytes(stale_sidecar)
    _warm_identity_authority(root, monkeypatch)
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "_current_readonly_connection",
        lambda *_args, **_kwargs: pytest.fail("graph-native batch opened stale refs SQL"),
    )

    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=10
    )

    assert native["status"] == "available"
    assert not any(
        item["review_id"] == legacy_item["review_id"]
        for group in native["groups"]
        for item in group["items"]
    )


@pytest.mark.parametrize("movement", ("before_graph_open", "after_graph_open"))
def test_relation_review_batch_revalidates_identity_checkpoint_around_graph_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, movement: str
) -> None:
    root = tmp_path / "vault"
    source_rel, _target_rel = _excluded_duplicate_case(root)
    _warm_identity_authority(root, monkeypatch)
    original_snapshot = semantic_contract.current_reference_identity_snapshot
    snapshot_captured = False

    def capture_snapshot(vault_root: Path):
        nonlocal snapshot_captured
        snapshot_captured = True
        return original_snapshot(vault_root)

    monkeypatch.setattr(
        semantic_contract, "current_reference_identity_snapshot", capture_snapshot
    )
    if movement == "before_graph_open":
        original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

        def move_before_open(self, *args, **kwargs):
            assert snapshot_captured, "identity snapshot must precede graph open"
            semantic_contract.evict_corpus_context(root)
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(
            epistemic_graph.EpistemicGraphIndex,
            "_open_read_snapshot",
            move_before_open,
        )
    else:
        original_load = review_state.ReviewStateStore.load

        def move_after_open(self, *args, **kwargs):
            semantic_contract.evict_corpus_context(root)
            return original_load(self, *args, **kwargs)

        monkeypatch.setattr(review_state.ReviewStateStore, "load", move_after_open)

    result = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=10
    )

    assert snapshot_captured is True
    assert result["status"] == "warming"
    assert result["groups"] == []


def test_relation_review_batch_rejects_replaced_context_reusing_identity_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    _source_rel, _target_rel = _excluded_duplicate_case(root)
    captured = _warm_identity_authority(root, monkeypatch)
    cache_key = semantic_contract._corpus_cache_key(root)
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot
    replaced = False

    def open_then_replace_context(self, *args, **kwargs):
        nonlocal replaced
        connection = original_open(self, *args, **kwargs)
        assert connection is not None
        with semantic_contract._CORPUS_CONTEXT_UPDATE_LOCK:
            with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
                entry = semantic_contract._CORPUS_CONTEXT_CACHE[cache_key]
                replacement = replace(entry[1])
                assert replacement is not entry[1]
                assert replacement.identity_census is entry[1].identity_census
                assert (
                    semantic_contract._CORPUS_CONTEXT_EVENT_CHECKPOINTS[cache_key]
                    == captured.checkpoint
                )
                semantic_contract._CORPUS_CONTEXT_CACHE[cache_key] = (
                    entry[0],
                    replacement,
                )
        replaced = True
        return connection

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_open_read_snapshot",
        open_then_replace_context,
    )

    result = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=10, limit_per_page=10
    )

    assert replaced is True
    assert result["status"] == "warming"
    assert result["groups"] == []


def test_relation_review_batch_matches_all_graph_native_legacy_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_doc = "Knowledge Base/Sources/Articles/shared-source.md"
    _write(
        root,
        source_doc,
        "---\ntype: source\nsource_type: article\ningested_into: []\n---\n"
        "# Shared Source\n\n## Capture\n\nMaterial.\n",
    )
    question = "Why is the queue latency spiky?"
    _write(
        root,
        f"{KB}/neighbour.md",
        "---\ntype: insight\nstatus: active\nsources:\n"
        f'  - "[[{source_doc.removesuffix(".md")}]]"\n---\n'
        f"# Neighbour\n\n## Open Question\n- id: q-n\n\n{question}\n",
    )
    _write(
        root,
        f"{KB}/target.md",
        "---\ntype: insight\nstatus: active\n---\n# Target\n",
    )
    source_rel = f"{KB}/source.md"
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n"
        "exomem_id: 33333333-3333-4333-8333-333333333333\nsources:\n"
        f'  - "[[{source_doc.removesuffix(".md")}]]"\n---\n'
        f"# Source\n\nSee [[{KB}/target]].\n\n"
        f"## Open Question\n- id: q-s\n- relations: answers: [[{KB}/target]]\n\n"
        f"{question}\n\n## Claim\n- id: c-s\n"
        f"- relations: resolves: [[{KB}/target]]\n\nA claim.\n",
    )
    _write(
        root,
        f"{KB}/rival.md",
        "---\ntype: insight\nstatus: active\n---\n# Rival\n\n"
        "## Claim\n- id: c-r\n"
        f"- relations: answers: [[{KB}/target]]\n\nA rival answer.\n",
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=50, limit_per_page=50)
    _warm_identity_authority(root, monkeypatch)
    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=50, limit_per_page=50
    )
    legacy_items = next(group for group in legacy["groups"] if group["path"] == source_rel)[
        "items"
    ]
    native_items = next(group for group in native["groups"] if group["path"] == source_rel)[
        "items"
    ]
    public_fields = (
        "review_id",
        "ref",
        "fingerprint",
        "from",
        "to",
        "relation_type",
        "method",
        "evidence",
        "bullet",
        "target_ref",
        "state",
    )
    expected = [{key: item[key] for key in public_fields} for item in legacy_items]
    actual = [{key: item[key] for key in public_fields} for item in native_items]

    assert [group["path"] for group in native["groups"]] == [
        group["path"] for group in legacy["groups"]
    ]
    assert native["coverage"] == legacy["coverage"]
    assert native["pages_scanned"] == legacy["pages_scanned"]
    assert native["filtered"] == legacy["filtered"]
    assert {item["method"] for item in actual} == {
        "unit_relation_lift",
        "shared_open_question",
        "shared_resolution_target",
        "wikilink",
        "frontmatter_sources",
        "shared_sources",
    }
    assert actual == expected


def test_relation_review_batch_has_honest_caps_and_no_inverse_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/source.md"
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n---\n# Source\n\n"
        + "\n".join(
            f"See [[{KB}/target-{index}]]." for index in range(6)
        ),
    )
    for index in range(6):
        _write(
            root,
            f"{KB}/target-{index}.md",
            f"---\ntype: insight\nstatus: active\n---\n# Target {index}\n",
        )
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()

    result = index.relation_review_batch(limit_pages=1, limit_per_page=2)

    assert result["status"] == "available"
    assert len(result["groups"]) == 1
    assert len(result["groups"][0]["items"]) == 2
    assert result["items_truncated"] is True
    assert result["pages_truncated"] is True
    assert all(item["relation_type"] == "links_to" for item in result["groups"][0]["items"])
    assert not any(
        item["from"].startswith(f"{KB}/target-") and item["to"] == source_rel
        for group in result["groups"]
        for item in group["items"]
    )


@pytest.mark.parametrize(
    ("candidate_count", "expected_truncated"),
    ((64, False), (65, True)),
    ids=("exact-cap", "cap-plus-one"),
)
def test_relation_review_batch_reports_per_source_sql_overflow_honestly(
    tmp_path: Path,
    candidate_count: int,
    expected_truncated: bool,
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/000-source.md"
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n---\n# Source\n\n"
        + "\n".join(
            f"See [[{KB}/target-{index:03d}]]." for index in range(candidate_count)
        ),
    )
    for index in range(candidate_count):
        _write(
            root,
            f"{KB}/target-{index:03d}.md",
            f"---\ntype: insight\nstatus: active\n---\n# Target {index}\n",
        )
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()

    result = index.relation_review_batch(limit_pages=1, limit_per_page=64)

    assert result["status"] == "available"
    assert len(result["groups"]) == 1
    assert len(result["groups"][0]["items"]) == 64
    assert result["items_truncated"] is expected_truncated


def test_relation_review_batch_suppresses_authored_membership_across_opposing_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    source_rel = f"{KB}/000-source.md"
    authored = "\n".join(
        f"- links_to [[{KB}/target-{index:03d}]]" for index in range(65)
    )
    opposing_wikilinks = "\n".join(
        f"See [[{KB}/target-{index:03d}]]." for index in (64, *range(63))
    )
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n---\n# Source\n\n"
        f"## Relations\n\n{authored}\n\n## Notes\n\n{opposing_wikilinks}\n",
    )
    for index in range(65):
        _write(
            root,
            f"{KB}/target-{index:03d}.md",
            f"---\ntype: insight\nstatus: active\n---\n# Target {index}\n",
        )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=50, limit_per_page=64)
    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=50, limit_per_page=64
    )

    assert legacy["filtered"]["authored_edge"] == 64
    assert native["filtered"]["authored_edge"] == legacy["filtered"][
        "authored_edge"
    ]
    assert not any(
        item["from"] == source_rel
        for group in native["groups"]
        for item in group["items"]
    )


def test_relation_review_batch_matches_placeholder_and_authored_filter_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    ghost = "Knowledge Base/Sources/ghost-source.md"
    _write(
        root,
        f"{KB}/placeholder.md",
        "---\ntype: insight\nstatus: active\nsources:\n"
        f"  - {ghost}\n---\n# Placeholder\n\nA dangling source.\n",
    )
    target_rel = f"{KB}/target.md"
    _write(
        root,
        target_rel,
        "---\ntype: insight\nstatus: active\n---\n# Target\n",
    )
    _write(
        root,
        f"{KB}/authored.md",
        "---\ntype: insight\nstatus: active\n---\n# Authored\n\n"
        f"## Relations\n\n- links_to [[{target_rel.removesuffix('.md')}]]\n\n"
        f"Also see [[{target_rel.removesuffix('.md')}]].\n",
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=50, limit_per_page=50)
    _warm_identity_authority(root, monkeypatch)
    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=50, limit_per_page=50
    )

    assert native["filtered"]["placeholder_target"] == legacy["filtered"][
        "placeholder_target"
    ] == 1
    assert native["filtered"]["authored_edge"] == legacy["filtered"][
        "authored_edge"
    ] == 1


def test_relation_review_batch_identity_source_keeps_placeholder_path_and_filter_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    ghost = "Knowledge Base/Sources/ghost-source.md"
    target_rel = f"{KB}/target.md"
    source_rel = f"{KB}/identity-source.md"
    _write(
        root,
        target_rel,
        "---\ntype: insight\nstatus: active\n---\n# Target\n",
    )
    _write(
        root,
        source_rel,
        "---\ntype: insight\nstatus: active\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n"
        "sources:\n"
        f"  - {ghost}\n---\n# Identity source\n\n"
        f"## Relations\n\n- links_to [[{target_rel.removesuffix('.md')}]]\n\n"
        f"Also see [[{target_rel.removesuffix('.md')}]].\n",
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    memory_refs.ReferenceIndex(root).rebuild_all()
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *_args, **_kwargs: {})

    legacy = _legacy_relation_queue(root, limit_pages=50, limit_per_page=50)
    _warm_identity_authority(root, monkeypatch)
    native = epistemic_graph.EpistemicGraphIndex(root).relation_review_batch(
        limit_pages=50, limit_per_page=50
    )

    assert native["status"] == "available"
    assert native["filtered"] == legacy["filtered"]
    assert native["filtered"]["authored_edge"] == 1
    assert native["filtered"]["placeholder_target"] == 1
    assert all(
        item["to"] != ghost
        for group in native["groups"]
        for item in group["items"]
    )
    graph = epistemic_graph.EpistemicGraphIndex(root)
    placeholder_edge = next(
        edge
        for edge in graph.edges(source_path=source_rel)
        if edge["origin"] == "frontmatter" and edge["source_anchor"] == "sources"
    )
    assert placeholder_edge["dst_key"] == f"file:{ghost}"
