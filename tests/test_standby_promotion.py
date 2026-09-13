"""Standby warm-up, cutover readiness and promotion (D7-D9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import readiness, service_standby


@pytest.fixture(autouse=True)
def _clean_standby_state():
    service_standby.reset_for_tests()
    readiness.reset()
    yield
    service_standby.reset_for_tests()
    readiness.reset()


def test_a_fresh_process_is_not_a_standby() -> None:
    assert service_standby.in_standby() is False
    assert service_standby.promoted() is False


def test_standby_reports_the_cutover_component_it_waits_on(monkeypatch) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    payload = service_standby.readiness_payload()
    assert payload["standby"] is True
    assert payload["components"] == {"lexical": "ready", "graph_snapshot": "waiting"}
    assert payload["cutover_ready"] is False
    assert service_standby.waiting_component() == "graph_snapshot"


def test_embeddings_join_the_cutover_set_only_when_preload_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: True)
    service_standby.enter_standby()
    payload = service_standby.readiness_payload()
    assert payload["components"] == {
        "lexical": "waiting",
        "embeddings": "waiting",
        "graph_snapshot": "waiting",
    }
    assert service_standby.waiting_component() == "lexical"


def test_cutover_becomes_ready_once_every_component_lands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    assert service_standby.prove_graph_snapshot(tmp_path) is True
    payload = service_standby.readiness_payload()
    assert payload["components"]["graph_snapshot"] == "ready"
    assert payload["cutover_ready"] is True
    assert service_standby.waiting_component() is None


def test_an_unprovable_snapshot_leaves_the_component_waiting(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: None)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    assert service_standby.prove_graph_snapshot(tmp_path) is False
    assert service_standby.readiness_payload()["cutover_ready"] is False


class _Activation:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def test_a_standby_takes_no_lease_and_starts_no_scheduler_until_promotion(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    leases: list[str] = []
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: leases.append("lease"))
    activation = _Activation()
    service_standby.enter_standby()
    service_standby.register_activation(activation)
    service_standby.prove_graph_snapshot(tmp_path)
    assert leases == []
    assert activation.released is False

    record = service_standby.promote(tmp_path, migrated=False)
    assert leases == ["lease"]
    assert activation.released is True
    assert service_standby.promoted() is True
    assert record["snapshot"] == "current"
    assert record["reproved"] is True


def test_promotion_records_an_advanced_checkpoint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    tokens = iter(["checkpoint-1", "checkpoint-2"])
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: next(tokens))
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    record = service_standby.promote(tmp_path, migrated=True)
    assert record["snapshot"] == "advanced"
    assert record["migrated"] is True


def test_a_failed_reproof_promotes_anyway_and_records_the_rebuild(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    tokens = iter(["checkpoint-1", None])
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: next(tokens))
    activation = _Activation()
    service_standby.enter_standby()
    service_standby.register_activation(activation)
    service_standby.prove_graph_snapshot(tmp_path)
    record = service_standby.promote(tmp_path, migrated=True)
    assert record["snapshot"] == "rebuild-after-promotion"
    assert activation.released is True
    assert service_standby.promoted() is True


def test_promotion_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    calls: list[str] = []
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: calls.append("lease"))
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    first = service_standby.promote(tmp_path, migrated=False)
    second = service_standby.promote(tmp_path, migrated=False)
    assert calls == ["lease"]
    assert second["already_promoted"] is True
    assert first.get("already_promoted") is None


def test_a_process_that_never_entered_standby_refuses_promotion(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="standby"):
        service_standby.promote(tmp_path, migrated=False)


def test_readiness_payload_is_absent_outside_standby_and_present_after_promotion(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    assert service_standby.readiness_payload()["standby"] is False
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    service_standby.promote(tmp_path, migrated=False)
    payload = service_standby.readiness_payload()
    assert payload["standby"] is False
    assert payload["cutover_ready"] is True
