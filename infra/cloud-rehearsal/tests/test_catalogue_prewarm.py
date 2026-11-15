"""The catalogue-prewarm mode reads a live cluster, so its readings of Kubernetes objects, logs and clocks
are checked here; the scenarios themselves run in the `catalogue-prewarm` workflow mode.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from decimal import Decimal

import pytest

from cloud_rehearsal import bump_parser_version, images
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
    samples = [{"mono": 0.0, "cpu_usage_usec": 0, "memory_current": 100, "memory_peak": 100, "memory_anon": 50},
               {"mono": 10.0, "cpu_usage_usec": 10_000_000, "memory_current": 300, "memory_peak": 900, "memory_anon": 200},
               {"mono": 20.0, "cpu_usage_usec": 40_000_000, "memory_current": 200, "memory_peak": 900, "memory_anon": 100}]

    peaks = cp.reduce_samples(samples)
    one = cp.reduce_samples(samples[:1])

    assert peaks["cpu_cores_peak"] == 3.0 and peaks["memory_peak_bytes"] == 900 and peaks["anon_peak_bytes"] == 200
    assert one["cpu_cores_peak"] is None and "cpu" in one["unknown"]


def test_memory_falls_back_to_the_highest_current_reading_when_the_kernel_has_no_peak_counter():
    samples = [{"mono": 0.0, "cpu_usage_usec": 0, "memory_current": 100, "memory_peak": None, "memory_anon": None},
               {"mono": 5.0, "cpu_usage_usec": 5_000_000, "memory_current": 700, "memory_peak": None, "memory_anon": None}]

    peaks = cp.reduce_samples(samples)

    assert peaks["memory_peak_bytes"] == 700 and "current" in peaks["memory_peak_source"]


# Logs as a cloud cell does: the product's redaction and JSON formatter, two adoption and route events and a
# plain line, which the redaction blanks.
CLOUD_CELL_LOG = """
import json, logging, sys
sys.path.insert(0, sys.argv[1])
from exomem import privacy_log
from exomem.log_events import JsonLinesFormatter, log_event
privacy_log.install_hosted_log_redaction()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonLinesFormatter())
log = logging.getLogger("exomem.rehearsal")
log.addHandler(handler)
log.setLevel(logging.INFO)
log_event(log, logging.INFO, sys.argv[2], fields={"outcome": "adopted", "staged": 1, "cleanup": "done"})
log.info("lexical catalogue pages materialized route=rebuilt")
log_event(log, logging.INFO, sys.argv[3], fields={"route": "rebuilt", "basis": "identity", "reparsed": 0, "removed": 0,
                                                  "inserted": 12000, "unchanged": 0, "elapsed_ms": 88000})
