from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import activation_manifest, relation_review, semantic_contract, vault

_ID = "00000000-0000-4000-8000-000000000061"
_OTHER_ID = "00000000-0000-4000-8000-000000000062"
_TRANSITION_AB = "00000000-0000-4000-8000-0000000000ab"
_TRANSITION_BC = "00000000-0000-4000-8000-0000000000bc"
_TRANSITION_CB = "00000000-0000-4000-8000-0000000000cb"
_PAGE = "Knowledge Base/Notes/Insights/lifecycle.md"
_OTHER_PAGE = "Knowledge Base/Notes/Insights/lifecycle-moved.md"


def _source(body: str, *, page_id: str | None = _ID) -> str:
    identity = f"exomem_id: {page_id}\n" if page_id is not None else ""
    return (
        "---\n"
        "title: Lifecycle\n"
        "type: insight\n"
        "status: active\n"
        f"{identity}"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n"
    )


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _binding(path: str, source: str) -> relation_review.LifecyclePrimaryBinding:
    return relation_review.LifecyclePrimaryBinding(
        path=path,
        source_hash=vault.content_hash(source),
        review_fingerprint=semantic_contract.review_content_fingerprint(_ID, source),
    )


def _decision(source: str, *, reason: str = "No honest relation exists"):
    return relation_review.build_lifecycle_decision(
        page_identity=_ID,
        after_fingerprint=semantic_contract.review_content_fingerprint(_ID, source),
        reason=reason,
    )


def _prepared(
    before_source: str,
    after_source: str,
    *,
    transition_id: str,
    decision,
    before_path: str = _PAGE,
    after_path: str = _PAGE,
    token: str = "bounded-transition-token",
    carried_from=None,
):
    return relation_review.build_lifecycle_prepared_transition(
        transition_id=transition_id,
        operation="edit",
        page_identity=_ID,
        before_path=before_path,
        before_source_hash=vault.content_hash(before_source),
        after_path=after_path,
        after_source_hash=vault.content_hash(after_source),
        after_fingerprint=semantic_contract.review_content_fingerprint(_ID, after_source),
        decision=decision,
        transition_token=token,
        auxiliary_hash=hashlib.sha256(b"auxiliaries").hexdigest(),
        carried_from=carried_from,
    )


def _apply_plan(root: Path, plan: relation_review.LifecycleTransitionPlan) -> None:
    vault.batch_atomic_write(
        plan.writes,
        vault_root=root,
        required_guards=plan.required_guards,
    )


def test_canonical_decision_and_prepared_round_trip_with_direct_current_lookup(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    page = _write(tmp_path, _PAGE, source_a)
    decision = _decision(source_b)
    prepared = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision,
    )

    plan = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision,
        prepared=prepared,
        current=_binding(_PAGE, source_a),
    )
    assert plan.state == "new"
    assert [write.path for write in plan.writes] == [
        relation_review.lifecycle_decision_path(
            tmp_path, _ID, decision.after_fingerprint
        ),
        relation_review.lifecycle_prepared_path(tmp_path, _ID),
    ]
    _apply_plan(tmp_path, plan)

    assert relation_review.load_lifecycle_decision(
        tmp_path, _ID, decision.after_fingerprint
    ) == decision
    assert relation_review.load_lifecycle_prepared(tmp_path, _ID) == prepared
    assert page.read_text(encoding="utf-8") == source_a

    corpus_a = semantic_contract.build_corpus_context(tmp_path)
    state_a = corpus_a.pages[_PAGE]
    assert relation_review.load_relation_review(tmp_path, state_a, corpus=corpus_a) is None

    page.write_text(source_b, encoding="utf-8")
    corpus_b = semantic_contract.build_corpus_context(tmp_path)
    state_b = corpus_b.pages[_PAGE]
    current = relation_review.load_relation_review(tmp_path, state_b, corpus=corpus_b)
    assert current is not None
    assert current.kind == "reviewed_none"
    assert current.reference == decision.reference


def test_pending_retry_committed_replay_pending_refusal_and_stale_detection(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    source_c = _source("C")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    initial = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_ab,
        current=_binding(_PAGE, source_a),
    )
    _apply_plan(tmp_path, initial)

    exact_pending = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_ab,
        current=_binding(_PAGE, source_a),
    )
    assert exact_pending.state == "pending_retry"
    assert exact_pending.writes == ()

    decision_c = _decision(source_c, reason="C has no honest relation")
    prepared_ac = _prepared(
        source_a,
        source_c,
        transition_id=_TRANSITION_BC,
        decision=decision_c,
    )
    with pytest.raises(relation_review.RelationReviewError) as pending:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_c,
            prepared=prepared_ac,
            current=_binding(_PAGE, source_a),
        )
    assert pending.value.code == "LIFECYCLE_TRANSITION_PENDING"

    with pytest.raises(relation_review.RelationReviewError) as stale:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_c),
        )
    assert stale.value.code == "LIFECYCLE_TRANSITION_STALE"

    exact_committed = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_ab,
        current=_binding(_PAGE, source_b),
    )
    assert exact_committed.state == "committed_replay"
    assert exact_committed.writes == ()


