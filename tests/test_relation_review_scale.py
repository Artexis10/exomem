from __future__ import annotations

import copy
import dataclasses
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import embeddings, find, graph_sync
from scripts import relation_review_scale


def _changed(report: dict, *path_and_value):
    changed = copy.deepcopy(report)
    *parents, key, value = path_and_value
    target = changed
    for parent in parents:
        target = target[parent]
    target[key] = value
    return changed


def test_calibrated_report_is_reconciled_from_typed_records() -> None:
    report = relation_review_scale.reference_report()

    relation_review_scale.validate_report(report)
    assert report["corpus"]["candidate_numerator"] == 20
    assert report["corpus"]["candidate_denominator"] == 3_600
    assert report["workload"]["participating_stream_ids"] == list(range(20))
    assert len(report["queue"]["samples"]) == report["structural"]["requests_measured"]
    assert report["mutations"]["committed"] == len(
        report["checkpoints"]["committed_generations"]
    )
    assert report["structural"]["mutation_full_rebuilds"] == 0


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda report: report["mutations"].update(
                {"attempted": 3, "committed": 3, "checkpoint_exact": 3,
                 "distinct_bytes": 3, "published_eligible": 3,
                 "published_source_hash_bound": 3, "published_identity_bound": 3,
                 "graph_completed": 3}
            ),
            "generation",
        ),
        (
            lambda report: (
                report["queue"]["available"].update({"count": 1}),
                report["queue"]["unavailable"].update({"count": 20}),
                report["queue"].update({"availability_ratio": 0.95}),
            ),
            "reconcile",
        ),
        (
            lambda report: report["structural"].update(
                {"indexed_query_count": 10_000, "max_indexed_queries_per_request": 8}
            ),
            "reconcile",
        ),
        (
            lambda report: report["workload"]["participating_stream_ids"].remove(17),
            "stream",
        ),
        (
            lambda report: (
                report["queue"]["samples"][0].update({"queries": 9}),
                report["structural"].update(
                    {"indexed_query_count": 161, "max_indexed_queries_per_request": 9}
                ),
            ),
            "fixed query",
        ),
        (
            lambda report: (
                report["queue"]["samples"][0].update({"markdown_reads": 1}),
                report["structural"].update({"markdown_reads": 1}),
            ),
            "hidden corpus work",
        ),
        (
            lambda report: (
                report["queue"]["samples"][0].update({"embedding_calls": 1}),
                report["structural"].update({"embedding_calls": 1}),
            ),
            "hidden corpus work",
        ),
    ],
)
def test_exact_accounting_mutants_are_rejected(mutate, expected) -> None:
    report = relation_review_scale.reference_report()
    mutate(report)

    with pytest.raises(relation_review_scale.ScaleGateError, match=expected):
        relation_review_scale.validate_report(report)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("mutations", "committed", 0), "mutation outcomes"),
        (("overlap", "queue_read_during_graph_mutation", False), "non-overlapping"),
        (("queue", "available", "count", 0), "reconcile"),
        (("checkpoints", "committed_generations", [1, 1]), "generation"),
        (("substitutes", "validation_only", 1), "invalid substitute"),
        (("substitutes", "no_op", 1), "invalid substitute"),
        (("substitutes", "graph_excluded", 1), "invalid substitute"),
        (("substitutes", "ineligible", 1), "invalid substitute"),
        (("substitutes", "wrong_path", 1), "invalid substitute"),
        (("substitutes", "wrong_hash", 1), "invalid substitute"),
        (("substitutes", "unchanged_generation", 1), "invalid substitute"),
        (("recovery", "current_available_after_final_commit_ms", None), "missing recovery"),
        (("structural", "max_snapshots_per_request", 2), "reconcile"),
        (("queue", "available", "p95_ms", 1_000), "reconcile"),
        (("structural", "mutation_full_rebuilds", 1), "whole-graph rebuild"),
    ],
)
def test_gate_fails_closed_for_invalid_runs(change, expected) -> None:
    changed = _changed(relation_review_scale.reference_report(), *change)

    with pytest.raises(relation_review_scale.ScaleGateError, match=expected):
        relation_review_scale.validate_report(changed)


