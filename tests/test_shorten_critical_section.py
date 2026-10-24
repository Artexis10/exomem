"""`shorten-mutation-critical-section`: the `remember` mutation boundary
covers only the commit seam, not pre-commit corpus validation or model
loading — turn 3 (C3): guard narrowing + pre-warm + telemetry for `remember`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from exomem import (
    curation,
    metrics,
    mutation_lock,
    relation_review,
    reserved_paths,
    semantic_writes,
    writer_lease,
)
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.commands import op_remember, product_commands_for
from exomem.vault import content_hash

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


def test_remember_validation_observes_a_free_boundary_in_narrow_mode(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _validated_kwargs(vault)

    real_evaluate = relation_review._evaluate
    observed_states: list[str] = []

    def observe_boundary(*args, **kw):
        observed_states.append(str(mutation_lock.active_mutation_snapshot()["state"]))
        return real_evaluate(*args, **kw)

    monkeypatch.setattr(relation_review, "_evaluate", observe_boundary)

    manager = _standalone_manager(vault.parent / "state")

    result = _invoke_remember(vault, manager, **kwargs)

    assert (vault / result["path"]).is_file()
    assert observed_states and set(observed_states) == {"free"}


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

    # The pre-boundary warm takes its own brief identity-scope hold when cold; warm
    # it now so the trap below watches only the write's mutation boundary.
    reserved_paths.warm_identity_catalogue_before_boundary(vault)

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
    # A capture write now waits out ordinary contention on the server
    # (`test_capture_contention_absorbed.py`); the refusal it still raises for an
    # overdue holder or an exhausted wait keeps this shape, so pin the shape
    # with the wait switched off.
    monkeypatch.setenv("EXOMEM_CAPTURE_CONTENTION_ABSORB", "0")
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


_TARGET_REL = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization"
_HOSTS_OVERLAY = (
    "schema_version: 1\n"
    "extensions:\n"
    "  vault.hosts:\n"
    "    parent: relates_to\n"
    "    description: A venue hosts an event.\n"
)


def _relation_overlay(vault: Path) -> Path:
    from exomem import relation_registry

    return relation_registry.extension_registry_path(vault)


def _unregister_hosts(vault: Path, _state_dir: Path) -> None:
    # A hand edit of the overlay moves no commit generation.
    _relation_overlay(vault).unlink()


def _delete_target_in_a_governed_commit(vault: Path, state_dir: Path) -> None:
    (vault / f"{_TARGET_REL}.md").unlink()
    writer_lease._bump_commit_generation(state_dir, vault)


@pytest.mark.usefixtures("corpus_cache_on")
def test_remember_reuses_its_preflight_relation_review_while_the_stamp_is_current(
    vault: Path,
) -> None:
    """Nothing changed between preflight and commit, so the commit takes the
    preflight's relation verdict instead of judging the draft a second time,
    and the span an operator reads says so."""
    from exomem import call_spans

    manager = _standalone_manager(vault.parent / "state")
    handle = call_spans.MCP_CALL_TOKEN.set("reuse-observed")
    try:
        result = _invoke_remember(
            vault,
            manager,
            content=(
                "# Bounded fusion stage\n\n"
                "## Observations\n\n"
                "- [operating constraint] Keep the fusion stage bounded #reliability\n\n"
                "## Relations\n\n"
                f"- supports [[{_TARGET_REL}]]\n"
            ),
            title="Bounded fusion stage",
            slug="bounded-fusion-stage",
            suggestions=False,
        )
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)
    spans = {span["name"]: span for span in call_spans.pop_call_spans("reuse-observed")}

    assert (vault / result["path"]).is_file()
    fields = spans["commit.relation_review_prepare"]["fields"]
    assert {key: value for key, value in fields.items() if key not in call_spans.CPU_FIELDS} == {
        "reused": 1,
    }


@pytest.mark.usefixtures("corpus_cache_on")
@pytest.mark.parametrize(
    "intervening_change", [_unregister_hosts, _delete_target_in_a_governed_commit]
)
def test_remember_judges_again_when_its_preflight_verdict_went_stale(
    vault: Path, monkeypatch: pytest.MonkeyPatch, intervening_change
) -> None:
    """The commit reuses the preflight's relation verdict only while its stamp
    is current. A vocabulary hand edit or a governed commit between the two
    takes away the only relation that qualified the draft, so the commit must
    judge the draft again and refuse it instead of writing a stale verdict."""
    overlay = _relation_overlay(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(_HOSTS_OVERLAY, encoding="utf-8")
    kwargs = {
        "content": (
            "# Hosted retrieval review\n\n"
            "## Observations\n\n"
            "- [operating constraint] Keep the fusion stage bounded #reliability\n\n"
            "## Relations\n\n"
            f"- vault.hosts [[{_TARGET_REL}]]\n"
        ),
        "title": "Hosted retrieval review",
        "slug": "hosted-retrieval-review",
        "suggestions": False,
    }
    draft = op_remember(vault, validate_only=True, **kwargs)
    assert draft["relation_disposition"] == "qualifying_relation"
    manager = _standalone_manager(vault.parent / "state")
    real_commit = semantic_writes.commit_creation

    def commit_after_the_change(*args, **kw):
        intervening_change(vault, manager.config.state_dir)
        return real_commit(*args, **kw)

    monkeypatch.setattr(semantic_writes, "commit_creation", commit_after_the_change)

    with pytest.raises(ValueError, match="RELATION_DISPOSITION_MISSING"):
        _invoke_remember(vault, manager, **kwargs)

    assert not (vault / "Knowledge Base/Notes/Insights/hosted-retrieval-review.md").exists()


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

def test_wide_boundary_kill_switch_observes_validation_inside_the_hold(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXOMEM_WIDE_MUTATION_BOUNDARY=1 must restore the wide boundary: the
    full-leaf guard wraps validation again."""
    monkeypatch.setenv("EXOMEM_WIDE_MUTATION_BOUNDARY", "1")
    kwargs = _validated_kwargs(vault)

    real_evaluate = relation_review._evaluate
    observed_states: list[str] = []

    def observe_boundary(*args, **kw):
        observed_states.append(str(mutation_lock.active_mutation_snapshot()["state"]))
        return real_evaluate(*args, **kw)

    monkeypatch.setattr(relation_review, "_evaluate", observe_boundary)

    manager = _standalone_manager(vault.parent / "state")
    result = _invoke_remember(vault, manager, **kwargs)

    assert (vault / result["path"]).is_file()
    assert observed_states and set(observed_states) == {"held"}


