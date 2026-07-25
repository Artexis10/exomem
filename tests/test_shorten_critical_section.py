"""`shorten-mutation-critical-section`: the `remember` mutation boundary
covers only the commit seam, not pre-commit corpus validation or model
loading — turn 3 (C3): guard narrowing + pre-warm + telemetry for `remember`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from exomem import metrics, mutation_lock, relation_review, semantic_writes, writer_lease
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.commands import op_remember, product_commands_for

TODAY_KWARGS = {
    "content": (
        "# Bounded-boundary save\n\n"
        "## Observations\n\n"
        "- [operating constraint] Keep the boundary short #reliability\n"
    ),
    "title": "Bounded-boundary save",
    "slug": "bounded-boundary-save",
    "suggestions": False,
}


def _remember_command():
    return next(c for c in product_commands_for("mcp") if c.name == "remember")


def _standalone_manager(state_dir: Path, **kwargs) -> writer_lease.LeaseManager:
    return writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=state_dir), **kwargs)


def _invoke_remember(vault: Path, manager: writer_lease.LeaseManager, **kwargs):
    """Drive the real `remember` product command through `LeaseManager.invoke`
    (the narrow-boundary machinery), bypassing the MCP-facing envelope layer
    (`command_surface.wrapper`) that would otherwise convert a raised
    `OpError` into an `ok=False` return value instead of letting it propagate.
    """
    return manager.invoke(
        _remember_command(),
        (vault,),
        kwargs,
        implicit_idempotency_scope="principal:test",
    )


def _validated_kwargs(vault: Path, **extra) -> dict:
    kwargs = dict(TODAY_KWARGS, **extra)
    validation = op_remember(vault, validate_only=True, **kwargs)
    kwargs.update(
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )
    return kwargs


@pytest.fixture(autouse=True)
def _reset_metrics_and_managers():
    metrics.reset()
    yield
    metrics.reset()
    writer_lease.reset_managers_for_tests()


def test_remember_boundary_hold_stays_bounded_under_a_slow_validator(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _validated_kwargs(vault)

    real_evaluate = relation_review._evaluate

    def slow_evaluate(*args, **kw):
        time.sleep(2.0)
        return real_evaluate(*args, **kw)

    monkeypatch.setattr(relation_review, "_evaluate", slow_evaluate)

    manager = _standalone_manager(vault.parent / "state")

    result = _invoke_remember(vault, manager, **kwargs)

    assert (vault / result["path"]).is_file()
    timing = mutation_lock.last_mutation_timing()
    assert timing is not None
    # The floor here is real disk I/O for the commit itself (batch_atomic_write
    # writing the primary page + log + index auxiliaries), which correctly
    # stays inside the boundary by design: ~700-750ms measured quiet on this
    # box, with spikes above 1s under load. 1800ms leaves headroom over that
    # floor while staying well under the 2000ms validator sleep this asserts
    # is excluded from the hold — a wide margin either side of the two costs
    # it must tell apart.
    assert timing["hold_ms"] < 1800


def test_pre_boundary_validation_failure_never_acquires_the_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_kwargs = {
        "content": "# Prose only\n\nOrdinary structural prose.\n",
        "note_type": "insight",
        "title": "Prose only",
        "slug": "prose-only-blocked",
    }
    validation = op_remember(vault, validate_only=True, **blocked_kwargs)
    committing_kwargs = dict(
        blocked_kwargs,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )

    # Baseline: the leaf raises the same way with no lease/boundary layer at
    # all involved (a plain function call).
    with pytest.raises(ValueError) as baseline:
        op_remember(vault, **committing_kwargs)
    assert "SEMANTIC_CONTRACT_BLOCKED" in str(baseline.value)

    def unreachable_hold(self, **kwargs):
        raise AssertionError(
            "the mutation boundary must never be acquired for a pre-boundary validation failure"
        )

    monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "hold", unreachable_hold)

    manager = _standalone_manager(vault.parent / "state")

    with pytest.raises(ValueError) as narrowed:
        _invoke_remember(vault, manager, **committing_kwargs)

    assert str(narrowed.value) == str(baseline.value)


def test_embedding_prewarm_observes_a_free_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings, readiness

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    observed_states: list[str] = []

    def spy_get_model():
        observed_states.append(str(mutation_lock.active_mutation_snapshot()["state"]))
        raise RuntimeError("no real model in the unit-test environment")

    monkeypatch.setattr(embeddings, "get_model", spy_get_model)

    kwargs = _validated_kwargs(vault)
    manager = _standalone_manager(vault.parent / "state")

    result = _invoke_remember(vault, manager, **kwargs)

    assert (vault / result["path"]).is_file()
    # The pre-warm call (before `commit_creation` acquires its boundary) is
    # always the FIRST `get_model` call, and it must see a free boundary.
    # Later calls (the in-boundary embedding-sidecar fan-out, the
    # post-commit advisory pass) are unaffected by this change and may see
    # other states — only the pre-warm call's position/state is asserted.
    assert observed_states
    assert observed_states[0] == "free"


def test_mutation_busy_shape_is_unchanged_under_a_narrow_hold(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _validated_kwargs(vault)
    state_dir = vault.parent / "state"
    manager = _standalone_manager(state_dir, mutation_timeout_seconds=0.2)
    contender = _standalone_manager(state_dir)

    holding = threading.Event()
    release = threading.Event()

    def hold_boundary() -> None:
        with contender.mutation_guard(vault, request_id="contender", operation="probe"):
            holding.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold_boundary)
    holder.start()
    assert holding.wait(2.0)
    try:
        with pytest.raises(OpError) as busy:
            _invoke_remember(vault, manager, **kwargs)
    finally:
        release.set()
        holder.join(timeout=5.0)

    assert busy.value.code == "MUTATION_BUSY"
    assert busy.value.details.get("status") == "retryable"
    assert busy.value.details.get("committed") is False
    assert isinstance(busy.value.details.get("retry_after_ms"), int)
    assert "request_id" in busy.value.details


def test_remember_commit_revalidates_when_a_governed_commit_lands_between_prepare_and_commit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A governed commit between the pre-boundary validation and the commit
    bumps the boundary commit-generation (every mutation-guard exit does), so
    the validity stamp must be refused and the in-boundary revalidation must
    run — the stamp's whole job."""
    kwargs = _validated_kwargs(vault)
    manager = _standalone_manager(vault.parent / "state")

    real_prepare = relation_review.prepare_commit_creation_draft

    def prepare_then_concurrent_commit(*args, **kw):
        prepared = real_prepare(*args, **kw)
        # Simulate a concurrent governed writer committing right after our
        # pre-boundary validation finished.
        writer_lease._bump_commit_generation(manager.config.state_dir, vault)
        return prepared

    monkeypatch.setattr(
        relation_review, "prepare_commit_creation_draft", prepare_then_concurrent_commit
    )

    result = _invoke_remember(vault, manager, **kwargs)

    assert (vault / result["path"]).is_file()
    snap = metrics.snapshot()
    outcomes = {
        dict(entry["labels"]).get("outcome"): entry["value"]
        for entry in snap["counters"]
        if entry["name"] == "exomem_prevalidated_commit_total"
    }
    assert outcomes.get("revalidated") == 1
    assert outcomes.get("reused") is None