def test_gate_fails_closed_for_an_all_busy_mutation_run() -> None:
    report = relation_review_scale.reference_report()
    report["mutations"].update(
        {
            "committed": 0,
            "busy": 2,
            "graph_completed": 0,
            "checkpoint_exact": 0,
            "distinct_bytes": 0,
            "published_eligible": 0,
            "published_source_hash_bound": 0,
            "published_identity_bound": 0,
        }
    )
    report["checkpoints"]["committed_generations"] = []

    with pytest.raises(relation_review_scale.ScaleGateError, match="all-busy"):
        relation_review_scale.validate_report(report)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: report.update(
            {"sourcePath": "Knowledge Base/Private/customer.md"}
        ),
        lambda report: report["corpus"].update(
            {"private": "Knowledge Base/Private/customer.md"}
        ),
        lambda report: report["queue"].update({"excerpt": "confidential content"}),
        lambda report: report["workload"].update({"hostname": "private-host"}),
        lambda report: report["corpus"].update({"vaultKey": "customer"}),
        lambda report: report["queue"]["available"].update({"unknown": {"nested": 1}}),
        lambda report: report["queue"]["samples"][0].update({"status": "surprise"}),
        lambda report: report["structural"].update({"comment": "unexpected"}),
    ],
)
def test_exact_recursive_schema_rejects_every_unknown_key_or_string(mutate) -> None:
    report = relation_review_scale.reference_report()
    mutate(report)

    with pytest.raises(relation_review_scale.ScaleGateError, match="schema"):
        relation_review_scale.assert_privacy_safe(report)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("corpus", "eligible_pages"), True),
        (("workload", "streams"), 20.0),
        (("overlap", "commit_boundary_entered"), 1),
        (("queue", "availability_ratio"), float("nan")),
        (("checkpoints", "committed_generations"), [41, True]),
    ],
)
def test_exact_recursive_schema_rejects_wrong_scalar_types(path, value) -> None:
    report = relation_review_scale.reference_report()
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(relation_review_scale.ScaleGateError, match="schema"):
        relation_review_scale.assert_privacy_safe(report)


def _valid_overlap() -> relation_review_scale.OverlapEvidence:
    state = relation_review_scale.OverlapCoordinator(required_readers=2)
    with state.hold():
        assert not state.reader_completed(2)
        assert not state.reader_completed(2)
        assert state.reader_completed(3)
        assert state.release_ready
    state.mutation_finished(committed=True)
    return state.evidence()


def test_overlap_state_machine_requires_two_distinct_held_readers_and_commit() -> None:
    evidence = _valid_overlap()
    relation_review_scale.validate_overlap(evidence)

    assert evidence.held_reader_stream_ids == (2, 3)
    assert evidence.release_after_reader_completions == 2
    assert evidence.held_mutation_committed
    assert not evidence.boundary_held_after_release


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"commit_boundary_entered": False}, "entry"),
        ({"held_reader_stream_ids": (2,)}, "distinct"),
        ({"held_reader_stream_ids": (2, 2)}, "distinct"),
        ({"release_after_reader_completions": 1}, "premature"),
        ({"boundary_held_after_release": True}, "release"),
        ({"held_mutation_committed": False}, "mutation"),
    ],
)
def test_overlap_kills_missing_fake_late_premature_duplicate_and_failed_mutants(
    change, expected
) -> None:
    evidence = dataclasses.replace(_valid_overlap(), **change)

    with pytest.raises(relation_review_scale.ScaleGateError, match=expected):
        relation_review_scale.validate_overlap(evidence)


def test_signal_before_entry_and_late_reader_never_count() -> None:
    state = relation_review_scale.OverlapCoordinator(required_readers=2)
    assert not state.reader_completed(2)
    with state.hold():
        assert not state.reader_completed(3)
    assert not state.reader_completed(4)
    state.mutation_finished(committed=True)

    with pytest.raises(relation_review_scale.ScaleGateError, match="distinct"):
        relation_review_scale.validate_overlap(state.evidence())


