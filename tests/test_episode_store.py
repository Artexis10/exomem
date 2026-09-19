from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from exomem import curation
from exomem import episode_model as model


def _store(vault: Path):
    from exomem.episode_store import EpisodeStore

    return EpisodeStore(vault)


def _advance(store, current, action, **args):
    return store.transition(
        current["state"]["episode_id"],
        expected_revision=current["revision"],
        expected_digest=current["journal_digest"],
        action=action,
        args=args,
    )


def test_new_owner_recovers_input_candidates_and_dispositions(vault: Path):
    store = _store(vault)
    initial = store.create("supplier-episode", {"excerpt": "A supplier confirmed a formulation."})
    current = _advance(store, initial, "declare_candidate", key="supplier-fact")
    candidate = current["state"]["candidates"][0]["candidate_id"]
    current = _advance(
        store,
        current,
        "set_disposition",
        candidate=candidate,
        disposition="deferred",
        reason="Provenance still needs resolving.",
    )
    recovered = _store(vault).read(current["state"]["episode_id"])
    assert recovered == current
    assert recovered["state"]["pending_leaf_ids"] == [candidate]
    assert recovered["state"]["input_revisions"][0]["evidence"]["excerpt"].startswith("A supplier")
    recovered["state"]["candidates"].clear()
    assert _store(vault).read(current["state"]["episode_id"]) == current


def test_creation_is_idempotent_without_resetting_later_progress(vault: Path):
    store = _store(vault)
    initial = store.create("episode", {"excerpt": "Keep this."})
    later = _advance(store, initial, "declare_candidate", key="fact")
    assert store.create("episode", {"excerpt": "Keep this."}) == later
    with pytest.raises(model.EpisodeError, match="EPISODE_IDENTITY_COLLISION"):
        store.create("episode", {"excerpt": "Different original input."})
    assert store.read(initial["state"]["episode_id"]) == later


def test_original_evidence_corruption_is_detected_before_any_transition(vault: Path):
    store = _store(vault)
    initial = store.create("episode", {"excerpt": "Original."})
    path = store.path(initial["state"]["episode_id"])
    journal = json.loads(path.read_text())
    journal["input_evidence"]["excerpt"] = "Accidentally changed."
    path.write_text(json.dumps(journal))
    with pytest.raises(model.EpisodeError, match="EPISODE_JOURNAL_INVALID"):
        store.read(initial["state"]["episode_id"])


def test_stale_session_cannot_overwrite_another_revision(vault: Path):
    store = _store(vault)
    first = store.create("episode", {"excerpt": "Original."})
    second = _advance(
        store, first, "append_input_revision", input_evidence={"excerpt": "Correction."}
    )
    with pytest.raises(model.EpisodeError, match="EPISODE_REVISION_CONFLICT"):
        _advance(_store(vault), first, "declare_candidate", key="stale")
    forged = {**second, "journal_digest": "f" * 64}
    with pytest.raises(model.EpisodeError, match="EPISODE_REVISION_CONFLICT"):
        _advance(store, forged, "declare_candidate", key="wrong-seal")
    assert store.read(first["state"]["episode_id"]) == second


def test_uncoordinated_replacement_is_not_overwritten(vault: Path, monkeypatch):
    from exomem import episode_store

    store = _store(vault)
    first = store.create("episode", {"excerpt": "Original."})
    path = store.path(first["state"]["episode_id"])
    replacement = path.read_bytes() + b"\n"
    real_write = episode_store.batch_atomic_write

    def race(writes, **kwargs):
        path.write_bytes(replacement)
        return real_write(writes, **kwargs)

    monkeypatch.setattr(episode_store, "batch_atomic_write", race)
    with pytest.raises(model.EpisodeError, match="EPISODE_STORE_WRITE_FAILED"):
        _advance(store, first, "declare_candidate", key="fact")
    assert path.read_bytes() == replacement


