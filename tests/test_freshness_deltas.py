"""Task 1.4 — RED: atomic freshness consumer deltas + sidecar delta apply.

Pins OpenSpec change ``restore-indexed-category-recall`` decision 5 and specs
*Freshness Registry Exposes Atomic Consumer Deltas*, *Unknown Delta Never
Returns A Partial Suffix*, and *Delta Application Advances Checkpoint
Atomically*:

* each live scope registry carries a process-instance id and a monotonic
  generation; a consumer checkpoint is ``{instance_id, generation, triple}``;
* ``delta_since`` returns ``{from, to, complete, changed, deleted}`` for one
  captured target generation, with duplicate-free, mutually disjoint,
  target-state-coalesced ``changed``/``deleted`` sets;
* multiple consumers read non-destructively; a later event after a captured
  ``to`` stays discoverable from that ``to``;
* restart/foreign instance, reconciliation mismatch, an over-old checkpoint, and
  history overflow return ``complete=false`` with no partial suffix;
* a sidecar consumer applies a complete delta's upserts/deletes and the exact
  target checkpoint in one transaction; on rollback neither rows nor checkpoint
  advance.

RED until ``freshness.consumer_checkpoint`` / ``freshness.delta_since`` /
``freshness.FreshnessCheckpoint`` and the sidecar ``apply_catalog_delta`` /
``catalog_checkpoint`` seams exist.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore

needs_fts5 = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5"
)


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    freshness.clear()
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    yield
    freshness.clear()
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()


def _kb_file(root: Path, name: str, body: str = "body") -> Path:
    path = root / "Knowledge Base" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"delta:{name}")
    path.write_text(
        f"---\ntype: insight\ntitle: {path.stem}\nexomem_id: {page_id}\n"
        f"updated: 2026-01-01\n---\n# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed(root: Path, paths: list[Path]) -> None:
    entries = [(str(p), freshness.stat_signature(p)) for p in paths]
    freshness.seed(root, "kb", entries)
    freshness.seed(root, "vault", entries)


def _touch_future(path: Path) -> None:
    future = time.time() + 10_000
    os.utime(path, (future, future))


# --------------------------------------------------------------------------- #
# Checkpoint identity + monotonic generation.
# --------------------------------------------------------------------------- #


def test_consumer_checkpoint_names_instance_generation_and_triple(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])

    checkpoint = freshness.consumer_checkpoint(tmp_path, "kb")
    assert isinstance(checkpoint.instance_id, str) and checkpoint.instance_id
    assert isinstance(checkpoint.generation, int)
    assert checkpoint.triple == freshness.triple(tmp_path, "kb")


def test_generation_advances_monotonically_on_each_event(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])
    first = freshness.consumer_checkpoint(tmp_path, "kb").generation

    b = _kb_file(tmp_path, "b.md")
    freshness.on_files_changed(tmp_path, changed=[b])
    second = freshness.consumer_checkpoint(tmp_path, "kb").generation

    _touch_future(a)
    freshness.on_files_changed(tmp_path, changed=[a])
    third = freshness.consumer_checkpoint(tmp_path, "kb").generation

    assert first < second < third


# --------------------------------------------------------------------------- #
# Atomic delta shape + target-state coalescing.
# --------------------------------------------------------------------------- #


def test_edit_and_rename_have_exact_representations(tmp_path: Path) -> None:
    edited = _kb_file(tmp_path, "edited.md")
    moved = _kb_file(tmp_path, "moved-src.md")
    _seed(tmp_path, [edited, moved])
    start = freshness.consumer_checkpoint(tmp_path, "kb")

    _touch_future(edited)
    freshness.on_files_changed(tmp_path, changed=[edited])
    dest = moved.with_name("moved-dest.md")
    os.replace(moved, dest)
    freshness.on_files_changed(tmp_path, changed=[dest], deleted=[moved])

    delta = freshness.delta_since(tmp_path, "kb", start)
    assert delta.complete is True
    assert str(edited) in delta.changed
    assert str(dest) in delta.changed
    assert str(moved) in delta.deleted
    assert delta.changed.isdisjoint(delta.deleted)
    # `to` identifies the exact snapshot that contains those events.
    assert delta.to.generation == freshness.consumer_checkpoint(tmp_path, "kb").generation
    assert delta.to.triple == freshness.triple(tmp_path, "kb")
    assert delta.from_ == start


def test_edit_then_delete_coalesces_to_deletion(tmp_path: Path) -> None:
    p = _kb_file(tmp_path, "vanishing.md")
    _seed(tmp_path, [p])
    start = freshness.consumer_checkpoint(tmp_path, "kb")

    _touch_future(p)
    freshness.on_files_changed(tmp_path, changed=[p])
    p.unlink()
    freshness.on_files_changed(tmp_path, deleted=[p])

    delta = freshness.delta_since(tmp_path, "kb", start)
    assert delta.complete is True
    assert str(p) in delta.deleted
    assert str(p) not in delta.changed


def test_delete_then_recreate_coalesces_to_change(tmp_path: Path) -> None:
    p = _kb_file(tmp_path, "reborn.md")
    _seed(tmp_path, [p])
    start = freshness.consumer_checkpoint(tmp_path, "kb")

    p.unlink()
    freshness.on_files_changed(tmp_path, deleted=[p])
    _kb_file(tmp_path, "reborn.md", body="recreated body")
    freshness.on_files_changed(tmp_path, changed=[p])

    delta = freshness.delta_since(tmp_path, "kb", start)
    assert delta.complete is True
    assert str(p) in delta.changed
    assert str(p) not in delta.deleted


# --------------------------------------------------------------------------- #
# Non-destructive reads + concurrency.
# --------------------------------------------------------------------------- #


def test_multiple_consumers_read_the_delta_non_destructively(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])
    start = freshness.consumer_checkpoint(tmp_path, "kb")

    b = _kb_file(tmp_path, "b.md")
    freshness.on_files_changed(tmp_path, changed=[b])

    first = freshness.delta_since(tmp_path, "kb", start)
    second = freshness.delta_since(tmp_path, "kb", start)
    assert first.changed == second.changed == {str(b)}
    assert first.to == second.to


def test_later_event_remains_discoverable_after_captured_to(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])
    start = freshness.consumer_checkpoint(tmp_path, "kb")

    b = _kb_file(tmp_path, "b.md")
    freshness.on_files_changed(tmp_path, changed=[b])
    first = freshness.delta_since(tmp_path, "kb", start)
    captured_to = first.to

    # A later event arrives after the first delta captured its target.
    c = _kb_file(tmp_path, "c.md")
    freshness.on_files_changed(tmp_path, changed=[c])

    # The already-captured `to` is unchanged, and requesting from it returns the
    # later event exactly once.
    assert first.to == captured_to
    follow = freshness.delta_since(tmp_path, "kb", captured_to)
    assert follow.complete is True
    assert follow.changed == {str(c)}


# --------------------------------------------------------------------------- #
# Incompleteness: never a partial suffix presented as complete.
# --------------------------------------------------------------------------- #


def test_foreign_instance_checkpoint_is_incomplete(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])
    live = freshness.consumer_checkpoint(tmp_path, "kb")
    foreign = freshness.FreshnessCheckpoint("foreign-instance", live.generation, live.triple)

    delta = freshness.delta_since(tmp_path, "kb", foreign)
    assert delta.complete is False
    assert not delta.changed
    assert not delta.deleted


def test_same_generation_with_wrong_triple_is_incomplete(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])
    live = freshness.consumer_checkpoint(tmp_path, "kb")
    wrong = live._replace(triple=(999, 999, "wrong"))

    delta = freshness.delta_since(tmp_path, "kb", wrong)
    assert delta.complete is False
    assert not delta.changed
    assert not delta.deleted


def test_reconciliation_mismatch_is_incomplete(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])
    start = freshness.consumer_checkpoint(tmp_path, "kb")

    # A missed event healed only by a drifted reconcile cannot yield a complete
    # delta across the gap.
    b = _kb_file(tmp_path, "b.md")
    fresh_entries = [
        (str(p), freshness.stat_signature(p))
        for p in find_module._walk_md(tmp_path / "Knowledge Base")
    ]
    reconcile_delta = freshness.reconcile(tmp_path, "kb", fresh_entries)
    assert reconcile_delta.drifted is True
    assert str(b) in reconcile_delta.changed

    delta = freshness.delta_since(tmp_path, "kb", start)
    assert delta.complete is False


def test_history_overflow_is_explicitly_incomplete(tmp_path: Path) -> None:
    a = _kb_file(tmp_path, "a.md")
    _seed(tmp_path, [a])
    start = freshness.consumer_checkpoint(tmp_path, "kb")

    limit = freshness.DELTA_HISTORY_LIMIT
    for index in range(limit + 5):
        target = _kb_file(tmp_path, f"churn-{index:04d}.md")
        freshness.on_files_changed(tmp_path, changed=[target])

    delta = freshness.delta_since(tmp_path, "kb", start)
    assert delta.complete is False
    # An incomplete response must not present a retained suffix as the full delta.
    assert not delta.changed
    assert not delta.deleted


# --------------------------------------------------------------------------- #
# Pre-initialization: a checkpoint taken before the first live snapshot is never
# a complete empty baseline for a later live corpus.
# --------------------------------------------------------------------------- #


def _kb_entries(root: Path) -> list[tuple[str, Any]]:
    return [
        (str(p), freshness.stat_signature(p))
        for p in find_module._walk_md(root / "Knowledge Base")
    ]


def test_uninitialized_scope_checkpoint_never_reads_as_complete(tmp_path: Path) -> None:
    # No seed, no reconcile: the scope is not live. Its checkpoint is the reserved
    # pre-initialization marker (generation 0, triple None). A complete empty delta
    # here would falsely prove an empty corpus; it must read as incomplete instead.
    checkpoint = freshness.consumer_checkpoint(tmp_path, "kb")
    assert checkpoint.generation == 0
    assert checkpoint.triple is None

    delta = freshness.delta_since(tmp_path, "kb", checkpoint)
    assert delta.complete is False
    assert not delta.changed
    assert not delta.deleted


def test_first_reconcile_mints_a_positive_generation(tmp_path: Path) -> None:
    # A scope initialized by the safety-net reconcile (old is None), not seed, must
    # still mint a strictly-positive generation for its first live snapshot — a
    # generation 0 here would let a pre-init checkpoint bridge across it.
    pre_init = freshness.consumer_checkpoint(tmp_path, "kb")
    assert pre_init.generation == 0

    _kb_file(tmp_path, "a.md")
    reconciled = freshness.reconcile(tmp_path, "kb", _kb_entries(tmp_path))
    assert reconciled.drifted is False  # first initialization is not a drift

    after = freshness.consumer_checkpoint(tmp_path, "kb")
    assert after.generation > 0
    assert after.generation > pre_init.generation
    assert after.triple is not None
    assert after.triple == freshness.triple(tmp_path, "kb")


def test_pre_initialization_checkpoint_then_first_reconcile_is_incomplete(
    tmp_path: Path,
) -> None:
    # The core regression: a consumer captures its checkpoint before the scope has
    # any live snapshot, then the live corpus appears via the first reconcile. The
    # delta from that pre-init checkpoint must NOT report a complete empty change
    # set (which would falsely prove the catalog already covers the live corpus).
    pre_init = freshness.consumer_checkpoint(tmp_path, "kb")
    assert pre_init.generation == 0 and pre_init.triple is None

    _kb_file(tmp_path, "a.md")
    freshness.reconcile(tmp_path, "kb", _kb_entries(tmp_path))

    delta = freshness.delta_since(tmp_path, "kb", pre_init)
    assert delta.complete is False
    assert not delta.changed
    assert not delta.deleted


def test_pre_initialization_checkpoint_never_coalesces_a_live_suffix(
    tmp_path: Path,
) -> None:
    # Even after the first reconcile AND a later live event, the pre-init checkpoint
    # cannot bridge the first snapshot: the later event must never surface as a
    # partial suffix presented as complete (which would silently omit the entire
    # first snapshot while claiming completeness).
    pre_init = freshness.consumer_checkpoint(tmp_path, "kb")

    _kb_file(tmp_path, "a.md")
    freshness.reconcile(tmp_path, "kb", _kb_entries(tmp_path))
    b = _kb_file(tmp_path, "b.md")
    freshness.on_files_changed(tmp_path, changed=[b])

    delta = freshness.delta_since(tmp_path, "kb", pre_init)
    assert delta.complete is False
    assert not delta.changed
    assert not delta.deleted


# --------------------------------------------------------------------------- #
# Sidecar delta application is transactional.
# --------------------------------------------------------------------------- #


@needs_fts5
def test_sidecar_delta_apply_rolls_back_rows_and_checkpoint_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _kb_file(tmp_path, "a.md", body="- [config] original ^orig")
    _seed(tmp_path, [a])
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    before_checkpoint = store.catalog_checkpoint("kb")

    # One missed edit as a complete delta from the catalog checkpoint.
    a.write_text(
        a.read_text(encoding="utf-8").replace("original ^orig", "patched ^orig"),
        encoding="utf-8",
    )
    _touch_future(a)
    freshness.on_files_changed(tmp_path, changed=[a])
    delta = freshness.delta_since(tmp_path, "kb", before_checkpoint)
    assert delta.complete is True

    def _failing_insert(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("apply failed mid-transaction")

    monkeypatch.setattr(store, "_insert_page", _failing_insert)
    with pytest.raises(RuntimeError, match="apply failed mid-transaction"):
        store.apply_catalog_delta("kb", delta)

    # Neither the rows nor the checkpoint may advance on rollback.
    assert store.catalog_checkpoint("kb") == before_checkpoint
    monkeypatch.undo()
    # Inspect the rolled-back catalog snapshot itself. A normal live search is
    # deliberately allowed to notice the edited Markdown and heal; using it
    # here would test stale-serving behavior rather than transaction rollback.
    still_original = lexstore.search_semantic_units(
        tmp_path,
        "original",
        k=5,
        categories=["config"],
        scope="kb",
        freshness=before_checkpoint.triple,
        _validate_current=False,
        repair=False,
    )
    assert still_original and still_original[0].content.strip().endswith("original")
