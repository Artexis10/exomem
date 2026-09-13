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


def test_the_real_standby_warm_never_publishes_index_state(monkeypatch, tmp_path: Path) -> None:
    """A standby proves the catalog; it never reconciles or repairs it.

    `ensure_fresh` discards verified state and mutates the live sidecar under
    the per-vault publication barrier, and `request_repair` schedules a
    publication. Both belong to the worker still serving. This runs the real
    `warm()` and pins that neither is reached and no sidecar byte changes.
    """
    from exomem import lexstore, state_paths

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "Notes").mkdir(parents=True)
    (vault / "Knowledge Base" / "Notes" / "one.md").write_text(
        "---\ntype: note\n---\n\n# One\n\nBody.\n", encoding="utf-8"
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    forbidden: list[str] = []
    monkeypatch.setattr(
        lexstore, "ensure_fresh", lambda root: forbidden.append("ensure_fresh")
    )
    monkeypatch.setattr(
        lexstore, "request_repair", lambda root: forbidden.append("request_repair")
    )
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: None)

    def _fingerprint() -> dict[str, bytes]:
        import hashlib

        state = state_paths.vault_state_dir(vault)
        prints: dict[str, bytes] = {}
        if not state.exists():
            return prints
        for path in sorted(state.rglob("*")):
            if path.is_file():
                prints[str(path.relative_to(state))] = hashlib.sha256(
                    path.read_bytes()
                ).digest()
        return prints

    before = _fingerprint()
    service_standby.enter_standby()
    service_standby.warm(vault)

    assert forbidden == [], forbidden
    assert _fingerprint() == before, "a standby must not publish index state"


def test_an_uncurrent_catalog_leaves_lexical_as_the_waiting_component(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "prove_retrieval_catalog", lambda root: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    service_standby.enter_standby()
    service_standby.warm(tmp_path)
    assert service_standby.waiting_component() == "lexical"
    assert service_standby.readiness_payload()["cutover_ready"] is False


def test_the_catalog_proof_never_schedules_a_repair(monkeypatch, tmp_path: Path) -> None:
    from exomem import lexstore

    seen: list[dict] = []

    def _current(root, *, require_live_projection=True, schedule_repair=True):
        seen.append({"schedule_repair": schedule_repair})
        return True

    monkeypatch.setattr(lexstore, "maintained_content_index_enabled", lambda: True)
    monkeypatch.setattr(lexstore, "runtime_retrieval_catalog_current", _current)
    assert service_standby.prove_retrieval_catalog(tmp_path) is True
    assert seen == [{"schedule_repair": False}]