"""


def test_the_adoption_and_rebuild_evidence_survives_a_cloud_cells_log_redaction():
    # A cloud cell blanks every plain log line, so reading free text would see no adoption and no rebuild at all.
    result = subprocess.run([sys.executable, "-c", CLOUD_CELL_LOG, str(images.REPO_ROOT / "src"), cp.ADOPTION_EVENT,
                             cp.ROUTE_EVENT], env={**os.environ, "EXOMEM_CLOUD_CELL": "1"}, capture_output=True,
                            text=True, check=True)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    events = [{"event": r["event"], "fields": r.get("fields") or {}} for r in records]

    log = cp.catalogue_log(events)

    assert "materialized" not in records[1]["message"]  # the redaction ran, so this is what a cloud cell keeps
    assert log["adoption"] == ["adopted"]
    assert [r["inserted"] for r in log["rebuilt"]] == [12000] and log["reconciled"] == []


def _watched(kind: str, uid: str, *, build: bool, deleting: bool = False) -> str:
    metadata = {"name": f"pod-{uid}", "uid": uid, "labels": BUILD_LABELS if build else {"app": "cell"},
                **({"deletionTimestamp": "2026-01-01T10:05:00Z"} if deleting else {})}
    return json.dumps({"type": kind, "object": {"kind": "Pod", "metadata": metadata}}, indent=4)


def test_the_build_counts_as_gone_before_the_stop_only_when_the_watch_saw_its_pod_deleted_first():
    # kubectl prints each watch event as an indented object; the order of those events is the only proof of "before".
    serving_stops = _watched("MODIFIED", "old", build=False, deleting=True)
    build_deleted = _watched("DELETED", "b1", build=True)
    start = [_watched("ADDED", "old", build=False), _watched("ADDED", "b1", build=True)]

    in_order = cp.watch_events("\n".join([*start, build_deleted, serving_stops]))
    too_late = cp.watch_events("\n".join([*start, serving_stops, build_deleted]))
    cut_short = cp.watch_events("\n".join([*start, build_deleted]) + '\n{"type": "MOD')

    assert cp.stop_order(in_order, "old")[0] is True
    assert cp.stop_order(too_late, "old")[0] is False
    assert cp.stop_order(cut_short, "old") == (False, "the pod watch saw no stop of the serving pod")


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


def test_a_scenario_that_would_cross_midnight_waits_until_just_after_it_and_one_that_ends_before_starts_at_once():
    # cellctl re-dates the window annotations at 00:00 UTC; a wrong day here would hold a run for a day or move its window.
    late = dt.datetime(2026, 1, 1, 23, 30, 0, tzinfo=UTC)

    assert cp.seconds_until_day_fits(late, 45 * 60) == 35 * 60
    assert cp.seconds_until_day_fits(late, 20 * 60) == 0


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


RELEASE = "registry.test/cell:v3"
SETTLED_ROW = {"observed_state": "running", "ready": True, "hold_kind": None, "observed_image": RELEASE}


def _cell_statefulset(**annotations: str) -> dict:
    return {"kind": "StatefulSet", "metadata": {"name": "cell", "annotations": {
        "exomem.io/upgrade-window": "4 default", **annotations}}}


def test_a_hold_annotation_left_after_the_ready_exit_blocks_the_next_scenario_while_the_row_still_reads_ready():
    # cellctl acts on the left annotations at its next pass and stops the upgraded cell on its previous image;
    # a gate that read only the row would start the next scenario on that cell.
    left = _cell_statefulset(**{"exomem.io/hold": "upgrade", "exomem.io/previous-image": "registry.test/cell:v2"})

    failed = cp.scenario_gate(SETTLED_ROW, left, {"paused": False}, RELEASE)

    assert list(failed) == ["statefulset_carries_no_hold"]
    assert "exomem.io/previous-image" in failed["statefulset_carries_no_hold"]


def test_a_cell_held_again_on_its_previous_image_fails_the_serving_and_the_image_gates():
    held = {"observed_state": "stopped", "ready": False, "hold_kind": "upgrade", "observed_image": "registry.test/cell:v2"}

    failed = cp.scenario_gate(held, _cell_statefulset(), {"paused": False}, RELEASE)

    assert set(failed) == {"cell_serving_with_no_hold", "cell_on_the_released_image"}


def test_a_paused_rollout_blocks_the_next_scenario_even_when_the_cell_serves_its_release():
    # Only an operator resumes the rollout, and the next release would never reach the cell.
    paused = {"paused": True, "error_code": "upgrade_failed", "held_cell_id": "0123456789abcdef"}

    failed = cp.scenario_gate(SETTLED_ROW, _cell_statefulset(), paused, RELEASE)

    assert list(failed) == [cp.ROLLOUT_GATE] and "upgrade_failed" in failed[cp.ROLLOUT_GATE]


def test_a_settled_cell_passes_an_unpublished_release_is_not_judged_and_an_unread_statefulset_never_passes():
    on_older = {**SETTLED_ROW, "observed_image": "registry.test/cell:v2"}

    assert cp.scenario_gate(SETTLED_ROW, _cell_statefulset(), {}, RELEASE) == {}
    assert cp.scenario_gate(on_older, _cell_statefulset(), {}, None) == {}
    assert list(cp.scenario_gate(SETTLED_ROW, None, {}, RELEASE)) == ["statefulset_carries_no_hold"]
