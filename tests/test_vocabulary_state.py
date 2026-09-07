import json

import pytest

from exomem import mutation_terminal, review_state, vocabulary_state
from exomem.vocabulary_workflow import Evidence, make_item


def item(version="v1", registry="r1", target="entity:a", question=None):
    return make_item(
        family="relation-type/v1",
        signal="generic-pair",
        targets={target: "t1", "entity:b": "t2"},
        evidence=[Evidence("source:1", version, "origin:1")],
        registry_hashes={"relations": registry},
        projection_status="current",
        question=question,
    )


def decision(current, outcome="defer"):
    choice = None
    if outcome == "propose-new":
        choice = {
            "canonical": "vault.supplies",
            "definition": {
                "parent": "relates_to",
                "description": "An organization supplies goods to another.",
                "direction": "directed",
            },
        }
    return {
        "item_ref": current.ref,
        "fingerprint": current.fingerprint,
        "family": current.family,
        "choice": choice,
        "registry_hashes": dict(current.registry_hashes),
        "target_versions": dict(current.target_versions),
        "outcome": outcome,
        "rationale": "Keep this for a later evidence review.",
    }


def terminal(request="operation-1", receipt="receipt-1"):
    return mutation_terminal.committed_terminal(
        {"path": "Knowledge Base/Entities/Organizations/example.md"},
        request_id=request,
        receipt_id=receipt,
        idempotency_key="same-operation",
    )


