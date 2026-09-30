from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exomem import curation
from exomem import episode_model as model


def _prepared(vault: Path):
    step = {
        "step_id": "save-observation",
        "kind": "create-note",
        "args": {
            "title": "Episode recovery observation",
            "slug": "episode-recovery-observation",
            "content": "## Observations\n\n- [finding] Preserve a supported result. ^result\n",
            "relation_disposition": "reviewed_none",
            "relation_review_reason": "No supported connection in this isolated fixture.",
        },
    }
    proposed = curation.propose(
        vault, {"version": 1, "title": "Recover an episode", "steps": [step]}
    )
    store = curation.CurationStore(vault)
    plan = store.load_plan(proposed["run_id"])
    step = plan["steps"][0]
    state = model.declare_candidate(
        model.start_episode("recovery-episode", {"excerpt": "Preserve a supported result."}),
        "observation",
    )
    candidate = state["candidates"][0]["candidate_id"]
    state = model.revise_proposal(
        state,
        candidate,
        {
            "route": "focused_note",
            "title": step["args"]["title"],
            "alternatives": [],
            "evidence": "complete",
            "reason": "This result has a separate durable scope.",
            "leaves": [
                {
                    "leaf_key": "write",
                    "effect_revision": 1,
                    "kind": step["kind"],
                    "args": step["args"],
                }
            ],
        },
    )
    leaf = state["candidates"][0]["leaves"][0]["leaf_id"]
    state = model.set_disposition(state, candidate, "routed", "Preserve the result.")
    state = model.bind_curation_leaf(
        state,
        candidate,
        leaf,
        {
            "sealed_plan": plan,
            "run_id": proposed["run_id"],
            "plan_id": proposed["plan_id"],
            "plan_fingerprint": proposed["plan_fingerprint"],
            "ordinal": 0,
            "step_id": step["step_id"],
            "operation_id": curation.operation_id(proposed["plan_id"], 0, step["step_id"]),
        },
    )
    state = model.mark_attempt_started(model.attest_precommit(state, 1), candidate, leaf)
    return state, candidate, leaf, proposed, store


def _apply(vault: Path, proposed: dict):
    return curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Execute this exact plan.",
    )


def _files(vault: Path) -> dict[str, bytes]:
    return {
        p.relative_to(vault).as_posix(): p.read_bytes() for p in vault.rglob("*") if p.is_file()
    }


def test_real_commit_reconciles_without_writes_or_reexecution(vault: Path, monkeypatch):
    state, candidate, leaf, proposed, store = _prepared(vault)
    result = _apply(vault, proposed)
    before, original = _files(vault), copy.deepcopy(state)

    def must_not_execute(*args, **kwargs):
        pytest.fail("reconciliation must not execute a writer")

    monkeypatch.setattr(curation, "_dispatch_step", must_not_execute)
    from exomem.episode_reconciliation import reconcile_curation_leaf

    reconciled = reconcile_curation_leaf(vault, state, candidate, leaf)

    observed = reconciled["candidates"][0]["leaves"][0]
    assert observed["outcome"] == "committed"
    receipt = store.reconstruct(proposed["run_id"])["receipts"][0]
    assert observed["outcome_proof"]["receipt_digest"] == curation._digest(receipt)
    assert observed["outcome_proof"]["result_digest"] == result["step"]["result_digest"]
    assert len(observed["outcome_proof"]["readback_digest"]) == 64
    assert state == original
    assert _files(vault) == before
    with pytest.raises(model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        model.mark_attempt_started(reconciled, candidate, leaf)


@pytest.mark.parametrize("barrier", ["after-prepared-state", "after-leaf-witness"])
def test_missing_receipt_stays_uncertain_until_existing_owner_recovers(
    vault: Path, monkeypatch, barrier: str
):
    state, candidate, leaf, proposed, _store = _prepared(vault)

    def interrupt(name):
        if name == barrier:
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", interrupt)
    with pytest.raises(curation.CurationFault, match=barrier):
        _apply(vault, proposed)
    before = _files(vault)
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)
    assert _files(vault) == before
    assert state["candidates"][0]["leaves"][0]["outcome"] == "uncertain"

    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    if barrier == "after-leaf-witness":
        monkeypatch.setattr(curation, "_dispatch_step", lambda *a, **kw: pytest.fail("reexecuted"))
    curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    reconciled = reconcile_curation_leaf(vault, state, candidate, leaf)
    assert reconciled["candidates"][0]["leaves"][0]["outcome"] == "committed"


