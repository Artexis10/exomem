import hashlib
import json

import pytest

from exomem import vocabulary_review as review
from exomem.governance.principal import library_scope
from exomem.vocabulary_state import VocabularyState
from exomem.vocabulary_workflow import Evidence, make_item


@pytest.mark.parametrize("disposition, expected", [("quiet", 1), ("off", 0), ("normal", 1)])
def test_explicit_review_honors_off_without_hiding_quiet_work(tmp_path, disposition, expected):
    from exomem import review_state
    from exomem.vocabulary_notifications import REVIEW_FAMILIES

    item, _ = setup_item(tmp_path)
    review_state.ReviewStateStore(tmp_path).set_disposition(
        REVIEW_FAMILIES[item.family], disposition, why="intentional: review preference"
    )
    with library_scope():
        assert len(review.review(tmp_path)["items"]) == expected
    assert VocabularyState(tmp_path).get(item.ref)["ref"] == item.ref


@pytest.mark.parametrize("family", ["entity_recurrence", "vocabulary_entity_instances"])
@pytest.mark.parametrize("disposition", ["off", "quiet"])
def test_family_dispositions_compose_for_fresh_and_continued_pages(
    tmp_path, monkeypatch, family, disposition
):
    from exomem import review_state

    template, _ = setup_item(tmp_path, observe=False)
    store = VocabularyState(tmp_path)
    for index in range(4):
        store.observe(make_item(
            family="entity-instance/v1", signal="entity-lifecycle",
            logical_identity=f"entity-candidate:{index}",
            targets=dict(template.target_versions), evidence=list(template.evidence),
            registry_hashes=dict(template.registry_hashes), projection_status="current",
            projection_currency=origin_currency(index),
        ))
    with library_scope():
        first = review.review(tmp_path, limit=1)
        assert first["continuation"]
        owner = review_state.ReviewStateStore(tmp_path)
        owner.set_disposition(family, disposition, why="intentional: review on request")

        def no_ledger_read(*_args, **_kwargs):
            raise AssertionError("queue serving must not load the canonical ledger")

        monkeypatch.setattr(review_state.ReviewStateStore, "load", no_ledger_read)
        fresh = review.review(tmp_path, limit=1)
        continued = review.review(tmp_path, continuation=first["continuation"], limit=1)
        if disposition == "off":
            assert fresh["items"] == continued["items"] == []
        else:
            assert fresh["items"][0]["family_dispositions"] == {family: "quiet"}
            assert continued["items"][0]["family_dispositions"] == {family: "quiet"}
            # A second continuation keeps the stored row fingerprint, not the annotation.
            third = review.review(tmp_path, continuation=continued["continuation"], limit=1)
            assert third["items"][0]["family_dispositions"] == {family: "quiet"}
        all_page = review.review(tmp_path, state="all", limit=1)
        assert all_page["items"][0]["family_dispositions"] == {family: disposition}
        all_next = review.review(
            tmp_path, state="all", continuation=all_page["continuation"], limit=1
        )
        assert all_next["items"][0]["family_dispositions"] == {family: disposition}


def test_empty_projection_initialization_copies_preexisting_origin_disposition(tmp_path):
    from exomem import deferred_index, review_state

    owner = review_state.ReviewStateStore(tmp_path)
    owner.set_disposition("entity_recurrence", "off", why="intentional: stop suggestions")
    deferred_index.store_path(tmp_path).unlink()
    # A different family's write initializes the empty projection from existing state.
    owner.set_disposition("vocabulary_entity_instances", "normal")
    current = make_item(
        family="entity-instance/v1", signal="entity-lifecycle",
        logical_identity="entity-candidate:one", targets={"source:one": "v1"},
        evidence=[], registry_hashes={}, projection_status="current",
        projection_currency=origin_currency(),
    )
    store = VocabularyState(tmp_path)
    store.observe(current)
    assert store.page()["items"] == []
    assert store.page(state="all")["items"][0]["family_dispositions"] == {
        "entity_recurrence": "off"
    }


