import json
from types import SimpleNamespace

import pytest

from exomem import (
    mutation_terminal,
    vocabulary_projection,
    vocabulary_recovery,
    vocabulary_review,
    writer_lease,
)
from exomem.governance.principal import library_scope
from exomem.vocabulary_state import VocabularyState


def terminal(path):
    return mutation_terminal.committed_terminal(
        {"path": path},
        request_id="vocabulary-write",
        receipt_id="receipt-vocabulary",
        idempotency_key="same-write",
    )


def test_warming_source_discovery_persists_each_bounded_cursor(tmp_path, monkeypatch):
    from exomem import vocabulary_delivery

    path = "Knowledge Base/Notes/origins.md"
    page = tmp_path / path
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: insight\n---\nOrigins under review.\n")
    calls = []

    def project(_root, *, path, continuation=None):
        calls.append(continuation)
        if len(calls) < 3:
            return {
                "status": "warming", "items": [], "signals": [],
                "continuation": f"page-{len(calls)}",
            }
        return {"status": "current", "items": [], "continuation": None}

    monkeypatch.setattr(vocabulary_projection, "for_write", project)
    with library_scope():
        first = vocabulary_delivery.after_commit(tmp_path, terminal(path))
        assert first["vocabulary_sync"]["state"] == "warming"
        assert "vocabulary_advisory" not in first
        second = vocabulary_delivery.recover(tmp_path)
        assert second == {"state": "warming", "processed": 1, "coverage": "bounded-pass"}
        third = vocabulary_delivery.recover(tmp_path)
        assert third == {"state": "current", "processed": 1, "coverage": "bounded-pass"}
    assert calls == [None, "page-1", "page-2"]
    assert not vocabulary_recovery.page(tmp_path, limit=4)


def test_completed_empty_projections_leave_no_per_write_recovery_history(tmp_path, monkeypatch):
    from exomem import vocabulary_delivery

    monkeypatch.setattr(
        vocabulary_delivery,
        "_project",
        lambda *args, **kwargs: {
            "sync": {"state": "current"},
            "items": [],
            "continuation": None,
        },
    )
    with library_scope():
        for index in range(250):
            result = vocabulary_delivery.after_commit(
                tmp_path, terminal(f"Knowledge Base/Notes/written-{index}.md")
            )
            assert result["vocabulary_sync"]["state"] == "current"
    owner = VocabularyState(tmp_path)
    assert not vocabulary_recovery.page(tmp_path, limit=4)
    assert not owner.store.path.exists()


def test_pending_recovery_coalesces_repeated_writes_to_the_same_page(tmp_path, monkeypatch):
    from exomem import vocabulary_delivery

    monkeypatch.setattr(
        vocabulary_delivery,
        "_project",
        lambda *args, **kwargs: {
            "sync": {"state": "unavailable"},
            "items": [],
        },
    )
    with library_scope():
        for index in range(20):
            vocabulary_delivery.after_commit(
                tmp_path,
                terminal("Knowledge Base/Notes/repeated.md") | {"receipt_id": f"receipt-{index}"},
            )
    assert len(vocabulary_recovery.page(tmp_path, limit=4)) == 1


def test_owner_compaction_retires_completed_recovery_but_preserves_pending_work(tmp_path):
    owner = VocabularyState(tmp_path)
    owner._update(lambda section: section.update(recoveries={
        "done": {"path": "Knowledge Base/Notes/done.md", "state": "current"},
        "pending": {"path": "Knowledge Base/Notes/pending.md", "state": "pending"},
    }))
    report = owner.store.compact()
    assert set(owner.store.load()["vocabulary"]["recoveries"]) == {"pending"}
    assert report["dropped"]["vocabulary_recoveries"] == 1


def test_optional_failure_preserves_commit_and_replay_identity(tmp_path, monkeypatch):
    from exomem import vocabulary_delivery

    path = "Knowledge Base/Entities/Organizations/first.md"
    original = terminal(path)

    def unavailable(*args, **kwargs):
        raise RuntimeError("optional provider failed")

    monkeypatch.setattr(vocabulary_projection, "for_write", unavailable)
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, original)
    for key in original:
        assert result[key] == original[key]
    assert result["vocabulary_sync"]["state"] == "unavailable"
    assert result["vocabulary_sync"]["recovery"]["tool"] == "review_memory"
    assert "optional provider failed" not in json.dumps(result)


def test_recovery_reconstructs_work_without_repeating_mutation(tmp_path, monkeypatch):
    from test_vocabulary_projection import fixture_graph

    from exomem import vocabulary_delivery

    path, _ = fixture_graph(tmp_path)
    original_projection = vocabulary_projection.for_write
    monkeypatch.setattr(
        vocabulary_projection,
        "for_write",
        lambda *args, **kwargs: {
            "status": "unavailable",
            "reason": "graph_projection_unavailable",
            "items": [],
        },
    )
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal(path))
    assert result["vocabulary_sync"]["state"] == "unavailable"
    before = (tmp_path / path).read_bytes()
    monkeypatch.setattr(vocabulary_projection, "for_write", original_projection)
    with library_scope():
        recovered = vocabulary_delivery.recover(tmp_path)
    assert recovered["state"] == "current"
    assert recovered["processed"] == 1
    assert (tmp_path / path).read_bytes() == before
    assert len(VocabularyState(tmp_path).page()["items"]) == 1
    with library_scope():
        assert vocabulary_delivery.recover(tmp_path)["processed"] == 0


