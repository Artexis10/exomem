"""Semantic read-your-writes after a fast acknowledgement.

A fast-acknowledged governed write hands its derived work to exact receipt
custody. Two things decided how long a semantic read of the new page then
waited, and neither was the work itself:

* the drain promoted one dependency level per pass and slept ``RETRY_SECONDS``
  between passes, so the embeddings component -- five levels deep -- completed,
  and the pending overlay that shadows the page's vector evidence retired,
  only after several idle ticks; and
* the receipt-owned fan-out reached the graph as a standalone caller, which
  joins a registered whole-vault rebuild for up to the standalone budget
  before the embedding step that follows it in the same fan-out can run.

Every node here is deterministic: one explicit drain pass at a fixed clock, and
spies on the graph join seam. Nothing sleeps.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from exomem import (
    derived_drain,
    derived_receipts,
    epistemic_graph,
    graph_sync,
    lexstore,
    pending_recall,
    semantic_index,
    vault,
    writer_lease,
)
from exomem import find as find_module
from exomem.derived_receipts import DerivedComponent

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

_SETTLED = {"completed", "not_required"}


def _compiled_page(title: str, marker: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        "updated: 2026-09-02\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [config] {marker} visibility probe paragraph ^visibility-anchor\n"
    )


def _manager(tmp_path: Path) -> writer_lease.LeaseManager:
    return writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )


def _governed_write(tmp_path: Path, root: Path, *, rel_path: str, source: str) -> dict:
    """One governed canonical batch through the real lease and batch seams."""
    target = root / rel_path

    def leaf(vault_root: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        planned = [vault.PlannedWrite(target, source, create_only=not target.exists())]
        states = {
            rel_path: semantic_index.build_parent_index_state(
                vault_root, rel_path, source=source
            )
        }
        vault.batch_atomic_write(planned, vault_root=vault_root, semantic_states=states)
        return {"path": rel_path, "warnings": []}

    return _manager(tmp_path).invoke(
        SimpleNamespace(name="remember", leaf=leaf, read_only=False),
        (root,),
        {"response_detail": "compact"},
        idempotency_key=None,
        mutation_request_id="33333333-3333-4333-8333-333333333333",
    )


def _only_receipt(root: Path) -> derived_receipts.DerivedBatchReceipt:
    from exomem import deferred_index

    connection = sqlite3.connect(deferred_index.store_path(root))
    try:
        batch_ids = [
            str(row[0])
            for row in connection.execute("SELECT batch_id FROM derived_batches")
        ]
    finally:
        connection.close()
    assert len(batch_ids) == 1, batch_ids
    return derived_receipts._load_receipt(root, batch_ids[0])


def _states(root: Path, receipt) -> dict[DerivedComponent, str]:
    return {
        component: derived_receipts.component_status(root, receipt, component).state
        for component in DerivedComponent
    }


def _one_pass(root: Path, *, mode_name: str = "normal", dispatch=None) -> int:
    """Exactly one production scheduler pass at a fixed clock."""
    return derived_drain.drain_once(
        root,
        dispatch=derived_drain.component_dispatcher() if dispatch is None else dispatch,
        observe_current_generation=derived_drain.canonical_generation_observer(),
        visibility_publisher=pending_recall.publish,
        retire_visibility=derived_drain.pending_visibility_retirer(),
        limit=derived_drain.progress_limit(mode_name=mode_name),
        now=time.time(),
    )


@pytest.fixture(autouse=True)
def _fast_ack_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    pending_recall.reset()
    find_module.clear_cache()


@pytest.fixture
def live_catalogue(vault: Path) -> Path:
    """A built lexical catalogue, as a running server has before any write.

    Without one the lexical upsert is a no-op that the first search heals by
    building the catalogue whole, and the overlay cannot retire before then.
    """
    assert lexstore.get_store(vault).rebuild_atomic() is True
    return vault


def _word_vector(text: str) -> np.ndarray:
    """Deterministic bag-of-words vector: texts sharing words point alike."""
    from exomem import embeddings

    total = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    for word in text.lower().split():
        seed = int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:8], "little")
        total += np.random.default_rng(seed).standard_normal(embeddings.VECTOR_DIM).astype(
            np.float32
        )
    norm = float(np.linalg.norm(total)) or 1.0
    return total / norm


@pytest.fixture
def model_free_encoder(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Real embedding publication with a deterministic in-process encoder."""
    from exomem import embeddings, readiness

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(readiness, "should_defer", lambda *_a, **_k: False)
    encoded: list[int] = []

    def encode(texts, *, is_query: bool = False):  # noqa: ANN001
        encoded.append(len(texts))
        return np.stack([_word_vector(text) for text in texts]).astype(np.float32)

    def encode_live(chunks):  # noqa: ANN001
        return encode(list(chunks))

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    monkeypatch.setattr(embeddings, "_embed_live_chunks", encode_live)
    embeddings.clear_embedding_indexes()
    return encoded


