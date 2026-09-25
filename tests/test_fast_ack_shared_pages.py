"""Fast-acknowledged batches that share a page all converge.

Every `remember` rewrites the knowledge base's `log.md` and `index.md`, and a
note that cites a source rewrites that source's `ingested_into` list. Each of
those pages rides in the writing batch's receipt. When a newer write rewrites a
shared page before an older batch has run all of its components, the older
batch's shared path has moved past the after-state it recorded. Before the
owner's ruling of 2026-09-25 (option A) the store then held the whole batch in
`reconcile_required` for good: its own page never reached the recall lanes,
and its live pending row for the shared page became unprovable, which turned
every managed recall in the vault into a warming answer.

The rule now is per path. A path whose bytes moved on counts as proven when a
newer exact batch carries it with live or retired pending custody, or (R2) when
both recall lanes already hold its current bytes; the older batch still
converges the paths it owns. An out-of-band move that nothing covers or holds
stays `reconcile_required`.

Every node drives the production drain explicitly at a fixed clock.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    deferred_index,
    derived_drain,
    derived_receipts,
    index_sync,
    lexstore,
    pending_recall,
    writer_lease,
)
from exomem import find as find_module
from exomem import note as note_module
from exomem.derived_receipts import DerivedComponent

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

_CITED_SOURCE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements"
_SETTLED_BATCH = {"completed", "superseded"}


@pytest.fixture(autouse=True)
def _fast_ack_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    pending_recall.reset()
    find_module.clear_cache()


@pytest.fixture
def live_catalogue(vault: Path) -> Path:
    """A built lexical catalogue, as a running server has before any write."""
    assert lexstore.get_store(vault).rebuild_atomic() is True
    return vault


def _remember(tmp_path: Path, root: Path, title: str, *, sources=None) -> dict:
    """One default `remember` through the real lease, as an MCP call routes it."""

    def leaf(vault_root: Path, **_surface_kwargs):
        result = note_module.note(
            vault_root,
            content=f"# {title}\n\n{title} shared page probe body.",
            note_type="insight",
            title=title,
            status="draft",
            sources=sources,
        )
        return {"path": result.path, "warnings": list(result.warnings)}

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )
    terminal = manager.invoke(
        SimpleNamespace(name="remember", leaf=leaf, read_only=False),
        (root,),
        {"response_detail": "compact"},
        idempotency_key=None,
        mutation_request_id=str(uuid.uuid4()),
    )
    assert terminal["status"] == "committed", terminal
    return terminal


def _one_pass(root: Path) -> int:
    """Exactly one production scheduler pass at the current clock."""
    return derived_drain.drain_once(
        root,
        dispatch=derived_drain.component_dispatcher(),
        observe_current_generation=derived_drain.canonical_generation_observer(),
        visibility_publisher=pending_recall.publish,
        retire_visibility=derived_drain.pending_visibility_retirer(),
        limit=derived_drain.progress_limit(mode_name="normal"),
        now=time.time(),
    )


def _drain_until_idle(root: Path, *, passes: int = 6) -> None:
    """Pass until nothing is due, recoverable or stranded, within a fixed bound.

    Stranded batches keep the service drain's cadence too: recovery re-proves
    them every pass.
    """
    for _ in range(passes):
        if not (
            derived_receipts.due_component_count(root, now=time.time() + 3600)
            or derived_receipts.recoverable_batch_count(root)
            or derived_receipts.stranded_batch_count(root)
        ):
            return
        _one_pass(root)


def _batches(root: Path) -> list[tuple[str, str, tuple[str, ...]]]:
    connection = sqlite3.connect(deferred_index.store_path(root))
    try:
        rows = connection.execute(
            "SELECT batch_id, state FROM derived_batches ORDER BY rowid"
        ).fetchall()
        return [
            (
                str(batch_id),
                str(state),
                tuple(
                    str(path)
                    for (path,) in connection.execute(
                        "SELECT rel_path FROM derived_batch_paths WHERE batch_id = ? "
                        "ORDER BY rel_path",
                        (batch_id,),
                    )
                ),
            )
            for batch_id, state in rows
        ]
    finally:
        connection.close()


def _row_states(root: Path, batch_id: str) -> dict[str, str]:
    connection = sqlite3.connect(deferred_index.store_path(root))
    try:
        return {
            str(rel): str(state)
            for rel, state in connection.execute(
                "SELECT rel_path, state FROM pending_recall_rows WHERE batch_id = ?",
                (batch_id,),
            )
        }
    finally:
        connection.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_converged(root: Path, pages: list[str]) -> None:
    batches = _batches(root)
    assert [state for _id, state, _paths in batches if state not in _SETTLED_BATCH] == [], (
        batches
    )
    overlay = pending_recall.overlay(root)
    assert overlay.outcome == "ready", overlay.failure_code
    assert not overlay.rows, sorted(overlay.rows)
    held = lexstore.holds_content_identities(
        root, {rel: _digest(root / rel) for rel in pages}
    )
    assert all(held.get(rel) for rel in pages), held


def test_distinct_pages_sharing_navigation_pages_all_converge(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """Three back-to-back remembers share `log.md` and `index.md`; all converge.

    The first two batches' navigation paths have moved on by the time the
    drain re-proves them. Each is covered by the next batch's live custody, so
    each batch still converges its own page.
    """
    vault = live_catalogue
    pages = [
        _remember(tmp_path, vault, f"Shared navigation probe {name}")["path"]
        for name in ("alpha", "bravo", "charlie")
    ]
    batches = _batches(vault)
    assert len(batches) == 3
    shared = set(batches[0][2]) & set(batches[1][2]) & set(batches[2][2])
    assert "Knowledge Base/log.md" in shared, batches

    _drain_until_idle(vault)

    _assert_converged(vault, pages)


def test_notes_citing_one_source_converge(live_catalogue: Path, tmp_path: Path) -> None:
    """A shared governed page that is not navigation converges the same way."""
    vault = live_catalogue
    source_rel = f"{_CITED_SOURCE}.md"
    pages = [
        _remember(
            tmp_path, vault, f"Shared source probe {name}", sources=[_CITED_SOURCE]
        )["path"]
        for name in ("delta", "echo")
    ]
    batches = _batches(vault)
    assert all(source_rel in paths for _id, _state, paths in batches), batches

    _drain_until_idle(vault)

    _assert_converged(vault, [*pages, source_rel])


def test_a_coverer_that_is_not_yet_proven_heals_on_the_next_pass(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A moved path whose newer batch is committed but unproven waits one pass.

    The re-proof cannot yet see the newer batch's custody, so the older batch
    is held in `reconcile_required`; the next pass's recovery proves the newer
    batch first and then re-proves the older one, which is covered by then.
    """
    vault = live_catalogue
    page = _remember(tmp_path, vault, "Unproven coverer probe")["path"]
    older = derived_receipts._load_receipt(vault, _batches(vault)[0][0])

    # A newer committed batch rewrites `log.md` and has not been proven yet, as
    # when its writer is still between the canonical commit and its ack.
    log_rel = "Knowledge Base/log.md"
    log_path = vault / log_rel
    before = log_path.read_bytes()
    after = before + b"\n- newer committed entry, not yet proven\n"
    derived_receipts.prepare_batch(
        vault,
        batch_id="unproven-coverer",
        mutation_attempt_digest=hashlib.sha256(b"unproven-coverer").hexdigest(),
        canonical_generation=older.canonical_generation,
        checkpoint_id=older.checkpoint_id,
        paths=(
            derived_receipts.DerivedBatchPath(
                rel_path=log_rel,
                before_hash=hashlib.sha256(before).hexdigest(),
                after_hash=hashlib.sha256(after).hexdigest(),
            ),
        ),
        required_components=frozenset({DerivedComponent.LEXSTORE}),
    )
    log_path.write_bytes(after)

    held = derived_receipts.prove_committed(
        vault, older, current_generation=older.canonical_generation
    )
    assert held.outcome == "reconcile_required"

    # The newer batch's own acknowledgement proves and publishes it ...
    newer = derived_receipts._load_receipt(vault, "unproven-coverer")
    assert derived_receipts.prove_committed(
        vault, newer, current_generation=newer.canonical_generation
    ).outcome == "ready"
    assert derived_receipts.publish_pending_visibility(
        vault, newer, publisher=pending_recall.publish
    )
    # ... after which the older batch proves again: it keeps its own page and
    # hands `log.md` on, retiring the row that could never prove again.
    healed = derived_receipts.prove_committed(
        vault, older, current_generation=older.canonical_generation
    )
    assert healed.outcome == "ready"
    assert _row_states(vault, older.batch_id)[log_rel] == "retired"
    assert _row_states(vault, older.batch_id)[page] == "live"

    _drain_until_idle(vault)

    _assert_converged(vault, [page, log_rel])