def test_failure_before_projection_leaves_a_recoverable_committed_path(tmp_path, monkeypatch):
    from test_vocabulary_projection import fixture_graph

    from exomem import vocabulary_delivery

    path, _ = fixture_graph(tmp_path)
    original = vocabulary_delivery.envelope.resolved

    def unavailable():
        raise RuntimeError("temporary envelope failure")

    monkeypatch.setattr(vocabulary_delivery.envelope, "resolved", unavailable)
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal(path))
    assert result["vocabulary_sync"]["state"] == "unavailable"
    monkeypatch.setattr(vocabulary_delivery.envelope, "resolved", original)
    with library_scope():
        recovered = vocabulary_review.review(tmp_path)
    assert len(recovered["items"]) == 1
    assert recovered["recovery"]["processed"] == 1


def test_one_notice_is_projected_on_compact_and_replay(tmp_path):
    from test_vocabulary_projection import fixture_graph

    from exomem import vocabulary_delivery

    path, _ = fixture_graph(tmp_path)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return terminal(path)

    with library_scope():
        result = manager.idempotency.run(
            "vocabulary-replay",
            "a" * 64,
            operation,
            after_operation_guard=lambda raw: vocabulary_delivery.after_commit(tmp_path, raw),
        )
        replay = manager.idempotency.run("vocabulary-replay", "a" * 64, operation)
        compact = mutation_terminal.project_terminal(result)
        assert compact == mutation_terminal.project_terminal(replay)
    assert calls == 1
    assert compact["state"] == "committed"
    assert compact["vocabulary_advisory"]["ref"].startswith("exomem://review/vocabulary/")
    assert len(json.dumps(compact["vocabulary_advisory"]).encode()) <= 1024
    assert str(tmp_path) not in json.dumps(compact)


def test_cached_notice_obeys_current_disclosure(tmp_path, monkeypatch):
    from test_vocabulary_projection import fixture_graph

    from exomem import vocabulary_delivery

    path, _ = fixture_graph(tmp_path)
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal(path))
        assert mutation_terminal.project_terminal(result)["vocabulary_advisory"]
    monkeypatch.setattr(vocabulary_review.egress, "release_level_for", lambda *args: 0)
    with library_scope():
        assert "vocabulary_advisory" not in mutation_terminal.project_terminal(result)


def test_canonical_command_boundary_delivers_guidance_after_commit(tmp_path):
    from test_vocabulary_projection import fixture_graph

    path, _ = fixture_graph(tmp_path)
    calls = 0

    def leaf(vault):
        nonlocal calls
        calls += 1
        writer_lease.mark_active_mutation_committed()
        return {"path": path}

    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    with library_scope():
        first = manager.invoke(command, (tmp_path,), {}, idempotency_key="canonical-write")
        replay = manager.invoke(command, (tmp_path,), {}, idempotency_key="canonical-write")
    assert first["vocabulary_advisory"]["ref"].startswith("exomem://review/vocabulary/")
    assert replay == first
    assert calls == 1


def test_explicit_review_recovers_a_missed_projection(tmp_path, monkeypatch):
    from test_vocabulary_projection import fixture_graph

    from exomem import vocabulary_delivery

    path, _ = fixture_graph(tmp_path)
    original = vocabulary_projection.for_write
    monkeypatch.setattr(
        vocabulary_projection,
        "for_write",
        lambda *args, **kwargs: {
            "status": "unavailable",
            "items": [],
        },
    )
    with library_scope():
        vocabulary_delivery.after_commit(tmp_path, terminal(path))
    monkeypatch.setattr(vocabulary_projection, "for_write", original)
    with library_scope():
        reviewed = vocabulary_review.review(tmp_path)
    assert len(reviewed["items"]) == 1
    assert reviewed["recovery"]["processed"] == 1


def test_review_recovery_continues_beyond_the_first_write_page(tmp_path):
    from test_vocabulary_projection import fixture_many_edges

    from exomem import vocabulary_delivery

    path = fixture_many_edges(tmp_path)
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal(path))
        assert result["vocabulary_advisory"]
        assert len(VocabularyState(tmp_path).page(limit=64)["items"]) == 4
        first = vocabulary_delivery.recover(tmp_path)
        assert first["state"] == "warming"
        second = vocabulary_delivery.recover(tmp_path)
    assert second["state"] == "current"
    assert len(VocabularyState(tmp_path).page(limit=64)["items"]) == 10


def test_unknown_independence_is_not_a_successful_empty_projection(tmp_path):
    from test_vocabulary_projection import fixture_graph

    from exomem import vocabulary_delivery

    path, _ = fixture_graph(tmp_path, unknown_origin=True)
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal(path))
    assert result["vocabulary_sync"]["state"] == "unavailable"
    assert "vocabulary_advisory" not in result


def test_unready_jobs_do_not_starve_later_recovery_work(tmp_path, monkeypatch):
    from exomem import vocabulary_delivery

    calls = []

    def unavailable(vault_root, *, path, **kwargs):
        calls.append(path)
        return {"status": "unavailable", "items": []}

    monkeypatch.setattr(vocabulary_projection, "for_write", unavailable)
    with library_scope():
        for index in range(5):
            vocabulary_delivery.after_commit(
                tmp_path, terminal(f"Knowledge Base/Notes/item-{index}.md")
            )
        calls.clear()
        vocabulary_delivery.recover(tmp_path)
        vocabulary_delivery.recover(tmp_path)
    assert len(set(calls)) == 5


@pytest.mark.parametrize("state", ["pending", "refused", "noop"])
def test_non_committed_result_does_not_create_review_state(tmp_path, state):
    from exomem import vocabulary_delivery

    result = terminal("Knowledge Base/Notes/example.md") | {"state": state}
    assert vocabulary_delivery.after_commit(tmp_path, result) == result
    assert not VocabularyState(tmp_path).store.path.exists()
