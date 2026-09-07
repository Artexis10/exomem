"""Vocabulary work consumes the existing lifecycle evidence without another scan."""

from test_entity_lifecycle_surface import _entity, _hydration, _promotion

import pytest

from exomem import attention, commands
from exomem.vocabulary_state import VocabularyState


@pytest.mark.parametrize("before_observation", [False, True])
@pytest.mark.parametrize("rebuild", [False, True])
def test_originating_family_off_survives_observation_and_rebuild(
    tmp_path, before_observation, rebuild
):
    from exomem import review_state, vocabulary_review
    from exomem.governance.principal import library_scope

    _promotion(tmp_path)
    owner = review_state.ReviewStateStore(tmp_path)
    with library_scope():
        if before_observation:
            owner.set_disposition("entity_recurrence", "off", why="intentional: stop suggestions")
        initial = commands.op_review_memory(
            tmp_path, categories=["entity_recurrence"], state="all", limit=3
        )
        ref = initial["items"][0]["vocabulary"]["ref"]
        if not before_observation:
            owner.set_disposition("entity_recurrence", "off", why="intentional: stop suggestions")
        canonical = owner.load()
        if rebuild:
            assert VocabularyState(tmp_path).rebuild_review_projection()["state"] == "current"
        assert commands.op_review_memory(
            tmp_path, categories=["entity_recurrence"], limit=3
        )["items"] == []
        assert vocabulary_review.review(tmp_path)["items"] == []
        all_items = vocabulary_review.review(tmp_path, state="all")["items"]
        assert [item["ref"] for item in all_items] == [ref]
        assert all_items[0]["family_dispositions"] == {"entity_recurrence": "off"}
        assert owner.load()["vocabulary"]["decisions"] == canonical["vocabulary"]["decisions"]
        owner.set_disposition("entity_recurrence", "normal")
        restored = vocabulary_review.review(tmp_path)["items"]
        assert [item["ref"] for item in restored] == [ref]
        assert restored[0].get("family_dispositions", {}) == {}


def test_ordinary_entity_attention_enters_vocabulary_queue_once(tmp_path, monkeypatch):
    _promotion(tmp_path)
    original = attention.attention
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs.get("categories"))
        return original(*args, **kwargs)

    monkeypatch.setattr(attention, "attention", counted)
    result = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], limit=3)
    assert calls == [["entity_recurrence"]]
    assert len(result["items"]) == 1
    route = result["items"][0]["vocabulary"]
    item = VocabularyState(tmp_path).get(route["ref"])
    assert item["family"] == "entity-instance/v1"
    assert item["logical_identity"] == result["items"][0]["ref"]
    assert item["projection"]["currency"]["candidate_state"] == "promotion"
    assert item["state"] == "pending"
    assert len(item["evidence"]) == 3
    assert item["projection"]["currency"]["review_fingerprint"] == result["items"][0]["fingerprint"]


@pytest.mark.parametrize("action", ["dismiss", "snooze"])
@pytest.mark.parametrize("before_observation", [False, True])
def test_original_item_triage_composes_with_adapted_vocabulary(tmp_path, action, before_observation):
    import datetime as dt

    from exomem import review_state, vocabulary_review
    from exomem.governance.principal import library_scope

    _promotion(tmp_path)
    with library_scope():
        if before_observation:
            row = attention.attention(tmp_path, categories=["entity_recurrence"]).items[0].as_dict()
        else:
            row = commands.op_review_memory(tmp_path, categories=["entity_recurrence"])["items"][0]
        kwargs = {"until": (dt.date.today() + dt.timedelta(days=10)).isoformat()} if action == "snooze" else {}
        commands.op_triage_memory(
            tmp_path, ref=row["ref"], action=action, expected_fingerprint=row["fingerprint"],
            why="intentional: leave identity unpromoted", **kwargs,
        )
        all_report = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], state="all")
        ref = all_report["items"][0]["vocabulary"]["ref"]
        store = VocabularyState(tmp_path)
        assert store.rebuild_review_projection()["state"] == "current"
        assert vocabulary_review.review(tmp_path)["items"] == []
        current = vocabulary_review.review(tmp_path, state="all")["items"][0]
        assert current["ref"] == ref
        assert current["state"] == "pending"
        assert current["origin_review"]["state"] == {"dismiss": "dismissed", "snooze": "snoozed"}[action]
        canonical = review_state.ReviewStateStore(tmp_path).load()
        assert current["decision"] is None
        commands.op_triage_memory(
            tmp_path, ref=row["ref"], action="reopen", expected_fingerprint=row["fingerprint"]
        )
        assert vocabulary_review.review(tmp_path)["items"][0]["ref"] == ref
        assert canonical["vocabulary"]["decisions"] == {}