def test_a_crash_cut_batch_whose_shared_page_moved_on_recovers(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """Recovery publishes only the paths a batch still owns.

    The older batch committed but its writer died before the acknowledgement,
    so its pending rows were never published. A newer write then moved the
    shared `log.md` on. Recovery proves the older batch per path; publishing
    the moved path would fail the publisher's exact proof and strand the batch.
    """
    vault = live_catalogue
    page_rel = "Knowledge Base/Notes/Insights/crash-cut-shared-probe.md"
    log_rel = "Knowledge Base/log.md"
    page = vault / page_rel
    log_path = vault / log_rel
    page_bytes = (
        b"---\ntitle: Crash cut shared probe\ntype: insight\nstatus: draft\n"
        b"updated: 2026-09-25\n---\n\n# Crash cut shared probe\n\nBody.\n"
    )
    log_before = log_path.read_bytes()
    log_after = log_before + b"\n- crash cut entry\n"
    derived_receipts.prepare_batch(
        vault,
        batch_id="crash-cut-older",
        mutation_attempt_digest=hashlib.sha256(b"crash-cut-older").hexdigest(),
        canonical_generation="generation-crash-cut",
        checkpoint_id="checkpoint-crash-cut",
        paths=(
            derived_receipts.DerivedBatchPath(
                rel_path=log_rel,
                before_hash=hashlib.sha256(log_before).hexdigest(),
                after_hash=hashlib.sha256(log_after).hexdigest(),
            ),
            derived_receipts.DerivedBatchPath(
                rel_path=page_rel,
                before_hash=None,
                after_hash=hashlib.sha256(page_bytes).hexdigest(),
            ),
        ),
        required_components=frozenset({DerivedComponent.LEXSTORE}),
    )
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(page_bytes)
    log_path.write_bytes(log_after)

    newer_page = _remember(tmp_path, vault, "Crash cut newer probe")["path"]

    _drain_until_idle(vault)

    _assert_converged(vault, [page_rel, newer_page, log_rel])


def test_a_shared_page_a_newer_write_returned_to_its_old_bytes_is_handed_on(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A shared page can move on and land back on an older batch's before-bytes.

    Two edits each re-render the knowledge base index; the second renders it
    exactly as it was before the first. The first batch then sees the index in
    its before-state -- not a torn write of its own, but a newer batch's proven
    after-state, and that batch holds its custody. Found by the 3-writer burst.
    """
    vault = live_catalogue
    index_rel = "Knowledge Base/index.md"
    index = vault / index_rel
    first_rel = "Knowledge Base/Notes/Insights/returned-bytes-first.md"
    second_rel = "Knowledge Base/Notes/Insights/returned-bytes-second.md"

    def page(title: str) -> bytes:
        return (
            f"---\ntitle: {title}\ntype: insight\nstatus: draft\n"
            f"updated: 2026-09-25\n---\n\n# {title}\n\nBody.\n"
        ).encode()

    def committed(batch_id: str, writes: dict[str, tuple[bytes | None, bytes]]):
        receipt = derived_receipts.prepare_batch(
            vault,
            batch_id=batch_id,
            mutation_attempt_digest=hashlib.sha256(batch_id.encode()).hexdigest(),
            canonical_generation=f"generation-{batch_id}",
            checkpoint_id=f"checkpoint-{batch_id}",
            paths=tuple(
                derived_receipts.DerivedBatchPath(
                    rel_path=rel,
                    before_hash=None if old is None else hashlib.sha256(old).hexdigest(),
                    after_hash=hashlib.sha256(new).hexdigest(),
                )
                for rel, (old, new) in sorted(writes.items())
            ),
            required_components=frozenset({DerivedComponent.LEXSTORE}),
        )
        for rel, (_old, new) in writes.items():
            target = vault / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new)
        assert derived_receipts.prove_committed(
            vault, receipt, current_generation=receipt.canonical_generation
        ).outcome == "ready"
        assert derived_receipts.publish_pending_visibility(
            vault, receipt, publisher=pending_recall.publish
        )
        return receipt

    # A real write first, so the vault has the canonical generation the drain
    # observes before it dispatches a component.
    seed = _remember(tmp_path, vault, "Returned bytes seed")["path"]
    original = index.read_bytes()
    rendered = original + b"\n- first edit's recent-activity line\n"
    first = committed(
        "returned-first",
        {first_rel: (None, page("Returned bytes first")), index_rel: (original, rendered)},
    )
    committed(
        "returned-second",
        {second_rel: (None, page("Returned bytes second")), index_rel: (rendered, original)},
    )

    proof = derived_receipts.prove_committed(
        vault, first, current_generation=first.canonical_generation
    )

    assert proof.outcome == "ready"
    assert _row_states(vault, first.batch_id)[index_rel] == "retired"
    _drain_until_idle(vault)
    _assert_converged(vault, [seed, first_rel, second_rel, index_rel])


def test_before_bytes_no_newer_batch_wrote_stay_owed(live_catalogue: Path) -> None:
    """Back at its before-bytes by a hand revert, a path is not handed on.

    A newer batch carries the page, but its recorded after-state is not what is
    on disk, so nothing proves the bytes belong to it.
    """
    vault = live_catalogue
    shared_rel = "Knowledge Base/index.md"
    shared = vault / shared_rel
    own_rel = "Knowledge Base/Notes/Insights/reverted-by-hand-own.md"
    x = shared.read_bytes()
    y = x + b"\n- older batch line\n"
    z = y + b"\n- newer batch line\n"
    own = b"---\ntitle: Reverted by hand own\ntype: insight\n---\n\n# Reverted\n"

    def prepared(batch_id: str, paths):
        return derived_receipts.prepare_batch(
            vault,
            batch_id=batch_id,
            mutation_attempt_digest=hashlib.sha256(batch_id.encode()).hexdigest(),
            canonical_generation=f"generation-{batch_id}",
            checkpoint_id=f"checkpoint-{batch_id}",
            paths=tuple(
                derived_receipts.DerivedBatchPath(
                    rel_path=rel,
                    before_hash=None if old is None else hashlib.sha256(old).hexdigest(),
                    after_hash=hashlib.sha256(new).hexdigest(),
                )
                for rel, old, new in paths
            ),
            required_components=frozenset({DerivedComponent.LEXSTORE}),
        )

    older = prepared("reverted-older", [(shared_rel, x, y), (own_rel, None, own)])
    (vault / own_rel).parent.mkdir(parents=True, exist_ok=True)
    (vault / own_rel).write_bytes(own)
    shared.write_bytes(y)
    # Proven committed at its own acknowledgement, as a fast-acked write is.
    assert derived_receipts.prove_committed(
        vault, older, current_generation=older.canonical_generation
    ).outcome == "ready"
    assert derived_receipts.publish_pending_visibility(
        vault, older, publisher=pending_recall.publish
    )
    newer = prepared("reverted-newer", [(shared_rel, y, z)])
    shared.write_bytes(z)
    assert derived_receipts.prove_committed(
        vault, newer, current_generation=newer.canonical_generation
    ).outcome == "ready"
    assert derived_receipts.publish_pending_visibility(
        vault, newer, publisher=pending_recall.publish
    )
    shared.write_bytes(x)
    # Both recall lanes hold the reverted bytes: that is not enough for bytes
    # the batch did not create, which could be its own torn write.
    index_sync.upsert_after_write(vault, [shared], publish_corpus_change=True)
    assert pending_recall.recall_lanes_hold(vault, {shared_rel: _digest(shared)})

    assert derived_receipts.prove_committed(
        vault, older, current_generation=older.canonical_generation
    ).outcome == "reconcile_required"
    from exomem import doctor

    assert doctor._check_fast_ack_custody(vault).status == "fail"


def _proven_batch(vault: Path, batch_id: str, writes) -> object:
    """One batch committed, proven and published: its rows are live custody."""
    receipt = derived_receipts.prepare_batch(
        vault,
        batch_id=batch_id,
        mutation_attempt_digest=hashlib.sha256(batch_id.encode()).hexdigest(),
        canonical_generation=f"generation-{batch_id}",
        checkpoint_id=f"checkpoint-{batch_id}",
        paths=tuple(
            derived_receipts.DerivedBatchPath(
                rel_path=rel,
                before_hash=None if old is None else hashlib.sha256(old).hexdigest(),
                after_hash=hashlib.sha256(new).hexdigest(),
            )
            for rel, old, new in writes
        ),
        required_components=frozenset({DerivedComponent.LEXSTORE}),
    )
    for rel, _old, new in writes:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new)
    assert derived_receipts.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    ).outcome == "ready"
    assert derived_receipts.publish_pending_visibility(
        vault, receipt, publisher=pending_recall.publish
    )
    return receipt


def test_an_older_batch_never_covers_a_newer_batch_s_moved_path(
    live_catalogue: Path,
) -> None:
    """Coverage runs forward only: custody an older batch held is not newer custody.

    Both batches carry the shared index; a hand edit then moves it past the
    newer batch's after-state, and neither lane holds the edit. Only a later
    batch could own those bytes, so the newer batch stays owed.
    """
    vault = live_catalogue
    shared_rel = "Knowledge Base/index.md"
    shared = vault / shared_rel
    x = shared.read_bytes()
    y = x + b"\n- older batch line\n"
    z = y + b"\n- newer batch line\n"
    _proven_batch(vault, "forward-older", [(shared_rel, x, y)])
    newer = _proven_batch(vault, "forward-newer", [(shared_rel, y, z)])
    shared.write_bytes(z + b"\n- edited by hand, never indexed\n")

    assert derived_receipts.prove_committed(
        vault, newer, current_generation=newer.canonical_generation
    ).outcome == "reconcile_required"


def test_an_older_batch_s_after_bytes_never_hand_on_a_newer_batch_s_revert(
    live_catalogue: Path,
) -> None:
    """Returned bytes an older batch wrote are not a newer batch's handover.

    The newer batch's path is reverted by hand to its before-bytes, which are
    exactly the older batch's recorded after-state. Only a later batch's
    after-state proves such bytes are not the newer batch's torn write.
    """
    vault = live_catalogue
    shared_rel = "Knowledge Base/index.md"
    shared = vault / shared_rel
    x = shared.read_bytes()
    y = x + b"\n- older batch line\n"
    z = y + b"\n- newer batch line\n"
    _proven_batch(vault, "revert-older", [(shared_rel, x, y)])
    newer = _proven_batch(vault, "revert-newer", [(shared_rel, y, z)])
    shared.write_bytes(y)

    assert derived_receipts.prove_committed(
        vault, newer, current_generation=newer.canonical_generation
    ).outcome == "reconcile_required"


def test_an_out_of_band_move_heals_once_the_recall_lanes_hold_it(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A hand edit no receipt covers is held until both recall lanes hold it.

    Owner ruling R2: the owner edits pages by hand, so an out-of-band move is
    ordinary. While neither lane holds the edited bytes the batch stays in
    `reconcile_required`; once they do -- as the watcher or a reconcile makes
    them -- recovery hands the path on and the batch converges the rest.
    """
    vault = live_catalogue
    page = _remember(tmp_path, vault, "Out of band probe")["path"]
    target = vault / page
    target.write_bytes(target.read_bytes() + b"\nEdited by hand in the editor.\n")

    _drain_until_idle(vault)

    assert [state for _id, state, _paths in _batches(vault)] == ["reconcile_required"]
    assert pending_recall.overlay(vault).outcome == "warming"

    index_sync.upsert_after_write(vault, [target], publish_corpus_change=True)
    _drain_until_idle(vault)

    _assert_converged(vault, [page])


def test_a_new_page_deleted_by_hand_before_it_converges_heals_without_reconcile(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A page the batch created, then deleted by hand, is back at its before-state.

    Absence is the page's before-state, which on its own could be the batch's
    own torn write. The batch was proven committed at its acknowledgement, so
    it is not: once both recall lanes hold the absence -- the watcher's removal
    fan-out makes them -- the path is handed on, the batch completes, and
    managed recall stays ready with no operator step.
    """
    from exomem import doctor, memory_refs

    vault = live_catalogue
    # Both recall lanes built, as a running server has them.
    memory_refs.ReferenceIndex(vault).rebuild_all()
    terminal = _remember(tmp_path, vault, "Deleted by hand probe")
    page = terminal["path"]
    (vault / page).unlink()
    index_sync.delete_after_remove(vault, [page])

    _drain_until_idle(vault)

    assert [state for _id, state, _paths in _batches(vault)] == ["completed"]
    overlay = pending_recall.overlay(vault)
    assert overlay.outcome == "ready", overlay.failure_code
    assert not overlay.rows, sorted(overlay.rows)
    assert doctor._check_fast_ack_custody(vault).status == "pass"
    # The advisory describes a page that no longer exists.
    assert _advisory_state(vault, terminal["advisory_result_ref"])[0] == "superseded"


def test_a_new_page_deleted_by_hand_waits_until_the_lanes_lose_it(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """While a lane still holds the deleted page, its absence is not yet visible."""
    vault = live_catalogue
    page = _remember(tmp_path, vault, "Deleted before removal probe")["path"]
    index_sync.upsert_after_write(vault, [vault / page], publish_corpus_change=True)
    (vault / page).unlink()

    _drain_until_idle(vault)

    assert [state for _id, state, _paths in _batches(vault)] == ["reconcile_required"]

    index_sync.delete_after_remove(vault, [page])
    _drain_until_idle(vault)

    assert [state for _id, state, _paths in _batches(vault)] == ["completed"]
    assert pending_recall.overlay(vault).outcome == "ready"


def test_a_crash_cut_batch_whose_new_page_is_absent_stays_owed(
    live_catalogue: Path,
) -> None:
    """Never proven committed, an absent new page may be the batch's own torn write."""
    vault = live_catalogue
    rel = "Knowledge Base/Notes/Insights/torn-new-page-probe.md"
    log_rel = "Knowledge Base/log.md"
    log_path = vault / log_rel
    log_before = log_path.read_bytes()
    log_after = log_before + b"\n- torn write entry\n"
    receipt = derived_receipts.prepare_batch(
        vault,
        batch_id="torn-new-page",
        mutation_attempt_digest=hashlib.sha256(b"torn-new-page").hexdigest(),
        canonical_generation="generation-torn",
        checkpoint_id="checkpoint-torn",
        paths=(
            derived_receipts.DerivedBatchPath(
                rel_path=log_rel,
                before_hash=hashlib.sha256(log_before).hexdigest(),
                after_hash=hashlib.sha256(log_after).hexdigest(),
            ),
            derived_receipts.DerivedBatchPath(
                rel_path=rel,
                before_hash=None,
                after_hash=hashlib.sha256(b"never written").hexdigest(),
            ),
        ),
        required_components=frozenset({DerivedComponent.LEXSTORE}),
    )
    log_path.write_bytes(log_after)

    assert derived_receipts.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    ).outcome == "reconcile_required"


def test_a_batch_whose_every_path_moved_out_of_band_retires(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """With nothing left to own, a batch the lanes cover is retired whole."""
    vault = live_catalogue
    rel = "Knowledge Base/Notes/Insights/hand-edited-only-page.md"
    target = vault / rel
    written = (
        b"---\ntitle: Hand edited only page\ntype: insight\nstatus: draft\n"
        b"updated: 2026-09-25\n---\n\n# Hand edited only page\n\nBody.\n"
    )
    receipt = derived_receipts.prepare_batch(
        vault,
        batch_id="hand-edited-only",
        mutation_attempt_digest=hashlib.sha256(b"hand-edited-only").hexdigest(),
        canonical_generation="generation-hand-edited",
        checkpoint_id="checkpoint-hand-edited",
        paths=(
            derived_receipts.DerivedBatchPath(
                rel_path=rel,
                before_hash=None,
                after_hash=hashlib.sha256(written).hexdigest(),
            ),
        ),
        required_components=frozenset({DerivedComponent.LEXSTORE}),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(written)
    assert derived_receipts.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    ).outcome == "ready"
    assert derived_receipts.publish_pending_visibility(
        vault, receipt, publisher=pending_recall.publish
    )
    target.write_bytes(written + b"\nEdited by hand.\n")
    assert derived_receipts.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    ).outcome == "reconcile_required"

    index_sync.upsert_after_write(vault, [target], publish_corpus_change=True)
    proof = derived_receipts.prove_committed(
        vault, receipt, current_generation=receipt.canonical_generation
    )

    assert proof.outcome == "superseded"
    assert _row_states(vault, receipt.batch_id) == {rel: "retired"}
    _assert_converged(vault, [rel])


# --------------------------------------------------------------------------- #
# Operator path (owner ruling R3): a stranded batch is visible and repairable
# --------------------------------------------------------------------------- #


def _strand_one(tmp_path: Path, vault: Path) -> tuple[str, Path]:
    """One batch held in `reconcile_required` by a hand edit the lanes lack."""
    page = _remember(tmp_path, vault, "Stranded operator probe")["path"]
    target = vault / page
    target.write_bytes(target.read_bytes() + b"\nEdited by hand, never indexed.\n")
    _drain_until_idle(vault)
    assert [state for _id, state, _paths in _batches(vault)] == ["reconcile_required"]
    return page, target


def test_a_stranded_batch_is_counted_apart_from_crash_cut_recovery(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A stranded batch is not crash-cut work that a drain pass will finish."""
    vault = live_catalogue
    _strand_one(tmp_path, vault)

    assert derived_receipts.stranded_batch_count(vault) == 1
    assert derived_receipts.recoverable_batch_count(vault) == 0
    census = derived_receipts.custody_census(vault)
    assert census["stranded_batches"] == 1
    assert census["recovering_batches"] == 0


def test_doctor_names_stranded_custody_in_one_content_free_line(
    live_catalogue: Path, tmp_path: Path
) -> None:
    from exomem import doctor

    vault = live_catalogue
    page, _target = _strand_one(tmp_path, vault)

    check = doctor._check_fast_ack_custody(vault)

    assert check.status == "fail"
    assert "stranded batches 1" in check.message
    assert "pending visibility warming(pending_visibility_unprovable)" in check.message
    assert "maintain --reconcile" in (check.remediation or "")
    rendered = f"{check.message} {check.remediation} {check.details}"
    assert page not in rendered and "Stranded operator probe" not in rendered


def test_reconcile_reconverges_a_stranded_batch_and_retires_it(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """`maintain --reconcile` converges the batch's pages from current bytes."""
    from exomem import doctor
    from exomem import reconcile as reconcile_module

    vault = live_catalogue
    page, _target = _strand_one(tmp_path, vault)

    preview = reconcile_module.reconcile(vault, dry_run=True)
    assert preview.as_dict()["derived_batch_reconcile"] == {
        "stranded": 1,
        "retired": 0,
        "remaining": 1,
    }
    assert derived_receipts.stranded_batch_count(vault) == 1

    report = reconcile_module.reconcile(vault)

    assert report.as_dict()["derived_batch_reconcile"] == {
        "stranded": 1,
        "retired": 1,
        "remaining": 0,
    }
    assert derived_receipts.stranded_batch_count(vault) == 0
    _assert_converged(vault, [page])
    assert doctor._check_fast_ack_custody(vault).status == "pass"


def test_reconcile_keeps_a_batch_stranded_while_the_lanes_lack_its_bytes(
    live_catalogue: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retirement waits for the lanes: a fan-out that proved nothing retires nothing."""
    vault = live_catalogue
    _strand_one(tmp_path, vault)
    monkeypatch.setattr(
        index_sync,
        "converge_paths_from_current_bytes",
        lambda _root, _paths, _created=(): True,
    )

    assert derived_receipts.reconcile_stranded_batches(
        vault, converge=index_sync.converge_paths_from_current_bytes
    ) == {"stranded": 1, "retired": 0, "remaining": 1}
    assert [state for _id, state, _paths in _batches(vault)] == ["reconcile_required"]


def _advisory_state(root: Path, ref: str) -> tuple[str, str | None]:
    stored = derived_receipts.read_advisory_result(root, ref)
    assert stored is not None
    return stored.state, stored.failure_code


def _force_advisory_state(root: Path, ref: str, state: str) -> None:
    """Stand in for a sweep that published (`ready`) or never ran (`pending`)."""
    result_id = derived_receipts._parse_advisory_ref(ref)
    connection = sqlite3.connect(deferred_index.store_path(root))
    try:
        connection.execute(
            "DELETE FROM write_advisory_result_candidates WHERE result_id = ?",
            (result_id,),
        )
        connection.execute(
            "UPDATE write_advisory_results SET state = ?, failure_code = NULL "
            "WHERE result_id = ?",
            (state, result_id),
        )
        connection.commit()
    finally:
        connection.close()


def _strand_by_shared_page(tmp_path: Path, vault: Path) -> tuple[str, str]:
    """A batch stranded by a hand edit of `log.md`; its own page is unchanged."""
    terminal = _remember(tmp_path, vault, "Advisory survives probe")
    ref = terminal.get("advisory_result_ref")
    assert terminal.get("advisory_sync") == "pending" and ref, terminal
    log_path = vault / "Knowledge Base/log.md"
    log_path.write_bytes(log_path.read_bytes() + b"\n- appended by hand\n")
    _drain_until_idle(vault)
    assert [state for _id, state, _paths in _batches(vault)] == ["reconcile_required"]
    return terminal["path"], ref


@pytest.mark.parametrize("finished", ["ready", "failed"])
def test_a_finished_advisory_survives_reconcile(
    live_catalogue: Path, tmp_path: Path, finished: str
) -> None:
    """Reconcile retires receipt custody only; a finished advisory is left alone.

    The batch was stranded by a shared page while its own page stayed in its
    after-state, so the advisory result still describes the current page.
    """
    from exomem import deferred_write_advisory
    from exomem import reconcile as reconcile_module

    vault = live_catalogue
    _page, ref = _strand_by_shared_page(tmp_path, vault)
    _force_advisory_state(vault, ref, "ready")
    if finished == "failed":
        connection = sqlite3.connect(deferred_index.store_path(vault))
        try:
            connection.execute(
                "UPDATE write_advisory_results SET state = 'failed', "
                "failure_code = 'embedding_unavailable' WHERE result_id = ?",
                (derived_receipts._parse_advisory_ref(ref),),
            )
            connection.commit()
        finally:
            connection.close()
    before = _advisory_state(vault, ref)

    report = reconcile_module.reconcile(vault)

    assert report.as_dict()["derived_batch_reconcile"]["retired"] == 1
    assert _advisory_state(vault, ref) == before
    resolved = deferred_write_advisory.resolve_result(vault, ref)
    assert resolved["status"] == finished, resolved


def test_a_pending_advisory_over_an_unchanged_page_fails_as_unavailable(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A sweep that never ran is owed an answer, not `superseded`."""
    from exomem import deferred_write_advisory
    from exomem import reconcile as reconcile_module

    vault = live_catalogue
    _page, ref = _strand_by_shared_page(tmp_path, vault)
    _force_advisory_state(vault, ref, "pending")

    reconcile_module.reconcile(vault)

    assert _advisory_state(vault, ref) == ("failed", "advisory_unavailable")
    resolved = deferred_write_advisory.resolve_result(vault, ref)
    assert resolved["status"] == "failed" and resolved["code"] == "advisory_unavailable"


def test_a_pending_advisory_over_a_moved_page_is_superseded_by_reconcile(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A pending advisory whose target moved describes nothing current."""
    from exomem import reconcile as reconcile_module

    vault = live_catalogue
    terminal = _remember(tmp_path, vault, "Advisory moved probe")
    ref = terminal.get("advisory_result_ref")
    assert ref, terminal
    target = vault / terminal["path"]
    target.write_bytes(target.read_bytes() + b"\nEdited by hand, never indexed.\n")
    _drain_until_idle(vault)
    assert [state for _id, state, _paths in _batches(vault)] == ["reconcile_required"]
    _force_advisory_state(vault, ref, "pending")

    reconcile_module.reconcile(vault)

    assert _advisory_state(vault, ref) == ("superseded", None)


# --------------------------------------------------------------------------- #
# Diagnosis (owner ruling R4): a committed-uncertain acknowledgement says why
# --------------------------------------------------------------------------- #


def test_a_failed_acknowledgement_logs_a_content_free_cause(
    live_catalogue: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log names the class, the stage and the proof outcome, and no path."""
    vault = live_catalogue
    title = "Uncertain cause probe"

    def unprovable(root, receipt, *, current_generation, **_kwargs):
        return derived_receipts.DerivedBatchProof(
            batch_id=receipt.batch_id,
            outcome="reconcile_required",
            canonical_generation=current_generation,
            path_states=(),
            ready_components=(),
        )

    monkeypatch.setattr(derived_receipts, "prove_committed", unprovable)

    def leaf(vault_root: Path, **_surface_kwargs):
        result = note_module.note(
            vault_root,
            content=f"# {title}\n\nBody.",
            note_type="insight",
            title=title,
            status="draft",
        )
        return {"path": result.path, "warnings": list(result.warnings)}

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )
    with caplog.at_level("WARNING", logger="exomem.writer_lease"):
        terminal = manager.invoke(
            SimpleNamespace(name="remember", leaf=leaf, read_only=False),
            (vault,),
            {"response_detail": "compact"},
            idempotency_key=None,
            mutation_request_id=str(uuid.uuid4()),
        )

    # The persisted terminal degrades to pending derived work; it never claims
    # the derived custody was proven.
    assert terminal["status"] == "committed", terminal
    assert terminal["derived_sync"] == "pending", terminal
    [record] = [
        r for r in caplog.records if "fast acknowledgement failed" in r.getMessage()
    ]
    message = record.getMessage()
    assert "class=RuntimeError" in message
    assert "stage=receipt_proof" in message
    assert "outcome=reconcile_required" in message
    assert "Knowledge Base" not in message and "uncertain-cause-probe" not in message


def test_the_cause_fragment_keeps_only_closed_tokens() -> None:
    """A code or field that is not a closed token is dropped, never echoed."""

    class Coded(Exception):
        code = "PATH_GUARD_CHANGED"

    class Leaky(Exception):
        code = "Knowledge Base/Notes/secret.md"

    assert writer_lease._content_free_cause(
        Coded(), stage="receipt_proof", component="lexstore", outcome=None
    ) == "class=Coded code=PATH_GUARD_CHANGED stage=receipt_proof component=lexstore"
    assert writer_lease._content_free_cause(
        Leaky("Knowledge Base/Notes/secret.md"), stage="Knowledge Base/x.md"
    ) == "class=Leaky"


def test_a_failed_route_advisory_sweep_logs_no_draft_content(
    live_catalogue: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deferred sweep runs over the draft's title and body; its failure log
    names the exception class and stage only, with no message or traceback."""
    import logging

    from exomem import corpus_aware, deferred_write_advisory

    vault = live_catalogue
    title = "Route sweep secret draft title"
    terminal = _remember(tmp_path, vault, title)
    assert terminal.get("advisory_sync") == "pending", terminal

    class Leaky(Exception):
        pass

    def leaky_sweep(_root, inputs, **_kwargs):
        raise Leaky(f"{inputs.title} {inputs.body}")

    monkeypatch.setattr(corpus_aware, "write_advisory_for", leaky_sweep)
    with caplog.at_level(logging.DEBUG, logger=deferred_write_advisory.__name__):
        _drain_until_idle(vault)

    assert _advisory_state(vault, terminal["advisory_result_ref"]) == (
        "failed",
        "advisory_failed",
    )
    records = [
        record
        for record in caplog.records
        if record.name == deferred_write_advisory.__name__
        and "advisory computation failed" in record.getMessage()
    ]
    assert records, [record.getMessage() for record in caplog.records]
    for record in records:
        assert record.exc_info is None
        assert "class=Leaky" in record.getMessage()
        assert "secret draft" not in record.getMessage()
        assert "shared page probe body" not in record.getMessage()


def test_an_edit_that_loses_its_guard_race_after_installing_the_manifest_is_retryable(
    live_catalogue: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing the activation manifest is not the edit's commit point.

    The first governed write into a vault installs the activation manifest in
    its own atomic batch, then commits the edit. When the edit's batch then
    loses a shared-auxiliary guard race it aborts with nothing written, and the
    documented answer is the retryable `STALE_SEMANTIC_WRITE`. Found by the
    3-writer burst (R4): the manifest batch had already marked the mutation
    committed, so the aborted edit came back committed-uncertain.
    """
    from exomem import activation_manifest, semantic_writes
    from exomem import edit as edit_module
    from exomem import vault as vault_module

    vault = live_catalogue
    assert activation_manifest.load_manifest(vault) is None
    rel = next(
        path.relative_to(vault).as_posix()
        for path in sorted((vault / "Knowledge Base" / "Notes").rglob("*.md"))
        if path.name not in {"index.md", "log.md"}
    )
    before = (vault / rel).read_bytes()

    def lose_the_race(*_args, **_kwargs):
        raise vault_module.PathGuardError("PATH_GUARD_CHANGED", "guarded leaf changed")

    monkeypatch.setattr(semantic_writes, "_commit_existing_locked", lose_the_race)

    def leaf(vault_root: Path, **_surface_kwargs):
        result = edit_module.edit(
            vault_root, path=rel, why="guard race probe", new_body="Guard race probe body.\n"
        )
        return {"path": result.path, "warnings": list(result.warnings)}

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )
    with pytest.raises(Exception) as raised:
        manager.invoke(
            SimpleNamespace(name="edit_memory", leaf=leaf, read_only=False),
            (vault,),
            {"response_detail": "compact"},
            idempotency_key=None,
            mutation_request_id=str(uuid.uuid4()),
        )

    assert activation_manifest.load_manifest(vault) is not None
    assert (vault / rel).read_bytes() == before
    assert "STALE_SEMANTIC_WRITE" in str(raised.value)
    assert "UNCERTAIN" not in str(raised.value)
