"""The catalogue-prewarm mode reads a live cluster, so its readings of Kubernetes objects, logs and clocks
are checked here; the scenarios themselves run in the `catalogue-prewarm` workflow mode.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from cloud_rehearsal import bump_parser_version
from cloud_rehearsal import catalogue_prewarm as cp

BUILD_LABELS = {"exomem.io/cell-job": "catalogue-build"}
UTC = dt.UTC


def _job(**status) -> dict:
    return {"kind": "Job", "metadata": {"name": "catalogue-build-0123456789abcdef", "labels": BUILD_LABELS,
                                        "creationTimestamp": "2026-01-01T10:00:00Z"},
            "spec": {"activeDeadlineSeconds": 1800}, "status": status}


def _pod(*, node: str | None = None, phase: str = "Pending", unschedulable: bool = False, labels=BUILD_LABELS) -> dict:
    conditions = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                   "message": "0/1 nodes are available: 1 Insufficient cpu."}] if unschedulable else []
    return {"kind": "Pod", "metadata": {"name": "catalogue-build-0123456789abcdef-x", "labels": labels},
            "spec": {"nodeName": node} if node else {}, "status": {"phase": phase, "conditions": conditions}}


def _manifest_source(line: str) -> str:
    return f'"""Parser notes: PARSER_VERSION = 1 is the old value."""\n\n# PARSER_VERSION = 2\n{line}\nOTHER = 3\n'


def test_the_image_script_raises_only_the_assignment_and_not_a_mention_of_the_name():
    # A text replace would also edit the docstring or the comment, and the image would keep its old catalogue identity.
    source = _manifest_source("PARSER_VERSION = 9  # bumped by the parser change")

    raised = bump_parser_version.bumped(source, 2)

    assert "PARSER_VERSION = 11  # bumped" in raised
    assert raised.replace("PARSER_VERSION = 11", "PARSER_VERSION = 9") == source


def test_the_image_script_keeps_the_text_before_a_non_ascii_assignment_intact():
    # ast offsets count UTF-8 bytes; slicing the string by them would corrupt a line that starts with a wide character.
    source = 'LABEL = "é"; PARSER_VERSION = 9\n'

    assert bump_parser_version.bumped(source, 1) == 'LABEL = "é"; PARSER_VERSION = 10\n'


def test_the_image_script_refuses_a_file_without_the_assignment_instead_of_building_an_unchanged_image():
    with pytest.raises(SystemExit, match="PARSER_VERSION"):
        bump_parser_version.bumped("OTHER = 1\n", 1)


def test_a_build_pod_bound_to_a_node_counts_as_running_and_an_unbound_one_as_waiting():
    # Scenario 3 relies on cellctl treating the unbound build as waiting, which does not defer the attempt.
    running = cp.build_view([_job(), _pod(node="node-1", phase="Pending")])
    waiting = cp.build_view([_job(), _pod(unschedulable=True)])

    assert (running["state"], running["bound"]) == ("running", True)
    assert (waiting["state"], waiting["bound"]) == ("waiting", False)
    assert waiting["pods"][0]["unschedulable"] and "Insufficient cpu" in waiting["pods"][0]["message"]


def test_a_completed_job_is_complete_even_though_its_pod_is_no_longer_running():
    view = cp.build_view([_job(succeeded=1, conditions=[{"type": "Complete", "status": "True"}],
                               startTime="2026-01-01T10:00:05Z", completionTime="2026-01-01T10:03:05Z"),
                          _pod(node="node-1", phase="Succeeded")])

    assert view["state"] == "complete"
    assert cp._seconds(view["started_at"], view["completed_at"]) == 180


def test_the_cells_own_pod_is_not_taken_for_a_build_pod():
    items = [_pod(node="node-1", phase="Running", labels={"app": "cell"}), _job()]

    assert cp.build_view(items)["pods"] == []


def test_a_build_that_vanished_unfinished_is_cancelled_and_one_that_hit_its_deadline_is_timed_out():
    # Reading both as "completed" would let a cancelled build pass scenario 2's precondition.
    running = cp.build_view([_job(startTime="2026-01-01T10:00:05Z"), _pod(node="n", phase="Running")])
    expired = cp.build_view([_job(failed=1, startTime="2026-01-01T10:00:05Z",
                                  conditions=[{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}])])
    gone_at = dt.datetime(2026, 1, 1, 10, 2, 0, tzinfo=UTC)

    assert cp.build_outcome(running, gone_at)["outcome"] == "cancelled"
    assert cp.build_outcome(running, None)["outcome"] == "still_running"
    assert cp.build_outcome(expired, None)["outcome"] == "timed_out"
    assert cp.build_outcome(None, None)["outcome"] == "never_created"


def test_the_old_pod_counts_as_stopping_when_it_is_terminating_or_gone_and_the_new_pod_when_it_is_ready():
    def cell_pod(uid, *, terminating=False, ready_at=None):
        metadata = {"name": f"cell-0-{uid}", "uid": uid, **({"deletionTimestamp": "2026-01-01T10:05:00Z"} if terminating else {})}
        status = {"conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": ready_at}]} if ready_at else {}
        return {"kind": "Pod", "metadata": metadata, "status": status}

    assert cp.serving_view([cell_pod("old")], "old")["old"] == "serving"
    assert cp.serving_view([cell_pod("old", terminating=True)], "old")["old"] == "terminating"
    after = cp.serving_view([cell_pod("new", ready_at="2026-01-01T10:06:00Z")], "old")
    assert (after["old"], after["new_ready_at"]) == ("gone", "2026-01-01T10:06:00Z")


def test_peaks_come_from_the_busiest_interval_and_a_single_sample_is_reported_unknown_not_zero():
    samples = [{"mono": 0.0, "usage_usec": 0, "current": 100, "peak": 100, "anon": 50},
               {"mono": 10.0, "usage_usec": 10_000_000, "current": 300, "peak": 900, "anon": 200},
               {"mono": 20.0, "usage_usec": 40_000_000, "current": 200, "peak": 900, "anon": 100}]

    peaks = cp.reduce_samples(samples)
    one = cp.reduce_samples(samples[:1])

    assert peaks["cpu_cores_peak"] == 3.0 and peaks["memory_peak_bytes"] == 900 and peaks["anon_peak_bytes"] == 200
    assert one["cpu_cores_peak"] is None and "cpu" in one["unknown"]


def test_memory_falls_back_to_the_highest_current_reading_when_the_kernel_has_no_peak_counter():
    samples = [{"mono": 0.0, "usage_usec": 0, "current": 100, "peak": None, "anon": None},
               {"mono": 5.0, "usage_usec": 5_000_000, "current": 700, "peak": None, "anon": None}]

    peaks = cp.reduce_samples(samples)

    assert peaks["memory_peak_bytes"] == 700 and "current" in peaks["memory_peak_source"]


def _line(message: str) -> str:
    return json.dumps({"level": "INFO", "message": message})


def test_a_cold_rebuild_is_told_from_a_delta_catch_up_by_the_products_own_log_lines():
    # The scenarios treat a cold rebuild as proof that no staged copy was adopted, so a catch-up must not read as one.
    cold = cp.catalogue_log([_line("lexical sync: rebuilding /x from 12000 markdown file(s)"),
                             _line("lexical catalogue pages materialized route=rebuilt basis=full reparsed=12000 "
                                   "removed=0 inserted=12000 unchanged=0 elapsed_ms=88000"), "not json"])
    catch_up = cp.catalogue_log([_line("lexical catalogue pages materialized route=reconciled basis=delta "
                                       "reparsed=1 removed=0 inserted=1 unchanged=11999 elapsed_ms=900")])

    assert len(cold["cold_rebuild_lines"]) == 2 and cold["cold_rebuild_ms"] == 88000
    assert catch_up["cold_rebuild_lines"] == [] and catch_up["reconciled"][0]["inserted"] == 1


def test_the_forced_boundary_is_a_whole_minute_the_chart_accepts_after_the_restart_margin():
    now = dt.datetime(2026, 1, 1, 10, 5, 20, tzinfo=UTC)

    window = cp.plan_force_window(now, lead_seconds=90)

    assert window.boundary_at == dt.datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    assert window.force_minutes == 30 and window.window_hour == 10
    assert window.start_at == window.boundary_at - dt.timedelta(seconds=90)


def test_a_boundary_too_late_in_the_hour_moves_to_the_next_hours_half_past():
    # forceMinutes counts back from the hour end and is at most 30, so the next hour half past is the first fit.
    now = dt.datetime(2026, 1, 1, 10, 58, 0, tzinfo=UTC)

    window = cp.plan_force_window(now, lead_seconds=60)

    assert window.boundary_at == dt.datetime(2026, 1, 1, 11, 30, tzinfo=UTC) and window.window_hour == 11


def test_the_window_opens_at_once_when_the_work_ends_before_the_boundary_and_waits_for_the_next_hour_otherwise():
    early = dt.datetime(2026, 1, 1, 10, 10, 0, tzinfo=UTC)
    late = dt.datetime(2026, 1, 1, 10, 55, 0, tzinfo=UTC)

    assert cp.seconds_until_window_fits(early, 540, 1) == 0
    assert cp.seconds_until_window_fits(late, 540, 1) == 5 * 60 + cp.WINDOW_OPEN_MARGIN
    with pytest.raises(ValueError, match="cannot fit"):
        cp.seconds_until_window_fits(early, 3500, 1)


def _node(cpu: str) -> dict:
    return {"metadata": {"name": "node-1"}, "status": {"allocatable": {"cpu": cpu}}}


def _requesting(cpu: str, *, node: str = "node-1", phase: str = "Running", init: str | None = None) -> dict:
    spec = {"nodeName": node, "containers": [{"resources": {"requests": {"cpu": cpu}}}]}
    if init:
        spec["initContainers"] = [{"resources": {"requests": {"cpu": init}}}]
    return {"spec": spec, "status": {"phase": phase}}


def test_free_cpu_counts_the_scheduler_view_of_running_pods_and_not_finished_ones():
    # Counting a finished pod, or an init container's request twice, would size the filler wrong and let the build bind.
    pods = [_requesting("500m"), _requesting("250m", init="1"), _requesting("2", phase="Succeeded"),
            _requesting("3", node="node-2")]

    assert cp.free_cpu(_node("4"), pods) == Decimal("2.5")


def test_the_filler_leaves_less_than_the_build_requests_and_is_skipped_when_the_node_is_already_that_full():
    assert cp.filler_millicores(Decimal("3"), "2") == 1500
    assert cp.filler_millicores(Decimal("1.5"), "2") is None


def test_manifest_diff_names_changed_removed_and_added_paths():
    before = {"a.md": [1, 1, "x"], "b.md": [2, 2, "y"], "c.md": [3, 3, "z"]}
    after = {"a.md": [1, 1, "x"], "b.md": [2, 9, "y"], "d.md": [4, 4, "w"]}

    assert cp.diff_manifests(before, after) == {"changed": ["b.md"], "removed": ["c.md"], "added": ["d.md"]}