def test_recovery_requires_post_commit_start_and_covering_current_snapshot() -> None:
    final_commit_ns = 1_000_000_000
    stale = relation_review_scale.reference_queue_sample(
        stream_id=2,
        started_ns=final_commit_ns - 1,
        completed_ns=final_commit_ns + 100_000_000,
        snapshot_generation=42,
    )
    old_generation = relation_review_scale.reference_queue_sample(
        stream_id=3,
        started_ns=final_commit_ns + 1,
        completed_ns=final_commit_ns + 100_000_000,
        snapshot_generation=41,
    )
    current = relation_review_scale.reference_queue_sample(
        stream_id=4,
        started_ns=final_commit_ns + 1,
        completed_ns=final_commit_ns + 200_000_000,
        snapshot_generation=42,
    )

    assert relation_review_scale.qualifying_recovery_ms(
        [stale, old_generation], final_commit_ns=final_commit_ns, final_generation=42
    ) is None
    assert relation_review_scale.qualifying_recovery_ms(
        [stale, old_generation, current],
        final_commit_ns=final_commit_ns,
        final_generation=42,
    ) == 200.0


def test_counting_connection_counts_connection_and_cursor_execute_once() -> None:
    class Cursor:
        def execute(self, *_args, **_kwargs):
            return "cursor"

    class Connection:
        def execute(self, *_args, **_kwargs):
            return "connection"

        def cursor(self, *_args, **_kwargs):
            return Cursor()

    sample = relation_review_scale.QueueCounters()
    connection = relation_review_scale.CountingConnection(Connection(), sample)

    assert connection.execute("SELECT 1") == "connection"
    assert connection.cursor().execute("SELECT 1") == "cursor"
    assert sample.queries == 2


def test_monitor_counts_direct_markdown_reads_parses_walks_opens_and_embedding(
    tmp_path: Path,
) -> None:
    markdown = tmp_path / "source.md"
    markdown.write_text("# source\n", encoding="utf-8")
    measurement = SimpleNamespace(sample=relation_review_scale.QueueCounters())

    with relation_review_scale.monitor_queue_cost(measurement, tmp_path):
        with pytest.raises(relation_review_scale.ScaleGateError, match="Markdown read"):
            markdown.read_text(encoding="utf-8")
        with pytest.raises(relation_review_scale.ScaleGateError, match="Markdown read"):
            markdown.read_bytes()
        with pytest.raises(relation_review_scale.ScaleGateError, match="Markdown open"):
            markdown.open(encoding="utf-8")
        with pytest.raises(relation_review_scale.ScaleGateError, match="Markdown walk"):
            list(find._walk_md(tmp_path))
        with pytest.raises(relation_review_scale.ScaleGateError, match="Markdown parse"):
            find._parse_page(markdown, 0.0, tmp_path)
        with pytest.raises(relation_review_scale.ScaleGateError, match="embedding"):
            embeddings.embed_texts(["hidden"])

    assert measurement.sample.markdown_reads == 3
    assert measurement.sample.markdown_walks == 1
    assert measurement.sample.markdown_parses == 1
    assert measurement.sample.embedding_calls == 1


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"post_hash": "b" * 64}, "checkpoint hash"),
        ({"checkpoint_paths": (("wrong.md", "a" * 64),)}, "checkpoint path"),
        ({"checkpoint_generation": 40}, "generation"),
        ({"post_eligible": False}, "post-mutation eligibility"),
        ({"published_source_hash": "b" * 64}, "published source hash"),
        ({"identity_bound": False}, "identity"),
        ({"substitute": "validation_only"}, "substitute"),
        ({"substitute": "no_op"}, "substitute"),
        ({"substitute": "graph_excluded"}, "substitute"),
        ({"substitute": "ineligible"}, "substitute"),
        ({"substitute": "wrong_path"}, "substitute"),
        ({"substitute": "wrong_hash"}, "substitute"),
        ({"substitute": "unchanged_generation"}, "substitute"),
    ],
)
def test_mutation_record_kills_substitute_path_hash_generation_and_eligibility_mutants(
    change, expected
) -> None:
    attempt = dataclasses.replace(
        relation_review_scale.reference_mutation_attempt(), **change
    )

    with pytest.raises(relation_review_scale.ScaleGateError, match=expected):
        relation_review_scale.validate_mutation_attempt(attempt)


