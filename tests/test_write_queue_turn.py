"""A keyed write's queue turn ends at boundary acquisition, not at command end.

Three defects made a long batch (a multi-path `process_media`) park every later
keyed write for the whole command:

- `_absorb_contention` kept the write's ticket at the head of `_FairCaptureQueue`
  until its attempt returned, so a write that arrived after the boundary was free
  still waited out the older command.
- A batch re-took the boundary per unit with no deference to a waiting write.
- The commit tail (`register_self_write_and_close`) published registries inline
  while the caller held the boundary, so a slow registry lock became boundary
  hold time.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    commands,
    deferred_index,
    file_watcher,
    find,
    media_processing,
    mutation_lock,
    semantic_contract,
    source_capture,
    writer_lease,
)

_OBSERVE_SECONDS = 15.0
_HOLD_SECONDS = 30.0


@pytest.fixture(autouse=True)
def _reset_managers():
    yield
    writer_lease.reset_managers_for_tests()


def _command(name: str):
    return next(c for c in commands.product_commands_for("mcp") if c.name == name)


def _managers(state_dir: Path) -> tuple[writer_lease.LeaseManager, writer_lease.LeaseManager]:
    config = writer_lease.LeaseConfig(state_dir=state_dir)
    return (
        writer_lease.LeaseManager(config, mutation_timeout_seconds=0.05),
        writer_lease.LeaseManager(config),
    )


def test_a_later_keyed_write_commits_before_a_long_batch_command_ends(
    vault: Path,
) -> None:
    """A's ticket left the line when its attempt held the boundary; B never waits out A's tail."""
    writer, holder = _managers(vault.parent / "state")
    release_holder = threading.Event()
    a_in_tail = threading.Event()
    b_committed = threading.Event()
    times: dict[str, float] = {}

    def leaf_a(vault_root: Path) -> dict:
        with writer.mutation_guard(vault_root, request_id="a", operation="batch_unit"):
            pass
        # The boundary is released; the command keeps running, the way a
        # multi-unit batch does between two commits. A later write's turn must
        # not wait for the rest of it.
        a_in_tail.set()
        b_committed.wait(5.0)
        times["a_end"] = time.monotonic()
        return {"ok": "a"}

    def leaf_b(vault_root: Path) -> dict:
        with writer.mutation_guard(vault_root, request_id="b", operation="edit"):
            times["b_commit"] = time.monotonic()
            b_committed.set()
        return {"ok": "b"}

    command_a = SimpleNamespace(name="remember", read_only=False, leaf=leaf_a)
    command_b = SimpleNamespace(name="remember", read_only=False, leaf=leaf_b)

    holder_entered = threading.Event()

    def hold() -> None:
        with holder.mutation_guard(vault, request_id="holder", operation="holder"):
            holder_entered.set()
            assert release_holder.wait(_HOLD_SECONDS)

    holder_thread = threading.Thread(target=hold, daemon=True)
    holder_thread.start()
    assert holder_entered.wait(_OBSERVE_SECONDS)

    result_a: list[dict] = []
    result_b: list[dict] = []

    def run_a() -> None:
        result_a.append(
            writer.invoke(command_a, (vault,), {}, idempotency_key="turn-a")
        )

    def run_b() -> None:
        result_b.append(
            writer.invoke(command_b, (vault,), {}, idempotency_key="turn-b")
        )

    try:
        thread_a = threading.Thread(target=run_a, daemon=True)
        thread_a.start()
        # A is refused by the holder's boundary and takes the head of the line.
        deadline = time.monotonic() + _OBSERVE_SECONDS
        while (
            not writer._capture_queue.waiting()  # noqa: SLF001 - the fixture reads the line directly
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert writer._capture_queue.waiting()  # noqa: SLF001
        release_holder.set()
        # A acquires, releases the boundary, and keeps running: B arrives now,
        # exactly like a write that lands mid-batch on a free boundary.
        assert a_in_tail.wait(_OBSERVE_SECONDS)
        thread_b = threading.Thread(target=run_b, daemon=True)
        thread_b.start()
        assert b_committed.wait(_OBSERVE_SECONDS)
        thread_b.join(timeout=_OBSERVE_SECONDS)
        thread_a.join(timeout=_OBSERVE_SECONDS)
    finally:
        release_holder.set()
        holder_thread.join(timeout=_HOLD_SECONDS)

    assert result_a == [{"ok": "a"}] and result_b == [{"ok": "b"}]
    assert times["b_commit"] < times["a_end"]


def test_a_released_ticket_rejoins_at_the_back_after_another_refusal() -> None:
    """The unit contract: release_turn leaves the line; the next join goes to the back."""
    queue = writer_lease._FairCaptureQueue()  # noqa: SLF001
    wait_a = writer_lease._ContentionWait(queue, 60.0)  # noqa: SLF001
    wait_b = writer_lease._ContentionWait(queue, 60.0)  # noqa: SLF001
    wait_a.join_line()
    wait_b.join_line()
    ticket_b = wait_b.ticket

    wait_a.release_turn()

    assert wait_a.ticket is None
    # B is the head now; nobody waits behind it.
    assert queue.waiting_behind_head() == 0
    assert queue.wait_turn(ticket_b, time.monotonic() + 1.0)

    # A's attempt met another absorbable refusal: it rejoins at the back.
    wait_a.join_line()
    assert wait_a.ticket is not None and wait_a.ticket is not ticket_b
    assert queue.waiting_behind_head() == 1
    wait_a.close()
    wait_b.close()


def test_the_acquisition_observer_fires_once_per_outer_acquire_and_never_breaks_it(
    vault: Path,
) -> None:
    writer, _holder = _managers(vault.parent / "state")
    calls: list[str] = []

    def observer() -> None:
        calls.append("fired")
        raise RuntimeError("an observer must never break the acquire")

    coordinator = writer._mutation_coordinator_for(vault)  # noqa: SLF001
    with mutation_lock.on_boundary_acquired(observer):
        with coordinator.hold(request_id="outer", operation="op"):
            # A re-entrant hold is not a new acquisition.
            with coordinator.hold(request_id="nested", operation="op"):
                pass
        with coordinator.hold(request_id="second", operation="op"):
            pass

    assert calls == ["fired", "fired"]

    # Reserved-identity coordinators never notify.
    reserved = writer._mutation_coordinator_for(
        "reserved-identity-v1:gate:vault:test", kind=mutation_lock.COORDINATOR_IDENTITY_GATE
    )
    with mutation_lock.on_boundary_acquired(observer):
        with reserved.hold(
            request_id="gate",
            operation="gate",
            holder_kind="reserved-state",
            publish_holder_metadata=False,
        ):
            pass
    assert calls == ["fired", "fired"]


def test_a_yield_waits_only_while_a_write_waits(vault: Path) -> None:
    writer, holder = _managers(vault.parent / "state")
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_done = threading.Event()

    # Nothing waits: the yield returns at once.
    started = time.monotonic()
    assert writer.yield_to_waiting_writes(vault, max_wait=1.0) < 0.5
    assert time.monotonic() - started < 0.5

    def hold() -> None:
        with holder.mutation_guard(vault, request_id="h", operation="hold"):
            holder_entered.set()
            assert release_holder.wait(_HOLD_SECONDS)

    def wait_for_the_boundary() -> None:
        with holder.mutation_guard(vault, request_id="w", operation="edit"):
            waiter_done.set()

    holder_thread = threading.Thread(target=hold, daemon=True)
    waiter_thread = threading.Thread(target=wait_for_the_boundary, daemon=True)
    try:
        holder_thread.start()
        assert holder_entered.wait(_OBSERVE_SECONDS)
        waiter_thread.start()
        # The waiter is now parked on the boundary, so a yield must wait too.
        waited = 0.0
        started = time.monotonic()
        while waited <= 0.0 and time.monotonic() - started < _OBSERVE_SECONDS:
            waited = writer.yield_to_waiting_writes(vault, max_wait=0.2)
        assert waited > 0.0
        release_holder.set()
        # With the waiter committed, the next yield is free again.
        waiter_thread.join(timeout=_OBSERVE_SECONDS)
        assert waiter_done.is_set()
        assert writer.yield_to_waiting_writes(vault, max_wait=1.0) < 0.5
    finally:
        release_holder.set()
        holder_thread.join(timeout=_HOLD_SECONDS)
        waiter_thread.join(timeout=_HOLD_SECONDS)


def test_a_yield_never_runs_under_a_boundary(vault: Path) -> None:
    writer, _holder = _managers(vault.parent / "state")
    with writer.mutation_guard(vault, request_id="hold", operation="hold"):
        started = time.monotonic()
        assert writer.yield_to_waiting_writes(vault, max_wait=5.0) == 0.0
        assert time.monotonic() - started < 0.5


def _publication_probe(monkeypatch: pytest.MonkeyPatch):
    """Count `publish_corpus_delta` calls and capture their arguments."""
    calls: list[dict] = []
    completed = threading.Event()
    real = file_watcher.index_sync.publish_corpus_delta

    def probe(*args, **kwargs):
        calls.append(dict(kwargs))
        try:
            return real(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(file_watcher.index_sync, "publish_corpus_delta", probe)
    return calls, completed


def _page(vault: Path, name: str = "target") -> Path:
    page = vault / "Knowledge Base" / "Notes" / f"{name}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntitle: t\ntype: note\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\nbody\n",
        encoding="utf-8",
    )
    return page


def test_a_publication_blocked_on_the_corpus_lock_releases_the_boundary_then_finishes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit tail never holds the boundary past its bound, whichever inner lock is slow."""
    writer, _holder = _managers(vault.parent / "state")
    lock_held = threading.Event()
    release_lock = threading.Event()
    calls, completed = _publication_probe(monkeypatch)

    def bulk() -> None:
        with semantic_contract._CORPUS_CONTEXT_UPDATE_LOCK:  # noqa: SLF001 - the incident's contended lock
            lock_held.set()
            assert release_lock.wait(_HOLD_SECONDS)

    bulk_thread = threading.Thread(target=bulk, daemon=True)
    bulk_thread.start()
    assert lock_held.wait(_OBSERVE_SECONDS)

    page = _page(vault)
    held_seconds = None
    published = None
    try:
        with writer.mutation_guard(vault, request_id="commit", operation="sidecar"):
            started = time.monotonic()
            published = file_watcher.register_self_write_and_close(vault, [page], [])
            held_seconds = time.monotonic() - started
    finally:
        release_lock.set()
        bulk_thread.join(timeout=_HOLD_SECONDS)

    assert held_seconds is not None and held_seconds < (
        file_watcher._IN_BOUNDARY_PUBLICATION_WAIT_SECONDS + 2.0  # noqa: SLF001
    )
    assert published is file_watcher.PUBLICATION_HANDED_OFF  # the worker publishes after release
    assert completed.wait(_OBSERVE_SECONDS)
    assert calls and calls[0].get("populate_on_miss") is False


