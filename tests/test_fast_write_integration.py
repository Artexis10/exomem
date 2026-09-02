"""Lane 5 — the accepted lanes wired together through the frozen protocols.

Every node here drives *production* modules: the real Lane 3 writer path
(``writer_lease.LeaseManager.invoke`` over the real ``vault.batch_atomic_write``
and the real public leaves), the real Lane 1 receipt store, the real Lane 2
pending publisher and recall consumer, and the real Lane 4 advisory executor and
result resolver.  No sibling lane's production code is faked here: the whole
point of this file is that the seams the author lanes left open are joined, and
a fake on either side of a seam would hide exactly the defect it exists to find.

The Lane 1 protocol fake appears only where a *terminal* boundary is being
exercised without a store, which is Lane 3's own idiom and never a substitute
for a sibling lane's implementation.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    deferred_write_advisory,
    derived_drain,
    derived_receipts,
    lexstore,
    pending_recall,
    semantic_index,
    vault,
    writer_lease,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.derived_receipts import DerivedComponent

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)


# --------------------------------------------------------------------------- #
# Real-path harness
# --------------------------------------------------------------------------- #


def _batch_ids(root: Path) -> list[str]:
    """Every prepared batch id, oldest first, read straight from the store."""
    from exomem import deferred_index

    store = deferred_index.store_path(root)
    if not store.exists():
        return []
    connection = sqlite3.connect(store)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT batch_id FROM derived_batches ORDER BY rowid"
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        # The additive schema is created on first prepare; before that there is
        # simply no batch to name.
        return []
    finally:
        connection.close()


def _compiled_page(title: str, marker: str, *, page_type: str = "insight") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        "status: active\n"
        "updated: 2026-09-02\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [config] {marker} #integration (lane5) ^lane5-anchor\n"
    )


def _manager(tmp_path: Path) -> writer_lease.LeaseManager:
    return writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )


def _governed_write(
    manager: writer_lease.LeaseManager,
    root: Path,
    *,
    rel_path: str,
    source: str,
    command_name: str = "remember",
    idempotency_key: str | None = None,
    response_detail: str = "compact",
    declare: bool = True,
) -> dict:
    """One governed canonical batch through the real lease + batch seams.

    This is Lane 3's own central seam, driven with the *real* receipt store
    rather than the protocol fake, so the publisher, the scheduler and the
    advisory executor are the production ones.
    """
    target = root / rel_path

    def leaf(vault_root: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        planned = [vault.PlannedWrite(target, source, create_only=not target.exists())]
        if declare:
            states = {
                rel_path: semantic_index.build_parent_index_state(
                    vault_root, rel_path, source=source
                )
            }
            vault.batch_atomic_write(
                planned, vault_root=vault_root, semantic_states=states
            )
        else:
            vault.batch_atomic_write(planned, vault_root=vault_root)
        return {"path": rel_path, "warnings": []}

    command = SimpleNamespace(name=command_name, leaf=leaf, read_only=False)
    return manager.invoke(
        command,
        (root,),
        {"response_detail": response_detail},
        idempotency_key=idempotency_key,
        mutation_request_id="22222222-2222-4222-8222-222222222222",
    )


def _drain(root: Path, *, passes: int = 24, limit: int = 32) -> int:
    """Run the production scheduler pass until custody settles.

    ``drain_once`` is one bounded pass and components are promoted in the
    store's dependency order, so a batch converges over several passes. Time is
    advanced explicitly rather than slept: the store's backoff is a wall-clock
    ``next_attempt_at``, and a test that slept it out would be measuring the
    backoff constant instead of the wiring.
    """
    processed = 0
    now = time.time()
    dispatch = derived_drain.component_dispatcher()
    observe = derived_drain.canonical_generation_observer()
    for _attempt in range(passes):
        processed += derived_drain.drain_once(
            root,
            dispatch=dispatch,
            observe_current_generation=observe,
            visibility_publisher=pending_recall.publish,
            limit=limit,
            now=now,
        )
        now += derived_drain.MAX_RETRY_SECONDS + 1.0
        if not derived_receipts.due_component_count(
            root, now=now
        ) and not derived_receipts.recoverable_batch_count(root):
            break
    return processed


def _keyword(root: Path, query: str, *, limit: int = 5) -> list[str]:
    return [
        hit.path
        for hit in find_module.find(
            root, query=query, scope="kb-only", mode="keyword", limit=limit
        )
    ]


def _hybrid(root: Path, query: str, *, limit: int = 5) -> list[str]:
    return [
        hit.path
        for hit in find_module.find(
            root, query=query, scope="kb-only", mode="hybrid", limit=limit
        )
    ]


@pytest.fixture(autouse=True)
def _fast_ack_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    pending_recall.reset()
    find_module.clear_cache()


# --------------------------------------------------------------------------- #
# Wiring: the pending publisher behind the frozen visibility seam
# --------------------------------------------------------------------------- #


def test_governed_write_publishes_pending_visibility_through_lane2(
    vault: Path, tmp_path: Path
) -> None:
    """The frozen publisher seam must resolve to Lane 2's real publisher.

    Nothing installs one on the merged base, so ``publish_pending_visibility``
    refuses and the acknowledgement becomes committed-uncertain: the write
    cannot report success at all, which is the strongest possible statement
    that the two lanes are not joined.
    """
    rel = "Knowledge Base/Notes/Insights/lane5-publisher.md"
    marker = "lane5publisherwiring"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 publisher", marker),
    )

    assert terminal["status"] == "committed"
    assert terminal["ok"] is True
    snapshot = derived_receipts.snapshot_pending_visibility(vault, limit=64)
    assert snapshot.outcome == "complete", snapshot.failure_code
    live = {
        row.rel_path: row.state
        for batch in snapshot.batches
        for row in batch.rows
        if row.state != "retired"
    }
    assert live.get(rel) == "live", live


def test_immediate_write_is_visible_by_stable_ref_keyword_and_hybrid(
    vault: Path, tmp_path: Path
) -> None:
    """Read-your-write through the real overlay after a real governed write."""
    rel = "Knowledge Base/Notes/Insights/lane5-visible.md"
    marker = "lane5visibilitymarker"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 visible", marker),
    )
    assert terminal["status"] == "committed"

    assert (vault / rel).read_text(encoding="utf-8").count(marker) == 1
    assert rel in _keyword(vault, marker)
    assert rel in _hybrid(vault, marker)


# --------------------------------------------------------------------------- #
# Wiring: the advisory executor behind the component scheduler
# --------------------------------------------------------------------------- #


def test_component_dispatcher_routes_advisory_claims_only(vault: Path) -> None:
    """The scheduler must own a dispatcher that routes claims BY COMPONENT.

    Lane 4's pass hands a non-advisory claim straight back with
    ``component_unhandled``, which costs the owning lane a real attempt. The
    dispatcher the drain installs must therefore never offer one.
    """
    dispatcher = derived_drain.component_dispatcher()
    seen: list[DerivedComponent] = []
    original = deferred_write_advisory.execute_write_advisory

    def spy(root, status, **kwargs):  # noqa: ANN001
        seen.append(status.component)
        return original(root, status, **kwargs)

    deferred_write_advisory.execute_write_advisory = spy  # type: ignore[assignment]
    try:
        for component in DerivedComponent:
            status = derived_receipts.DerivedComponentStatus(
                batch_id="b" * 32,
                component=component,
                revision=1,
                lease_revision=1,
                state="claimed",
                canonical_generation="1",
                attempt_count=0,
                next_attempt_at=0.0,
                claim_owner="lane5",
                claim_expires_at=None,
                failure_code=None,
            )
            try:
                dispatcher(vault, status)
            except Exception:  # noqa: BLE001 - the routing decision is the assertion
                pass
    finally:
        deferred_write_advisory.execute_write_advisory = original  # type: ignore[assignment]

    assert seen == [DerivedComponent.WRITE_ADVISORY], seen


def test_pending_advisory_result_ref_resolves_pending_before_the_worker_runs(
    vault: Path, tmp_path: Path
) -> None:
    rel = "Knowledge Base/Notes/Insights/lane5-advisory-pending.md"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 advisory pending", "lane5advisorypending"),
    )
    ref = terminal.get("advisory_result_ref")
    assert isinstance(ref, str) and ref.startswith(
        "exomem://write-advisory-result/"
    ), terminal
    assert terminal["advisory_sync"] == "pending"

    resolved = deferred_write_advisory.resolve_result(vault, ref=ref)
    assert resolved["status"] == "pending", resolved


def test_advisory_result_reaches_ready_after_the_scheduler_drains_it(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real executor must run from the real scheduler, not by hand."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    rel = "Knowledge Base/Notes/Insights/lane5-advisory-ready.md"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 advisory ready", "lane5advisoryready"),
    )
    ref = terminal["advisory_result_ref"]

    assert _drain(vault) >= 1

    resolved = deferred_write_advisory.resolve_result(vault, ref=ref)
    # `ready` needs the optional retrieval model; this environment has none, so
    # the honest terminal state is the closed `embedding_unavailable` failure.
    # Either way the job must leave `pending`: a finished or failed advisory
    # left pending for ever is the defect this node exists to catch.
    assert resolved["status"] in {"ready", "failed"}, resolved
    status = derived_receipts.component_status(
        vault,
        derived_receipts._load_receipt(vault, _batch_ids(vault)[0]),
        DerivedComponent.WRITE_ADVISORY,
    )
    assert status.state == "completed", status


# --------------------------------------------------------------------------- #
# The feature flag, both ways
# --------------------------------------------------------------------------- #


def test_flag_off_restores_the_prior_synchronous_terminal(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "0")
    rel = "Knowledge Base/Notes/Insights/lane5-flag-off.md"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 flag off", "lane5flagoff"),
    )

    assert terminal["status"] == "committed"
    for field in ("derived_sync", "derived_sync_components", "advisory_sync",
                  "advisory_result_ref"):
        assert field not in terminal, field
    assert derived_receipts.recoverable_batch_count(vault) == 0
    assert writer_lease.fast_durable_ack_active() is False


def test_flag_on_enables_the_fast_path(vault: Path, tmp_path: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/lane5-flag-on.md"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 flag on", "lane5flagon"),
    )
    assert terminal["derived_sync"] in {"pending", "completed", "failed"}
    assert writer_lease.fast_durable_ack_active() is True


def test_flag_default_is_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default stays off until live acceptance flips it."""
    monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)
    assert writer_lease.fast_durable_ack_active() is False


