"""Lane 2 — exact pending read-your-write over the frozen receipt protocol.

Every scenario below models the acknowledgement shape the change introduces:
canonical Markdown is already current, the freshness registry and the lexical
catalogue are still at the pre-write projection (both are deferred derived
components), and the only durable record of the change is a Lane 1 receipt whose
pending-visibility rows are live.  Recall must nevertheless be exact.

The receipts are published through the frozen protocol seam
``derived_receipts.publish_pending_visibility``.  Lane 2's publisher is used when
the module exists and a custody-only publisher otherwise, because the read path
hydrates the overlay from durable custody either way -- so every assertion here
is made on production recall output (``find``, ``memory_refs``), never on Lane 2
internals.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import uuid
from pathlib import Path

import pytest

from exomem import (
    derived_receipts,
    find_corpus,
    freshness,
    lexstore,
    memory_refs,
    readiness,
    recall_policy,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.derived_receipts import DerivedBatchPath, DerivedComponent
from exomem.find_types import FindTimings

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

#: Filler pages, sized to keep the corpus above
#: ``find._FOREGROUND_LEXICAL_REPAIR_PAGE_CAP`` so recall takes the maintained
#: catalogue rather than the small-corpus reference scan.
_FILLER_PAGES = 80

_REQUIRED = frozenset({DerivedComponent.LEXSTORE, DerivedComponent.MEMORY_REFS})


# --------------------------------------------------------------------------- #
# Lane 2 module access
# --------------------------------------------------------------------------- #


def _pending_module():
    """Lane 2's overlay module, or None on the pristine Lane 1 base."""
    try:
        return importlib.import_module("exomem.pending_recall")
    except ModuleNotFoundError:
        return None


def _pending_publisher():
    """Lane 2's publisher when present; a custody-only publisher otherwise."""
    module = _pending_module()
    if module is None:
        return lambda _root, _receipt: True
    return module.publish


def _reset_overlay() -> None:
    module = _pending_module()
    if module is not None:
        module.reset()


def _hydration_limit() -> int:
    module = _pending_module()
    if module is None:
        # The bound is Lane 2-owned; the base has none, so the overflow node
        # still exercises a real oversized pending set.
        return 512
    return int(module.PENDING_HYDRATION_LIMIT)


# --------------------------------------------------------------------------- #
# Corpus construction
# --------------------------------------------------------------------------- #


def _page(
    *,
    title: str,
    body: str,
    updated: str,
    exomem_id: str | None = None,
) -> str:
    identity = f"exomem_id: {exomem_id}\n" if exomem_id else ""
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        f"updated: {updated}\n"
        f"{identity}"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


def _write(vault: Path, rel: str, text: str) -> str:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(vault: Path, rel: str) -> str:
    return (vault / rel).read_text(encoding="utf-8")


def _seed_corpus(vault: Path, count: int = _FILLER_PAGES) -> None:
    for index in range(count):
        _write(
            vault,
            f"Knowledge Base/Notes/filler-{index:03d}.md",
            _page(
                title=f"Filler {index:03d}",
                body="Unremarkable filler prose about ordinary operations.",
                updated="2026-01-01",
            ),
        )


def _prime(vault: Path) -> None:
    """Bring every persistent projection to the current corpus, then freeze it.

    After this the freshness registry is live at the pre-write projection and
    the lexical catalogue and reference sidecar are published at exactly that
    projection -- the acknowledgement-time state a governed write leaves behind
    once freshness, memory_refs and lexstore are deferred components.
    """
    find_module.clear_cache()
    lexstore.clear_stores()
    # `clear_cache` also drops the freshness registry, so seeding must follow it
    # and the final reset must keep the registry and catalogue warm.
    freshness.rebaseline(vault)
    for scope in freshness.SCOPES:
        assert freshness.recall_is_live(vault, scope), scope
    memory_refs.ReferenceIndex(vault).rebuild_all()
    assert lexstore.get_store(vault).rebuild_atomic() is True
    find_module.reset_page_and_result_caches()


# --------------------------------------------------------------------------- #
# Receipt lifecycle helpers (frozen Lane 1 protocol only)
# --------------------------------------------------------------------------- #


def _batch_path(
    rel: str,
    *,
    before: str | None,
    after: str | None,
    stable_memory_ref: str | None = None,
) -> DerivedBatchPath:
    return DerivedBatchPath(
        rel_path=rel,
        before_hash=None if before is None else _digest(before),
        after_hash=None if after is None else _digest(after),
        stable_memory_ref=stable_memory_ref,
    )


def _prepare(
    vault: Path,
    *,
    batch_id: str,
    generation: str,
    paths: tuple[DerivedBatchPath, ...],
    required=None,
    now: float = 10.0,
):
    return derived_receipts.prepare_batch(
        vault,
        batch_id=batch_id,
        mutation_attempt_digest=_digest(batch_id),
        canonical_generation=generation,
        checkpoint_id=f"checkpoint-{generation}",
        paths=paths,
        required_components=_REQUIRED if required is None else frozenset(required),
        now=now,
    )


def _prove_and_publish(vault: Path, receipt, *, generation: str | None = None) -> None:
    proof = derived_receipts.prove_committed(
        vault,
        receipt,
        current_generation=generation or receipt.canonical_generation,
    )
    assert proof.outcome == "ready", proof.outcome
    assert derived_receipts.publish_pending_visibility(
        vault,
        receipt,
        publisher=_pending_publisher(),
    )


def _publish_change(
    vault: Path,
    *,
    batch_id: str,
    generation: str,
    changes: tuple[tuple[str, str | None, str | None, str | None], ...],
    required=None,
) -> None:
    """Prepare custody, apply the canonical change, then prove and publish.

    ``changes`` carries ``(rel_path, before_text, after_text, stable_ref)``;
    ``None`` text on either side means absence (a create or a tombstone).
    """
    paths = tuple(
        _batch_path(rel, before=before, after=after, stable_memory_ref=ref)
        for rel, before, after, ref in changes
    )
    receipt = _prepare(
        vault,
        batch_id=batch_id,
        generation=generation,
        paths=paths,
        required=required,
    )
    for rel, _before, after, _ref in changes:
        target = vault / rel
        if after is None:
            target.unlink()
        else:
            _write(vault, rel, after)
    _prove_and_publish(vault, receipt)


def _non_retired_rows(vault: Path) -> dict[str, str]:
    """Durable pending custody as ``{rel_path: state}``, retired rows excluded."""
    snapshot = derived_receipts.snapshot_pending_visibility(
        vault, limit=_hydration_limit() + 1
    )
    assert snapshot.outcome == "complete", snapshot.outcome
    return {
        row.rel_path: row.state
        for batch in snapshot.batches
        for row in batch.rows
        if row.state != "retired"
    }


# --------------------------------------------------------------------------- #
# Recall helpers
# --------------------------------------------------------------------------- #