def test_oversized_input_is_refused_before_creating_history(vault: Path):
    store = _store(vault)
    with pytest.raises(model.EpisodeError, match="EPISODE_EVIDENCE_INVALID"):
        store.create("episode", {"excerpt": "x" * 8193})
    assert not store.path(model.episode_id("episode")).exists()


def test_operational_history_does_not_trigger_knowledge_index_fanout(vault: Path, monkeypatch):
    monkeypatch.setattr(
        "exomem.vault.post_commit_batch_fanout",
        lambda *a, **kw: pytest.fail("episode history is not indexed knowledge"),
    )
    store = _store(vault)
    first = store.create("episode", {"excerpt": "Original."})
    _advance(store, first, "declare_candidate", key="fact")


def test_competing_sessions_preserve_exactly_one_revision(vault: Path):
    first = _store(vault).create("concurrent", {"excerpt": "Original."})
    barrier = Barrier(2)

    def change(key):
        barrier.wait(timeout=5)
        try:
            return _advance(_store(vault), first, "declare_candidate", key=key)
        except model.EpisodeError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(change, ["one", "two"]))
    assert results.count("EPISODE_REVISION_CONFLICT") == 1
    recovered = _store(vault).read(first["state"]["episode_id"])
    assert recovered["revision"] == 2
    assert len(recovered["state"]["candidates"]) == 1


@pytest.mark.parametrize(
    "action,args",
    [
        ("execute", {"code": "anything"}),
        ("reconcile_leaf", {"outcome": "committed", "receipt_digest": "a" * 64}),
        ("declare_candidate", {"key": "fact", "complete": True}),
    ],
)
def test_caller_cannot_supply_execution_or_success_state(vault: Path, action, args):
    store = _store(vault)
    first = store.create("episode", {"excerpt": "Original."})
    before = store.path(first["state"]["episode_id"]).read_bytes()
    with pytest.raises(model.EpisodeError, match="EPISODE_TRANSITION_INVALID"):
        _advance(store, first, action, **args)
    assert store.path(first["state"]["episode_id"]).read_bytes() == before


def test_digest_only_input_remains_unavailable_after_restart(vault: Path):
    first = _store(vault).create("lost-input", {"digest": "a" * 64})
    recovered = _store(vault).read(first["state"]["episode_id"])
    assert recovered["state"]["input_revisions"][0]["recovery"] == "unavailable"
    with pytest.raises(model.EpisodeError, match="EPISODE_RECOVERY_UNAVAILABLE"):
        _advance(_store(vault), recovered, "attest_precommit", input_revision=1)


def test_symlinked_episode_storage_cannot_read_or_write_outside(vault: Path, tmp_path: Path):
    store = _store(vault)
    outside = tmp_path / "outside"
    outside.mkdir()
    store.root.parent.mkdir(parents=True, exist_ok=True)
    store.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises((curation.CurationError, model.EpisodeError), match="UNSAFE"):
        store.create("episode", {"excerpt": "Original."})
    with pytest.raises((curation.CurationError, model.EpisodeError), match="UNSAFE"):
        store.read(model.episode_id("episode"))
    assert list(outside.iterdir()) == []


def test_v2_owner_directory_is_hashed_and_cannot_follow_a_symlink(vault: Path, tmp_path: Path):
    store = _store(vault)
    from exomem.episode_store import EpisodeStore

    owner = "client-a"
    bounded = EpisodeStore(vault, owner_audience_id=owner)
    identity = model.episode_id("owner-directory")
    owner_hash = model._hash("exomem-episode-owner-directory-v2", owner)
    assert bounded.path(identity) == bounded.root / owner_hash / f"{identity}.json"

    outside = tmp_path / "outside-owner"
    outside.mkdir()
    bounded.root.mkdir(parents=True, exist_ok=True)
    (bounded.root / owner_hash).symlink_to(outside, target_is_directory=True)
    with pytest.raises((curation.CurationError, model.EpisodeError), match="UNSAFE"):
        bounded.create("owner-directory", {"digest": "a" * 64})
    assert list(outside.iterdir()) == []
    assert store.root == bounded.root


