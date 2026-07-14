from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    path = ROOT / "infra/helm/platform/files/scheduler_runtime.py"
    spec = importlib.util.spec_from_file_location("hosted_scheduler_runtime_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scheduler_histogram_is_cumulative_and_persistent() -> None:
    module = _load()
    state = module.initial_state("exomem-reconcile", 60)
    state = module.record_attempt(state, success=False, duration_seconds=3.5, observed_at=1_000)
    state = module.record_attempt(state, success=True, duration_seconds=0.25, observed_at=1_060)

    assert state["attempts_total"] == 2
    assert state["failures_total"] == 1
    assert state["consecutive_failures"] == 0
    assert state["last_success_unixtime"] == 1_060
    assert state["duration_seconds"] == {
        "buckets": {"1": 1, "5": 2, "20": 2, "+Inf": 2},
        "count": 2,
        "sum": 3.75,
    }
    metrics = module.render_metrics([state], module.initial_alert_state())
    assert (
        'exomem_hosted_scheduler_duration_seconds_bucket{contract_version="1",job="exomem-reconcile",le="5"} 2'
        in metrics
    )
    assert (
        'exomem_hosted_scheduler_duration_seconds_sum{contract_version="1",job="exomem-reconcile"} 3.75'
        in metrics
    )


def test_scheduler_alerts_transition_at_180_seconds_and_two_failures() -> None:
    module = _load()
    state = module.initial_state("exomem-reconcile", 60)
    alert_state, transitions = module.evaluate_alerts(
        [state],
        module.initial_alert_state(),
        observed_at=1_000,
        missed_run_seconds=180,
        failure_threshold=2,
    )
    assert transitions == []

    alert_state, transitions = module.evaluate_alerts(
        [state],
        alert_state,
        observed_at=1_239,
        missed_run_seconds=180,
        failure_threshold=2,
    )
    assert transitions == []
    alert_state, transitions = module.evaluate_alerts(
        [state],
        alert_state,
        observed_at=1_240,
        missed_run_seconds=180,
        failure_threshold=2,
    )
    assert transitions == [{"job": "exomem-reconcile", "alert": "missed-run", "active": True}]

    state = module.record_attempt(state, success=False, duration_seconds=0.1, observed_at=1_241)
    state = module.record_attempt(state, success=False, duration_seconds=0.1, observed_at=1_242)
    alert_state, transitions = module.evaluate_alerts(
        [state],
        alert_state,
        observed_at=1_242,
        missed_run_seconds=180,
        failure_threshold=2,
    )
    assert {item["alert"] for item in transitions} == {
        "missed-run",
        "consecutive-failures",
    }
    assert next(item for item in transitions if item["alert"] == "missed-run")["active"] is False
    assert (
        next(item for item in transitions if item["alert"] == "consecutive-failures")["active"]
        is True
    )


def test_scheduler_transport_rejects_redirects_and_non_exact_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()

    class Response:
        status = 200

        def __init__(self, final_url: str) -> None:
            self.final_url = final_url

        def geturl(self) -> str:
            return self.final_url

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        final_url = "https://substratesystems.io/other"

        def open(self, _request: object, timeout: int) -> Response:
            assert timeout == 5
            return Response(self.final_url)

    opener = Opener()
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_args: opener)
    target = "https://substratesystems.io/api/cron/exomem-reconcile"
    assert module._exact_https_request(target, "bearer", 5) is False
    opener.final_url = target
    assert module._exact_https_request(target, "bearer", 5) is True
    with pytest.raises(ValueError, match="exact HTTPS"):
        module._exact_https_request(
            "http://substratesystems.io/api/cron/exomem-reconcile", "bearer", 5
        )


def test_alert_evaluator_scrapes_content_free_snapshot_and_requires_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    state = module.record_attempt(
        module.initial_state("exomem-reconcile", 60),
        success=False,
        duration_seconds=0.25,
        observed_at=1_000,
    )
    snapshot = module.render_snapshot([state])
    assert json.loads(snapshot) == {"schema_version": 1, "states": [state]}
    assert "authorization" not in snapshot.lower()

    target = "https://alerts.example.invalid/hooks/opaque"

    class Response:
        status = 204

        def __init__(self, final_url: str) -> None:
            self.final_url = final_url

        def geturl(self) -> str:
            return self.final_url

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Opener:
        final_url = target

        def open(self, request: object, timeout: int) -> Response:
            assert timeout == 10
            headers = dict(request.header_items())
            assert headers["X-exomem-alert-transition"] == "transition-0001"
            assert b"exomem-reconcile" in request.data
            return Response(self.final_url)

    opener = Opener()
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_args: opener)
    transition = {"job": "exomem-reconcile", "alert": "missed-run", "active": True}
    first_id = module.transition_identifier(module.initial_alert_state(), transition, 0)
    assert first_id == module.transition_identifier(module.initial_alert_state(), transition, 0)
    later_state = module.initial_alert_state()
    later_state["transitions_total"] = 1
    assert module.transition_identifier(later_state, transition, 0) != first_id
    module.deliver_transition(
        transition,
        webhook_url=target,
        transition_id="transition-0001",
    )
    opener.final_url = "https://alerts.example.invalid/redirected"
    with pytest.raises(RuntimeError, match="delivery failed"):
        module.deliver_transition(
            transition,
            webhook_url=target,
            transition_id="transition-0001",
        )