def test_legacy_projection_requires_rebuild_before_serving_origin_dispositions(tmp_path, monkeypatch):
    from exomem import deferred_index, review_state

    current = make_item(
        family="entity-instance/v1", signal="entity-lifecycle",
        logical_identity="entity-candidate:one", targets={"source:one": "v1"},
        evidence=[], registry_hashes={}, projection_status="current",
        projection_currency=origin_currency(),
    )
    store = VocabularyState(tmp_path)
    store.observe(current)
    owner = review_state.ReviewStateStore(tmp_path)
    owner.set_disposition("entity_recurrence", "off", why="intentional: stop suggestions")
    conn = deferred_index._connect(tmp_path, create=True)
    try:
        with conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(vocabulary_review_meta)")}
            if "family_projection_version" in columns:
                conn.execute("ALTER TABLE vocabulary_review_meta DROP COLUMN family_projection_version")
            conn.execute("DELETE FROM vocabulary_review_families WHERE family = 'entity_recurrence'")
    finally:
        conn.close()

    def no_ledger_read(*_args, **_kwargs):
        raise AssertionError("legacy projections must warm without loading the ledger")

    with monkeypatch.context() as patch:
        patch.setattr(review_state.ReviewStateStore, "load", no_ledger_read)
        assert store.page()["state"] == "warming"
    # A point write cannot bless rows built with the old family semantics.
    owner.set_disposition("vocabulary_entity_instances", "normal")
    assert store.page()["state"] == "warming"
    assert store.rebuild_review_projection()["state"] == "current"
    assert store.page()["items"] == []
    assert store.page(state="all")["items"][0]["family_dispositions"] == {
        "entity_recurrence": "off"
    }


def test_origin_suppression_keeps_finite_window_and_fairness(tmp_path, monkeypatch):
    from exomem import review_state, vocabulary_review_index

    store = VocabularyState(tmp_path)
    for index in range(40):
        store.observe(make_item(
            family="entity-instance/v1", signal="entity-lifecycle",
            logical_identity=f"entity-candidate:{index}", targets={"source:one": "v1"},
            evidence=[], registry_hashes={}, projection_status="current",
            projection_currency=origin_currency(index),
        ))
    review_state.ReviewStateStore(tmp_path).set_disposition(
        "entity_recurrence", "off", why="intentional: stop suggestions"
    )
    decoded = []
    decode = vocabulary_review_index._decode_view

    def counted(raw):
        view = decode(raw)
        decoded.append(view["ref"])
        return view

    monkeypatch.setattr(vocabulary_review_index, "_decode_view", counted)
    assert store.page(limit=1)["items"] == []
    first_window = set(decoded)
    assert len(decoded) == len(first_window) == 16
    decoded.clear()
    second = store.page(limit=1)
    assert second["items"] == []
    assert second["coverage"]["exhaustive"] is False
    assert len(decoded) == 16
    assert first_window.isdisjoint(decoded)


def origin_currency(index=0, fingerprint="f" * 24):
    return {"review_ref": f"exomem://review/{index:024x}", "review_fingerprint": fingerprint}


def origin_item(index=0, fingerprint="f" * 24):
    return make_item(
        family="entity-instance/v1", signal="entity-lifecycle",
        logical_identity=f"exomem://review/{index:024x}", targets={"source:one": "v1"},
        evidence=[], registry_hashes={}, projection_status="current",
        projection_currency=origin_currency(index, fingerprint),
    )