@pytest.mark.parametrize("identity", ["../escape", "A" * 64, "x", "a" * 65])
def test_invalid_storage_identity_refuses(vault: Path, identity):
    with pytest.raises(model.EpisodeError, match="EPISODE_ID_INVALID"):
        _store(vault).read(identity)


def test_journal_bounds_and_malformed_transition_refuse(vault: Path, monkeypatch):
    from exomem import episode_store

    store = _store(vault)
    first = store.create("episode", {"excerpt": "Original."})
    monkeypatch.setattr(episode_store, "MAX_TRANSITIONS", 1)
    second = _advance(store, first, "declare_candidate", key="one")
    with pytest.raises(model.EpisodeError, match="EPISODE_TOO_LARGE"):
        _advance(store, second, "declare_candidate", key="two")
    path = store.path(first["state"]["episode_id"])
    journal = json.loads(path.read_text())
    journal["transitions"][0]["args"]["complete"] = True
    path.write_text(json.dumps(journal))
    with pytest.raises(model.EpisodeError, match="EPISODE_JOURNAL_INVALID"):
        store.read(first["state"]["episode_id"])


def _prepared(store, *, start_attempt=True):
    proposed = curation.propose(
        store.vault_root,
        {
            "version": 1,
            "title": "Episode storage fixture",
            "steps": [
                {
                    "step_id": "save",
                    "kind": "create-note",
                    "args": {
                        "title": "Durable observation",
                        "slug": "durable-observation",
                        "content": "## Observations\n\n- [finding] Keep the supported result. ^result\n",
                        "relation_disposition": "reviewed_none",
                        "relation_review_reason": "No supported relationship in this fixture.",
                    },
                }
            ],
        },
    )
    plan = curation.CurationStore(store.vault_root).load_plan(proposed["run_id"])
    step = plan["steps"][0]
    current = store.create("write-episode", {"excerpt": "Keep the supported result."})
    current = _advance(store, current, "declare_candidate", key="result")
    candidate = current["state"]["candidates"][0]["candidate_id"]
    current = _advance(
        store,
        current,
        "revise_proposal",
        candidate=candidate,
        proposal={
            "route": "focused_note",
            "title": "Durable observation",
            "alternatives": [],
            "evidence": "complete",
            "reason": "Independent durable result.",
            "leaves": [
                {
                    "leaf_key": "save",
                    "effect_revision": 1,
                    "kind": step["kind"],
                    "args": step["args"],
                }
            ],
        },
    )
    leaf = current["state"]["candidates"][0]["leaves"][0]["leaf_id"]
    current = _advance(
        store,
        current,
        "set_disposition",
        candidate=candidate,
        disposition="routed",
        reason="Preserve result.",
    )
    current = _advance(
        store,
        current,
        "bind_curation_leaf",
        candidate=candidate,
        leaf=leaf,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        plan_fingerprint=proposed["plan_fingerprint"],
        ordinal=0,
    )
    current = _advance(store, current, "attest_precommit", input_revision=1)
    if start_attempt:
        current = _advance(store, current, "mark_attempt_started", candidate=candidate, leaf=leaf)
    return current, candidate, leaf, proposed


def _apply(vault, proposed):
    return curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved this fixture's exact plan.",
    )


