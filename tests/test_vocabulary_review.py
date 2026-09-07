import hashlib

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


def setup_item(vault):
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
            f"---\ntype: {'source' if name == 'source' else 'organization'}\ntitle: {name}\n---\nRecorded facts about {name}.\n"
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
        evidence.append(Evidence(name, hashlib.sha256(page.read_bytes()).hexdigest(), "origin:checkpoint"))
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