def test_the_bounded_tail_is_generic_across_inner_locks(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same bound with the resolver lock held: the fix is not specific to the corpus lock."""
    writer, _holder = _managers(vault.parent / "state")
    lock_held = threading.Event()
    release_lock = threading.Event()
    calls, completed = _publication_probe(monkeypatch)

    def bulk() -> None:
        with find._RESOLVER_LOCK:  # noqa: SLF001 - the other plausible incident lock
            lock_held.set()
            assert release_lock.wait(_HOLD_SECONDS)

    bulk_thread = threading.Thread(target=bulk, daemon=True)
    bulk_thread.start()
    assert lock_held.wait(_OBSERVE_SECONDS)

    page = _page(vault, "resolver")
    try:
        with writer.mutation_guard(vault, request_id="commit", operation="sidecar"):
            started = time.monotonic()
            file_watcher.register_self_write_and_close(vault, [page], [])
            held_seconds = time.monotonic() - started
    finally:
        release_lock.set()
        bulk_thread.join(timeout=_HOLD_SECONDS)

    assert held_seconds < (
        file_watcher._IN_BOUNDARY_PUBLICATION_WAIT_SECONDS + 2.0  # noqa: SLF001
    )
    assert completed.wait(_OBSERVE_SECONDS)


def test_handed_off_publications_keep_commit_order(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One worker runs handed-off publications in the order their commits queued them."""
    writer, _holder = _managers(vault.parent / "state")
    order: list[str] = []
    second_done = threading.Event()
    real = file_watcher.index_sync.publish_corpus_delta

    def probe(vault_root, *, changed, deleted, **kwargs):
        order.extend(path.name for path in changed)
        result = real(vault_root, changed=changed, deleted=deleted, **kwargs)
        if len(order) == 2:
            second_done.set()
        return result

    monkeypatch.setattr(file_watcher.index_sync, "publish_corpus_delta", probe)
    first = _page(vault, "first")
    second = _page(vault, "second")

    with writer.mutation_guard(vault, request_id="one", operation="sidecar"):
        file_watcher.register_self_write_and_close(vault, [first], [])
    with writer.mutation_guard(vault, request_id="two", operation="sidecar"):
        file_watcher.register_self_write_and_close(vault, [second], [])

    assert second_done.wait(_OBSERVE_SECONDS)
    assert order == [first.name, second.name]


def test_a_publish_with_no_boundary_held_stays_inline_and_populates_on_miss(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no boundary held, the tail is exactly today's synchronous behaviour."""
    calls, completed = _publication_probe(monkeypatch)
    page = _page(vault, "inline")

    file_watcher.register_self_write_and_close(vault, [page], [])

    assert completed.wait(_OBSERVE_SECONDS)
    assert calls and calls[0].get("populate_on_miss") is True


def test_a_multi_path_reprocess_lets_a_keyed_edit_commit_mid_batch(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident's regression: an edit queued during a multi-path batch commits within
    about one unit's hold, not after the whole batch."""
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    names = [f"clip-{index}.m4a" for index in range(5)]
    for name in names:
        binary = vault / "Knowledge Base" / "Evidence" / "Audio" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\x00\x00\x00\x18ftypM4A fake audio")
    rels = [f"Knowledge Base/Evidence/Audio/{name}" for name in names]

    # One slow unit per path, inside the commit's boundary hold.
    first_hold = threading.Event()
    real_committing = media_processing._committing  # noqa: SLF001

    def slow_committing(vault_root, guard, operation, unit):
        def slowed():
            first_hold.set()
            time.sleep(0.6)
            return unit()

        return real_committing(vault_root, guard, operation, slowed)

    monkeypatch.setattr(media_processing, "_committing", slow_committing)  # noqa: SLF001

    # A holder makes the batch's first attempt meet a busy boundary once, so its
    # ticket exists -- the defect needs a ticket that outlives its turn.
    state_dir = tmp_path / "writer-state"
    holder = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=state_dir))
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def hold() -> None:
        with holder.mutation_guard(vault, request_id="holder", operation="holder"):
            holder_entered.set()
            assert release_holder.wait(_HOLD_SECONDS)

    holder_thread = threading.Thread(target=hold, daemon=True)
    holder_thread.start()
    assert holder_entered.wait(_OBSERVE_SECONDS)

    # The edit thread's first boundary acquisition stamps `edit_acquire` --
    # `on_boundary_acquired` itself can't carry a second observer (the
    # attempt's own `release_turn` rightly shadows it), so time the acquire
    # directly.
    edit_ident: list[int] = []
    real_acquire = mutation_lock.VaultMutationCoordinator._acquire_boundary  # noqa: SLF001

    def timed_acquire(self, *args, **kwargs):
        result = real_acquire(self, *args, **kwargs)
        if (
            self.kind == mutation_lock.COORDINATOR_VAULT_BOUNDARY
            and edit_ident
            and threading.get_ident() == edit_ident[0]
        ):
            times.setdefault("edit_acquire", time.monotonic())
        return result

    monkeypatch.setattr(
        mutation_lock.VaultMutationCoordinator, "_acquire_boundary", timed_acquire
    )

    batch_result: list[dict] = []
    batch_error: list[BaseException] = []
    times: dict[str, float] = {}

    def run_batch() -> None:
        try:
            batch_result.append(
                writer_lease.invoke_command(
                    _command("process_media"),
                    vault,
                    paths=rels,
                    operation="reprocess",
                    idempotency_key="media-batch",
                )
            )
        except BaseException as error:  # noqa: BLE001 - report it to the assertion
            batch_error.append(error)
        finally:
            times["batch_end"] = time.monotonic()

    def run_edit() -> None:
        edit_ident.append(threading.get_ident())
        times["edit_start"] = time.monotonic()
        writer_lease.invoke_command(
            _command("edit_memory"),
            vault,
            path="Knowledge Base/Notes/edit-target.md",
            why="concurrent correction",
            operation={
                "kind": "replace_string",
                "old_string": "wording before",
                "new_string": "wording after",
            },
            idempotency_key="edit-during-batch",
        )
        times["edit_commit"] = time.monotonic()

    note = vault / "Knowledge Base" / "Notes" / "edit-target.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntitle: edit-target\ntype: note\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        "---\n\n## Observations\n\n- wording before\n",
        encoding="utf-8",
    )

    try:
        batch_thread = threading.Thread(target=run_batch, daemon=True)
        batch_thread.start()
        # The batch is refused once (its ticket is at the head of the line),
        # then the holder steps away and the batch's run begins.
        manager = writer_lease.get_manager()
        deadline = time.monotonic() + _OBSERVE_SECONDS
        while (
            not manager._capture_queue.waiting()  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert manager._capture_queue.waiting()  # noqa: SLF001
        release_holder.set()
        # The edit lands mid-batch, while a unit's commit holds the boundary.
        assert first_hold.wait(_OBSERVE_SECONDS)
        edit_thread = threading.Thread(target=run_edit, daemon=True)
        edit_thread.start()
        batch_thread.join(timeout=_OBSERVE_SECONDS * 2)
        edit_thread.join(timeout=_OBSERVE_SECONDS * 2)
    finally:
        release_holder.set()
        holder_thread.join(timeout=_HOLD_SECONDS)

    assert not batch_error, batch_error
    assert len(batch_result) == 1
    # Every unit completed (the yield slows the batch; it never drops one).
    assert all((vault / f"{rel}.md").exists() for rel in rels)
    assert "wording after" in note.read_text(encoding="utf-8")
    assert times["edit_commit"] < times["batch_end"]
    # The boundary wait is bounded by about one unit's hold, not by the batch.
    assert times["edit_acquire"] - times["edit_start"] < 0.6 * 3
    # The whole command adds at most the bounded publication tail on top.
    assert times["edit_commit"] - times["edit_start"] < (
        file_watcher._IN_BOUNDARY_PUBLICATION_WAIT_SECONDS + 0.6 * 3 + 2.0  # noqa: SLF001
    )


def test_a_handed_off_publication_is_not_republished_inside_the_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real `post_commit_batch_fanout`: a handoff owes the delta to the worker, so
    `upsert_after_write` must not publish it again under the same boundary — and an
    owed-then-owned delta mints no full-index receipt."""
    from exomem import vault as vault_module

    writer, _holder = _managers(vault.parent / "state")
    lock_held = threading.Event()
    release_lock = threading.Event()
    calls, published_once = _publication_probe(monkeypatch)

    full_receipts: list[list[str]] = []
    real_add_full = deferred_index.add_full

    def full_probe(root, rels):
        full_receipts.append(list(rels))
        return real_add_full(root, rels)

    monkeypatch.setattr(deferred_index, "add_full", full_probe)

    registered: list = []
    real_register = file_watcher.register_publication_intents

    def register_probe(root, artifacts):
        intents = real_register(root, artifacts)
        registered.extend(intents)
        return intents

    monkeypatch.setattr(file_watcher, "register_publication_intents", register_probe)

    intents_closed = threading.Event()
    real_finalize = file_watcher.finalize_publication_intents
    real_abort = file_watcher.abort_publication_intents

    def finalize_probe(intents, **kwargs):
        real_finalize(intents, **kwargs)
        intents_closed.set()

    def abort_probe(intents, **kwargs):
        real_abort(intents, **kwargs)
        intents_closed.set()

    monkeypatch.setattr(file_watcher, "finalize_publication_intents", finalize_probe)
    monkeypatch.setattr(file_watcher, "abort_publication_intents", abort_probe)

    # Unrelated components are held to outcomes that complete in every cell:
    # a concurrent graph rebuild can be refused by this batch's own boundary
    # hold (its own bounded-wait incident), and embeddings may be absent here.
    from exomem import epistemic_graph

    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda *a, **k: epistemic_graph.GraphDispatchResult.not_required(),
    )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    def bulk() -> None:
        with semantic_contract._CORPUS_CONTEXT_UPDATE_LOCK:  # noqa: SLF001
            lock_held.set()
            assert release_lock.wait(_HOLD_SECONDS)

    bulk_thread = threading.Thread(target=bulk, daemon=True)
    bulk_thread.start()
    assert lock_held.wait(_OBSERVE_SECONDS)

    page = _page(vault, "fanout")
    held_seconds = None
    try:
        with writer.mutation_guard(vault, request_id="fanout", operation="batch"):
            started = time.monotonic()
            vault_module.batch_atomic_write(
                [vault_module.PlannedWrite(page, "fanout body\n")],
                vault_root=vault,
            )
            held_seconds = time.monotonic() - started
    finally:
        release_lock.set()
        bulk_thread.join(timeout=_HOLD_SECONDS)

    assert held_seconds is not None and held_seconds < (
        file_watcher._IN_BOUNDARY_PUBLICATION_WAIT_SECONDS + 2.0  # noqa: SLF001
    )
    # The worker publishes the delta once, after the boundary is released; the
    # inline upsert does not run a second publish of the same delta.
    assert published_once.wait(_OBSERVE_SECONDS)
    assert len(calls) == 1
    assert not full_receipts
    assert intents_closed.wait(_OBSERVE_SECONDS)
    assert registered and all(intent.disposition != "active" for intent in registered)


def test_raw_enumeration_fails_closed_when_a_staged_entry_vanishes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry gone between listing and stat cannot prove the enumeration complete."""
    from exomem import vault as vault_module

    for lane in vault_module.APPEND_ONLY_KB_SUBPATHS:
        (vault / "Knowledge Base" / lane).mkdir(parents=True, exist_ok=True)

    def vanished(directory, onerror=None, followlinks=False):
        if onerror is not None:
            onerror(FileNotFoundError("staged directory removed mid-walk"))
        return iter(())

    monkeypatch.setattr(source_capture.os, "walk", vanished)
    with pytest.raises(source_capture.RawEnumerationMoved):
        list(source_capture.raw_markdown_files(vault))


def test_a_moved_enumeration_replans_the_capture_as_retryable_busy(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-commit planning path converts a moved enumeration into the same
    retryable movement the in-boundary commit reports, then re-plans: the write
    neither fails `SOURCE_IDENTITY_UNAVAILABLE` nor silently commits a plan that
    missed the moved page."""
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    writer = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    remember = _command("remember")
    content = (
        "# Moved enumeration\n\n## Observations\n\n"
        "- [operating constraint] Keep enumeration retries honest #reliability\n"
    )
    validation = commands.op_remember(
        vault,
        validate_only=True,
        content=content,
        title="Moved enumeration",
        slug="moved-enumeration",
        suggestions=False,
    )
    kwargs = {
        "content": content,
        "title": "Moved enumeration",
        "slug": "moved-enumeration",
        "suggestions": False,
        "draft_id": validation["draft_id"],
        "draft_hash": validation["draft_hash"],
        "draft_token": validation["draft_token"],
        "relation_disposition": "reviewed_none",
        "relation_review_hash": validation["draft_hash"],
        "relation_review_reason": "No honest relation exists in the isolated fixture.",
    }

    calls = 0
    real = source_capture.raw_markdown_files

    def moved(root, *, subtrees=()):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise source_capture.RawEnumerationMoved(
                "SOURCE_IDENTITY_UNAVAILABLE: canonical raw enumeration is incomplete"
            )
        return real(root, subtrees=subtrees)

    monkeypatch.setattr(source_capture, "raw_markdown_files", moved)

    converted = []
    real_busy = mutation_lock.planned_write_moved_busy

    def probe():
        error = real_busy()
        converted.append(error)
        return error

    monkeypatch.setattr(mutation_lock, "planned_write_moved_busy", probe)

    result = writer.invoke(
        remember, (vault,), kwargs, implicit_idempotency_scope="principal:test"
    )

    assert result["path"]
    assert converted
    assert all(
        error.code == "MUTATION_BUSY"
        and error.details["cause"] == "auxiliary_changed"
        for error in converted
    )
    assert calls >= 2  # the absorbed retry re-planned and enumerated again