def _edit_memory_command():
    return next(c for c in product_commands_for("mcp") if c.name == "edit_memory")


def _invoke_edit_memory(vault: Path, manager: writer_lease.LeaseManager, **kwargs):
    """Mirrors `_invoke_remember`: drive `edit_memory` through
    `LeaseManager.invoke` directly, bypassing the envelope-catching MCP layer.
    """
    return manager.invoke(
        _edit_memory_command(),
        (vault,),
        kwargs,
        implicit_idempotency_scope="principal:test",
    )


def _write_existing_page(vault: Path, rel: str, *, page_id: str, body: str = "Before.") -> Path:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        "title: Existing page\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {page_id}\n"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def test_two_concurrent_edit_memory_writers_on_different_pages_both_succeed(
    vault: Path,
) -> None:
    """Two `edit_memory` calls on different primary pages both complete under
    the narrow boundary, with no deadlock and no corruption.

    Both edits share auxiliary write targets (`log.md`, the top/sub indexes)
    whose guards `edit.commit_edit` captures before `commit_existing`'s own
    (now narrow) boundary is acquired — a genuinely new race this change
    introduces for that shared auxiliary specifically (the wide boundary
    used to serialize guard-capture and commit together for every caller on
    one manager; the narrow boundary only serializes the commit). A losing
    guard is refused honestly (`PATH_GUARD_CHANGED`), never silently
    dropped, so a single immediate retry is the correct, expected recovery —
    the same "semantically honest interleaving" class already accepted for
    `STALE_SEMANTIC_WRITE` (see this change's write-latency spec delta).
    """
    rel_a = "Knowledge Base/Notes/Insights/narrow-edit-a.md"
    rel_b = "Knowledge Base/Notes/Insights/narrow-edit-b.md"
    _write_existing_page(vault, rel_a, page_id="00000000-0000-4000-8000-0000000000a1")
    _write_existing_page(vault, rel_b, page_id="00000000-0000-4000-8000-0000000000b1")

    manager = _standalone_manager(vault.parent / "state")
    results: list[dict] = []
    errors: list[BaseException] = []

    def edit(rel: str, new_body: str) -> None:
        for attempt in range(2):
            try:
                results.append(
                    _invoke_edit_memory(
                        vault,
                        manager,
                        path=rel,
                        why="narrow-boundary interleaving check",
                        new_body=new_body,
                    )
                )
                return
            except (vault_module.PathGuardError, ValueError) as error:
                # A losing auxiliary guard now surfaces as the documented
                # retryable STALE_SEMANTIC_WRITE (translated at the commit
                # seam) rather than a raw PathGuardError; one retry is the
                # correct, expected recovery either way.
                retryable = isinstance(
                    error, vault_module.PathGuardError
                ) or "STALE_SEMANTIC_WRITE" in str(error)
                if attempt == 0 and retryable:
                    continue
                errors.append(error)
                return
            except BaseException as error:  # noqa: BLE001 - inspect thread outcome
                errors.append(error)
                return

    threads = [
        threading.Thread(target=edit, args=(rel_a, "Updated A.")),
        threading.Thread(target=edit, args=(rel_b, "Updated B.")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert "Updated A." in (vault / rel_a).read_text(encoding="utf-8")
    assert "Updated B." in (vault / rel_b).read_text(encoding="utf-8")


def test_commit_existing_revalidates_on_census_mismatch_and_surfaces_stale_write(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/narrow-edit-stale.md"
    _write_existing_page(vault, rel, page_id="00000000-0000-4000-8000-0000000000c1")
    after_source = (vault / rel).read_text(encoding="utf-8").replace("Before.", "Updated.")

    preflight = semantic_writes.preflight_existing(
        vault,
        path=rel,
        after_source=after_source,
        operation="edit",
    )
    assert preflight.census_token is not None

    # A sibling write races ahead between preflight and commit — flips both
    # the corpus census and the file's own guarded content out from under
    # the captured preflight.
    (vault / rel).write_text(
        (vault / rel).read_text(encoding="utf-8").replace("Before.", "A sibling write raced ahead."),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as exc:
        semantic_writes.commit_existing(vault, preflight=preflight)

    assert exc.value.code == "STALE_SEMANTIC_WRITE"

def test_wide_boundary_kill_switch_restores_validation_inside_the_hold(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXOMEM_WIDE_MUTATION_BOUNDARY=1 must restore the wide boundary: the
    full-leaf guard wraps validation again, so the slow validator's 2000ms
    lands back INSIDE the measured hold."""
    monkeypatch.setenv("EXOMEM_WIDE_MUTATION_BOUNDARY", "1")
    kwargs = _validated_kwargs(vault)

    real_evaluate = relation_review._evaluate

    def slow_evaluate(*args, **kw):
        time.sleep(2.0)
        return real_evaluate(*args, **kw)

    monkeypatch.setattr(relation_review, "_evaluate", slow_evaluate)

    manager = _standalone_manager(vault.parent / "state")
    result = _invoke_remember(vault, manager, **kwargs)

    assert (vault / result["path"]).is_file()
    timing = mutation_lock.last_mutation_timing()
    assert timing is not None
    assert timing["hold_ms"] >= 2000