def test_legacy_origin_binding_requires_context_refresh_then_preserves_dismissal(tmp_path, monkeypatch):
    from exomem import review_state, vocabulary_review
    from exomem.governance.principal import library_scope

    _promotion(tmp_path)
    with library_scope():
        row = commands.op_review_memory(tmp_path, categories=["entity_recurrence"])["items"][0]
        ref = row["vocabulary"]["ref"]
        commands.op_triage_memory(
            tmp_path, ref=row["ref"], action="dismiss", expected_fingerprint=row["fingerprint"]
        )
        owner = review_state.ReviewStateStore(tmp_path)
        canonical = owner.load()
        canonical["vocabulary"]["items"][ref]["projection"]["currency"].pop("review_fingerprint")
        owner._write(canonical)
        assert VocabularyState(tmp_path).rebuild_review_projection()["state"] == "current"

        def no_ledger_read(*_args, **_kwargs):
            raise AssertionError("legacy queue reads must not load the canonical ledger")

        with monkeypatch.context() as patch:
            patch.setattr(review_state.ReviewStateStore, "load", no_ledger_read)
            report = vocabulary_review.review(tmp_path)
            assert report["state"] == "warming"
            assert report["recovery"] == {"tool": "review_item_context", "args": {"ref": ref}}
            legacy = vocabulary_review.review(tmp_path, state="all")["items"][0]
            assert legacy["origin_review"]["state"] == "refresh_required"
        refreshed = vocabulary_review.context(tmp_path, **report["recovery"]["args"])
        assert refreshed["item"]["projection"]["currency"]["review_fingerprint"] == row["fingerprint"]
        assert vocabulary_review.review(tmp_path)["items"] == []
        assert vocabulary_review.review(tmp_path, state="all")["items"][0]["origin_review"]["state"] == "dismissed"


def test_existing_entity_offers_enrichment_and_preserves_provider_binding(tmp_path):
    target = _hydration(tmp_path)
    result = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], limit=3)
    route = result["items"][0]["vocabulary"]
    item = VocabularyState(tmp_path).get(route["ref"])
    assert item["projection"]["currency"]["candidate_state"] == "hydration"
    assert target in item["target_versions"]
    assert route["work_item_route"]["args"] == {
        "mode": "curation", "curation_action": "work-item", "review_ref": item["logical_identity"]
    }
    assert route["recommended_outcome"] == "enrich"
    served = commands.op_maintain_memory(tmp_path, **route["work_item_route"]["args"])
    assert served["entity_candidate"]["candidate_state"] == "hydration"


def test_ambiguity_never_selects_a_target_or_proposes_duplicate_creation(tmp_path):
    _hydration(tmp_path)
    _entity(tmp_path, title="cobalt workshop", slug="another-workshop")
    result = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], limit=3)
    route = result["items"][0]["vocabulary"]
    item = VocabularyState(tmp_path).get(route["ref"])
    assert item["projection"]["currency"]["candidate_state"] == "ambiguous"
    assert route["recommended_outcome"] == "defer"
    assert "selected_target" not in route
    assert item["decision"] is None


def test_incidental_name_does_not_acquire_vocabulary_work(tmp_path):
    from test_entity_lifecycle_surface import _note

    for index in range(3):
        _note(tmp_path, index, "The name amber guild appears in this boilerplate.")
    result = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], limit=3)
    assert result["items"] == []
    assert VocabularyState(tmp_path).page()["items"] == []


def test_context_refreshes_provider_when_a_new_identity_changes_promotion_to_hydration(tmp_path):
    import pytest

    from exomem import vocabulary_review

    _promotion(tmp_path)
    report = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], limit=3)
    previous = report["items"][0]["vocabulary"]
    _entity(tmp_path, title="amber guild", slug="amber-guild")
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        vocabulary_review.context(
            tmp_path, ref=previous["ref"], expected_fingerprint=previous["fingerprint"]
        )
    refreshed = vocabulary_review.context(tmp_path, ref=previous["ref"])["item"]
    assert refreshed["ref"] == previous["ref"]
    assert refreshed["projection"]["currency"]["candidate_state"] == "hydration"


def test_enrichment_cannot_select_a_source_page_instead_of_an_entity(tmp_path):
    import pytest
    from test_vocabulary_review import payload

    from exomem import vocabulary_review
    from exomem.governance.principal import library_scope

    _hydration(tmp_path)
    report = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], limit=3)
    route = report["items"][0]["vocabulary"]
    current = VocabularyState(tmp_path).get(route["ref"])
    selected = payload(current, "enrich") | {
        "choice": {"canonical": current["evidence"][0]["ref"]}
    }
    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_DECISION_INVALID"):
        vocabulary_review.decide(tmp_path, ref=current["ref"], decision=selected)


def test_ambiguous_provider_state_refuses_executable_choice(tmp_path):
    import pytest
    from test_vocabulary_review import payload

    from exomem import vocabulary_review
    from exomem.governance.principal import library_scope

    target = _hydration(tmp_path)
    _entity(tmp_path, title="cobalt workshop", slug="another-workshop")
    report = commands.op_review_memory(tmp_path, categories=["entity_recurrence"], limit=3)
    current = VocabularyState(tmp_path).get(report["items"][0]["vocabulary"]["ref"])
    selected = payload(current, "enrich") | {"choice": {"canonical": target}}
    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_DECISION_INVALID"):
        vocabulary_review.decide(tmp_path, ref=current["ref"], decision=selected)