@pytest.mark.parametrize("action", ["dismiss", "snooze", "competing"])
def test_origin_item_decision_filters_fresh_and_continued_pages(tmp_path, monkeypatch, action):
    import datetime as dt

    from exomem import review_state

    store = VocabularyState(tmp_path)
    for index in range(4):
        store.observe(origin_item(index))
    first = store.page(limit=1)
    hidden = next(item for item in store.page(state="all")["items"] if item["ref"] != first["items"][0]["ref"])
    binding = hidden["projection"]["currency"]
    review_id = review_state.parse_review_ref(binding["review_ref"])
    owner = review_state.ReviewStateStore(tmp_path)
    kwargs = {"until": (dt.date.today() + dt.timedelta(days=1)).isoformat()} if action == "snooze" else {}
    owner.apply(review_id, binding["review_fingerprint"], action=action, **kwargs)

    def no_ledger_read(*_args, **_kwargs):
        raise AssertionError("serving originating decisions must not load the canonical ledger")

    with monkeypatch.context() as patch:
        patch.setattr(review_state.ReviewStateStore, "load", no_ledger_read)
        fresh = store.page()
        continued = store.page(continuation=first["continuation"])
        assert all(item["ref"] != hidden["ref"] for item in fresh["items"] + continued["items"])
        assert len(continued["items"]) == 2
        history = next(item for item in store.page(state="all")["items"] if item["ref"] == hidden["ref"])
        assert history["origin_review"]["state"] == {"dismiss": "dismissed", "snooze": "snoozed", "competing": "competing"}[action]
        assert history["state"] == "pending"
    owner.apply(review_id, binding["review_fingerprint"], action="reopen")
    assert hidden["ref"] in {item["ref"] for item in store.page()["items"]}


def test_origin_snooze_expiry_uses_owner_inclusive_day_semantics(tmp_path, monkeypatch):
    import datetime as dt

    from exomem import review_state

    store = VocabularyState(tmp_path)
    store.observe(origin_item())
    owner = review_state.ReviewStateStore(tmp_path)
    owner.apply("0" * 24, "f" * 24, action="snooze", until="2030-01-02")
    effective_state = review_state.ReviewStateStore.effective_state
    day = dt.date(2030, 1, 2)

    def on_day(self, *args, **kwargs):
        return effective_state(self, *args, today=day, **kwargs)

    monkeypatch.setattr(review_state.ReviewStateStore, "effective_state", on_day)
    assert store.page()["items"] == []
    assert store.page(state="all")["items"][0]["origin_review"]["state"] == "snoozed"
    day = dt.date(2030, 1, 3)
    assert store.page()["items"][0]["origin_review"]["state"] == "open"


def test_origin_changed_fingerprint_reopens_without_rewriting_either_decision(tmp_path):
    from exomem import review_state

    store = VocabularyState(tmp_path)
    original = origin_item()
    store.observe(original)
    owner = review_state.ReviewStateStore(tmp_path)
    owner.apply("0" * 24, "f" * 24, action="dismiss")
    before = owner.load()["records"]
    assert store.page()["items"] == []
    changed = origin_item(fingerprint="e" * 24)
    store.observe(changed)
    assert store.page()["items"][0]["origin_review"]["state"] == "open"
    assert owner.load()["records"] == before
    store.decide(changed, payload(changed.to_dict(), "defer"), actor="principal:test")
    owner.apply("0" * 24, "f" * 24, action="reopen")
    assert store.page()["items"] == []
    assert store.page(state="all")["items"][0]["state"] == "deferred"


def test_origin_decision_point_update_is_indexed_and_overflow_requires_rebuild(tmp_path):
    from exomem import deferred_index, review_state

    store = VocabularyState(tmp_path)
    # Separate adapted rows can bind one originating review identity.
    for index in range(5):
        store.observe(make_item(
            family="entity-instance/v1", signal="entity-lifecycle",
            logical_identity=f"variant:{index}", targets={"source:one": "v1"},
            evidence=[], registry_hashes={}, projection_status="current",
            projection_currency=origin_currency(),
        ))
    owner = review_state.ReviewStateStore(tmp_path)
    owner.apply("0" * 24, "f" * 24, action="dismiss")
    assert store.page()["state"] == "warming"
    assert store.rebuild_review_projection()["state"] == "current"
    assert store.page()["items"] == []
    conn = deferred_index._connect(tmp_path, create=True)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT ref, origin_fingerprint FROM vocabulary_review_rows "
            "WHERE origin_review_id = ? LIMIT 5", ("0" * 24,),
        ).fetchall()
    finally:
        conn.close()
    assert any("USING INDEX vocabulary_review_rows_origin" in row[-1] for row in plan)