def test_upsert_and_defer_survive_restart_and_registry_refresh(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    current = item()
    assert store.observe(current)["state"] == "pending"
    store.decide(current, decision(current), actor="principal:test")
    restarted = vocabulary_state.VocabularyState(tmp_path)
    assert restarted.observe(item(registry="r2"))["state"] == "deferred"
    assert restarted.get(current.ref)["decision"]["actor"] == "principal:test"
    assert restarted.observe(item(version="v2"))["state"] == "pending"
    assert len(review_state.ReviewStateStore(tmp_path).load()["vocabulary"]["decisions"]) == 1


def test_logical_identity_keeps_one_ref_while_changed_targets_refresh_currency():
    original = make_item(
        family="entity-instance/v1",
        signal="entity-lifecycle",
        targets={"entity:provisional": "v1"},
        evidence=[Evidence("source:1", "v1", "origin:1")],
        registry_hashes={"entity_types": "r1"},
        projection_status="current",
        logical_identity="review:entity-lifecycle:stable",
    )
    hydrated = make_item(
        family="entity-instance/v1",
        signal="entity-lifecycle",
        targets={"exomem://memory/entity-id": "v2"},
        evidence=[Evidence("source:1", "v1", "origin:1")],
        registry_hashes={"entity_types": "r1"},
        projection_status="current",
        logical_identity="review:entity-lifecycle:stable",
    )
    assert hydrated.ref == original.ref
    assert hydrated.fingerprint != original.fingerprint
    assert hydrated.to_dict()["logical_identity"] == "review:entity-lifecycle:stable"


def test_stale_decision_does_not_change_any_review_state(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    current = item()
    store.observe(current)
    before = review_state.state_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        store.decide(item(registry="r2"), decision(current), actor="principal:test")
    assert review_state.state_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("transition", ["approval", "begin", "receipt"])
def test_refreshed_item_cannot_launder_a_stale_decision_into_application(tmp_path, transition):
    store, original = (
        applying(tmp_path)
        if transition == "receipt"
        else (vocabulary_state.VocabularyState(tmp_path), item())
    )
    if transition != "receipt":
        store.observe(original)
        store.decide(original, decision(original, "propose-new"), actor="principal:test")
    refreshed = item(registry="unrelated-r3")
    store.observe(refreshed)
    before = review_state.state_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        if transition == "approval":
            store.await_approval(refreshed)
        elif transition == "begin":
            store.begin_application(
                refreshed,
                request_id="operation-1",
                expected_results={"registry:relations": "r3"},
                choice=decision(refreshed, "propose-new")["choice"],
            )
        else:
            store.record_committed_receipt(
                refreshed,
                terminal(),
                resulting_versions={"registry:relations": "r2"},
                applied_choice=decision(refreshed, "propose-new")["choice"],
            )
    assert review_state.state_path(tmp_path).read_bytes() == before


def test_registry_stale_resolution_is_history_not_a_current_semantic_answer(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    original = item()
    store.observe(original)
    store.decide(original, decision(original, "generic"), actor="principal:test")
    refreshed = store.observe(item(registry="r2"))
    assert refreshed["state"] == "pending"
    assert refreshed["decision_currency"] == "refresh_required"
    assert refreshed["decision"]["state"] == "resolved_without_mutation"


def test_proposal_and_approval_do_not_claim_application(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    current = item()
    store.observe(current)
    assert (
        store.decide(current, decision(current, "propose-new"), actor="principal:test")["state"]
        == "proposed"
    )
    assert store.await_approval(current)["state"] == "awaiting_approval"
    result = store.begin_application(
        current,
        request_id="operation-1",
        expected_results={"registry:relations": "r2"},
        choice=decision(current, "propose-new")["choice"],
    )
    assert result["state"] == "applying"
    assert not result["receipts"]


def applying(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    current = item()
    store.observe(current)
    store.decide(current, decision(current, "propose-new"), actor="principal:test")
    store.begin_application(
        current,
        request_id="operation-1",
        expected_results={"registry:relations": "r2"},
        choice=decision(current, "propose-new")["choice"],
    )
    return store, current


def test_completion_binds_canonical_receipt_and_resulting_state_once(tmp_path):
    store, current = applying(tmp_path)
    completed = store.record_committed_receipt(
        current,
        terminal(),
        resulting_versions={"registry:relations": "r2"},
        applied_choice=decision(current, "propose-new")["choice"],
    )
    assert completed["state"] == "applied"
    assert completed["receipts"] == ["receipt-1"]
    before = review_state.state_path(tmp_path).read_bytes()
    assert (
        store.record_committed_receipt(
            current,
            terminal(),
            resulting_versions={"registry:relations": "r2"},
            applied_choice=decision(current, "propose-new")["choice"],
        )
        == completed
    )
    assert review_state.state_path(tmp_path).read_bytes() == before
    assert vocabulary_state.VocabularyState(tmp_path).get(current.ref)["state"] == "applied"


@pytest.mark.parametrize("observe_before_receipt", [True, False])
def test_result_projection_and_receipt_can_arrive_in_either_order(tmp_path, observe_before_receipt):
    store, original = applying(tmp_path)
    resulting = item(registry="r2")
    if observe_before_receipt:
        store.observe(resulting)
    completed = store.record_committed_receipt(
        resulting if observe_before_receipt else original,
        terminal(),
        resulting_versions={"registry:relations": "r2"},
        applied_choice=decision(original, "propose-new")["choice"],
    )
    assert completed["state"] == "applied"
    assert store.observe(resulting)["state"] == "applied"
    restarted = vocabulary_state.VocabularyState(tmp_path)
    assert restarted.get(original.ref)["decision_currency"] == "current"
    assert restarted.get(original.ref)["receipts"] == ["receipt-1"]


def test_expected_target_change_can_reconcile_after_projection(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    original = item()
    store.observe(original)
    store.decide(original, decision(original, "propose-new"), actor="principal:test")
    store.begin_application(
        original,
        request_id="operation-1",
        expected_results={"entity:a": "changed"},
        choice=decision(original, "propose-new")["choice"],
    )
    resulting = make_item(
        family=original.family,
        signal=original.signal,
        targets=dict(original.target_versions) | {"entity:a": "changed"},
        evidence=original.evidence,
        registry_hashes=dict(original.registry_hashes),
        projection_status="current",
    )
    store.observe(resulting)
    completed = store.record_committed_receipt(
        resulting,
        terminal(),
        resulting_versions={"entity:a": "changed"},
        applied_choice=decision(original, "propose-new")["choice"],
    )
    assert completed["state"] == "applied"
    assert completed["decision"]["fingerprint"] == original.fingerprint


def test_question_bound_target_change_can_reconcile_after_projection(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    original = item(question="Does this relation need a distinct meaning?")
    store.observe(original)
    store.decide(original, decision(original, "propose-new"), actor="principal:test")
    store.begin_application(
        original,
        request_id="operation-1",
        expected_results={"entity:a": "changed"},
        choice=decision(original, "propose-new")["choice"],
    )
    resulting = make_item(
        family=original.family,
        signal=original.signal,
        targets=dict(original.target_versions) | {"entity:a": "changed"},
        evidence=original.evidence,
        registry_hashes=dict(original.registry_hashes),
        projection_status="current",
        question=original.question,
    )
    store.observe(resulting)
    completed = store.record_committed_receipt(
        resulting,
        terminal(),
        resulting_versions={"entity:a": "changed"},
        applied_choice=decision(original, "propose-new")["choice"],
    )
    assert completed["state"] == "applied"
    assert completed["decision"]["fingerprint"] == original.fingerprint


def test_changed_evidence_preserves_applied_history_until_a_fresh_decision(tmp_path):
    store, original = applying(tmp_path)
    store.record_committed_receipt(
        original,
        terminal(),
        resulting_versions={"registry:relations": "r2"},
        applied_choice=decision(original, "propose-new")["choice"],
    )
    changed = item(version="v2", registry="r2")

    historical = store.observe(changed)

    assert historical["state"] == "pending"
    assert historical["decision_currency"] == "refresh_required"
    assert historical["decision"]["state"] == "applied"
    assert historical["receipts"] == ["receipt-1"]
    assert store.decide(changed, decision(changed, "propose-new"), actor="principal:test")[
        "state"
    ] == "proposed"


def test_changed_evidence_cannot_overwrite_an_uncertain_application(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    current = item()
    store.observe(current)
    store.decide(current, decision(current, "propose-new"), actor="principal:test")
    store.begin_application(
        current,
        request_id="operation-1",
        operation_id="operation-id",
        expected_results={"registry:relations": "r2"},
        choice=decision(current, "propose-new")["choice"],
    )
    store.record_application_uncertain(current, operation_id="operation-id")
    changed = item(version="v2")

    historical = store.observe(changed)

    assert historical["state"] == "applying"
    assert historical["decision_currency"] == "refresh_required"
    assert historical["decision"]["application"]["state"] == "uncertain"
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        store.decide(changed, decision(changed, "propose-new"), actor="principal:test")


def test_ambiguous_result_currency_remains_historical(tmp_path):
    def snapshot(version: str):
        return make_item(
            family="relation-type/v1",
            signal="generic-pair",
            targets={"entity:a": version, "entity:b": "t2"},
            evidence=[Evidence("source:1", "v1", "origin:1")],
            registry_hashes={"relations": "r1"},
            projection_status="current",
        )

    store = vocabulary_state.VocabularyState(tmp_path)
    first = snapshot("t1")
    store.observe(first)
    store.decide(first, decision(first, "propose-new"), actor="principal:test")
    store.begin_application(
        first,
        request_id="operation-1",
        expected_results={"entity:a": "t3"},
        choice=decision(first, "propose-new")["choice"],
    )
    store.record_committed_receipt(
        first,
        terminal(receipt="receipt-1"),
        resulting_versions={"entity:a": "t3"},
        applied_choice=decision(first, "propose-new")["choice"],
    )
    second = snapshot("t2")
    store.observe(second)
    store.decide(second, decision(second, "propose-new"), actor="principal:test")
    store.begin_application(
        second,
        request_id="operation-2",
        expected_results={"entity:a": "t3"},
        choice=decision(second, "propose-new")["choice"],
    )
    store.record_committed_receipt(
        second,
        terminal(request="operation-2", receipt="receipt-2"),
        resulting_versions={"entity:a": "t3"},
        applied_choice=decision(second, "propose-new")["choice"],
    )

    historical = store.observe(snapshot("t3"))

    assert historical["state"] == "pending"
    assert historical["decision_currency"] == "refresh_required"
    assert historical["receipts"] == ["receipt-2"]


def test_unrelated_evidence_cannot_be_resolved_by_an_old_application(tmp_path):
    store, original = applying(tmp_path)
    changed = item(version="new-source-content", registry="r2")
    store.observe(changed)
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        store.record_committed_receipt(
            changed,
            terminal(),
            resulting_versions={"registry:relations": "r2"},
            applied_choice=decision(original, "propose-new")["choice"],
        )


@pytest.mark.parametrize(
    "result,versions",
    [
        (terminal(request="other-operation"), {"registry:relations": "r2"}),
        (terminal(), {"registry:relations": "not-yet-registered"}),
        ({"state": "committed", "receipt_id": "forged"}, {"registry:relations": "r2"}),
        (terminal() | {"state": "pending"}, {"registry:relations": "r2"}),
        (terminal() | {"receipt_id": None}, {"registry:relations": "r2"}),
    ],
)
def test_unproven_completion_remains_recoverable(tmp_path, result, versions):
    store, current = applying(tmp_path)
    before = review_state.state_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        store.record_committed_receipt(
            current,
            result,
            resulting_versions=versions,
            applied_choice=decision(current, "propose-new")["choice"],
        )
    assert review_state.state_path(tmp_path).read_bytes() == before
    assert store.get(current.ref)["state"] == "applying"


def test_applying_identity_cannot_be_rebound_or_decided_away(tmp_path):
    store, current = applying(tmp_path)
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        store.begin_application(
            current,
            request_id="operation-2",
            expected_results={"registry:relations": "r2"},
            choice=decision(current, "propose-new")["choice"],
        )
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        store.decide(current, decision(current), actor="principal:test")


def test_application_and_receipt_must_name_the_reviewed_semantic_choice(tmp_path):
    store, current = applying(tmp_path)
    wrong = decision(current, "propose-new")["choice"] | {"canonical": "vault.supports"}
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        store.begin_application(
            current,
            request_id="operation-1",
            expected_results={"registry:relations": "r2"},
            choice=wrong,
        )
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        store.record_committed_receipt(
            current,
            terminal(),
            resulting_versions={"registry:relations": "r2"},
            applied_choice=wrong,
        )
    assert store.get(current.ref)["state"] == "applying"


def test_vocabulary_disposition_never_edits_canonical_integrity_decisions(tmp_path):
    review = review_state.ReviewStateStore(tmp_path)
    review.apply(
        "a" * 24, "b" * 24, action="snooze", until="2099-01-01", family="entity_type_unregistered"
    )
    before = review.load()["records"]
    store = vocabulary_state.VocabularyState(tmp_path)
    current = item()
    store.observe(current)
    store.decide(current, decision(current, "generic"), actor="principal:test")
    assert review.load()["records"] == before


def test_malformed_vocabulary_section_is_not_silently_reset(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    store.observe(item())
    path = review_state.state_path(tmp_path)
    raw = json.loads(path.read_text())
    raw["vocabulary"] = {"items": [], "decisions": {}}
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="REVIEW_STATE_INVALID"):
        store.get(item().ref)


def test_v3_migration_preserves_existing_decisions(tmp_path):
    path = review_state.state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 3, "records": {"old": {"action": "dismiss"}}}))
    vocabulary_state.VocabularyState(tmp_path).observe(item())
    payload = json.loads(path.read_text())
    assert payload["version"] == 4
    assert payload["records"]["old"]["action"] == "dismiss"


def test_queue_is_bounded_with_explicit_continuation(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    for index in range(7):
        store.observe(item(target=f"entity:{index}"))
    first = store.page()
    assert len(first["items"]) == 4
    assert first["continuation"]
    second = store.page(continuation=first["continuation"])
    assert len(second["items"]) == 3
    assert second["continuation"] is None
    assert not ({i["ref"] for i in first["items"]} & {i["ref"] for i in second["items"]})


def test_queue_continuation_refuses_changed_buffered_view(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    observed = [item(target=f"entity:buffered-{index}") for index in range(5)]
    for current in observed:
        store.observe(current)
    first = store.page(limit=1)
    changed = next(current for current in observed if current.ref != first["items"][0]["ref"])
    store.decide(changed, decision(changed), actor="principal:test")
    with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
        store.page(continuation=first["continuation"])


def test_review_page_uses_fresh_point_projection_without_json_enumeration(tmp_path, monkeypatch):
    store = vocabulary_state.VocabularyState(tmp_path)
    for index in range(5):
        store.observe(item(target=f"entity:projection-{index}"))

    def refuse_load(*args, **kwargs):
        raise AssertionError("review page must not enumerate canonical review JSON")

    monkeypatch.setattr(store.store, "load", refuse_load)
    page = store.page(limit=2)

    assert page["state"] == "current"
    assert len(page["items"]) == 2
    assert page["continuation"].startswith("vrp1.")
    assert page["coverage"] == {
        "source": "observed-work-items",
        "mode": "bounded-pass",
        "exhaustive": False,
        "full_corpus_scan": False,
    }



def test_cold_review_page_is_authoritatively_empty_without_a_projection(tmp_path):
    page = vocabulary_state.VocabularyState(tmp_path).page()

    assert page == {
        "state": "current",
        "items": [],
        "continuation": None,
        "coverage": {
            "source": "observed-work-items",
            "mode": "bounded-pass",
            "exhaustive": False,
            "full_corpus_scan": False,
        },
    }

def test_review_page_reports_warming_when_existing_json_has_no_projection(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    store.observe(item())
    from exomem import deferred_index

    deferred_index.store_path(tmp_path).unlink()
    page = vocabulary_state.VocabularyState(tmp_path).page()

    assert page == {
        "state": "warming",
        "reason": "review_projection_rebuild_required",
        "recovery": {"operator_required": True, "command": "exomem maintain --reconcile"},
        "items": [],
        "continuation": None,
        "coverage": {
            "source": "observed-work-items",
            "mode": "bounded-pass",
            "exhaustive": False,
            "full_corpus_scan": False,
        },
    }


def test_rebuild_review_projection_recovers_existing_canonical_items(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    store.observe(item())
    from exomem import deferred_index

    deferred_index.store_path(tmp_path).unlink()
    assert vocabulary_state.VocabularyState(tmp_path).page()["state"] == "warming"

    rebuilt = vocabulary_state.VocabularyState(tmp_path).rebuild_review_projection()

    assert rebuilt["state"] == "current"
    assert rebuilt["rows"] == 1
    assert vocabulary_state.VocabularyState(tmp_path).page()["state"] == "current"


def test_review_page_requires_state_all_for_deferred_history(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    current = item()
    store.observe(current)
    store.decide(current, decision(current), actor="principal:test")

    assert store.page()["items"] == []
    assert [row["ref"] for row in store.page(state="all")["items"]] == [current.ref]


def test_review_history_projects_canonical_applied_application(tmp_path):
    store, current = applying(tmp_path)
    store.record_committed_receipt(
        current,
        terminal(),
        resulting_versions={"registry:relations": "r2"},
        applied_choice=decision(current, "propose-new")["choice"],
    )

    rows = store.page(state="all")["items"]

    assert [(row["ref"], row["state"], row["receipts"]) for row in rows] == [
        (current.ref, "applied", ["receipt-1"])
    ]


def test_review_continuation_is_bound_to_the_request_principal(tmp_path):
    from exomem.governance.principal import RequestPrincipal, request_scope

    store = vocabulary_state.VocabularyState(tmp_path)
    for index in range(3):
        store.observe(item(target=f"entity:principal-{index}"))
    with request_scope(RequestPrincipal("principal:one")):
        continuation = store.page(limit=1)["continuation"]
    with request_scope(RequestPrincipal("principal:two")), pytest.raises(
        ValueError, match="VOCABULARY_CONTINUATION_INVALID"
    ):
        store.page(continuation=continuation)


def test_corrupt_derived_review_row_is_typed_unavailable(tmp_path):
    store = vocabulary_state.VocabularyState(tmp_path)
    store.observe(item())
    from exomem import deferred_index

    conn = deferred_index._connect(tmp_path, create=True)
    try:
        conn.execute("UPDATE vocabulary_review_rows SET view_json = '[]'")
        conn.commit()
    finally:
        conn.close()

    assert store.page()["state"] == "unavailable"
    assert store.page()["reason"] == "review_projection_unavailable"

@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("vocabulary_review_rows", "view_json", "'{'"),
        ("vocabulary_review_meta", "source_dev", "'not-an-integer'"),
    ],
)
def test_corrupt_projection_values_are_typed_unavailable(tmp_path, table, column, value):
    store = vocabulary_state.VocabularyState(tmp_path)
    store.observe(item())
    from exomem import deferred_index

    conn = deferred_index._connect(tmp_path, create=True)
    try:
        conn.execute(f"UPDATE {table} SET {column} = {value}")
        conn.commit()
    finally:
        conn.close()

    assert store.page()["state"] == "unavailable"


@pytest.mark.parametrize(
    ("column", "value"),
    [("visible_refs_json", "'{'"), ("expires_at", "'not-a-number'")],
)
def test_corrupt_stored_continuation_is_typed_unavailable(tmp_path, column, value):
    store = vocabulary_state.VocabularyState(tmp_path)
    for index in range(3):
        store.observe(item(target=f"entity:corrupt-cursor-{index}"))
    continuation = store.page(limit=1)["continuation"]
    from exomem import deferred_index

    conn = deferred_index._connect(tmp_path, create=True)
    try:
        conn.execute(f"UPDATE vocabulary_review_continuations SET {column} = {value}")
        conn.commit()
    finally:
        conn.close()

    assert store.page(continuation=continuation)["state"] == "unavailable"


def test_first_meaning_question_initializes_then_review_is_current(tmp_path):
    from exomem import commands

    path = tmp_path / "Knowledge Base/Notes/first-question.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: note\n---\nA durable fact.\n", encoding="utf-8")

    submitted = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path="Knowledge Base/Notes/first-question.md",
        query="Does this need a more specific meaning?",
        family="relation-type/v1",
    )
    reviewed = commands.op_review_memory(tmp_path, mode="vocabulary")

    assert reviewed["state"] == "current"
    assert [row["ref"] for row in reviewed["items"]] == [submitted["item"]["ref"]]