def test_committed_slot_replacement_and_exact_revert_reuse_decision(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    source_c = _source("C")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    _apply_plan(
        tmp_path,
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_a),
        ),
    )
    decision_path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision_b.after_fingerprint
    )
    decision_bytes = decision_path.read_bytes()

    decision_c = _decision(source_c, reason="C has no honest relation")
    prepared_bc = _prepared(
        source_b,
        source_c,
        transition_id=_TRANSITION_BC,
        decision=decision_c,
    )
    plan_bc = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_c,
        prepared=prepared_bc,
        current=_binding(_PAGE, source_b),
    )
    assert plan_bc.state == "replace_committed"
    _apply_plan(tmp_path, plan_bc)

    prepared_cb = _prepared(
        source_c,
        source_b,
        transition_id=_TRANSITION_CB,
        decision=decision_b,
    )
    plan_cb = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_cb,
        current=_binding(_PAGE, source_c),
    )
    assert plan_cb.state == "replace_committed"
    _apply_plan(tmp_path, plan_cb)

    assert decision_path.read_bytes() == decision_bytes
    assert relation_review.load_lifecycle_prepared(tmp_path, _ID) == prepared_cb
    assert len({_TRANSITION_AB, _TRANSITION_BC, _TRANSITION_CB}) == 3


def test_decision_reason_and_pending_token_drift_fail_before_mutation(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    _apply_plan(
        tmp_path,
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_a),
        ),
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    }

    changed_reason = _decision(source_b, reason="A different judgment")
    with pytest.raises(relation_review.RelationReviewError) as collision:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=changed_reason,
            prepared=_prepared(
                source_a,
                source_b,
                transition_id=_TRANSITION_AB,
                decision=changed_reason,
            ),
            current=_binding(_PAGE, source_a),
        )
    assert collision.value.code == "RELATION_REVIEW_DECISION_COLLISION"

    token_drift = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
        token="changed-token",
    )
    with pytest.raises(relation_review.RelationReviewError) as mismatch:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=token_drift,
            current=_binding(_PAGE, source_a),
        )
    assert mismatch.value.code == "LIFECYCLE_TRANSITION_MISMATCH"
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    } == before


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: {**value, "extra": True}, "RELATION_REVIEW_INVALID_SCHEMA"),
        (
            lambda value: {key: item for key, item in value.items() if key != "reason"},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            lambda value: {**value, "schema_version": True},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            lambda value: {**value, "schema_version": 99},
            "RELATION_REVIEW_UNSUPPORTED_VERSION",
        ),
        (
            lambda value: {**value, "decision_hash": "A" * 64},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (lambda value: {**value, "reason": "  padded  "}, "RELATION_REVIEW_INVALID_SCHEMA"),
    ],
)
def test_lifecycle_decision_schema_is_strict(
    tmp_path: Path, mutate, code: str
) -> None:
    decision = _decision(_source("B"))
    path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(mutate(decision.storage_dict())), encoding="utf-8")

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert exc.value.code == code


def test_lifecycle_bounds_alias_symlink_and_history_limit_are_closed(
    tmp_path: Path,
) -> None:
    decision = _decision(_source("B"))
    path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{" + b"x" * (16 * 1024))
    with pytest.raises(relation_review.RelationReviewError) as too_large:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert too_large.value.code == "RELATION_REVIEW_TOO_LARGE"

    path.unlink()
    alias = path.with_name(f"{decision.after_fingerprint.upper()}.JSON")
    alias.write_text("{}", encoding="utf-8")
    with pytest.raises(relation_review.RelationReviewError) as alias_error:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert alias_error.value.code == "RELATION_REVIEW_ALIAS"

    alias.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(relation_review.RelationReviewError) as unsafe:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert unsafe.value.code == "RELATION_REVIEW_UNSAFE_FILE"
    path.unlink()

    for index in range(257):
        fingerprint = f"{index:064x}"
        item = relation_review.build_lifecycle_decision(
            page_identity=_ID,
            after_fingerprint=fingerprint,
            reason="Reviewed",
        )
        item_path = relation_review.lifecycle_decision_path(
            tmp_path, _ID, fingerprint
        )
        item_path.write_text(
            relation_review.serialize_lifecycle_decision(item), encoding="utf-8"
        )
    with pytest.raises(relation_review.RelationReviewError) as overflow:
        relation_review.load_lifecycle_decision(tmp_path, _ID, f"{0:064x}")
    assert overflow.value.code == "RELATION_REVIEW_HISTORY_LIMIT"


def test_planning_a_257th_lifecycle_decision_fails_closed(tmp_path: Path) -> None:
    directory = relation_review.lifecycle_prepared_path(tmp_path, _ID).parent
    directory.mkdir(parents=True)
    for index in range(256):
        fingerprint = f"{index:064x}"
        decision = relation_review.build_lifecycle_decision(
            page_identity=_ID,
            after_fingerprint=fingerprint,
            reason="Reviewed",
        )
        relation_review.lifecycle_decision_path(
            tmp_path, _ID, fingerprint
        ).write_text(
            relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
        )

    source_a = _source("A")
    source_b = _source("B")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    with pytest.raises(relation_review.RelationReviewError) as overflow:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_a),
        )
    assert overflow.value.code == "RELATION_REVIEW_HISTORY_LIMIT"


