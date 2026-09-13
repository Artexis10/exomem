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
import sqlite3
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
    delta = freshness.recall_delta_since(tmp_path, "kb", before_checkpoint)
    assert delta.complete is True

    def _failing_insert(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("apply failed mid-transaction")

    monkeypatch.setattr(store, "_insert_page", _failing_insert)
    with pytest.raises(RuntimeError, match="apply failed mid-transaction"):
        store.apply_catalog_delta("kb", delta)

    # Neither the rows nor the checkpoint may advance on rollback.
    assert store.catalog_checkpoint("kb") == before_checkpoint
    # Inspect the rolled-back SQLite snapshot directly. A public live search is
    # deliberately allowed to notice the edited Markdown and apply the now-valid
    # projected delta; that would test healing, not transaction rollback.
    with sqlite3.connect(store.path) as conn:
        contents = {
            row[0] for row in conn.execute("SELECT content FROM semantic_units")
        }
    assert any(content.strip().endswith("original") for content in contents)
    assert not any("patched" in content for content in contents)


# --- adopted origins: a checkpoint this process did not publish --------------


def _foreign(checkpoint: freshness.RecallFreshnessCheckpoint):
    """The same checkpoint as another process instance would have stamped it."""
    return checkpoint._replace(instance_id=uuid.uuid4().hex)


def test_a_foreign_recall_origin_is_unbridgeable_until_it_is_adopted(
    tmp_path: Path,
) -> None:
    """The refusal that made a replacement worker rebuild its whole vault.

    A worker replacement inherits a derived sidecar stamped with the previous
    process's instance id. Nothing here has seen that checkpoint, so every delta
    from it is incomplete and the graph's bounded repair -- which is a delta
    from exactly that point -- has no lineage to advance
    (`seamless-managed-worker-handoff`).
    """
    page = _kb_file(tmp_path, "adopted-origin.md")
    _seed(tmp_path, [page])
    inherited = _foreign(freshness.recall_checkpoint(tmp_path, "vault"))

    assert freshness.recall_delta_since(tmp_path, "vault", inherited).complete is False

    assert freshness.adopt_recall_origin(tmp_path, "vault", inherited) is True
    adopted = freshness.recall_delta_since(tmp_path, "vault", inherited)

    assert adopted.complete is True
    assert adopted.changed == frozenset() and adopted.deleted == frozenset()


def test_an_adopted_origin_still_reports_what_changed_after_it(tmp_path: Path) -> None:
    """Adoption sets the origin; the ordinary event history supplies the delta."""
    page = _kb_file(tmp_path, "adopted-then-edited.md")
    other = _kb_file(tmp_path, "adopted-sibling.md")
    _seed(tmp_path, [page, other])
    inherited = _foreign(freshness.recall_checkpoint(tmp_path, "vault"))
    assert freshness.adopt_recall_origin(tmp_path, "vault", inherited) is True

    page.write_text(page.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, [page], [])

    delta = freshness.recall_delta_since(tmp_path, "vault", inherited)

    assert delta.complete is True
    assert delta.changed == frozenset({str(page)})
    assert delta.deleted == frozenset()


def test_adoption_refuses_a_cold_scope_and_a_different_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a live scope projecting the same corpus may adopt."""
    page = _kb_file(tmp_path, "adopted-refusal.md")
    _seed(tmp_path, [page])
    current = freshness.recall_checkpoint(tmp_path, "vault")
    inherited = _foreign(current)

    other_policy = inherited._replace(policy_version="a-different-policy")
    assert freshness.adopt_recall_origin(tmp_path, "vault", other_policy) is False
    assert freshness.recall_delta_since(tmp_path, "vault", other_policy).complete is False

    freshness.invalidate(tmp_path)
    assert freshness.adopt_recall_origin(tmp_path, "vault", inherited) is False


def test_invalidating_a_scope_drops_the_origin_adopted_against_it(
    tmp_path: Path,
) -> None:
    """An adopted origin is a statement about a map; it goes with that map."""
    page = _kb_file(tmp_path, "adopted-invalidated.md")
    _seed(tmp_path, [page])
    inherited = _foreign(freshness.recall_checkpoint(tmp_path, "vault"))
    assert freshness.adopt_recall_origin(tmp_path, "vault", inherited) is True

    freshness.invalidate(tmp_path)
    _seed(tmp_path, [page])

    assert freshness.adopted_recall_origin(tmp_path, "vault", inherited) is None
    assert freshness.recall_delta_since(tmp_path, "vault", inherited).complete is False


def test_this_process_own_checkpoint_needs_no_adoption(tmp_path: Path) -> None:
    page = _kb_file(tmp_path, "adopted-own.md")
    _seed(tmp_path, [page])
    own = freshness.recall_checkpoint(tmp_path, "vault")

    assert freshness.adopt_recall_origin(tmp_path, "vault", own) is True
    assert freshness.adopted_recall_origin(tmp_path, "vault", own) is None


def test_an_edit_published_during_the_proof_is_not_swallowed_by_the_origin(
    tmp_path: Path,
) -> None:
    """The adoption window, and the silent drift it used to hide.

    The proof a caller runs before adopting walks the whole corpus -- seconds on
    a real vault -- and the watcher publishes unattributed edits while it runs.
    An origin recorded at the generation the proof *ended* on declares those
    edits already accounted for: the next delta comes back complete and empty,
    and the bounded repair advances a snapshot that never indexed the edited
    page. The origin is therefore the minimum of the pre-proof and post-proof
    samples.
    """
    page = _kb_file(tmp_path, "adoption-window-page.md")
    victim = _kb_file(tmp_path, "adoption-window-victim.md")
    _seed(tmp_path, [page, victim])
    inherited = _foreign(freshness.recall_checkpoint(tmp_path, "vault"))

    sampled = freshness.recall_generation(tmp_path, "vault")
    assert sampled is not None
    # Inside the proof's window: an edit the process did not author, published
    # by the watcher while the proof is still walking.
    victim.write_text(victim.read_text(encoding="utf-8") + "\nexternal\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, [victim], [])
    assert freshness.recall_generation(tmp_path, "vault") > sampled

    assert (
        freshness.adopt_recall_origin(
            tmp_path, "vault", inherited, sampled_generation=sampled
        )
        is True
    )

    delta = freshness.recall_delta_since(tmp_path, "vault", inherited)
    assert delta.complete is True
    assert str(victim) in delta.changed, (
        "an edit published during the proof must still be in the next delta; "
        f"changed={sorted(delta.changed)}"
    )


def test_the_adopted_origin_never_moves_forward_on_a_second_adoption(
    tmp_path: Path,
) -> None:
    """Re-proving must not retire work the first origin still owes."""
    page = _kb_file(tmp_path, "adoption-monotonic.md")
    _seed(tmp_path, [page])
    inherited = _foreign(freshness.recall_checkpoint(tmp_path, "vault"))
    first = freshness.recall_generation(tmp_path, "vault")
    assert freshness.adopt_recall_origin(
        tmp_path, "vault", inherited, sampled_generation=first
    )

    page.write_text(page.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    freshness.on_files_changed(tmp_path, [page], [])
    assert freshness.adopt_recall_origin(tmp_path, "vault", inherited) is True

    assert freshness.adopted_recall_origin(tmp_path, "vault", inherited) == first
    assert str(page) in freshness.recall_delta_since(tmp_path, "vault", inherited).changed