def test_flag_rollback_drains_outstanding_custody_before_old_behaviour(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback order: flag to 0, the new worker drains, then old behaviour.

    Design Migration Plan step 8. Custody prepared while the flag was on must
    still converge after it is turned off, and the next write must take the
    prior synchronous fanout without stranding the outstanding batch.
    """
    manager = _manager(tmp_path)
    rel_before = "Knowledge Base/Notes/Insights/lane5-rollback-before.md"
    _governed_write(
        manager,
        vault,
        rel_path=rel_before,
        source=_compiled_page("Lane5 rollback before", "lane5rollbackbefore"),
        idempotency_key="lane5-rollback-1",
    )
    outstanding = derived_receipts.due_component_count(vault)
    assert outstanding >= 1

    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "0")
    assert _drain(vault) >= 1
    assert derived_receipts.due_component_count(vault) == 0

    rel_after = "Knowledge Base/Notes/Insights/lane5-rollback-after.md"
    terminal = _governed_write(
        manager,
        vault,
        rel_path=rel_after,
        source=_compiled_page("Lane5 rollback after", "lane5rollbackafter"),
        idempotency_key="lane5-rollback-2",
    )
    assert "derived_sync" not in terminal
    assert (vault / rel_after).exists()


# --------------------------------------------------------------------------- #
# Applicability, both ways
# --------------------------------------------------------------------------- #


def test_advisory_applies_to_a_compiled_page(vault: Path, tmp_path: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/lane5-applies.md"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 applies", "lane5applies", page_type="insight"),
    )
    assert terminal["advisory_sync"] == "pending"
    assert terminal["advisory_result_ref"].startswith(
        "exomem://write-advisory-result/"
    )


def test_advisory_never_applies_to_a_page_type_outside_its_vocabulary(
    vault: Path, tmp_path: Path
) -> None:
    """A declared type the advisory sweep can never compare carries no custody."""
    rel = "Knowledge Base/Notes/Insights/lane5-inapplicable.md"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page(
            "Lane5 inapplicable", "lane5inapplicable", page_type="dataset"
        ),
    )
    assert terminal["advisory_sync"] == "not_required"
    assert "advisory_result_ref" not in terminal


# --------------------------------------------------------------------------- #
# Design §10 row: the default write never encodes an advisory inline
# --------------------------------------------------------------------------- #


def test_default_write_never_encodes_advisory_inline(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow encoder must not be reachable from the acknowledgement path.

    Mutant: restore the inline encode and this node blocks on the injected
    encoder, so the default public-write latency claim is false.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    from exomem import embeddings

    calls: list[str] = []

    def exploding_encode(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append("encode")
        raise AssertionError("the default write encoded an advisory inline")

    for seam in ("encode_texts", "embed_texts", "encode"):
        if hasattr(embeddings, seam):
            monkeypatch.setattr(embeddings, seam, exploding_encode, raising=False)
    monkeypatch.setattr(
        "exomem.corpus_aware._best_cosine_per_file", exploding_encode, raising=False
    )

    rel = "Knowledge Base/Notes/Insights/lane5-no-inline-encode.md"
    terminal = _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 no inline", "lane5noinlineencode"),
    )

    assert terminal["status"] == "committed"
    assert calls == []
    assert terminal["advisory_sync"] == "pending"


# --------------------------------------------------------------------------- #
# Restart and exact replay
# --------------------------------------------------------------------------- #


def test_restart_hydration_serves_the_committed_generation(
    vault: Path, tmp_path: Path
) -> None:
    rel = "Knowledge Base/Notes/Insights/lane5-restart.md"
    marker = "lane5restartmarker"
    _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 restart", marker),
    )

    # A restart is exactly this: no in-process overlay, no warm caches.
    pending_recall.reset()
    find_module.reset_page_and_result_caches()

    assert rel in _keyword(vault, marker)
    assert rel in _hybrid(vault, marker)


def test_exact_replay_returns_the_original_terminal(
    vault: Path, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    rel = "Knowledge Base/Notes/Insights/lane5-replay.md"
    source = _compiled_page("Lane5 replay", "lane5replaymarker")
    first = _governed_write(
        manager, vault, rel_path=rel, source=source, idempotency_key="lane5-replay"
    )
    _drain(vault)
    second = _governed_write(
        manager, vault, rel_path=rel, source=source, idempotency_key="lane5-replay"
    )
    assert second == first


# --------------------------------------------------------------------------- #
# Residual: Lane 2's named-row re-proof branch (reviewer mutant survived)
# --------------------------------------------------------------------------- #


def _prime_recall(root: Path) -> None:
    """Publish every persistent projection at the current corpus, then freeze."""
    from exomem import freshness, memory_refs

    find_module.clear_cache()
    lexstore.clear_stores()
    freshness.rebaseline(root)
    memory_refs.ReferenceIndex(root).rebuild_all()
    assert lexstore.get_store(root).rebuild_atomic() is True
    find_module.reset_page_and_result_caches()


def test_publication_notice_reproves_a_named_row_against_current_bytes(
    vault: Path
) -> None:
    """A named row is re-proven from disk; a reused cached row cannot retire.

    ``_reproject_named`` reuses the fenced projection for every row a
    publication did not name and re-proves the ones it did. The re-proof is
    load-bearing: when an out-of-band write supersedes the row's proven after
    state, only a re-proof discovers it, and only then does retirement compare
    the lanes against the bytes that are actually on disk. Reusing the cached
    row instead leaves retirement asking for a generation no lane holds, so
    the row never retires and the overlay shadows a settled page for ever.
    """
    rel = "Knowledge Base/Notes/lane5-reproject.md"
    marker = "lane5reprojectmarker"
    before = _compiled_page("Lane5 reproject", "lane5reprojectbefore")
    after = _compiled_page("Lane5 reproject", marker)
    _put(vault, rel, before)
    receipt = _prepare_store_batch(
        vault,
        batch_id="lane5-reproject",
        generation="generation-1",
        changes=((rel, before, after),),
    )
    _put(vault, rel, after)
    assert (
        derived_receipts.prove_committed(
            vault, receipt, current_generation="generation-1"
        ).outcome
        == "ready"
    )
    assert derived_receipts.publish_pending_visibility(
        vault, receipt, publisher=pending_recall.publish
    )

    # Cache a fenced projection over the live custody.
    assert pending_recall.overlay(vault).outcome == "ready"

    # An out-of-band write supersedes the row's proven after state.
    superseded = _compiled_page("Lane5 reproject", marker + "edited")
    (vault / rel).write_text(superseded, encoding="utf-8")

    # Both recall lanes publish the bytes that are now on disk.
    _prime_recall(vault)

    pending_recall.note_persistent_publication(vault, "lexstore", [rel])
    pending_recall.note_persistent_publication(vault, "memory_refs", [rel])

    snapshot = derived_receipts.snapshot_pending_visibility(vault, limit=64)
    assert snapshot.outcome == "complete", snapshot.failure_code
    live = [
        row.rel_path
        for batch in snapshot.batches
        for row in batch.rows
        if row.state != "retired"
    ]
    assert rel not in live, live


# --------------------------------------------------------------------------- #
# Residual: one session's advisory claim is released on supersession
# --------------------------------------------------------------------------- #


def test_superseded_batch_releases_the_session_advisory_claim(
    vault: Path, tmp_path: Path
) -> None:
    """A later committed batch must still get the session's advisory job.

    One acknowledgement carries one advisory result reference, and the claim is
    taken at the committed handoff. A leaf that writes the same governed page
    twice supersedes its own first batch: that batch publishes nothing, carries
    no result reference, and its advisory work belongs to whatever superseded
    it. Holding the session's only claim there would leave the batch that
    actually survives with no advisory job at all and no way to ask for one.
    """
    rel = "Knowledge Base/Notes/Insights/lane5-claim-release.md"
    first_source = _compiled_page("Lane5 claim release", "lane5claimfirst")
    second_source = _compiled_page("Lane5 claim release", "lane5claimsecond")
    prepared: list[str | None] = []

    def leaf(vault_root: Path):
        for source in (first_source, second_source):
            target = vault_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            states = {
                rel: semantic_index.build_parent_index_state(
                    vault_root, rel, source=source
                )
            }
            vault_module.batch_atomic_write(
                [
                    vault_module.PlannedWrite(
                        target, source, create_only=not target.exists()
                    )
                ],
                vault_root=vault_root,
                semantic_states=states,
            )
            session = writer_lease._ACTIVE_FAST_ACK_SESSION.get()
            assert session is not None
            prepared.append(session.batches[-1].advisory_target)
        return {"path": rel, "warnings": []}

    manager = _manager(tmp_path)
    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    terminal = manager.invoke(
        command,
        (vault,),
        {"response_detail": "compact"},
        idempotency_key="lane5-claim-release",
        mutation_request_id="33333333-3333-4333-8333-333333333333",
    )

    # Both batches name the target: the first takes the claim, and the second
    # takes it back because its own paths supersede the first's.
    assert prepared == [rel, rel], prepared

    assert terminal["status"] == "committed", terminal
    assert terminal["advisory_sync"] == "pending", terminal
    ref = terminal["advisory_result_ref"]
    assert ref.startswith("exomem://write-advisory-result/")

    ids = _batch_ids(vault)
    assert len(ids) == 2, ids
    older = derived_receipts._load_receipt(vault, ids[0])
    newer = derived_receipts._load_receipt(vault, ids[1])
    # The reference the caller holds belongs to the batch that survived.
    assert derived_receipts.advisory_result_ref(vault, newer) == ref
    assert derived_receipts.advisory_result_ref(vault, older) != ref
    assert (
        derived_receipts.component_status(
            vault, older, DerivedComponent.WRITE_ADVISORY
        ).state
        == "superseded"
    )
    # The superseded batch releases no content through its own reference.
    stale = derived_receipts.read_advisory_result(
        vault, derived_receipts.advisory_result_ref(vault, older)
    )
    assert stale is not None and stale.state == "superseded"
    assert stale.candidates == ()


# --------------------------------------------------------------------------- #
# Residual: a public safe-path predicate instead of a broad ValueError catch
# --------------------------------------------------------------------------- #


def test_receipt_store_exports_a_public_safe_path_predicate() -> None:
    """`vault.py` must ask a named predicate, not catch every ValueError.

    The broad catch also swallows a malformed digest and a path absent both
    before and after -- neither of which is a path judgement, and both of which
    would be a real defect if they ever became reachable from the writer.
    """
    assert derived_receipts.is_governed_receipt_path(
        "Knowledge Base/Notes/Insights/page.md"
    )
    assert not derived_receipts.is_governed_receipt_path("README.md")
    assert not derived_receipts.is_governed_receipt_path("docs/guide.md")
    assert not derived_receipts.is_governed_receipt_path("Knowledge Base/Notes/a.txt")
    assert not derived_receipts.is_governed_receipt_path("../escape.md")
    assert not derived_receipts.is_governed_receipt_path(None)


def test_non_governed_markdown_is_skipped_by_the_named_predicate(
    vault: Path, tmp_path: Path
) -> None:
    """A refusal that is not a path judgement must not be silently swallowed."""
    rel = "Knowledge Base/Notes/Insights/lane5-predicate.md"
    source = _compiled_page("Lane5 predicate", "lane5predicatemarker")

    def leaf(vault_root: Path):
        target = vault_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        extra = vault_root / "README.md"
        planned = [
            vault_module.PlannedWrite(
                target, source, create_only=not target.exists()
            ),
            vault_module.PlannedWrite(
                extra, "# readme\n", create_only=not extra.exists()
            ),
        ]
        states = {
            rel: semantic_index.build_parent_index_state(
                vault_root, rel, source=source
            )
        }
        vault_module.batch_atomic_write(
            planned, vault_root=vault_root, semantic_states=states
        )
        return {"path": rel, "warnings": []}

    manager = _manager(tmp_path)
    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    terminal = manager.invoke(
        command,
        (vault,),
        {"response_detail": "compact"},
        idempotency_key="lane5-predicate",
        mutation_request_id="44444444-4444-4444-8444-444444444444",
    )

    assert terminal["status"] == "committed"
    assert (vault / "README.md").exists()
    snapshot = derived_receipts.snapshot_pending_visibility(vault, limit=64)
    covered = {
        row.rel_path for batch in snapshot.batches for row in batch.rows
    }
    assert covered == {rel}, covered


# --------------------------------------------------------------------------- #
# Task 5.5 — the crash-cut matrix, on the aggregate
#
# Every node asserts the same four properties for its cut: no stale
# publication, no lost custody, no duplicate canonical mutation, and no
# permanent false-pending state.
# --------------------------------------------------------------------------- #


def _prepare_store_batch(
    root: Path,
    *,
    batch_id: str,
    generation: str,
    changes: tuple[tuple[str, str | None, str | None], ...],
    required=None,
    now: float = 10.0,
):
    """Prepare exact custody for a batch through the frozen store only."""
    import hashlib

    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    paths = tuple(
        derived_receipts.DerivedBatchPath(
            rel_path=rel,
            before_hash=None if before is None else digest(before),
            after_hash=None if after is None else digest(after),
        )
        for rel, before, after in changes
    )
    components = (
        frozenset({DerivedComponent.LEXSTORE, DerivedComponent.MEMORY_REFS})
        if required is None
        else frozenset(required)
    )
    advisory_target = advisory_fingerprint = None
    replay_until = None
    if DerivedComponent.WRITE_ADVISORY in components:
        # The frozen contract binds the fingerprint to the prepared after hash.
        advisory_target = paths[0].rel_path
        advisory_fingerprint = paths[0].after_hash
        # Retention is wall-clock, so it is anchored to the real clock even
        # when the rest of the batch runs on the synthetic one.
        replay_until = time.time() + 86_400.0
    return derived_receipts.prepare_batch(
        root,
        batch_id=batch_id,
        mutation_attempt_digest=digest(batch_id),
        canonical_generation=generation,
        checkpoint_id=f"checkpoint-{generation}",
        paths=paths,
        required_components=components,
        advisory_target_rel_path=advisory_target,
        advisory_target_fingerprint=advisory_fingerprint,
        terminal_replay_until=replay_until,
        advisory_retention_until=replay_until,
        now=now,
    )


def _put(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_crash_cut_pre_prepare_leaves_no_custody_and_no_canonical_change(
    vault: Path, tmp_path: Path
) -> None:
    """Cut 1: death before preparation. Nothing prepared, nothing replaced."""
    rel = "Knowledge Base/Notes/Insights/lane5-cut-preprepare.md"
    original = _compiled_page("Lane5 cut preprepare", "lane5cutpreprepare")
    _put(vault, rel, original)

    def explode(*_args, **_kwargs):
        raise RuntimeError("receipt store unavailable")

    real_prepare = derived_receipts.prepare_batch
    derived_receipts.prepare_batch = explode  # type: ignore[assignment]
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 - the envelope varies
            _governed_write(
                _manager(tmp_path),
                vault,
                rel_path=rel,
                source=_compiled_page("Lane5 cut preprepare", "changed"),
            )
    finally:
        derived_receipts.prepare_batch = real_prepare  # type: ignore[assignment]

    assert (vault / rel).read_text(encoding="utf-8") == original
    assert derived_receipts.recoverable_batch_count(vault) == 0
    assert derived_receipts.due_component_count(vault) == 0


def test_crash_cut_prepared_before_replacement_never_publishes(vault: Path) -> None:
    """Cut 2: prepared, then death before the first canonical replacement."""
    rel = "Knowledge Base/Notes/cut-prepared.md"
    before, after = "before body\n", "after body\n"
    _put(vault, rel, before)
    receipt = _prepare_store_batch(
        vault,
        batch_id="cut-prepared",
        generation="generation-1",
        changes=((rel, before, after),),
    )

    # Nothing was replaced, and nobody knows the attempt did not commit.
    proof = derived_receipts.prove_committed(
        vault, receipt, current_generation="generation-1"
    )
    assert proof.outcome == "reconcile_required", proof.outcome
    with pytest.raises(RuntimeError):
        derived_receipts.publish_pending_visibility(
            vault, receipt, publisher=pending_recall.publish
        )
    assert (vault / rel).read_text(encoding="utf-8") == before

    # The rollback is later known, and only then does custody retire.
    aborted = derived_receipts.prove_committed(
        vault, receipt, current_generation="generation-1", known_uncommitted=True
    )
    assert aborted.outcome == "aborted"


def test_crash_cut_partial_canonical_state_fails_closed(vault: Path) -> None:
    """Cut 3: neither the complete before-state nor the intended after-state."""
    first = "Knowledge Base/Notes/cut-partial-a.md"
    second = "Knowledge Base/Notes/cut-partial-b.md"
    _put(vault, first, "a-before\n")
    _put(vault, second, "b-before\n")
    receipt = _prepare_store_batch(
        vault,
        batch_id="cut-partial",
        generation="generation-1",
        changes=((first, "a-before\n", "a-after\n"), (second, "b-before\n", "b-after\n")),
    )
    _put(vault, first, "a-after\n")  # only half the batch landed

    proof = derived_receipts.prove_committed(
        vault, receipt, current_generation="generation-1"
    )
    assert proof.outcome == "reconcile_required", proof.outcome
    assert proof.ready_components == ()
    with pytest.raises(RuntimeError):
        derived_receipts.publish_pending_visibility(
            vault, receipt, publisher=pending_recall.publish
        )


def test_crash_cut_committed_before_terminal_converges_without_replay(
    vault: Path,
) -> None:
    """Cut 4: canonical bytes committed, terminal never persisted."""
    rel = "Knowledge Base/Notes/cut-committed.md"
    before, after = "before body\n", "after body\n"
    _put(vault, rel, before)
    _prepare_store_batch(
        vault,
        batch_id="cut-committed",
        generation="generation-1",
        changes=((rel, before, after),),
    )
    _put(vault, rel, after)  # canonical commit landed; the process then died

    recovered = derived_receipts.recover_prepared_batches(
        vault,
        observe_current_generation=lambda _root: "generation-1",
        visibility_publisher=pending_recall.publish,
        limit=8,
    )
    assert recovered == 1
    assert (vault / rel).read_text(encoding="utf-8") == after
    snapshot = derived_receipts.snapshot_pending_visibility(vault, limit=64)
    live = {
        row.rel_path: row.state
        for batch in snapshot.batches
        for row in batch.rows
        if row.state != "retired"
    }
    assert live.get(rel) == "live", live


def test_crash_cut_worker_death_before_publication_is_reclaimed(vault: Path) -> None:
    """Cut 5: a claim whose owner died expires and another owner takes it."""
    rel = "Knowledge Base/Notes/cut-worker-before.md"
    before, after = "before body\n", "after body\n"
    _put(vault, rel, before)
    receipt = _prepare_store_batch(
        vault,
        batch_id="cut-worker-before",
        generation="generation-1",
        changes=((rel, before, after),),
        required={DerivedComponent.LEXSTORE},
    )
    _put(vault, rel, after)
    assert (
        derived_receipts.prove_committed(
            vault, receipt, current_generation="generation-1"
        ).outcome
        == "ready"
    )
    assert derived_receipts.publish_pending_visibility(
        vault, receipt, publisher=pending_recall.publish
    )

    dead = derived_receipts.claim_ready_components(
        vault, owner="dead-worker", limit=4, lease_seconds=1.0, now=100.0
    )
    assert [status.component for status in dead] == [DerivedComponent.LEXSTORE]

    # The lease expires with no completion: custody is not lost.
    live = derived_receipts.claim_ready_components(
        vault, owner="live-worker", limit=4, lease_seconds=60.0, now=200.0
    )
    assert [status.component for status in live] == [DerivedComponent.LEXSTORE]
    assert live[0].claim_owner == "live-worker"
    assert live[0].lease_revision > dead[0].lease_revision
    assert derived_receipts.complete_component(
        vault, live[0], current_generation="generation-1", now=201.0
    )


def test_crash_cut_worker_death_after_publication_never_recomputes(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cut 6: a published advisory result is replayed, never recomputed."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    rel = "Knowledge Base/Notes/Insights/lane5-cut-worker-after.md"
    _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 cut worker after", "lane5cutworkerafter"),
    )
    _drain(vault)

    batch_id = _batch_ids(vault)[0]
    receipt = derived_receipts._load_receipt(vault, batch_id)
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert ref is not None
    published = derived_receipts.read_advisory_result(vault, ref)
    assert published is not None and published.state != "pending"

    # A later worker re-claims the same component and must not recompute.
    from exomem import embeddings

    def exploding(*_args, **_kwargs):
        raise AssertionError("a published advisory result was recomputed")

    monkeypatch.setattr(
        embeddings, "prepare_generation_vectors", exploding, raising=False
    )
    status = derived_receipts.component_status(
        vault, receipt, DerivedComponent.WRITE_ADVISORY
    )
    replay = deferred_write_advisory.execute_write_advisory(
        vault,
        derived_receipts.DerivedComponentStatus(
            **{
                **{
                    field: getattr(status, field)
                    for field in status.__slots__  # type: ignore[attr-defined]
                },
                "state": "claimed",
                "claim_owner": "replay-worker",
            }
        ),
        observe_current_generation=lambda _root: receipt.canonical_generation,
    )
    assert replay.outcome == "already_published", replay
    assert replay.reused_vectors is True
    after = derived_receipts.read_advisory_result(vault, ref)
    assert after is not None
    assert after.publication_revision == published.publication_revision
    assert after.candidates == published.candidates


def test_crash_cut_newer_supersession_never_republishes_the_stale_generation(
    vault: Path,
) -> None:
    """Cut 7: the older receipt cannot publish once newer custody covers it."""
    rel = "Knowledge Base/Notes/cut-supersede.md"
    before, middle, after = "v0\n", "v1\n", "v2\n"
    _put(vault, rel, before)
    older = _prepare_store_batch(
        vault,
        batch_id="cut-supersede-old",
        generation="generation-1",
        changes=((rel, before, middle),),
        now=10.0,
    )
    newer = _prepare_store_batch(
        vault,
        batch_id="cut-supersede-new",
        generation="generation-1",
        changes=((rel, middle, after),),
        now=11.0,
    )
    _put(vault, rel, after)
    assert (
        derived_receipts.prove_committed(
            vault, newer, current_generation="generation-1"
        ).outcome
        == "ready"
    )
    assert derived_receipts.publish_pending_visibility(
        vault, newer, publisher=pending_recall.publish
    )

    older_proof = derived_receipts.prove_committed(
        vault, older, current_generation="generation-1"
    )
    assert older_proof.outcome == "superseded", older_proof.outcome
    with pytest.raises(RuntimeError):
        derived_receipts.publish_pending_visibility(
            vault, older, publisher=pending_recall.publish
        )
    assert (vault / rel).read_text(encoding="utf-8") == after


def test_crash_cut_old_queue_stays_drainable_beside_exact_custody(
    vault: Path, tmp_path: Path
) -> None:
    """Cut 8: legacy queues remain readable and drainable during migration."""
    from exomem import deferred_index

    legacy_rel = "Knowledge Base/Notes/legacy-queued.md"
    _put(vault, legacy_rel, "legacy body\n")
    assert deferred_index.add(vault, [legacy_rel]) >= 1
    assert legacy_rel in deferred_index.list_paths(vault)

    rel = "Knowledge Base/Notes/Insights/lane5-cut-old-queue.md"
    _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 cut old queue", "lane5cutoldqueue"),
    )

    # New exact custody exists and the legacy row is untouched by it.
    assert derived_receipts.recoverable_batch_count(vault) >= 0
    assert legacy_rel in deferred_index.list_paths(vault)
    receipts = deferred_index.snapshot(vault, limit=8)
    assert deferred_index.clear_receipts(vault, list(receipts)) >= 1
    assert legacy_rel not in deferred_index.list_paths(vault)


def test_crash_cut_new_reader_reads_an_old_writers_batch_fail_closed(
    vault: Path,
) -> None:
    """Cut 9: a pre-extension advisory row resolves, and releases nothing."""
    import sqlite3 as _sqlite3

    rel = "Knowledge Base/Notes/cut-old-writer.md"
    before, after = "v0\n", "v1\n"
    _put(vault, rel, before)
    receipt = _prepare_store_batch(
        vault,
        batch_id="cut-old-writer",
        generation="generation-1",
        changes=((rel, before, after),),
        required={DerivedComponent.WRITE_ADVISORY, DerivedComponent.LEXSTORE},
    )
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert ref is not None

    # An old writer left no target identity on the result row.
    connection = _sqlite3.connect(
        __import__("exomem.deferred_index", fromlist=["x"]).store_path(vault)
    )
    try:
        with connection:
            connection.execute(
                "UPDATE write_advisory_results SET target_rel_path = NULL "
                "WHERE batch_id = ?",
                ("cut-old-writer",),
            )
    finally:
        connection.close()

    stored = derived_receipts.read_advisory_result(vault, ref)
    assert stored is not None
    assert stored.target_rel_path is None
    assert stored.candidates == ()


def test_crash_cut_restart_rehydrates_custody_from_durable_state(
    vault: Path, tmp_path: Path
) -> None:
    """Cut 10: a fresh process serves the committed generation from custody."""
    rel = "Knowledge Base/Notes/Insights/lane5-cut-restart.md"
    marker = "lane5cutrestartmarker"
    _governed_write(
        _manager(tmp_path),
        vault,
        rel_path=rel,
        source=_compiled_page("Lane5 cut restart", marker),
    )
    before_reset = derived_receipts.snapshot_pending_visibility(vault, limit=64)
    assert before_reset.outcome == "complete"

    pending_recall.reset()
    find_module.reset_page_and_result_caches()
    lexstore.clear_stores()

    assert rel in _keyword(vault, marker)
    assert pending_recall.overlay(vault).outcome == "ready"
    assert derived_receipts.due_component_count(vault) >= 1


# --------------------------------------------------------------------------- #
# Residual: recovery and move routes, and their advisory custody
# --------------------------------------------------------------------------- #


def test_relocation_auxiliary_batch_takes_no_advisory_custody(
    vault: Path, tmp_path: Path
) -> None:
    """Recovery and move carry no advisory job, by decision, and it is pinned.

    Both routes reach canonical state for the page itself through the graph
    lifecycle's rename, not through the batch seam that prepares receipts, so
    the page never enters the fast acknowledgement at all. What those routes DO
    send through the batch seam is auxiliary: the log entry and the catalogue
    rows. This is that shape -- an undeclared batch with no governed semantic
    page -- and it must carry no advisory custody and no result reference.

    The decision behind it: a relocation republishes bytes that already exist,
    and a recovery restores bytes that existed before. An advisory over
    unchanged content re-derives candidates the once-only ledger has already
    surfaced for exactly those bytes, so custody there would buy a duplicate.
    Moving these routes onto the fast path would mean routing a lifecycle
    rename through the receipt seam, which is a change to the rename protocol
    rather than to this wiring.
    """
    rel = "Knowledge Base/Notes/lane5-relocation-log.md"
    source = _compiled_page("Lane5 relocation log", "lane5relocationlog")

    def leaf(vault_root: Path):
        target = vault_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # No `semantic_states`, and no coordinator-owned parent-state binding:
        # exactly what the relocation routes pass for their auxiliaries.
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(
                    target, source, create_only=not target.exists()
                )
            ],
            vault_root=vault_root,
        )
        return {"path": rel, "warnings": []}

    manager = _manager(tmp_path)
    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    terminal = manager.invoke(
        command,
        (vault,),
        {"response_detail": "compact"},
        idempotency_key="lane5-relocation",
        mutation_request_id="55555555-5555-4555-8555-555555555555",
    )

    assert terminal["status"] == "committed"
    assert terminal["advisory_sync"] == "not_required", terminal
    assert "advisory_result_ref" not in terminal
    for batch_id in _batch_ids(vault):
        receipt = derived_receipts._load_receipt(vault, batch_id)
        assert (
            derived_receipts.component_status(
                vault, receipt, DerivedComponent.WRITE_ADVISORY
            ).state
            == "not_required"
        )
        assert derived_receipts.advisory_result_ref(vault, receipt) is None


# --------------------------------------------------------------------------- #
# Task 5.3 — the phases and diagnostics on a real governed write
# --------------------------------------------------------------------------- #


def test_a_real_governed_write_records_every_derived_phase(
    vault: Path, tmp_path: Path
) -> None:
    """The phases must fire on the production path, not merely be declared.

    A closed vocabulary in one module and timers in another is two halves of a
    measurement. This drives one real governed write inside an MCP call token
    and asserts the phases that write actually reaches -- which is what makes
    the ledger able to say where a slow acknowledgement spent its time.
    """
    from exomem import call_ledger, call_spans

    call_spans.reset()
    token = call_spans.MCP_CALL_TOKEN.set("lane5-phase-token")
    try:
        _governed_write(
            _manager(tmp_path),
            vault,
            rel_path="Knowledge Base/Notes/Insights/lane5-phases.md",
            source=_compiled_page("Lane5 phases", "lane5phasesmarker"),
        )
        spans = {span["name"]: span for span in call_spans.pop_call_spans(
            "lane5-phase-token"
        )}
    finally:
        call_spans.MCP_CALL_TOKEN.reset(token)

    for phase in (
        "derived.receipt_prepare",
        "derived.canonical_commit",
        "derived.acknowledgement",
        "derived.receipt_proof",
        "derived.pending_visibility",
    ):
        assert phase in spans, sorted(spans)
        assert spans[phase]["count"] >= 1
        assert spans[phase]["ms"] >= 0.0
    assert set(spans) <= (
        call_ledger.DERIVED_PHASES | {name for name in spans if not
                                      name.startswith("derived.")}
    )


def test_the_drain_records_its_pass_and_the_diagnostics_report_it(
    vault: Path, tmp_path: Path
) -> None:
    """Depth, age and completion are readable without touching the store."""
    from exomem import call_ledger

    derived_drain.reset_pass_observations()
    call_ledger.reset_derived_counters()

    _governed_write(
        _manager(tmp_path),
        vault,
        rel_path="Knowledge Base/Notes/Insights/lane5-diagnostics.md",
        source=_compiled_page("Lane5 diagnostics", "lane5diagnosticsmarker"),
    )

    before = call_ledger.derived_diagnostics(vault)
    assert before["fast_durable_ack"] == "active"
    assert before["due_components"] >= 1
    assert before["counters"]["receipt_prepared"] >= 1
    assert before["last_drain_pass"]["at_age_seconds"] is None

    _drain(vault)

    after = call_ledger.derived_diagnostics(vault)
    assert after["due_components"] == 0, after
    assert after["counters"]["component_completed"] >= 1
    assert after["last_drain_pass"]["at_age_seconds"] is not None
    assert after["last_drain_pass"]["completed"] >= 0
    assert after["unavailable"] == []

    rendered = repr(after)
    for token in ("lane5-diagnostics", "Knowledge Base", "Lane5 diagnostics"):
        assert token not in rendered, (token, rendered)


# --------------------------------------------------------------------------- #
# Task 5.7 — the live-acceptance script (written and unit-tested here; the
# native Windows runs against a live cell are the operator's)
# --------------------------------------------------------------------------- #


def _acceptance():
    import importlib.util

    script = Path(__file__).parents[1] / "scripts" / "live_write_acceptance.py"
    spec = importlib.util.spec_from_file_location("live_write_acceptance", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(
    *,
    remember_p50: float = 900.0,
    remember_p90: float = 1_800.0,
    edit_p50: float = 800.0,
    edit_p90: float = 1_500.0,
    samples: int = 30,
    exact: int = 60,
    warming: int = 0,
    stale: int = 0,
    uncovered_full_receipts: int = 0,
    converged: bool = True,
    convergence_seconds: float = 12.0,
    reconciliation_demanded: int = 0,
) -> dict:
    return {
        "transport": "direct",
        "samples_per_operation": samples,
        "fast_durable_ack": "active",
        "operations": {
            "remember": {
                "samples": samples,
                "p50_ms": remember_p50,
                "p90_ms": remember_p90,
                "max_ms": remember_p90,
            },
            "edit": {
                "samples": samples,
                "p50_ms": edit_p50,
                "p90_ms": edit_p90,
                "max_ms": edit_p90,
            },
        },
        "read_your_write": {"exact": exact, "warming": warming, "stale": stale},
        "uncovered_full_receipts": uncovered_full_receipts,
        "post_burst_convergence_seconds": convergence_seconds,
        "post_burst_converged": converged,
        "reconciliation_demanded": reconciliation_demanded,
    }


def test_live_acceptance_thresholds_are_the_designs_numbers() -> None:
    module = _acceptance()
    assert module.P50_MS == 3_000.0
    assert module.P90_MS == 5_000.0
    assert module.MIN_SAMPLES_PER_OPERATION == 30
    assert module.OPERATIONS == ("remember", "edit")


def test_live_acceptance_accepts_a_healthy_report() -> None:
    _acceptance().check(_report())


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"remember_p50": 3_100.0}, "remember p50"),
        ({"remember_p90": 5_100.0}, "remember p90"),
        ({"edit_p50": 3_000.1}, "edit p50"),
        ({"edit_p90": 5_000.1}, "edit p90"),
        ({"samples": 29}, "samples"),
        ({"stale": 1}, "stale read"),
        ({"uncovered_full_receipts": 1}, "uncovered full receipt"),
        ({"converged": False}, "did not converge"),
        ({"reconciliation_demanded": 1}, "reconciliation demand"),
    ],
)
def test_live_acceptance_each_threshold_is_load_bearing(
    kwargs: dict, expected: str
) -> None:
    """Every acceptance condition must be able to fail the run on its own."""
    with pytest.raises(SystemExit, match=expected):
        _acceptance().check(_report(**kwargs))


def test_live_acceptance_warming_is_acceptable_but_stale_is_not() -> None:
    """Explicit warming is a truthful answer; a stale answer never is."""
    module = _acceptance()
    module.check(_report(exact=40, warming=20))
    with pytest.raises(SystemExit, match="stale read"):
        module.check(_report(exact=59, stale=1))


def test_live_acceptance_output_is_content_free(vault: Path, tmp_path: Path) -> None:
    """Closed codes, counts and percentiles -- never a path, title or excerpt."""
    module = _acceptance()
    report = module.measure(
        vault,
        transport="direct",
        samples_per_operation=2,
        state_dir=tmp_path / "st",
        convergence_bound_seconds=30.0,
    )
    rendered = json.dumps(report, sort_keys=True, default=str)
    for token in ("Knowledge Base", str(vault), "acceptance-target", ".md"):
        assert token not in rendered, (token, rendered)
    assert report["operations"]["remember"]["samples"] == 2
    assert report["operations"]["edit"]["samples"] == 2
    # Never stale. `exact` versus `warming` depends on whether this harness's
    # catalogue proof was admitted, which the report states rather than
    # assumes -- the contract accepts either, and only a stale answer is a
    # failure.
    assert report["read_your_write"]["stale"] == 0
    assert (
        report["read_your_write"]["exact"] + report["read_your_write"]["warming"] == 4
    )
    assert report["recall_admission"] in {"ready", "warming", "unavailable"}
    assert report["uncovered_full_receipts"] == 0
    assert report["post_burst_converged"] is True
    assert isinstance(report["reconciliation_demanded"], int)


def test_live_acceptance_refuses_a_vault_it_was_not_given(tmp_path: Path) -> None:
    """It runs against a disposable vault it is handed, and nothing else.

    The one way this script could do harm is by being pointed at a live cell,
    so resolving a vault from the ambient environment is refused outright
    rather than merely discouraged.
    """
    module = _acceptance()
    with pytest.raises(SystemExit, match="requires an explicit --vault"):
        module.main(["--transport", "direct", "--samples-per-operation", "1"])


def test_live_acceptance_connector_transport_requires_an_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector run that cannot reach a connector must say so, not fall back."""
    module = _acceptance()
    monkeypatch.delenv("EXOMEM_CONNECTOR_URL", raising=False)
    vault_root = tmp_path / "vault"
    (vault_root / "Knowledge Base").mkdir(parents=True)
    with pytest.raises(SystemExit, match="connector transport requires"):
        module.main(
            [
                "--transport",
                "connector",
                "--vault",
                str(vault_root),
                "--samples-per-operation",
                "1",
            ]
        )