@pytest.mark.parametrize("edited", ["unrelated", "bound"])
def test_curation_proposal_preparation_leaves_the_boundary_free_and_the_seal_rechecks(
    vault: Path, monkeypatch: pytest.MonkeyPatch, edited: str
) -> None:
    """A proposal parked in preparation blocks no governed write, and its seal
    refuses a plan whose bound page a write changed in the meantime."""
    rel_a = "Knowledge Base/Notes/Insights/propose-edit-a.md"
    rel_b = "Knowledge Base/Notes/Insights/propose-edit-b.md"
    _write_existing_page(vault, rel_a, page_id="00000000-0000-4000-8000-0000000000d1")
    _write_existing_page(vault, rel_b, page_id="00000000-0000-4000-8000-0000000000e1")
    plan = {
        "version": 1,
        "title": "Reword page A",
        "steps": [
            {
                "step_id": "reword-a",
                "kind": "edit",
                "args": {
                    "path": rel_a,
                    "why": "Apply the reviewed wording.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": "Before.",
                        "new_string": "Reworded.",
                        "expected_hash": content_hash((vault / rel_a).read_text(encoding="utf-8")),
                        "relation_disposition": "reviewed_none",
                        "relation_review_reason": "No relation changes.",
                    },
                },
            }
        ],
    }

    prepared = threading.Event()
    release = threading.Event()
    real_prepare_step = curation._prepare_step

    def park_after_preparation(*args, **kwargs):
        result = real_prepare_step(*args, **kwargs)
        prepared.set()
        assert release.wait(30.0)
        return result

    boundary_at_seal: list[str] = []
    real_batch_atomic_write = curation.batch_atomic_write

    def observe_seal_write(*args, **kwargs):
        boundary_at_seal.append(str(mutation_lock.active_mutation_snapshot()["state"]))
        return real_batch_atomic_write(*args, **kwargs)

    monkeypatch.setattr(curation, "_prepare_step", park_after_preparation)
    monkeypatch.setattr(curation, "batch_atomic_write", observe_seal_write)

    manager = _standalone_manager(vault.parent / "state", mutation_timeout_seconds=0.5)
    maintain = next(c for c in product_commands_for("mcp") if c.name == "maintain_memory")
    outcome: dict[str, object] = {}

    def propose() -> None:
        try:
            outcome["result"] = manager.invoke(
                maintain,
                (vault,),
                {"mode": "curation", "curation_action": "propose", "plan": plan},
                implicit_idempotency_scope="principal:test",
            )
        except BaseException as error:  # noqa: BLE001 - inspect the thread outcome
            outcome["error"] = error

    proposer = threading.Thread(target=propose)
    proposer.start()
    try:
        assert prepared.wait(30.0)
        rel_edited = rel_b if edited == "unrelated" else rel_a
        _invoke_edit_memory(
            vault,
            manager,
            path=rel_edited,
            why="Write while a proposal is being prepared.",
            operation={"kind": "replace_body", "new_body": "Written during preparation."},
        )
        assert "Written during preparation." in (vault / rel_edited).read_text(encoding="utf-8")
    finally:
        release.set()
        proposer.join(timeout=30.0)

    assert not proposer.is_alive()
    runs = curation.CurationStore(vault).root
    if edited == "unrelated":
        assert "error" not in outcome
        assert (runs / outcome["result"]["run_id"]).is_dir()
        assert boundary_at_seal and set(boundary_at_seal) == {"held"}
    else:
        error = outcome["error"]
        assert getattr(error, "code", None) == "CURATION_BINDING_STALE", error
        assert boundary_at_seal == []
        assert not runs.exists() or not any(runs.iterdir())