def test_lifecycle_identity_directory_swap_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(_source("B"))
    artifact = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
    )
    directory = artifact.parent
    displaced = directory.with_name(f"{directory.name}-displaced")
    real_open = os.open
    swapped = False

    def swap_before_artifact_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).name == artifact.name:
            swapped = True
            directory.rename(displaced)
            directory.mkdir()
            os.link(displaced / artifact.name, directory / artifact.name)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(relation_review.os, "open", swap_before_artifact_open)

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert exc.value.code == "RELATION_REVIEW_SWAPPED"


def test_duplicate_key_prepared_alias_and_unsafe_identity_directory_are_rejected(
    tmp_path: Path,
) -> None:
    prepared_path = relation_review.lifecycle_prepared_path(tmp_path, _ID)
    prepared_path.parent.mkdir(parents=True)
    prepared_path.write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )
    with pytest.raises(relation_review.RelationReviewError) as duplicate:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)
    assert duplicate.value.code == "RELATION_REVIEW_DUPLICATE_KEY"

    prepared_path.unlink()
    prepared_path.with_name("PREPARED.JSON").write_text("{}", encoding="utf-8")
    with pytest.raises(relation_review.RelationReviewError) as alias:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)
    assert alias.value.code == "RELATION_REVIEW_ALIAS"

    identity_dir = prepared_path.parent
    for child in identity_dir.iterdir():
        child.unlink()
    identity_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        identity_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(relation_review.RelationReviewError) as unsafe:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)
    assert unsafe.value.code == "RELATION_REVIEW_DIRECTORY_UNSAFE"


def test_lifecycle_loader_prefers_decision_then_current_creation_receipt_and_never_legacy(
    tmp_path: Path,
) -> None:
    source = _source("Reviewed")
    _write(tmp_path, _PAGE, source)
    decision = _decision(source, reason="Lifecycle decision wins")
    decision_path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text(
        relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
    )
    legacy_receipt = relation_review.review_artifact_path(tmp_path, _ID)
    legacy_receipt.parent.mkdir(parents=True, exist_ok=True)
    legacy_receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "reviewed_none",
                "page_identity": _ID,
                "page_path_at_review": _PAGE,
                "content_fingerprint": decision.after_fingerprint,
                "draft_hash": "a" * 64,
                "auxiliary_hash": "b" * 64,
                "reason": "Creation decision",
            }
        ),
        encoding="utf-8",
    )
    corpus = semantic_contract.build_corpus_context(tmp_path)
    page = corpus.pages[_PAGE]
    loaded = relation_review.load_relation_review(tmp_path, page, corpus=corpus)
    assert loaded is not None
    assert loaded.reference == decision.reference
    assert loaded.reason == "Lifecycle decision wins"

    legacy_source = _source("Legacy", page_id=None)
    legacy_path = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/legacy.md",
        legacy_source,
    )
    legacy_corpus = semantic_contract.build_corpus_context(tmp_path)
    legacy_state = legacy_corpus.pages[legacy_path.relative_to(tmp_path).as_posix()]
    assert legacy_state.identity_kind == "path"
    assert (
        relation_review.load_relation_review(
            tmp_path, legacy_state, corpus=legacy_corpus
        )
        is None
    )
    with pytest.raises(relation_review.RelationReviewError) as invalid:
        relation_review.build_lifecycle_decision(
            page_identity=legacy_state.identity,
            after_fingerprint="c" * 64,
            reason="must backfill first",
        )
    assert invalid.value.code == "RELATION_REVIEW_INVALID_ID"