def test_restart_after_commit_reuses_receipt_without_executing_again(vault: Path, monkeypatch):
    store = _store(vault)
    current, candidate, leaf, proposed = _prepared(store)
    _apply(vault, proposed)
    recovered = _store(vault).read(current["state"]["episode_id"])
    assert recovered["state"]["candidates"][0]["leaves"][0]["outcome"] == "uncertain"
    with pytest.raises(model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        _advance(store, recovered, "mark_attempt_started", candidate=candidate, leaf=leaf)
    monkeypatch.setattr(curation, "_dispatch_step", lambda *a, **kw: pytest.fail("reexecuted"))
    reconciled = _advance(
        store, recovered, "reconcile_curation_leaf", candidate=candidate, leaf=leaf
    )
    complete = _advance(store, reconciled, "attest_postcommit", input_revision=1, leaf_ids=[leaf])
    assert _store(vault).read(current["state"]["episode_id"]) == complete
    assert complete["state"]["complete"]


def test_lost_receipt_keeps_history_but_refuses_fresh_coverage(vault: Path):
    store = _store(vault)
    current, candidate, leaf, proposed = _prepared(store)
    _apply(vault, proposed)
    current = _advance(store, current, "reconcile_curation_leaf", candidate=candidate, leaf=leaf)
    receipts = curation.CurationStore(vault).receipts_dir(proposed["run_id"])
    for receipt in receipts.rglob("*.json"):
        receipt.unlink()
    recovered = store.read(current["state"]["episode_id"])
    assert recovered["state"]["candidates"][0]["leaves"][0]["outcome"] == "committed"
    assert not recovered["state"]["complete"]
    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        _advance(store, recovered, "attest_postcommit", input_revision=1, leaf_ids=[leaf])
    later = _advance(store, recovered, "declare_candidate", key="independent")
    assert len(later["state"]["candidates"]) == 2


def test_reconciliation_retry_returns_already_accepted_outcome(vault: Path, monkeypatch):
    store = _store(vault)
    current, candidate, leaf, proposed = _prepared(store)
    _apply(vault, proposed)
    current = _advance(store, current, "reconcile_curation_leaf", candidate=candidate, leaf=leaf)
    monkeypatch.setattr(
        "exomem.episode_store.reconcile_curation_leaf",
        lambda *a, **kw: pytest.fail("already accepted commit should be reused"),
    )
    assert (
        _advance(store, current, "reconcile_curation_leaf", candidate=candidate, leaf=leaf)
        == current
    )


def test_changed_postimage_never_reverses_commit_or_reexecutes_on_read(vault: Path, monkeypatch):
    store = _store(vault)
    current, candidate, leaf, proposed = _prepared(store)
    _apply(vault, proposed)
    current = _advance(store, current, "reconcile_curation_leaf", candidate=candidate, leaf=leaf)
    current = _advance(store, current, "attest_postcommit", input_revision=1, leaf_ids=[leaf])
    notes = list((vault / "Knowledge Base" / "Notes").rglob("durable-observation.md"))
    assert len(notes) == 1
    notes[0].write_text(notes[0].read_text() + "\nA later independently edited result.\n")
    with pytest.raises(model.EpisodeError, match="EPISODE_OUTCOME_UNCERTAIN"):
        _advance(store, current, "attest_postcommit", input_revision=1, leaf_ids=[leaf])
    monkeypatch.setattr(
        "exomem.episode_store.reconcile_curation_leaf",
        lambda *a, **kw: pytest.fail("replay consulted mutable live evidence"),
    )
    recovered = _store(vault).read(current["state"]["episode_id"])
    assert recovered == current
    assert recovered["coverage_current"] == "unchecked"
    with pytest.raises(model.EpisodeError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        _advance(store, recovered, "mark_attempt_started", candidate=candidate, leaf=leaf)


def test_failed_attempt_record_write_does_not_start_executor(vault: Path, monkeypatch):
    store = _store(vault)
    current, candidate, leaf, _proposed = _prepared(store, start_attempt=False)
    path = store.path(current["state"]["episode_id"])
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("exomem.episode_store.batch_atomic_write", fail)
    monkeypatch.setattr(curation, "_dispatch_step", lambda *a, **kw: pytest.fail("executed"))
    with pytest.raises(model.EpisodeError, match="EPISODE_STORE_WRITE_FAILED"):
        _advance(store, current, "mark_attempt_started", candidate=candidate, leaf=leaf)
    assert path.read_bytes() == before
    recovered = store.read(current["state"]["episode_id"])
    assert recovered["state"]["candidates"][0]["leaves"][0]["attempts"] == 0