def test_failed_receipt_is_not_proof_of_noncommit(vault: Path, monkeypatch):
    state, candidate, leaf, proposed, store = _prepared(vault)

    def fail(*args, **kwargs):
        raise OSError("interrupted leaf")

    monkeypatch.setattr(curation, "_dispatch_step", fail)
    with pytest.raises(curation.CurationError, match="CURATION_RETRYABLE_FAILURE"):
        _apply(vault, proposed)
    assert store.reconstruct(proposed["run_id"])["receipts"][0]["outcome"] == "failed"
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)
    with pytest.raises(model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        model.mark_attempt_started(state, candidate, leaf)


def test_older_retryable_receipt_cannot_release_a_newer_uncertain_attempt(vault: Path, monkeypatch):
    state, candidate, leaf, proposed, store = _prepared(vault)

    def interrupted(*args, **kwargs):
        raise OSError("first attempt did not reach its writer")

    monkeypatch.setattr(curation, "_dispatch_step", interrupted)
    with pytest.raises(curation.CurationError, match="CURATION_RETRYABLE_FAILURE"):
        _apply(vault, proposed)

    def second_attempt_started(name):
        if name == "after-prepared-state":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", second_attempt_started)
    with pytest.raises(curation.CurationFault, match="after-prepared-state"):
        curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    receipts = store.reconstruct(proposed["run_id"])["receipts"]
    assert len(receipts) == 1 and receipts[0]["retryable"] is True
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)
    with pytest.raises(model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        model.mark_attempt_started(state, candidate, leaf)


@pytest.mark.parametrize("artifact", ["plan", "identity", "approval", "receipt", "witness"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_authoritative_evidence_cannot_confirm_commit(
    vault: Path, artifact: str, damage: str
):
    state, candidate, leaf, proposed, store = _prepared(vault)
    _apply(vault, proposed)
    run = proposed["run_id"]
    paths = {
        "plan": store.plan_path(run),
        "identity": store.identity_path(run),
        "approval": store.approval_path(run),
        "receipt": next(store.receipts_dir(run).glob("*/*.json")),
        "witness": next(store.evidence_dir(run).glob("*.json")),
    }
    if damage == "missing":
        paths[artifact].unlink()
    else:
        paths[artifact].write_text('{"version":false}', encoding="utf-8")
    before = _files(vault)
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)
    assert _files(vault) == before


def test_changed_postimage_never_becomes_a_safe_retry(vault: Path):
    state, candidate, leaf, proposed, _store = _prepared(vault)
    result = _apply(vault, proposed)
    destination = vault / result["step"]["path"]
    destination.write_text(destination.read_text() + "\nLater independent change.\n")
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)
    assert state["candidates"][0]["leaves"][0]["outcome"] == "uncertain"


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_id", "0" * 64),
        ("plan_fingerprint", "0" * 64),
        ("ordinal", True),
        ("step_id", "another-step"),
        ("operation_id", "0" * 64),
    ],
)
def test_receipt_cannot_be_rebound_to_another_leaf(vault: Path, field: str, value):
    state, candidate, leaf, proposed, _store = _prepared(vault)
    _apply(vault, proposed)
    state["candidates"][0]["leaves"][0]["binding"][field] = value
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_"):
        reconcile_curation_leaf(vault, state, candidate, leaf)


def test_witness_through_symlink_is_not_accepted(vault: Path, tmp_path: Path):
    state, candidate, leaf, proposed, store = _prepared(vault)
    _apply(vault, proposed)
    witness = next(store.evidence_dir(proposed["run_id"]).glob("*.json"))
    external = tmp_path / "external-witness.json"
    external.write_bytes(witness.read_bytes())
    witness.unlink()
    witness.symlink_to(external)
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)


def test_projection_state_does_not_substitute_for_a_receipt(vault: Path):
    state, candidate, leaf, proposed, store = _prepared(vault)
    store.write_state(
        proposed["run_id"], {"phase": "completed", "committed_steps": ["save-observation"]}
    )
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)


def test_receipt_result_must_match_atomic_witness(vault: Path):
    state, candidate, leaf, proposed, store = _prepared(vault)
    _apply(vault, proposed)
    path = next(store.receipts_dir(proposed["run_id"]).glob("*/*.json"))
    receipt = json.loads(path.read_text())
    receipt["result_digest"] = "0" * 64
    path.write_text(json.dumps(receipt))
    from exomem.episode_reconciliation import reconcile_curation_leaf

    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        reconcile_curation_leaf(vault, state, candidate, leaf)


def test_evidence_read_waits_for_existing_vault_consistency_boundary(vault: Path, monkeypatch):
    state, candidate, leaf, proposed, _store = _prepared(vault)
    _apply(vault, proposed)
    from exomem.episode_reconciliation import reconcile_curation_leaf
    from exomem.writer_lease import active_manager

    started, evidence_read = threading.Event(), threading.Event()
    original_load = curation.CurationStore.load_plan

    def observe_read(self, run):
        evidence_read.set()
        return original_load(self, run)

    monkeypatch.setattr(curation.CurationStore, "load_plan", observe_read)

    def reconcile():
        started.set()
        return reconcile_curation_leaf(vault, state, candidate, leaf)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with active_manager().consistency_guard(vault):
            future = pool.submit(reconcile)
            assert started.wait(5)
            assert not evidence_read.wait(0.2)
        result = future.result(timeout=10)
    assert evidence_read.is_set()
    assert result["candidates"][0]["leaves"][0]["outcome"] == "committed"