@pytest.mark.parametrize("malformed", [False, True])
def test_lifecycle_state_reserves_uuid_against_new_6d_creation(
    tmp_path: Path, malformed: bool
) -> None:
    source = _source("New creation")
    fingerprint = semantic_contract.review_content_fingerprint(_ID, source)
    if malformed:
        path = relation_review.lifecycle_prepared_path(tmp_path, _ID)
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")
    else:
        decision = relation_review.build_lifecycle_decision(
            page_identity=_ID,
            after_fingerprint=fingerprint,
            reason="Reserved historical identity",
        )
        path = relation_review.lifecycle_decision_path(tmp_path, _ID, fingerprint)
        path.parent.mkdir(parents=True)
        path.write_text(
            relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
        )

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.validate_creation_draft(
            tmp_path,
            path=_PAGE,
            source=source,
            draft_id=_ID,
            operation="create",
        )
    assert exc.value.code == "DRAFT_ID_IN_USE"
    assert not (tmp_path / _PAGE).exists()


def test_malformed_lifecycle_state_does_not_break_exact_existing_6d_replay(
    tmp_path: Path,
) -> None:
    source = _source("Committed creation")
    committed = relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE,
        source=source,
        draft_id=_ID,
        operation="create",
    )
    malformed = relation_review.lifecycle_prepared_path(tmp_path, _ID)
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not-json", encoding="utf-8")

    with pytest.raises(relation_review.RelationReviewError) as replay:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE,
            source=source,
            draft_id=_ID,
            operation="create",
        )
    assert committed.relation_disposition == "bootstrap"
    assert replay.value.code == "DRAFT_ALREADY_COMMITTED"


def test_qualifying_v2_receipt_is_recovery_only_not_review_truth(tmp_path: Path) -> None:
    source = _source("Qualifying")
    _write(tmp_path, _PAGE, source)
    artifact = relation_review.review_artifact_path(tmp_path, _ID)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "qualifying",
                "page_identity": _ID,
                "page_path_at_review": _PAGE,
                "content_fingerprint": semantic_contract.review_content_fingerprint(
                    _ID, source
                ),
                "draft_hash": "a" * 64,
                "auxiliary_hash": "b" * 64,
                "reason": None,
                "operation": "create",
                "draft_token_hash": hashlib.sha256(b"").hexdigest(),
                "predecessor_path": None,
                "predecessor_content_hash": None,
            }
        ),
        encoding="utf-8",
    )
    corpus = semantic_contract.build_corpus_context(tmp_path)
    assert (
        relation_review.load_relation_review(
            tmp_path, corpus.pages[_PAGE], corpus=corpus
        )
        is None
    )


def test_pure_activation_snapshot_and_boundary_plan_write_nothing_and_do_not_mutate(
    tmp_path: Path,
) -> None:
    candidates = (
        activation_manifest.ActivationCandidate(
            _PAGE, "a" * 64, _ID
        ),
        activation_manifest.ActivationCandidate(
            _OTHER_PAGE, "b" * 64, _OTHER_ID
        ),
    )
    census = activation_manifest.ActivationCensus.from_candidates(candidates)
    before_candidates = census.candidates
    before_files = tuple(tmp_path.rglob("*"))

    snapshot = activation_manifest.snapshot_from_census(census)
    prospective = activation_manifest.plan_activation_boundary(census, manifest=None)
    observed = activation_manifest.plan_activation_boundary(
        census, manifest=snapshot
    )

    assert snapshot == activation_manifest._snapshot(tmp_path, census=census)
    assert prospective.manifest == snapshot
    assert prospective.install_required is True
    assert observed.manifest == snapshot
    assert observed.install_required is False
    assert census.candidates == before_candidates
    assert tuple(tmp_path.rglob("*")) == before_files


def test_prepared_currentness_is_pure_and_never_treats_pending_as_review() -> None:
    source_a = _source("A")
    source_b = _source("B")
    decision_b = _decision(source_b)
    prepared = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    pending = _binding(_PAGE, source_a)
    committed = _binding(_PAGE, source_b)
    stale = replace(pending, source_hash="f" * 64)

    assert relation_review.lifecycle_prepared_state(prepared, pending) == "pending"
    assert relation_review.lifecycle_prepared_state(prepared, committed) == "committed"
    assert relation_review.lifecycle_prepared_state(prepared, stale) == "stale"
    assert not semantic_contract.is_relation_review_current(
        semantic_contract.RelationReviewState(
            "reviewed_none",
            _ID,
            decision_b.after_fingerprint,
            reason=decision_b.reason,
        ),
        semantic_contract.build_page_state(Path("."), _PAGE, source_a),
        semantic_contract.SemanticCorpusContext.from_states(
            Path("."),
            (semantic_contract.build_page_state(Path("."), _PAGE, source_a),),
            registry=semantic_contract.relation_registry.core_registry(),
            identity_census=semantic_contract.StableIdentityCensus(
                (semantic_contract.StableIdentityEntry(_PAGE, _ID),)
            ),
        ),
    )
