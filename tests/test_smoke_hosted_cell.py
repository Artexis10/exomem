"""The smoke harness must fail for the right reasons, and pass for real ones.

The assertion logic is kept free of kubectl so it can be tested against fixture
pod status and index counts. Two traps are pinned here because each produced a
wrong answer against a live cell:

* `status.containerStatuses[].image` is the local *config* digest and never
  equals the manifest digest that was deployed. Checking it reports a mismatch
  against a correctly-rolled pod. `imageID` carries the manifest digest.
* A harness that only ever prints `ok` is worthless, so every check has a
  negative case here as well as a positive one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/scripts/smoke_hosted_cell.py"

RUNTIME = "ghcr.io/artexis10/exomem@sha256:fb6ebd669aa60832ddb135948b58762975639bfc7a642274c722848ae576c430"
CONFIG_DIGEST = "sha256:87df8a72aa810e440824efdb184dc7da3e254d8d527af65f418bba5a5a838a16"


@pytest.fixture(scope="module")
def smoke():
    # Deliberately a path insert and a real import, not
    # `spec_from_file_location`: a module loaded that way is absent from
    # `sys.modules`, and @dataclass then fails resolving its own module with
    # `AttributeError: 'NoneType' object has no attribute '__dict__'`. The same
    # trap already cost time on `hosted_composition_lock.py`.
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import smoke_hosted_cell

        return smoke_hosted_cell
    finally:
        sys.path.pop(0)


def healthy_pod():
    return {
        "status": {
            "containerStatuses": [
                {"name": "exomem", "ready": True, "restartCount": 0,
                 "image": CONFIG_DIGEST, "imageID": RUNTIME}
            ],
            "initContainerStatuses": [
                {"name": "authorization-session-custody", "ready": True, "started": False,
                 "restartCount": 0, "image": CONFIG_DIGEST, "imageID": RUNTIME},
                {"name": "authorization-session-refresh", "ready": True, "started": True,
                 "restartCount": 0, "image": CONFIG_DIGEST, "imageID": RUNTIME},
            ],
        }
    }


def results(report):
    return {c.name: c.ok for c in report.checks}


def test_a_healthy_cell_passes_every_pod_check(smoke):
    report = smoke.Report()
    smoke.evaluate_pod(healthy_pod(), RUNTIME, report)
    assert report.failed == [], report.render()


def test_image_check_reads_imageID_not_the_config_digest(smoke):
    """The trap: `image` holds a config digest that never matches the deployment."""
    report = smoke.Report()
    smoke.evaluate_pod(healthy_pod(), RUNTIME, report)
    assert results(report)["image matches"] is True

    # A pod genuinely running something else must fail, so the check is not
    # passing merely because it looks at a field that happens to be present.
    stale = healthy_pod()
    for container in stale["status"]["containerStatuses"] + stale["status"]["initContainerStatuses"]:
        container["imageID"] = "ghcr.io/artexis10/exomem@sha256:aa61a1cd1f70308c1b702384ebc98e1bb5e3e70b"
    report = smoke.Report()
    smoke.evaluate_pod(stale, RUNTIME, report)
    assert results(report)["image matches"] is False


def test_a_restarted_pod_fails(smoke):
    pod = healthy_pod()
    pod["status"]["containerStatuses"][0]["restartCount"] = 2
    report = smoke.Report()
    smoke.evaluate_pod(pod, None, report)
    assert results(report)["no restarts"] is False


def test_a_missing_renewal_sidecar_fails(smoke):
    """A cell without the 0.73.1 sidecar goes read-only an hour after provisioning."""
    pod = healthy_pod()
    pod["status"]["initContainerStatuses"] = [
        c for c in pod["status"]["initContainerStatuses"] if "refresh" not in c["name"]
    ]
    report = smoke.Report()
    smoke.evaluate_pod(pod, None, report)
    assert results(report)["renewal sidecar present"] is False


def test_a_stopped_renewal_sidecar_fails(smoke):
    pod = healthy_pod()
    for container in pod["status"]["initContainerStatuses"]:
        if "refresh" in container["name"]:
            container["started"] = False
    report = smoke.Report()
    smoke.evaluate_pod(pod, None, report)
    assert results(report)["renewal sidecar running"] is False


def test_a_converged_index_passes_and_finds_the_expected_note(smoke):
    report = smoke.Report()
    smoke.evaluate_index(
        {"pages": 9, "chunks": 16, "semantic_upserts": 0, "full_upserts": 0, "graph_upserts": 0},
        ["Knowledge Base/Notes/Insights/hosted-alpha-held-at-five-users-pending-clean-renewal-week.md"],
        "five-users",
        report,
    )
    assert report.failed == [], report.render()


def test_a_backlogged_index_queue_fails(smoke):
    report = smoke.Report()
    smoke.evaluate_index(
        {"pages": 9, "chunks": 16, "semantic_upserts": 3, "full_upserts": 7, "graph_upserts": 0},
        [],
        None,
        report,
    )
    assert results(report)["index queue drained"] is False


def test_a_note_on_disk_but_absent_from_the_index_fails(smoke):
    """The failure mode worth catching: durable write, unfindable by recall."""
    report = smoke.Report()
    smoke.evaluate_index(
        {"pages": 9, "chunks": 16, "semantic_upserts": 0, "full_upserts": 0, "graph_upserts": 0},
        ["Knowledge Base/Notes/index.md"],
        "five-users",
        report,
    )
    assert results(report)["expected note indexed"] is False


def test_an_empty_index_fails_even_with_a_drained_queue(smoke):
    report = smoke.Report()
    smoke.evaluate_index(
        {"pages": 0, "chunks": 0, "semantic_upserts": 0, "full_upserts": 0, "graph_upserts": 0},
        [],
        None,
        report,
    )
    assert results(report)["lexical index populated"] is False
    assert results(report)["embeddings populated"] is False