# --------------------------------------------------------------------------- #
# The pass cadence: dependency promotion inside one bounded pass
# --------------------------------------------------------------------------- #


def test_one_normal_drain_pass_converges_every_component_of_a_fast_acked_write(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """No component waits a RETRY tick for a predecessor this pass completed.

    Before, one pass completed only the first dependency level (`freshness`);
    `embeddings` sat five idle ticks behind it, and the pending overlay kept
    shadowing the page's vector evidence the whole time.
    """
    vault = live_catalogue
    rel = "Knowledge Base/Notes/Insights/visibility-one-pass.md"
    terminal = _governed_write(
        tmp_path, vault, rel_path=rel, source=_compiled_page("One pass", "onepassmarker")
    )
    assert terminal["status"] == "committed"
    assert terminal["derived_sync"] == "pending"
    receipt = _only_receipt(vault)

    _one_pass(vault)

    states = _states(vault, receipt)
    assert all(state in _SETTLED for state in states.values()), states
    assert not pending_recall.overlay(vault).covers(rel)


def test_quiet_mode_pass_still_converges_exactly_one_component(
    vault: Path, tmp_path: Path
) -> None:
    """Quiet mode keeps throttling: its one correctness slot is one component."""
    rel = "Knowledge Base/Notes/Insights/visibility-quiet.md"
    _governed_write(
        tmp_path, vault, rel_path=rel, source=_compiled_page("Quiet pass", "quietpassmarker")
    )
    receipt = _only_receipt(vault)

    assert _one_pass(vault, mode_name="quiet") == 1

    states = _states(vault, receipt)
    assert [c for c, state in states.items() if state == "completed"] == [
        DerivedComponent.FRESHNESS
    ], states
    assert pending_recall.overlay(vault).covers(rel)


def test_overlay_retires_once_the_components_that_gate_it_complete(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """Retirement is re-derived when the gating components complete.

    The recall lanes publish inside the fan-out, before the drain records the
    components that fan-out proved, so the publication-time retirement attempt
    cannot succeed; completing a component does not move the pending fence.
    Before, the cached projection therefore kept shadowing the page's vector
    and graph evidence until an unrelated write invalidated it. Quiet mode is
    used so that this holds independently of in-pass promotion.
    """
    vault = live_catalogue
    rel = "Knowledge Base/Notes/Insights/visibility-retire.md"
    _governed_write(
        tmp_path, vault, rel_path=rel, source=_compiled_page("Retire pass", "retirepassmarker")
    )
    receipt = _only_receipt(vault)
    assert pending_recall.overlay(vault).covers(rel)

    for _level in range(len(DerivedComponent)):
        _one_pass(vault, mode_name="quiet")
    assert all(state in _SETTLED for state in _states(vault, receipt).values())

    assert not pending_recall.overlay(vault).covers(rel)


def test_in_pass_promotion_never_dispatches_a_component_before_its_dependencies(
    vault: Path, tmp_path: Path
) -> None:
    rel = "Knowledge Base/Notes/Insights/visibility-order.md"
    _governed_write(
        tmp_path, vault, rel_path=rel, source=_compiled_page("Order pass", "orderpassmarker")
    )
    receipt = _only_receipt(vault)
    production = derived_drain.component_dispatcher()
    dispatched: list[DerivedComponent] = []
    violations: list[tuple[str, str, str]] = []

    def recording(root: Path, status) -> bool:  # noqa: ANN001
        for predecessor in derived_receipts._COMPONENT_DEPENDENCIES[status.component]:
            state = derived_receipts.component_status(root, receipt, predecessor).state
            if state not in _SETTLED:
                violations.append((status.component.value, predecessor.value, state))
        dispatched.append(status.component)
        return production(root, status)

    _one_pass(vault, dispatch=recording)

    assert violations == []
    assert DerivedComponent.EMBEDDINGS in dispatched
    assert dispatched.index(DerivedComponent.LEXSTORE) < dispatched.index(
        DerivedComponent.EMBEDDINGS
    )


def test_fast_acked_note_vector_row_is_readable_after_one_drain_pass(
    live_catalogue: Path, tmp_path: Path, model_free_encoder: list[int]
) -> None:
    """The semantic read the acknowledgement promised, within one pass.

    The page's published vectors are the exact current generation, and the
    pending overlay no longer shadows them, so vector recall reads the note's
    own row rather than being withheld behind idle scheduler ticks.
    """
    from exomem import embeddings

    vault = live_catalogue
    rel = "Knowledge Base/Notes/Insights/visibility-vector.md"
    marker = "vectorvisibilitymarker"
    terminal = _governed_write(
        tmp_path, vault, rel_path=rel, source=_compiled_page("Vector pass", marker)
    )
    assert terminal["status"] == "committed"
    receipt = _only_receipt(vault)

    _one_pass(vault)

    assert _states(vault, receipt)[DerivedComponent.EMBEDDINGS] == "completed"
    page = find_module._CACHE.get(vault / rel, vault)
    assert page is not None
    chunks = embeddings._chunks_for_page(vault, page)
    assert embeddings.published_generation_vectors(vault, rel, chunks=chunks) is not None
    assert not pending_recall.overlay(vault).covers(rel)


# --------------------------------------------------------------------------- #
# The graph join inside the receipt-owned fan-out
# --------------------------------------------------------------------------- #


def test_receipt_owned_fanout_starts_a_registered_graph_rebuild_without_waiting_on_it(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered rebuild is handed to the graph pipeline, never waited on.

    The receipt already treats a started graph handoff as convergent, so the
    only effect of the standalone join was to hold the embedding step that
    follows the graph in the same fan-out for up to the standalone budget.
    """
    rel = "Knowledge Base/Notes/Insights/visibility-graph.md"
    _governed_write(
        tmp_path, vault, rel_path=rel, source=_compiled_page("Graph pass", "graphpassmarker")
    )
    receipt = _only_receipt(vault)

    def deferred_without_queue(self, paths, **_kwargs):  # noqa: ANN001
        # The incremental refresh cannot converge these paths and did not
        # queue them, so the dispatcher registers a whole-vault rebuild.
        return {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1}

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "refresh_paths", deferred_without_queue
    )
    waits: list[float | None] = []
    started: list[str] = []
    monkeypatch.setattr(
        graph_sync,
        "wait_for_registered",
        lambda _root, timeout=None, **_k: waits.append(timeout),
    )
    monkeypatch.setattr(
        graph_sync, "start_registered", lambda *_a, **_k: started.append("start")
    )

    _one_pass(vault)

    # The registered whole-vault rebuild still starts: graph work is handed
    # off, not dropped. What the fan-out no longer spends is any wait on it.
    assert started, "the registered rebuild was never started"
    assert waits and all(timeout == 0.0 for timeout in waits), waits
    states = _states(vault, receipt)
    assert states[DerivedComponent.GRAPH] == "completed", states
    assert states[DerivedComponent.EMBEDDINGS] in _SETTLED, states


def test_standalone_callers_keep_their_join_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the receipt-owned fan-out waives the wait; a library caller joins."""
    from exomem import request_budget

    monkeypatch.setattr(request_budget, "current", lambda: None)

    assert (
        graph_sync.standalone_join_budget_seconds()
        == graph_sync.STANDALONE_JOIN_BUDGET_SECONDS
    )
    with graph_sync.standalone_join_waived():
        assert graph_sync.standalone_join_budget_seconds() == 0.0
    assert (
        graph_sync.standalone_join_budget_seconds()
        == graph_sync.STANDALONE_JOIN_BUDGET_SECONDS
    )