def test_mutation_path_uses_the_governed_request_and_real_fanout() -> None:
    source = inspect.getsource(relation_review_scale.mutate_eligible_page)

    assert "get_manager().invoke" in source
    assert "post_commit_fanout=True" in source
    assert "index_reports=" in source
    assert "upsert_after_write" not in source


def test_real_bounded_orchestration_changes_canonical_bytes_and_current_graph(
    tmp_path: Path,
) -> None:
    root = tmp_path / "semantic-scenario"
    report = relation_review_scale.run_calibrated(
        root=root,
        config=relation_review_scale.ScaleConfig(pages=8, streams=20, groups=8),
        _policy=relation_review_scale.semantic_test_policy(minimum_pages=8),
    )

    assert "Synthetic canonical mutation 1" in (
        root / "Knowledge Base/Notes/Insights/source-0000.md"
    ).read_text(encoding="utf-8")
    assert "Synthetic canonical mutation 2" in (
        root / "Knowledge Base/Notes/Insights/source-0001.md"
    ).read_text(encoding="utf-8")
    checkpoint = graph_sync.read_checkpoint(root)
    assert checkpoint is not None and checkpoint.generation == 2
    assert graph_sync.status(root) == {"state": "current", "generation": 2}
    assert report["overlap"]["held_mutation_committed"] is True
    assert report["recovery"]["current_available_after_final_commit_ms"] is not None
    assert report["mutations"]["published_eligible"] == 2
    assert report["structural"]["mutation_full_rebuilds"] == 0


def test_inactive_post_mutation_is_rejected_by_real_orchestration(
    monkeypatch, tmp_path: Path,
) -> None:
    original = relation_review_scale.mutation_content

    def inactive(content: str, sequence: int) -> str:
        return original(content.replace("status: active", "status: archived"), sequence)

    monkeypatch.setattr(relation_review_scale, "mutation_content", inactive)
    with pytest.raises(
        relation_review_scale.ScaleGateError, match="post-mutation eligibility"
    ):
        relation_review_scale.run_calibrated(
            root=tmp_path / "inactive-scenario",
            config=relation_review_scale.ScaleConfig(pages=4, streams=20, groups=4),
            _policy=relation_review_scale.semantic_test_policy(minimum_pages=4),
        )


def test_cli_dispatches_the_executable_calibrated_api(monkeypatch, capsys, tmp_path) -> None:
    expected = relation_review_scale.reference_report()
    called = []

    def run(*, root, config):
        called.append((root, config.pages, config.streams))
        return expected

    monkeypatch.setattr(relation_review_scale, "run_calibrated", run)
    assert relation_review_scale.main(["--root", str(tmp_path / "synthetic")]) == 0
    assert called == [(tmp_path / "synthetic", 3_600, 20)]
    assert capsys.readouterr().out == relation_review_scale.render_report(expected) + "\n"


def test_six_second_fanout_is_measured_from_the_pre_fanout_commit_seam() -> None:
    clock = relation_review_scale.FakeClock(1_000_000_000)
    capture = relation_review_scale.CommitSeamCapture(clock.now_ns)
    checkpoint = relation_review_scale.reference_checkpoint(generation=42)
    capture.activate(expected_path="Knowledge Base/Notes/Insights/source-0000.md")

    def delayed_fanout(*_args, **_kwargs):
        clock.advance_ms(6_000)
        return True

    wrapped = capture.wrap_post_commit_fanout(
        delayed_fanout, checkpoint_reader=lambda _root: checkpoint
    )
    wrapped(
        Path("/synthetic"),
        [Path("/synthetic/Knowledge Base/Notes/Insights/source-0000.md")],
        [],
        None,
    )
    sample = relation_review_scale.reference_queue_sample(
        stream_id=2,
        started_ns=clock.now_ns() + 1,
        completed_ns=clock.now_ns() + 400_000_000,
        snapshot_generation=42,
    )

    assert capture.commit_seam_ns == 1_000_000_000
    assert relation_review_scale.qualifying_recovery_ms(
        [sample], final_commit_ns=capture.commit_seam_ns, final_generation=42
    ) == 6_400.0
