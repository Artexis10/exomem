from __future__ import annotations

import copy
import inspect

import pytest

from scripts import relation_review_scale


def test_calibrated_report_contract_requires_real_graph_mutations_and_bounded_reads() -> None:
    config = relation_review_scale.ScaleConfig(pages=3_600, streams=20)
    config.validate()
    report = relation_review_scale.reference_report()

    assert report["corpus"]["eligible_pages"] >= 3_600
    assert report["workload"]["streams"] == 20
    assert report["overlap"] == {
        "commit_boundary_entered": True,
        "queue_reads_completed_while_commit_boundary_held": 2,
        "queue_read_during_graph_mutation": True,
    }
    assert report["mutations"]["committed"] >= 2
    assert len(report["checkpoints"]["committed_generations"]) >= 2
    assert report["checkpoints"]["committed_generations"] == sorted(
        set(report["checkpoints"]["committed_generations"])
    )
    assert report["queue"]["available"]["count"] >= 18
    assert report["queue"]["availability_ratio"] >= 0.90
    assert report["recovery"]["current_available_after_final_commit_ms"] <= 5_000
    assert report["structural"]["requests_measured"] >= 20
    assert report["structural"]["snapshot_count"] == report["structural"]["requests_measured"]
    assert report["structural"]["max_snapshots_per_request"] == 1
    assert report["structural"]["max_indexed_queries_per_request"] <= 12
    assert report["structural"]["markdown_parses"] == 0
    assert report["structural"]["embedding_calls"] == 0
    assert report["queue"]["available"]["p95_ms"] < 1_000
    assert report["queue"]["available"]["max_ms"] < 2_000
    if report["queue"]["unavailable"]["count"]:
        assert report["queue"]["unavailable"]["p95_ms"] < 250
    relation_review_scale.validate_report(report)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("mutations", "committed", 0), "fewer than two"),
        (("overlap", "queue_read_during_graph_mutation", False), "non-overlapping"),
        (("queue", "available", "count", 0), "all-warming"),
        (("checkpoints", "committed_generations", [1, 1]), "unchanged checkpoint"),
        (("substitutes", "validation_only", 1), "invalid substitute"),
        (("substitutes", "no_op", 1), "invalid substitute"),
        (("substitutes", "graph_excluded", 1), "invalid substitute"),
        (("recovery", "current_available_after_final_commit_ms", None), "missing recovery"),
        (("structural", "max_snapshots_per_request", 2), "structural"),
        (("structural", "max_indexed_queries_per_request", 13), "structural"),
        (("structural", "markdown_parses", 1), "structural"),
        (("queue", "available", "p95_ms", 1_000), "timing"),
    ],
)
def test_gate_fails_closed_for_invalid_runs(change, expected) -> None:
    report = relation_review_scale.reference_report()
    changed = copy.deepcopy(report)
    target = changed
    *parents, key, value = change
    for parent in parents:
        target = target[parent]
    target[key] = value

    with pytest.raises(relation_review_scale.ScaleGateError, match=expected):
        relation_review_scale.validate_report(changed)


def test_gate_fails_closed_for_an_all_busy_mutation_run() -> None:
    report = relation_review_scale.reference_report()
    report["mutations"] = {"committed": 0, "busy": 20, "failed": 0}

    with pytest.raises(relation_review_scale.ScaleGateError, match="all-busy"):
        relation_review_scale.validate_report(report)


def test_report_is_privacy_safe_and_cli_renders_only_aggregate_fields() -> None:
    report = relation_review_scale.reference_report()
    relation_review_scale.assert_privacy_safe(report)
    rendered = relation_review_scale.render_report(report)
    assert "source-" not in rendered
    assert "hostname" not in rendered.lower()


def test_mutation_path_uses_the_governed_request_and_real_fanout() -> None:
    source = inspect.getsource(relation_review_scale._mutate_eligible_page)

    assert "get_manager().invoke" in source
    assert "post_commit_fanout=True" in source
    assert "mark_active_mutation_committed" in source
    assert "upsert_after_write" not in source


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