def setup_item(vault, *, observe=True):
    paths = {
        "a": "Knowledge Base/Entities/Organizations/a.md",
        "b": "Knowledge Base/Entities/Organizations/b.md",
        "source": "Knowledge Base/Sources/Articles/source.md",
    }
    versions = {}
    for name, path in paths.items():
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"---\ntype: {'source' if name == 'source' else 'organization'}\n"
            f"title: {name}\n---\nRecorded facts about {name}.\n"
        )
        versions[path] = hashlib.sha256(target.read_bytes()).hexdigest()
    current = make_item(
        family="relation-type/v1",
        signal="generic-pair",
        targets={paths["a"]: versions[paths["a"]], paths["b"]: versions[paths["b"]]},
        evidence=[Evidence(paths["source"], versions[paths["source"]], "origin:one")],
        registry_hashes=review.registry_hashes(vault),
        projection_status="current",
    )
    if observe:
        VocabularyState(vault).observe(current)
    return current, paths


def payload(current, outcome="generic"):
    return {
        "item_ref": current["ref"],
        "fingerprint": current["fingerprint"],
        "family": current["family"],
        "choice": None,
        "registry_hashes": current["registry_hashes"],
        "target_versions": current["target_versions"],
        "outcome": outcome,
        "rationale": "No narrower meaning is supported by this evidence.",
    }


def test_review_context_supplies_live_evidence_and_existing_definitions(tmp_path):
    current, paths = setup_item(tmp_path)
    result = review.context(tmp_path, ref=current.ref, max_body_chars=12)
    assert result["item"]["fingerprint"] == current.fingerprint
    assert {entry["path"] for entry in result["targets"]} == {paths["a"], paths["b"]}
    assert result["definitions"]["selected_relation"] is None
    assert all(len(entry["body"]) <= 12 for entry in result["targets"] + result["evidence"])
    assert result["authority"]["mutation_granted"] is False


def test_live_target_change_refuses_old_decision_without_mutating_disposition(tmp_path):
    current, paths = setup_item(tmp_path)
    previous = review.context(tmp_path, ref=current.ref)["item"]
    (tmp_path / paths["a"]).write_text("---\ntype: organization\n---\nNew facts.\n")
    before = VocabularyState(tmp_path).store.path.read_bytes()
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        review.decide(tmp_path, ref=current.ref, decision=payload(previous))
    assert VocabularyState(tmp_path).store.path.read_bytes() == before


def test_decision_actor_is_not_supplied_by_agent(tmp_path):
    current, _ = setup_item(tmp_path)
    visible = review.context(tmp_path, ref=current.ref)["item"]
    with library_scope():
        result = review.decide(tmp_path, ref=current.ref, decision=payload(visible))
    assert result["state"] == "resolved_without_mutation"
    assert result["decision"]["actor"]
    with pytest.raises(ValueError, match="VOCABULARY_DECISION_INVALID"):
        review.decide(tmp_path, ref=current.ref, decision=payload(visible) | {"actor": "owner"})


def test_reuse_must_name_a_current_registered_canonical_meaning(tmp_path):
    current, _ = setup_item(tmp_path)
    visible = review.context(tmp_path, ref=current.ref)["item"]
    selected = payload(visible, "reuse") | {"choice": {"canonical": "vault.not_registered"}}
    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_DECISION_INVALID"):
        review.decide(tmp_path, ref=current.ref, decision=selected)
    selected["choice"] = {"canonical": "part_of"}
    with library_scope():
        result = review.decide(tmp_path, ref=current.ref, decision=selected)
    assert result["decision"]["choice"]["canonical"] == "part_of"