def _keyword(
    vault: Path,
    query: str,
    *,
    limit: int = 5,
    degraded: list[str] | None = None,
    timings: FindTimings | None = None,
) -> list[str]:
    hits = find_module.find(
        vault,
        query=query,
        scope="kb-only",
        mode="keyword",
        limit=limit,
        degraded_out=degraded,
        timings=timings,
    )
    return [hit.path for hit in hits]


def _hybrid(
    vault: Path,
    query: str,
    *,
    limit: int = 5,
    degraded: list[str] | None = None,
    timings: FindTimings | None = None,
) -> list[str]:
    hits = find_module.find(
        vault,
        query=query,
        scope="kb-only",
        mode="hybrid",
        limit=limit,
        degraded_out=degraded,
        timings=timings,
    )
    return [hit.path for hit in hits]


def _hybrid_hits(vault: Path, query: str, *, limit: int = 5):
    return find_module.find(
        vault,
        query=query,
        scope="kb-only",
        mode="hybrid",
        limit=limit,
    )


def _resolve_ref(vault: Path, identity: str) -> str:
    return memory_refs.resolve_identifier(vault, f"{memory_refs.REF_PREFIX}{identity}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _lane2_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    find_module.clear_cache()
    find_module.reset_degradation_counts()
    lexstore.reset_memo()
    lexstore.clear_stores()
    readiness.reset()
    _reset_overlay()
    yield
    lexstore.await_repairs_idle(Path.cwd(), timeout=0.0)
    find_module.clear_cache()
    find_module.reset_degradation_counts()
    lexstore.clear_stores()
    readiness.reset()
    _reset_overlay()


@pytest.fixture
def managed_runtime(monkeypatch: pytest.MonkeyPatch):
    """Managed recall admission without a live warm, mirroring the server."""
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(
        readiness,
        "retrieval_admission",
        lambda _root=None: {"state": "ready", "admitted": True},
    )
    monkeypatch.setattr(
        lexstore,
        "runtime_retrieval_catalog_admission",
        lambda root, **_kwargs: lexstore.RecallProjectionAdmission(
            {
                scope: freshness.recall_checkpoint(root, scope)
                for scope in freshness.SCOPES
            },
            (),
        ),
    )


def _forbid_whole_corpus(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """Fail fast on every whole-corpus walk/rebuild route reachable from here."""

    def _sentinel(name: str):
        def _fire(*_args, **_kwargs):
            raise AssertionError(f"{reason}: {name} performed whole-corpus work")

        return _fire

    monkeypatch.setattr(find_corpus, "walk_md", _sentinel("find_corpus.walk_md"))
    monkeypatch.setattr(
        vault_module, "walk_vault_md", _sentinel("vault.walk_vault_md")
    )
    monkeypatch.setattr(
        recall_policy, "iter_recall_markdown", _sentinel("recall_policy.iter_recall_markdown")
    )
    monkeypatch.setattr(
        lexstore.LexicalStore,
        "rebuild_atomic",
        _sentinel("lexstore.LexicalStore.rebuild_atomic"),
    )
    monkeypatch.setattr(
        memory_refs.ReferenceIndex,
        "rebuild_all",
        _sentinel("memory_refs.ReferenceIndex.rebuild_all"),
    )
    monkeypatch.setattr(freshness, "rebaseline", _sentinel("freshness.rebaseline"))
    monkeypatch.setattr(freshness, "seed", _sentinel("freshness.seed"))


# --------------------------------------------------------------------------- #
# Task 2.1 — operation / read-surface matrix
# --------------------------------------------------------------------------- #


def test_pending_create_is_exact_by_direct_ref_keyword_and_hybrid(tmp_path: Path) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    _prime(vault)

    identity = str(uuid.UUID(int=0x0C1EA7E))
    rel = "Knowledge Base/Notes/pending-create.md"
    marker = "zetacreatemarker"
    after = _page(
        title="Pending create",
        body=f"A newly committed page about {marker} handling.",
        updated="2026-08-30",
        exomem_id=identity,
    )

    _publish_change(
        vault,
        batch_id="batch-create",
        generation="generation-create",
        changes=((rel, None, after, f"{memory_refs.REF_PREFIX}{identity}"),),
    )

    # direct path
    assert marker in _read(vault, rel)
    # stable reference
    assert _resolve_ref(vault, identity) == rel
    # keyword and hybrid recall
    assert _keyword(vault, marker) == [rel]
    assert _hybrid(vault, marker) == [rel]


def test_pending_edit_is_exact_by_direct_ref_keyword_and_hybrid(tmp_path: Path) -> None:
    vault = tmp_path
    _seed_corpus(vault)

    identity = str(uuid.UUID(int=0x0ED17))
    rel = "Knowledge Base/Notes/pending-edit.md"
    old_marker = "alphaoldmarker"
    new_marker = "betanewmarker"
    before = _page(
        title="Pending edit",
        body=f"The original conclusion recorded {old_marker} evidence.",
        updated="2026-08-01",
        exomem_id=identity,
    )
    _write(vault, rel, before)
    _prime(vault)
    assert _keyword(vault, old_marker) == [rel]

    after = _page(
        title="Pending edit",
        body=f"The revised conclusion records {new_marker} evidence.",
        updated="2026-08-30",
        exomem_id=identity,
    )
    _publish_change(
        vault,
        batch_id="batch-edit",
        generation="generation-edit",
        changes=((rel, before, after, f"{memory_refs.REF_PREFIX}{identity}"),),
    )

    # direct path exposes only the after-state
    body = _read(vault, rel)
    assert new_marker in body
    assert old_marker not in body
    # stable reference still resolves the same identity
    assert _resolve_ref(vault, identity) == rel
    # keyword and hybrid expose only the after-state
    assert _keyword(vault, new_marker) == [rel]
    assert _hybrid(vault, new_marker) == [rel]
    assert _keyword(vault, old_marker) == []
    assert _hybrid(vault, old_marker) == []


def test_pending_delete_is_exact_by_direct_ref_keyword_and_hybrid(tmp_path: Path) -> None:
    vault = tmp_path
    _seed_corpus(vault)

    marker = "gammadeletemarker"
    deleted_identity = str(uuid.UUID(int=0x0DE1))
    kept_identity = str(uuid.UUID(int=0x0CEE))
    deleted_rel = "Knowledge Base/Notes/pending-delete.md"
    kept_rel = "Knowledge Base/Notes/pending-kept.md"

    deleted_before = _page(
        title="Pending delete",
        body=f"A retired page mentioning {marker} once.",
        updated="2026-08-20",
        exomem_id=deleted_identity,
    )
    kept_before = _page(
        title="Pending kept",
        body="A surviving page with no distinguishing terms.",
        updated="2026-08-02",
        exomem_id=kept_identity,
    )
    _write(vault, deleted_rel, deleted_before)
    _write(vault, kept_rel, kept_before)
    _prime(vault)
    assert _keyword(vault, marker) == [deleted_rel]

    kept_after = _page(
        title="Pending kept",
        body=f"The surviving page now records {marker} instead.",
        updated="2026-08-25",
        exomem_id=kept_identity,
    )
    _publish_change(
        vault,
        batch_id="batch-delete",
        generation="generation-delete",
        changes=(
            (deleted_rel, deleted_before, None, f"{memory_refs.REF_PREFIX}{deleted_identity}"),
            (kept_rel, kept_before, kept_after, f"{memory_refs.REF_PREFIX}{kept_identity}"),
        ),
    )

    # direct path reports absence
    assert not (vault / deleted_rel).exists()
    assert marker in _read(vault, kept_rel)
    # the stable reference of a deleted page stops resolving
    with pytest.raises(memory_refs.ReferenceError) as absent:
        _resolve_ref(vault, deleted_identity)
    assert absent.value.code == "REFERENCE_NOT_FOUND"
    assert _resolve_ref(vault, kept_identity) == kept_rel
    # both search modes suppress the old path and expose the after-state
    assert _keyword(vault, marker) == [kept_rel]
    assert _hybrid(vault, marker) == [kept_rel]


def test_pending_move_is_exact_by_direct_ref_keyword_and_hybrid(tmp_path: Path) -> None:
    vault = tmp_path
    _seed_corpus(vault)

    identity = str(uuid.UUID(int=0x0000_0000_0000_0000_0000_0000_0000_0F0F))
    old_rel = "Knowledge Base/Notes/pending-move-old.md"
    new_rel = "Knowledge Base/Notes/pending-move-new.md"
    marker = "deltamovemarker"
    body = _page(
        title="Pending move",
        body=f"A relocated page carrying {marker} throughout.",
        updated="2026-08-18",
        exomem_id=identity,
    )
    _write(vault, old_rel, body)
    _prime(vault)
    assert _keyword(vault, marker) == [old_rel]

    ref = f"{memory_refs.REF_PREFIX}{identity}"
    paths = (
        _batch_path(old_rel, before=body, after=None, stable_memory_ref=ref),
        _batch_path(new_rel, before=None, after=body, stable_memory_ref=ref),
    )
    receipt = _prepare(
        vault, batch_id="batch-move", generation="generation-move", paths=paths
    )
    (vault / new_rel).parent.mkdir(parents=True, exist_ok=True)
    os.replace(vault / old_rel, vault / new_rel)
    _prove_and_publish(vault, receipt)

    # the old path is absent, the new path holds the canonical bytes
    assert not (vault / old_rel).exists()
    assert marker in _read(vault, new_rel)
    # the stable reference resolves the current path
    assert _resolve_ref(vault, identity) == new_rel
    # each search mode returns the canonical identity exactly once
    assert _keyword(vault, marker) == [new_rel]
    assert _hybrid(vault, marker) == [new_rel]


# --------------------------------------------------------------------------- #
# Stale-row shadowing (mutation guard 1)
# --------------------------------------------------------------------------- #


def test_pending_edit_delete_hides_stale_rows(tmp_path: Path) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    marker = "epsilonshadowmarker"

    hot_rel = "Knowledge Base/Notes/shadow-hot.md"
    gone_rel = "Knowledge Base/Notes/shadow-gone.md"
    cool_rel = "Knowledge Base/Notes/shadow-cool.md"
    fresh_rel = "Knowledge Base/Notes/shadow-fresh.md"

    hot_before = _page(
        title="Shadow hot",
        body=" ".join([marker] * 40),
        updated="2026-09-01",
    )
    gone_before = _page(
        title="Shadow gone",
        body=f"A deleted page about {marker} evidence.",
        updated="2026-08-15",
    )
    cool_page = _page(
        title="Shadow cool",
        body=f"A stable page that mentions {marker} once.",
        updated="2026-08-01",
    )
    fresh_before = _page(
        title="Shadow fresh",
        body="A page with no distinguishing terms yet.",
        updated="2026-09-05",
    )
    _write(vault, hot_rel, hot_before)
    _write(vault, gone_rel, gone_before)
    _write(vault, cool_rel, cool_page)
    _write(vault, fresh_rel, fresh_before)
    _prime(vault)

    hot_after = _page(
        title="Shadow hot",
        body="Every distinguishing term has been removed from this page.",
        updated="2026-09-02",
    )
    fresh_after = _page(
        title="Shadow fresh",
        body=f"The revised page now records {marker} evidence.",
        updated="2026-09-05",
    )
    _publish_change(
        vault,
        batch_id="batch-shadow",
        generation="generation-shadow",
        changes=(
            (hot_rel, hot_before, hot_after, None),
            (gone_rel, gone_before, None, None),
            (fresh_rel, fresh_before, fresh_after, None),
        ),
    )

    # The stale lexical rows for the edited and deleted pages must be suppressed
    # before scoring and caps, so a bounded answer spends both of its slots on
    # pages that actually carry the term. Membership rather than order: the
    # fused ranking weights are unchanged by this lane and are not what this
    # guard is about.
    bounded = _hybrid(vault, marker, limit=2)
    assert set(bounded) == {fresh_rel, cool_rel}, bounded
    assert _keyword(vault, marker, limit=3) == [fresh_rel, cool_rel]
    for hit in _hybrid_hits(vault, marker, limit=5):
        assert hit.path not in {hot_rel, gone_rel}


# --------------------------------------------------------------------------- #
# O(changed-paths) publication
# --------------------------------------------------------------------------- #


def test_pending_publisher_reads_only_changed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    first_rel = "Knowledge Base/Notes/bounded-one.md"
    second_rel = "Knowledge Base/Notes/bounded-two.md"
    marker = "zetaboundedmarker"
    first_before = _page(title="Bounded one", body="Nothing yet.", updated="2026-08-01")
    second_before = _page(title="Bounded two", body="Nothing yet.", updated="2026-08-02")
    _write(vault, first_rel, first_before)
    _write(vault, second_rel, second_before)
    _prime(vault)

    first_after = _page(
        title="Bounded one",
        body=f"Now records {marker} evidence.",
        updated="2026-08-20",
    )
    second_after = _page(
        title="Bounded two",
        body=f"Also records {marker} evidence.",
        updated="2026-08-21",
    )
    changed = {first_rel, second_rel}
    paths = (
        _batch_path(first_rel, before=first_before, after=first_after),
        _batch_path(second_rel, before=second_before, after=second_after),
    )
    receipt = _prepare(
        vault, batch_id="batch-bounded", generation="generation-bounded", paths=paths
    )
    _write(vault, first_rel, first_after)
    _write(vault, second_rel, second_after)
    proof = derived_receipts.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    )
    assert proof.outcome == "ready"

    opened: list[str] = []
    real_read_bytes = Path.read_bytes
    real_open = Path.open

    def _record(path: Path) -> None:
        try:
            rel = path.resolve().relative_to(vault.resolve()).as_posix()
        except (OSError, ValueError):
            return
        if rel.lower().endswith(".md"):
            opened.append(rel)

    def _spy_read_bytes(self: Path):
        _record(self)
        return real_read_bytes(self)

    def _spy_open(self: Path, *args, **kwargs):
        _record(self)
        return real_open(self, *args, **kwargs)

    with monkeypatch.context() as guard:
        _forbid_whole_corpus(guard, "pending publication")
        guard.setattr(Path, "read_bytes", _spy_read_bytes)
        guard.setattr(Path, "open", _spy_open)
        assert derived_receipts.publish_pending_visibility(
            vault, receipt, publisher=_pending_publisher()
        )

    assert set(opened) <= changed, sorted(set(opened) - changed)
    assert _keyword(vault, marker, limit=5) == [second_rel, first_rel]


# --------------------------------------------------------------------------- #
# Restart, overflow, corruption (mutation guards 2 and 3)
# --------------------------------------------------------------------------- #


def test_restart_hydrates_pending_before_ready(
    tmp_path: Path, managed_runtime: None
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/restart-hydration.md"
    marker = "etarestartmarker"
    before = _page(title="Restart hydration", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    after = _page(
        title="Restart hydration",
        body=f"Committed before the restart with {marker} evidence.",
        updated="2026-08-22",
    )
    _publish_change(
        vault,
        batch_id="batch-restart",
        generation="generation-restart",
        changes=((rel, before, after, None),),
    )

    # A fresh in-memory overlay over the same durable custody: no process global
    # from the publishing pass survives, and no new write or watcher echo runs.
    _reset_overlay()
    find_module.reset_page_and_result_caches()
    lexstore.clear_stores()

    assert _keyword(vault, marker) == [rel]
    assert _hybrid(vault, marker) == [rel]


def test_unprovable_pending_overflow_returns_warming(
    tmp_path: Path, managed_runtime: None
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    marker = "thetaoverflowmarker"
    limit = _hydration_limit()

    bounded: list[tuple[str, str | None, str | None, str | None]] = []
    for index in range(limit):
        rel = f"Knowledge Base/Notes/overflow-{index:04d}.md"
        after = _page(
            title=f"Overflow {index:04d}",
            body=f"Bounded pending page {index:04d}.",
            updated="2026-08-10",
        )
        bounded.append((rel, None, after, None))
    _prime(vault)
    _publish_change(
        vault,
        batch_id="batch-overflow-bounded",
        generation="generation-overflow",
        changes=tuple(bounded),
    )

    # Exactly at the bound the overlay is complete and recall stays exact.
    sentinel_rel = "Knowledge Base/Notes/overflow-0000.md"
    assert _keyword(vault, "Bounded pending page 0000") == [sentinel_rel]

    # One row past the bound the snapshot overflows and recall must fail closed
    # rather than serve the last published catalogue as if it were complete.
    overflow_rel = "Knowledge Base/Notes/overflow-past-bound.md"
    overflow_after = _page(
        title="Overflow past bound",
        body=f"This page carries {marker} evidence.",
        updated="2026-08-11",
    )
    _publish_change(
        vault,
        batch_id="batch-overflow-past",
        generation="generation-overflow",
        changes=((overflow_rel, None, overflow_after, None),),
    )
    _reset_overlay()
    find_module.reset_page_and_result_caches()

    with pytest.raises(find_module.RetrievalIndexWarming) as warming:
        _keyword(vault, "Bounded pending page 0000")
    assert warming.value.complete is False
    with pytest.raises(find_module.RetrievalIndexWarming):
        _hybrid(vault, marker)


def test_corrupt_pending_row_returns_warming_before_catalogue_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed_runtime: None
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/corrupt-pending.md"
    marker = "iotacorruptmarker"
    before = _page(title="Corrupt pending", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    after = _page(
        title="Corrupt pending",
        body=f"Committed with {marker} evidence.",
        updated="2026-08-23",
    )
    _publish_change(
        vault,
        batch_id="batch-corrupt",
        generation="generation-corrupt",
        changes=((rel, before, after, None),),
    )

    # The canonical bytes no longer equal the intended after state the receipt
    # recorded, so the pending row can no longer be proven.
    _write(
        vault,
        rel,
        _page(title="Corrupt pending", body="Unrelated bytes.", updated="2026-08-24"),
    )
    _reset_overlay()
    find_module.reset_page_and_result_caches()

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("unprovable pending coverage reached the catalogue")

    monkeypatch.setattr(lexstore, "search_substring_result", _forbidden)
    monkeypatch.setattr(lexstore, "search_substring", _forbidden)
    monkeypatch.setattr(lexstore.LexicalStore, "catalog_readiness", _forbidden)

    with pytest.raises(find_module.RetrievalIndexWarming) as warming:
        _keyword(vault, marker)
    assert warming.value.complete is False


def test_newer_pending_generation_supersedes_older_without_stale_republication(
    tmp_path: Path,
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/supersede.md"
    first_marker = "kappafirstmarker"
    second_marker = "lambdasecondmarker"
    original = _page(title="Supersede", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, original)
    _prime(vault)

    first = _page(
        title="Supersede",
        body=f"The first committed generation records {first_marker}.",
        updated="2026-08-10",
    )
    _publish_change(
        vault,
        batch_id="batch-supersede-old",
        generation="generation-supersede-1",
        changes=((rel, original, first, None),),
    )
    assert _keyword(vault, first_marker) == [rel]

    second = _page(
        title="Supersede",
        body=f"The newer committed generation records {second_marker}.",
        updated="2026-08-11",
    )
    _publish_change(
        vault,
        batch_id="batch-supersede-new",
        generation="generation-supersede-2",
        changes=((rel, first, second, None),),
    )

    # Only the newest proven generation may publish as current, and the older
    # pending state can neither reappear nor duplicate the identity.
    assert _keyword(vault, second_marker) == [rel]
    assert _hybrid(vault, second_marker) == [rel]
    assert _keyword(vault, first_marker) == []
    assert _hybrid(vault, first_marker) == []


# --------------------------------------------------------------------------- #
# Retirement and publication order
# --------------------------------------------------------------------------- #


def test_pending_overlay_retires_only_after_exact_lexical_publication(
    tmp_path: Path,
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/retire-after-publication.md"
    marker = "muretiremarker"
    before = _page(title="Retire", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    after = _page(
        title="Retire",
        body=f"Committed with {marker} evidence.",
        updated="2026-08-26",
    )
    _publish_change(
        vault,
        batch_id="batch-retire",
        generation="generation-retire",
        changes=((rel, before, after, None),),
    )
    assert _non_retired_rows(vault) == {rel: "live"}
    assert _keyword(vault, marker) == [rel]

    # The identity lane alone does not prove the exact lexical after generation.
    assert memory_refs.upsert_after_write(vault, [vault / rel]) is True
    assert _non_retired_rows(vault) == {rel: "live"}
    assert _keyword(vault, marker) == [rel]

    # The persistent lexical publication proves it, and only then may the
    # overlay row retire.
    assert lexstore.get_store(vault).upsert_paths([vault / rel]) is True
    assert _non_retired_rows(vault) == {}
    assert _keyword(vault, marker) == [rel]


def test_pending_publication_order_has_no_removal_before_publication_gap(
    tmp_path: Path,
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/publication-order.md"
    marker = "nuordermarker"
    before = _page(title="Publication order", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    after = _page(
        title="Publication order",
        body=f"Committed with {marker} evidence.",
        updated="2026-08-27",
    )
    _publish_change(
        vault,
        batch_id="batch-order",
        generation="generation-order",
        changes=((rel, before, after, None),),
    )

    # Visible at every step, from publication through each persistent lane to
    # retirement: there is no window in which the page belongs to neither the
    # overlay nor the published catalogue.
    assert _keyword(vault, marker) == [rel]
    assert lexstore.get_store(vault).upsert_paths([vault / rel]) is True
    assert _keyword(vault, marker) == [rel]
    assert _non_retired_rows(vault) == {rel: "live"}
    assert memory_refs.upsert_after_write(vault, [vault / rel]) is True
    assert _keyword(vault, marker) == [rel]
    assert _non_retired_rows(vault) == {}
    assert _keyword(vault, marker) == [rel]


# --------------------------------------------------------------------------- #
# Authorization and privacy
# --------------------------------------------------------------------------- #


def _withheld_corpus(vault: Path) -> tuple[str, str, str]:
    """Seed one excluded and one allowed page sharing a marker."""
    (vault / "Knowledge Base").mkdir(parents=True, exist_ok=True)
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Withheld\n", encoding="utf-8"
    )
    _seed_corpus(vault)
    marker = "xiwithheldmarker"
    withheld_rel = "Knowledge Base/Withheld/withheld.md"
    allowed_rel = "Knowledge Base/Notes/allowed.md"
    _write(
        vault,
        withheld_rel,
        _page(title="Withheld", body="Nothing yet.", updated="2026-07-01"),
    )
    _write(
        vault,
        allowed_rel,
        _page(title="Allowed", body="Nothing yet.", updated="2026-07-02"),
    )
    _prime(vault)
    return marker, withheld_rel, allowed_rel


def _publish_withheld(vault: Path, marker: str, withheld_rel: str, allowed_rel: str) -> None:
    withheld_before = _read(vault, withheld_rel)
    allowed_before = _read(vault, allowed_rel)
    withheld_after = _page(
        title="Withheld",
        body=" ".join([marker] * 30),
        updated="2026-09-09",
    )
    allowed_after = _page(
        title="Allowed",
        body=f"An ordinary page recording {marker} once.",
        updated="2026-08-05",
    )
    _publish_change(
        vault,
        batch_id="batch-withheld",
        generation="generation-withheld",
        changes=(
            (withheld_rel, withheld_before, withheld_after, None),
            (allowed_rel, allowed_before, allowed_after, None),
        ),
    )


def test_pending_projection_reauthorizes_current_request_before_scoring(
    tmp_path: Path,
) -> None:
    vault = tmp_path
    marker, withheld_rel, allowed_rel = _withheld_corpus(vault)
    _publish_withheld(vault, marker, withheld_rel, allowed_rel)

    from exomem import access

    # The request's own policy decides release; custody says nothing about it.
    assert access.is_indexable(vault, withheld_rel) is False
    assert _keyword(vault, marker, limit=1) == [allowed_rel]
    assert _hybrid(vault, marker, limit=1) == [allowed_rel]
    for hit in _hybrid_hits(vault, marker, limit=5):
        assert hit.path != withheld_rel
        assert withheld_rel not in hit.excerpt


def test_pending_withheld_and_l0_rows_are_absent_before_caps(tmp_path: Path) -> None:
    vault = tmp_path
    marker, withheld_rel, allowed_rel = _withheld_corpus(vault)
    _publish_withheld(vault, marker, withheld_rel, allowed_rel)

    # The withheld candidate is newer and denser, so it would take the single
    # slot if it were scored. Exclusion must precede scoring and the cap.
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query=marker,
        scope="kb-only",
        mode="hybrid",
        limit=1,
        degraded_out=degraded,
    )
    assert [hit.path for hit in hits] == [allowed_rel]
    assert len(hits) == 1
    assert all("Withheld" not in component for component in degraded)


def test_pending_operational_status_and_telemetry_are_content_free(
    tmp_path: Path,
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/telemetry.md"
    marker = "omicrontelemetrymarker"
    before = _page(title="Telemetry", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    secret_body = f"A committed page whose body records {marker} evidence."
    after = _page(title="Telemetry", body=secret_body, updated="2026-08-28")
    _publish_change(
        vault,
        batch_id="batch-telemetry",
        generation="generation-telemetry",
        changes=((rel, before, after, None),),
    )

    degraded: list[str] = []
    timings = FindTimings()
    assert _keyword(vault, marker, degraded=degraded, timings=timings) == [rel]

    forbidden = (marker, secret_body, rel, "Telemetry", "Knowledge Base")
    observed = repr(degraded) + repr(timings.profile) + repr(timings.stages)
    module = _pending_module()
    if module is not None:
        observed += repr(module.status(vault))
    for value in forbidden:
        assert value not in observed, value
    # The pending projection state itself must still be disclosed.
    assert any("pending" in component for component in degraded), degraded


def test_vector_and_graph_pending_coverage_is_disclosed(tmp_path: Path) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/coverage.md"
    marker = "picoveragemarker"
    quiet_rel = "Knowledge Base/Notes/coverage-quiet.md"
    before = _page(title="Coverage", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _write(
        vault,
        quiet_rel,
        _page(title="Coverage quiet", body="A settled page.", updated="2026-08-01"),
    )
    _prime(vault)

    settled: list[str] = []
    assert _keyword(vault, "settled", degraded=settled) == [quiet_rel]
    assert not any("pending" in component for component in settled), settled

    after = _page(
        title="Coverage",
        body=f"Committed with {marker} evidence.",
        updated="2026-08-29",
    )
    _publish_change(
        vault,
        batch_id="batch-coverage",
        generation="generation-coverage",
        changes=((rel, before, after, None),),
    )

    degraded: list[str] = []
    assert _keyword(vault, marker, degraded=degraded) == [rel]
    assert any("pending" in component for component in degraded), degraded


def test_no_pending_fast_path_preserves_existing_recall_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/fast-path.md"
    marker = "rhofastpathmarker"
    before = _page(
        title="Fast path",
        body=f"A settled page recording {marker} evidence.",
        updated="2026-08-01",
    )
    _write(vault, rel, before)
    _prime(vault)

    # With no pending custody the overlay performs no work, adds no corpus walk,
    # discloses nothing, and the hot result cache still serves the repeat.
    with monkeypatch.context() as guard:
        _forbid_whole_corpus(guard, "no-pending fast path")
        degraded: list[str] = []
        first = FindTimings()
        assert _keyword(vault, marker, degraded=degraded, timings=first) == [rel]
        assert degraded == []
        second = FindTimings()
        assert _keyword(vault, marker, timings=second) == [rel]
        assert second.cache.get("hit") is True

    # Publishing pending custody must take the other branch, so the fast path
    # above is genuinely the no-pending one.
    after = _page(
        title="Fast path",
        body=f"The revised page records {marker} and gamma evidence.",
        updated="2026-08-31",
    )
    _publish_change(
        vault,
        batch_id="batch-fast-path",
        generation="generation-fast-path",
        changes=((rel, before, after, None),),
    )
    pending_degraded: list[str] = []
    assert _keyword(vault, "gamma", degraded=pending_degraded) == [rel]
    assert any("pending" in component for component in pending_degraded)


# --------------------------------------------------------------------------- #
# Correction round 1 — reviewer findings
# --------------------------------------------------------------------------- #


def _publish_lanes(vault: Path, rel_paths: tuple[str, ...]) -> None:
    """Publish the persistent lexical and identity lanes for exact paths."""
    present = [vault / rel for rel in rel_paths if (vault / rel).exists()]
    absent = [rel for rel in rel_paths if not (vault / rel).exists()]
    if present:
        assert lexstore.get_store(vault).upsert_paths(present) is True
        assert memory_refs.upsert_after_write(vault, present) is True
    if absent:
        assert lexstore.delete_after_remove(vault, absent) is True
        assert memory_refs.delete_after_remove(vault, absent) is True


def _overlay(vault: Path):
    return freshness.recall_pending_coverage(vault)


def _records_manifest() -> str:
    return (
        "---\n"
        "type: collection\n"
        "exomem_id: 12345678-1234-4abc-8def-123456789abc\n"
        "title: Measurements\n"
        "semantic_profile: records\n"
        "collection_version: 1\n"
        "lifecycle: active\n"
        "schema_version: 1\n"
        "storage:\n"
        "  strategy: markdown-items\n"
        "  format_version: 1\n"
        "  source: items\n"
        "item_schema:\n"
        "  natural_key: [observed]\n"
        "  fields:\n"
        "    observed:\n"
        "      type: string\n"
        "---\n"
    )


def test_unmanaged_recall_keeps_its_exact_source_walk_fallback(tmp_path: Path) -> None:
    """Finding 1(a): only managed recall fails closed on unprovable coverage."""
    vault = tmp_path
    _seed_corpus(vault)
    settled_rel = "Knowledge Base/Notes/unmanaged-settled.md"
    pending_rel = "Knowledge Base/Notes/unmanaged-pending.md"
    marker = "sigmaunmanagedmarker"
    _write(
        vault,
        settled_rel,
        _page(
            title="Unmanaged settled",
            body=f"A settled page recording {marker} evidence.",
            updated="2026-08-01",
        ),
    )
    before = _page(title="Unmanaged pending", body="Nothing yet.", updated="2026-08-02")
    _write(vault, pending_rel, before)
    _prime(vault)

    after = _page(
        title="Unmanaged pending", body="Committed bytes.", updated="2026-08-20"
    )
    _publish_change(
        vault,
        batch_id="batch-unmanaged",
        generation="generation-unmanaged",
        changes=((pending_rel, before, after, None),),
    )
    # An out-of-band editor save makes the pending row unprovable.
    _write(
        vault,
        pending_rel,
        _page(title="Unmanaged pending", body="Hand edit.", updated="2026-08-21"),
    )
    _reset_overlay()
    find_module.reset_page_and_result_caches()

    # `readiness.runtime_managed()` is False here: this is the offline/CLI
    # contract, which keeps its existing exact source-walk fallback rather than
    # being refused for custody it never consults.
    assert readiness.runtime_managed() is False
    assert _keyword(vault, marker) == [settled_rel]
    assert _hybrid(vault, marker) == [settled_rel]


def test_out_of_band_supersession_recovers_after_persistent_publication(
    tmp_path: Path, managed_runtime: None
) -> None:
    """Finding 1(b): an unprovable row must be able to retire and self-heal."""
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/self-heal.md"
    marker = "tauselfhealmarker"
    before = _page(title="Self heal", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    after = _page(title="Self heal", body="Committed bytes.", updated="2026-08-20")
    _publish_change(
        vault,
        batch_id="batch-self-heal",
        generation="generation-self-heal",
        changes=((rel, before, after, None),),
    )
    hand_edited = _page(
        title="Self heal",
        body=f"A hand edit that records {marker} evidence.",
        updated="2026-08-22",
    )
    _write(vault, rel, hand_edited)
    _reset_overlay()
    find_module.reset_page_and_result_caches()

    # The receipt's intended after state is no longer on disk, so managed recall
    # fails closed rather than serving an unprovable projection.
    with pytest.raises(find_module.RetrievalIndexWarming):
        _keyword(vault, marker)

    # Once the persistent lanes publish the current canonical bytes, the row is
    # superseded out of band and retires; recall recovers instead of warming
    # forever on a row that can never prove its original after state.
    _publish_lanes(vault, (rel,))
    _reset_overlay()
    find_module.reset_page_and_result_caches()

    assert _non_retired_rows(vault) == {}
    assert _keyword(vault, marker) == [rel]


def test_retirement_requires_every_non_omittable_component(tmp_path: Path) -> None:
    """Finding 2: only graph/embeddings/claims/advisory may omit the generation."""
    vault = tmp_path
    _seed_corpus(vault)
    blocked_rel = "Knowledge Base/Notes/component-blocked.md"
    omittable_rel = "Knowledge Base/Notes/component-omittable.md"
    blocked_before = _page(title="Blocked", body="Nothing yet.", updated="2026-08-01")
    omittable_before = _page(title="Omittable", body="Nothing yet.", updated="2026-08-02")
    _write(vault, blocked_rel, blocked_before)
    _write(vault, omittable_rel, omittable_before)
    _prime(vault)

    _publish_change(
        vault,
        batch_id="batch-component-blocked",
        generation="generation-components",
        changes=(
            (
                blocked_rel,
                blocked_before,
                _page(title="Blocked", body="Committed.", updated="2026-08-10"),
                None,
            ),
        ),
        required={
            DerivedComponent.LEXSTORE,
            DerivedComponent.MEMORY_REFS,
            DerivedComponent.RESOLVER,
            DerivedComponent.SEMANTIC_PURGE,
        },
    )
    _publish_change(
        vault,
        batch_id="batch-component-omittable",
        generation="generation-components",
        changes=(
            (
                omittable_rel,
                omittable_before,
                _page(title="Omittable", body="Committed.", updated="2026-08-11"),
                None,
            ),
        ),
        required={
            DerivedComponent.LEXSTORE,
            DerivedComponent.MEMORY_REFS,
            DerivedComponent.GRAPH,
            DerivedComponent.EMBEDDINGS,
            DerivedComponent.CLAIMS,
        },
    )

    _publish_lanes(vault, (blocked_rel, omittable_rel))
    _reset_overlay()

    # The lanes ordinary recall reads through have published both pages, but the
    # blocked batch still owes resolver and semantic-purge convergence.
    assert _non_retired_rows(vault) == {blocked_rel: "live"}


def test_lane_proof_survives_restart_and_retires_at_hydration(tmp_path: Path) -> None:
    """Finding 3: a restart between lane publications must not strand a row."""
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/restart-retire.md"
    before = _page(title="Restart retire", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    after = _page(title="Restart retire", body="Committed.", updated="2026-08-20")
    _publish_change(
        vault,
        batch_id="batch-restart-retire",
        generation="generation-restart-retire",
        changes=((rel, before, after, None),),
    )
    assert _non_retired_rows(vault) == {rel: "live"}

    assert lexstore.get_store(vault).upsert_paths([vault / rel]) is True
    # The process restarts between the two lane publications.
    _reset_overlay()
    lexstore.clear_stores()
    assert memory_refs.upsert_after_write(vault, [vault / rel]) is True
    _reset_overlay()

    assert _overlay(vault).ready is True
    assert _non_retired_rows(vault) == {}


def test_pending_projection_excludes_non_recall_candidates(tmp_path: Path) -> None:
    """Finding 4: pending identities join the projection under current policy."""
    vault = tmp_path
    _seed_corpus(vault)
    manifest_rel = "Knowledge Base/Records/Health/_collection.md"
    raw_rel = "Knowledge Base/Records/Health/items/raw.md"
    _write(vault, manifest_rel, _records_manifest())
    raw_before = "private measurement\n"
    _write(vault, raw_rel, raw_before)
    _prime(vault)

    raw_after = "private measurement, revised\n"
    _publish_change(
        vault,
        batch_id="batch-records",
        generation="generation-records",
        changes=((raw_rel, raw_before, raw_after, None),),
    )
    overlay = _overlay(vault)
    assert overlay.ready is True
    assert overlay.covers(raw_rel) is True

    snapshot = find_module.FreshnessSnapshot(vault, pending=overlay)
    assert raw_rel not in snapshot.recall_paths("vault")
    assert raw_rel not in snapshot.recall_paths("kb")


def test_resolve_page_applies_current_recall_admission(tmp_path: Path) -> None:
    """Finding 10: the hydration seam carries its own admission check."""
    vault = tmp_path
    _seed_corpus(vault)
    manifest_rel = "Knowledge Base/Records/Health/_collection.md"
    raw_rel = "Knowledge Base/Records/Health/items/raw.md"
    _write(vault, manifest_rel, _records_manifest())
    raw_before = "private measurement\n"
    _write(vault, raw_rel, raw_before)
    _prime(vault)

    raw_after = "private measurement, revised\n"
    _publish_change(
        vault,
        batch_id="batch-records-resolve",
        generation="generation-records-resolve",
        changes=((raw_rel, raw_before, raw_after, None),),
    )
    overlay = _overlay(vault)
    assert overlay.covers(raw_rel) is True
    assert overlay.page(raw_rel) is not None, "custody holds the row"

    # Custody is not release: the hydration seam refuses the suppressed identity
    # on its own, without relying on a caller having checked first.
    assert find_module._resolve_page(vault, raw_rel, overlay) is None


def test_shadowed_identity_is_absent_from_lane_rankings_before_fusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5: shadowing precedes scoring, fusion and every lane cap."""
    vault = tmp_path
    _seed_corpus(vault)
    stale_rel = "Knowledge Base/Notes/prefusion-stale.md"
    live_rel = "Knowledge Base/Notes/prefusion-live.md"
    marker = "upsilonprefusionmarker"
    stale_before = _page(
        title="Prefusion stale",
        body=" ".join([marker] * 30),
        updated="2026-09-01",
    )
    _write(vault, stale_rel, stale_before)
    _write(
        vault,
        live_rel,
        _page(
            title="Prefusion live",
            body=f"A settled page recording {marker} once.",
            updated="2026-08-01",
        ),
    )
    _prime(vault)

    stale_after = _page(
        title="Prefusion stale",
        body="Every distinguishing term has been removed.",
        updated="2026-09-02",
    )
    _publish_change(
        vault,
        batch_id="batch-prefusion",
        generation="generation-prefusion",
        changes=((stale_rel, stale_before, stale_after, None),),
    )

    requested: list[set[str]] = []
    real_hints = lexstore.emitted_parent_hints_result

    def _spy(vault_root, paths, **kwargs):
        requested.append(set(paths))
        return real_hints(vault_root, paths, **kwargs)

    monkeypatch.setattr(lexstore, "emitted_parent_hints_result", _spy)
    assert _hybrid(vault, marker, limit=5) == [live_rel]

    assert requested, "the candidate lanes did not reach the parent-hint seam"
    for admitted in requested:
        assert stale_rel not in admitted, (
            "a shadowed identity reached the fused lane set"
        )


def test_keyword_merge_does_not_evict_bounded_catalogue_rows(tmp_path: Path) -> None:
    """Finding 6: the merge over-fetches instead of displacing settled rows.

    Two ways the merge could shorten the settled half of a bounded lane, and
    both are pinned: the merged answer must not spend the caller's whole budget
    on pending rows, and the persistent side must be asked for enough rows that
    shadowing an identity inside the window does not simply lose a slot.
    """
    vault = tmp_path
    _seed_corpus(vault)
    marker = "phimergemarker"
    settled: list[str] = []
    for index in range(6):
        rel = f"Knowledge Base/Notes/merge-settled-{index:02d}.md"
        _write(
            vault,
            rel,
            _page(
                title=f"Merge settled {index:02d}",
                body=f"A settled page recording {marker} evidence.",
                updated=f"2026-07-{index + 1:02d}",
            ),
        )
        settled.append(rel)
    # The newest settled page is inside the bounded window and is the one a
    # pending edit shadows out of it.
    shadowed_rel = settled[-1]
    shadowed_before = _read(vault, shadowed_rel)

    pending_changes: list[tuple[str, str | None, str | None, str | None]] = [
        (
            shadowed_rel,
            shadowed_before,
            _page(
                title="Merge settled 05",
                body="Every distinguishing term has been removed.",
                updated="2026-07-06",
            ),
            None,
        )
    ]
    for index in range(10):
        rel = f"Knowledge Base/Notes/merge-pending-{index:02d}.md"
        pending_changes.append(
            (
                rel,
                None,
                _page(
                    title=f"Merge pending {index:02d}",
                    body=f"A committed page recording {marker} evidence.",
                    updated=f"2026-08-{index + 1:02d}",
                ),
                None,
            )
        )
    _prime(vault)

    def _lane(pending=None) -> list[str]:
        return find_module._keyword_match_paths(
            vault,
            marker,
            "kb",
            freshness=freshness.recall_checkpoint(vault, "kb").triple,
            repair=False,
            k=5,
            pending=pending,
        )

    baseline = _lane()
    assert len(baseline) == 5, baseline
    assert shadowed_rel in baseline

    _publish_change(
        vault,
        batch_id="batch-merge",
        generation="generation-merge",
        changes=tuple(pending_changes),
    )
    merged = _lane(pending=_overlay(vault))

    # Nothing the base kept and still carries the term was displaced.
    surviving = [rel for rel in baseline if rel != shadowed_rel]
    for rel in surviving:
        assert rel in merged, (rel, merged)
    # And the settled half is no shorter than it was, because the persistent
    # side is asked for enough rows to replace the one shadowing removed.
    settled_in_merged = [rel for rel in merged if rel in settled]
    assert len(settled_in_merged) >= len(baseline), (settled_in_merged, merged)
    assert shadowed_rel not in merged


def test_stale_pending_snapshot_generation_is_refused(tmp_path: Path) -> None:
    """Finding 7: the completeness fence must gate the cached projection."""
    vault = tmp_path
    _seed_corpus(vault)
    first_rel = "Knowledge Base/Notes/fence-first.md"
    second_rel = "Knowledge Base/Notes/fence-second.md"
    marker = "chifencemarker"
    _prime(vault)

    _publish_change(
        vault,
        batch_id="batch-fence-first",
        generation="generation-fence",
        changes=(
            (
                first_rel,
                None,
                _page(title="Fence first", body="Committed.", updated="2026-08-01"),
                None,
            ),
        ),
    )
    # Hydrate and cache this process's projection.
    assert _overlay(vault).covers(first_rel) is True

    # Another process publishes a second batch against the same store: the
    # durable rows and the store's pending generation both advance, while this
    # process's cached projection does not.
    second_after = _page(
        title="Fence second",
        body=f"A second committed page recording {marker} evidence.",
        updated="2026-08-02",
    )
    paths = (_batch_path(second_rel, before=None, after=second_after),)
    receipt = _prepare(
        vault, batch_id="batch-fence-second", generation="generation-fence", paths=paths
    )
    _write(vault, second_rel, second_after)
    proof = derived_receipts.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    )
    assert proof.outcome == "ready"
    assert derived_receipts.publish_pending_visibility(
        vault, receipt, publisher=lambda _root, _receipt: True
    )

    refreshed = _overlay(vault)
    assert refreshed.covers(second_rel) is True, "a stale cached projection was served"
    assert _keyword(vault, marker) == [second_rel]


def test_retirement_refuses_a_lane_pass_that_indexed_an_older_generation(
    tmp_path: Path, managed_runtime: None
) -> None:
    """Finding 8: a lane pass proves the identity it holds, not that it ran.

    The lanes index one generation and the file moves before their publication
    is accounted for. Retiring on "a pass ran" would clear custody while the
    catalogue still holds the older bytes, so the row must stay pending and
    managed recall must keep failing closed until the lanes hold the current
    generation.
    """
    vault = tmp_path
    _seed_corpus(vault)
    rel = "Knowledge Base/Notes/lane-race.md"
    marker = "psilaneracemarker"
    before = _page(title="Lane race", body="Nothing yet.", updated="2026-08-01")
    _write(vault, rel, before)
    _prime(vault)

    first = _page(title="Lane race", body="The committed bytes.", updated="2026-08-20")
    _publish_change(
        vault,
        batch_id="batch-lane-race",
        generation="generation-lane-race",
        changes=((rel, before, first, None),),
    )

    # Both lanes index the committed generation.
    assert lexstore.get_store(vault).upsert_paths([vault / rel]) is True
    assert memory_refs.upsert_after_write(vault, [vault / rel]) is True
    assert _non_retired_rows(vault) == {}

    # A second committed generation, whose custody the lanes have not published.
    second = _page(
        title="Lane race",
        body=f"A newer generation recording {marker} evidence.",
        updated="2026-08-21",
    )
    _publish_change(
        vault,
        batch_id="batch-lane-race-newer",
        generation="generation-lane-race-newer",
        changes=((rel, first, second, None),),
    )
    assert _non_retired_rows(vault) == {rel: "live"}

    # The file moves again before anything accounts for the lanes' pass, so the
    # lanes now hold neither the receipt's after state nor the current bytes.
    _write(
        vault,
        rel,
        _page(title="Lane race", body="A third generation.", updated="2026-08-22"),
    )
    _reset_overlay()
    memory_refs.delete_after_remove(vault, [])
    lexstore.get_store(vault)._note_pending_publication([vault / rel], [])

    assert _non_retired_rows(vault) == {rel: "live"}, (
        "custody was cleared while the lanes held an older generation"
    )
    _reset_overlay()
    find_module.reset_page_and_result_caches()
    with pytest.raises(find_module.RetrievalIndexWarming):
        _keyword(vault, marker)


def test_publication_notice_reprojects_only_the_published_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Micro-round M: one publication must not re-parse unrelated custody.

    A lane publishing one path used to drop the whole cached projection and
    re-derive it, so a single one-path watcher batch parsed every other
    outstanding pending path -- quadratic across a burst. Publishing one changed
    path must stay bounded by that path's own batch (invariant 1), while
    hydration itself keeps costing exactly one parse per pending row.
    """
    vault = tmp_path
    _seed_corpus(vault)
    _prime(vault)

    batch_rows = 10
    batch_count = 25
    published_rel = "Knowledge Base/Notes/amp-b00-r00.md"
    batch_paths: dict[str, list[str]] = {}
    for batch_index in range(batch_count):
        changes: list[tuple[str, str | None, str | None, str | None]] = []
        rels: list[str] = []
        for row_index in range(batch_rows):
            rel = f"Knowledge Base/Notes/amp-b{batch_index:02d}-r{row_index:02d}.md"
            rels.append(rel)
            changes.append(
                (
                    rel,
                    None,
                    _page(
                        title=f"Amp {batch_index:02d} {row_index:02d}",
                        body="A committed page awaiting derived convergence.",
                        updated="2026-08-01",
                    ),
                    None,
                )
            )
        batch_paths[f"batch-amp-{batch_index:02d}"] = rels
        _publish_change(
            vault,
            batch_id=f"batch-amp-{batch_index:02d}",
            generation="generation-amp",
            changes=tuple(changes),
        )

    pending_total = batch_rows * batch_count
    assert len(_non_retired_rows(vault)) == pending_total

    published_batch = next(
        rels for rels in batch_paths.values() if published_rel in rels
    )
    unrelated = {
        rel
        for rels in batch_paths.values()
        for rel in rels
        if rel not in published_batch
    }

    # Warm the projection so the notice has a current cached fence to reuse.
    assert _overlay(vault).ready is True

    parsed: list[str] = []
    real_parse = find_corpus.parse_page

    def _spy(path, mtime, vault_root, **kwargs):
        rel = find_module._vault_rel(vault, Path(path))
        if rel is not None:
            parsed.append(rel)
        return real_parse(path, mtime, vault_root, **kwargs)

    module = _pending_module()
    assert module is not None
    with monkeypatch.context() as guard:
        guard.setattr(find_corpus, "parse_page", _spy)
        module.note_persistent_publication(vault, "lexstore", [published_rel])

    assert not (set(parsed) & unrelated), sorted(set(parsed) & unrelated)[:5]
    assert len(parsed) <= len(published_batch) + 2, len(parsed)

    # Hydration is unchanged: exactly one parse per outstanding pending row.
    _reset_overlay()
    hydration: list[str] = []

    def _hydration_spy(path, mtime, vault_root, **kwargs):
        rel = find_module._vault_rel(vault, Path(path))
        if rel is not None:
            hydration.append(rel)
        return real_parse(path, mtime, vault_root, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(find_corpus, "parse_page", _hydration_spy)
        assert _overlay(vault).ready is True

    assert len(hydration) == pending_total, len(hydration)
