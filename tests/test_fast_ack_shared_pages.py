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
newer exact batch carries it with live or retired pending custody; the older
batch still converges the paths it owns. An out-of-band move that no batch
covers stays `reconcile_required`.

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
    """Pass until nothing is due or recoverable, within a fixed bound."""
    for _ in range(passes):
        if not (
            derived_receipts.due_component_count(root, now=time.time() + 3600)
            or derived_receipts.recoverable_batch_count(root)
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


def test_an_out_of_band_move_that_no_batch_covers_stays_reconcile_required(
    live_catalogue: Path, tmp_path: Path
) -> None:
    """A hand edit no receipt covers is not accepted by per-path supersession."""
    vault = live_catalogue
    page = _remember(tmp_path, vault, "Out of band probe")["path"]
    target = vault / page
    target.write_bytes(target.read_bytes() + b"\nEdited by hand in the editor.\n")

    _drain_until_idle(vault)

    assert [state for _id, state, _paths in _batches(vault)] == ["reconcile_required"]
    assert pending_recall.overlay(vault).outcome == "warming"