def test_live_entity_type_proposal_uses_the_existing_registry_validator(tmp_path):
    original, _ = setup_item(tmp_path)
    current = make_item(
        family="entity-type/v1",
        signal="agent-meaning-question",
        targets=dict(original.target_versions),
        evidence=original.evidence,
        registry_hashes=dict(original.registry_hashes),
        projection_status="current",
    )
    VocabularyState(tmp_path).observe(current)
    selected = payload(current.to_dict(), "propose-new") | {
        "choice": {
            "canonical": "school",
            "definition": {
                "folder": "Schools",
                "label": "School",
                "aliases": ["academy"],
                "capture_guidance": "Capture the institution and its educational purpose.",
                "status": "active",
            },
        }
    }
    with library_scope():
        result = review.decide(tmp_path, ref=current.ref, decision=selected)
    assert result["state"] == "proposed"


@pytest.mark.parametrize("family", ["relation-type/v1", "entity-type/v1"])
@pytest.mark.parametrize("registered", [True, False])
def test_proposal_references_use_the_complete_live_registry(tmp_path, family, registered):
    from exomem import entity_types, relation_registry

    if family == "relation-type/v1":
        existing = {
            "parent": "relates_to",
            "description": "Applies to",
            "direction": "directed",
        }
        canonical, reference = "vault.applied_from", "vault.applies_to"
        proposed = existing | {"description": "Applied from", "inverse": reference}
        path = relation_registry.extension_registry_path(tmp_path)
        registry = {"schema_version": 1, "extensions": {reference: existing}}
    else:
        existing = {
            "folder": "Workshops",
            "label": "Workshop",
            "aliases": [],
            "capture_guidance": "A recurring practical activity.",
        }
        canonical, reference = "legacy-workshop", "workshop"
        proposed = existing | {
            "folder": "LegacyWorkshops",
            "label": "Legacy workshop",
            "status": "deprecated",
            "replaced_by": reference,
        }
        path = entity_types.extension_registry_path(tmp_path)
        registry = {"schema_version": 1, "entity_types": {reference: existing}}
    if registered:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(registry))
    original, _ = setup_item(tmp_path)
    current = make_item(
        family=family,
        signal="agent-meaning-question",
        targets=dict(original.target_versions),
        evidence=original.evidence,
        registry_hashes=review.registry_hashes(tmp_path),
        projection_status="current",
    )
    state = VocabularyState(tmp_path)
    state.observe(current)
    selected = payload(current.to_dict(), "propose-new") | {
        "choice": {"canonical": canonical, "definition": proposed}
    }
    before = state.store.path.read_bytes()
    with library_scope():
        if registered:
            assert (
                review.decide(tmp_path, ref=current.ref, decision=selected)["state"] == "proposed"
            )
        else:
            with pytest.raises(
                ValueError, match="proposal conflicts with the current registry"
            ) as rejected:
                review.decide(tmp_path, ref=current.ref, decision=selected)
            assert "inverse" in str(rejected.value) or "replaced_by" in str(rejected.value)
            assert state.store.path.read_bytes() == before


def test_relation_context_publishes_a_proposal_that_the_canonical_route_accepts(tmp_path):
    from exomem import commands

    current, _ = setup_item(tmp_path)
    guidance = review.context(tmp_path, ref=current.ref)["definitions"]["registration"]
    proposal = guidance["propose"]["proposal"] | {
        "requested_label": "coordinates",
        "namespace": "activities",
        "parent": "relates_to",
        "description": "Coordinates the activities of another entity.",
        "direction": "directed",
    }
    result = commands.op_schema_memory(
        tmp_path,
        subject="relations",
        operation="propose-relation",
        proposal=proposal,
    )
    assert result["valid"] is True
    assert "activities.coordinates" in result["delta"]["upsert"]
    assert guidance["save"]["proposal_from"] == "delta"
    assert guidance["save"]["expected_hash_from"] == "expected_hash"