def test_curation_proposal_refuses_a_registry_that_changed_during_preparation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparation runs unguarded, so it captures registry identities before it
    reads anything: a registry change during preparation must refuse at the seal,
    not be absorbed into the plan's bindings."""
    rel = "Knowledge Base/Notes/Insights/propose-registry.md"
    _write_existing_page(vault, rel, page_id="00000000-0000-4000-8000-0000000000f1")
    plan = {
        "version": 1,
        "title": "Reword the page",
        "steps": [
            {
                "step_id": "reword",
                "kind": "edit",
                "args": {
                    "path": rel,
                    "why": "Apply the reviewed wording.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": "Before.",
                        "new_string": "Reworded.",
                        "expected_hash": content_hash((vault / rel).read_text(encoding="utf-8")),
                        "relation_disposition": "reviewed_none",
                        "relation_review_reason": "No relation changes.",
                    },
                },
            }
        ],
    }
    real_prepare_step = curation._prepare_step

    def change_a_registry_after_preparation(*args, **kwargs):
        result = real_prepare_step(*args, **kwargs)
        contracts = vault / "Knowledge Base" / "_Schema" / "contracts"
        contracts.mkdir(parents=True, exist_ok=True)
        (contracts / "added-during-preparation.yaml").write_text("version: 1\n", encoding="utf-8")
        return result

    monkeypatch.setattr(curation, "_prepare_step", change_a_registry_after_preparation)

    with pytest.raises(Exception) as refused:
        curation.propose(vault, plan)

    assert getattr(refused.value, "code", None) == "CURATION_REGISTRY_CHANGED", refused.value
    runs = curation.CurationStore(vault).root
    assert not runs.exists() or not any(runs.iterdir())