def test_withheld_targets_make_review_item_unavailable_without_leaking_queue_count(
    tmp_path, monkeypatch
):
    current, _ = setup_item(tmp_path)
    monkeypatch.setattr(review.egress, "release_level_for", lambda *args, **kwargs: 0)
    assert review.review(tmp_path) == {
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
    with pytest.raises(ValueError, match="VOCABULARY_ITEM_NOT_FOUND"):
        review.context(tmp_path, ref=current.ref)


def test_context_pages_stored_evidence_with_a_bound_cursor(tmp_path):
    original, paths = setup_item(tmp_path)
    entries = list(original.evidence)
    for index in range(5):
        path = f"Knowledge Base/Sources/Articles/additional-{index}.md"
        (tmp_path / path).write_text(f"---\ntype: source\n---\nAccount {index}.\n")
        entries.append(
            Evidence(
                path, hashlib.sha256((tmp_path / path).read_bytes()).hexdigest(), f"origin:{index}"
            )
        )
    current = make_item(
        family=original.family,
        signal=original.signal,
        targets=dict(original.target_versions),
        evidence=entries,
        registry_hashes=dict(original.registry_hashes),
        projection_status="current",
    )
    VocabularyState(tmp_path).observe(current)
    first = review.context(tmp_path, ref=current.ref, max_related_pages=4)
    second = review.context(
        tmp_path, ref=current.ref, max_related_pages=4, continuation=first["evidence_continuation"]
    )
    assert [len(first["evidence"]), len(second["evidence"])] == [4, 2]
    assert second["evidence_continuation"] is None
    assert not {entry["ref"] for entry in first["evidence"]} & {
        entry["ref"] for entry in second["evidence"]
    }


def test_context_keeps_a_provenance_page_cursor_while_paging_within_that_page(
    tmp_path, monkeypatch
):
    """A one-item display page must not return to the item's anchor evidence."""
    from exomem import vocabulary_projection

    original, paths = setup_item(tmp_path)
    checkpoint = '{"kind":"vocabulary-provenance-context/v1"}'
    evidence = []
    evidence_paths = {}
    for name in ("checkpoint-a", "checkpoint-b"):
        path = f"Knowledge Base/Sources/Articles/{name}.md"
        page = tmp_path / path
        page.write_text(f"---\ntype: source\n---\n{name}.\n")
        evidence.append(
            Evidence(name, hashlib.sha256(page.read_bytes()).hexdigest(), "origin:checkpoint")
        )
        evidence_paths[name] = path
    current = make_item(
        family=original.family,
        signal=original.signal,
        targets=dict(original.target_versions),
        evidence=original.evidence,
        registry_hashes=dict(original.registry_hashes),
        projection_status="current",
        continuation=checkpoint,
    )
    VocabularyState(tmp_path).observe(current)
    calls = []

    def more_evidence(_vault, *, continuation):
        calls.append(continuation)
        return {
            "evidence": evidence,
            "paths": evidence_paths,
            "continuation": "next-checkpoint-page",
        }

    monkeypatch.setattr(vocabulary_projection, "more_evidence", more_evidence)

    anchor = review.context(tmp_path, ref=current.ref, max_related_pages=1)
    first = review.context(
        tmp_path,
        ref=current.ref,
        max_related_pages=1,
        continuation=anchor["evidence_continuation"],
    )
    second = review.context(
        tmp_path,
        ref=current.ref,
        max_related_pages=1,
        continuation=first["evidence_continuation"],
    )

    assert [entry["ref"] for entry in first["evidence"]] == ["checkpoint-a"]
    assert [entry["ref"] for entry in second["evidence"]] == ["checkpoint-b"]
    assert second["evidence_continuation"] == "next-checkpoint-page"
    assert calls == [checkpoint, checkpoint]
