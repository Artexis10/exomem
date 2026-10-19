"""Integrity is never hidden by quieting upkeep (design §5.1).

The dreamer filters nothing but its own families. Quieting every `upkeep_*`
family, setting `structural_suggestions=off`, pausing the worker or crashing it
leaves these byte-identical: `due_state` (with `supersession_integrity`), the
attention union, audit output and the relation queue.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from types import SimpleNamespace

import dreamer_fixture as fx
import pytest

from exomem import (
    attention,
    audit,
    commands,
    dreamer,
    dreamer_families,
    dreamer_policy,
    dreamer_store,
    due_state,
    envelope,
    foreground_activity,
    freshness,
    mode,
    relation_queue,
    resource_status,
    review_state,
    upkeep,
    writer_lease,
)
from exomem.governance.principal import library_scope

NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
LATER = time.time() + 3 * 3600
BROKEN = f"{fx.KB}/Notes/Insights/pump-priming.md"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    upkeep.reset_delivery_state()
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()
    upkeep.reset_delivery_state()


def _vault(tmp_path: Path) -> Path:
    """The upkeep positives, plus two integrity defects the dreamer must not touch:
    a successor pointer to a page that does not exist, and a link to one."""
    vault = fx.build(tmp_path, with_graph=False)
    text = fx.insight(
        "Pump priming",
        sources=["field-report-three"],
        updated="2026-05-04",
        status="superseded",
        links="Priming follows the [[Notes/Insights/no-such-procedure]] checklist.",
        observation="Priming takes two minutes.",
    )
    successor = 'superseded_by: "[[Notes/Insights/no-such-successor]]"\n'
    fx.write(
        vault, BROKEN, text.replace("status: superseded\n", f"status: superseded\n{successor}")
    )
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    for now in (None, LATER):
        results = fx.run_to_quiet(vault, now=now)
        assert all(result.stop_reason != "error" for result in results), results
    view = dreamer_store.read_view(vault)
    families = {row["family"] for row in view.candidates if row["state"] == "open"}
    assert families == {
        dreamer_families.LINK_FAMILY,
        dreamer_families.HYDRATION_FAMILY,
        dreamer_families.PROFILE_FAMILY,
    }
    return vault


def _surfaces(vault: Path) -> dict[str, str]:
    """Each integrity surface, serialised exactly."""
    union = attention.attention(vault, today=TODAY, now=NOW, record_surfacing=False)
    report = audit.audit(vault, today=TODAY, now=NOW)
    return {
        "due_state": json.dumps(
            due_state.recompute(vault, today=TODAY, now=NOW), sort_keys=True, default=str
        ),
        "attention": json.dumps(union.as_dict(), sort_keys=True, default=str),
        "audit": json.dumps(report.as_dict(), sort_keys=True, default=str),
        "relations": json.dumps(
            relation_queue.build_queue(vault, today=TODAY), sort_keys=True, default=str
        ),
    }


def _non_vacuous(surfaces: dict[str, str]) -> None:
    due = json.loads(surfaces["due_state"])
    assert due["categories"]["supersession_integrity"], "no supersession defect to hide"
    categories = {finding["category"] for finding in json.loads(surfaces["audit"])["findings"]}
    assert {"supersession_integrity", "forward_reference"} <= categories, categories
    assert json.loads(surfaces["attention"])["items"], "an empty union proves nothing"


def _quiet_families(vault: Path, disposition: str) -> None:
    store = review_state.ReviewStateStore(vault)
    for family in dreamer_families.REGISTRY:
        store.set_disposition(family.name, disposition, why="too_frequent: quiet upkeep")


def _structural_off(vault: Path) -> None:
    envelope.set_disposition("structural_suggestions", "off")
    assert envelope.active().get("structural_suggestions") == "off"


def _pause(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DREAMER", "paused")
    assert dreamer.setting() == "paused"


def _crash(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self):
        raise RuntimeError("broken sidecar")

    monkeypatch.setattr(dreamer_store.DreamerStore, "connect", broken)
    for _ in range(3):
        assert dreamer.run_once(vault).stop_reason == "error"
    assert dreamer.status()["state"] == "failed"


@pytest.mark.parametrize(
    "mode", ["families_quiet", "families_off", "structural_off", "paused", "crashed"]
)
def test_quieting_upkeep_leaves_integrity_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    vault = _vault(tmp_path)
    before = _surfaces(vault)
    _non_vacuous(before)
    if mode == "families_quiet":
        _quiet_families(vault, "quiet")
    elif mode == "families_off":
        _quiet_families(vault, "off")
    elif mode == "structural_off":
        _structural_off(vault)
    elif mode == "paused":
        _pause(vault, monkeypatch)
    else:
        _crash(vault, monkeypatch)
    if mode not in {"paused", "crashed"}:
        # The worker keeps detecting under a quiet delivery.
        results = fx.run_to_quiet(vault, now=LATER + 3600)
        assert all(result.stop_reason != "error" for result in results), results
    after = _surfaces(vault)
    for surface in before:
        assert after[surface] == before[surface], (mode, surface)


@pytest.mark.parametrize("mode", ["families_quiet", "crashed"])
def test_capture_and_recall_continue_when_optional_upkeep_is_quiet_or_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    vault = _vault(tmp_path)
    if mode == "families_quiet":
        _quiet_families(vault, "quiet")
        payload = review_state.ReviewStateStore(vault).load()
        assert dreamer.setting() == "on"
        assert all(
            review_state.disposition_for(family.name, payload=payload) == "quiet"
            for family in dreamer_families.REGISTRY
        )
    else:
        _crash(vault, monkeypatch)

    observation = "The primingcheck inspection records nine successful starts."
    kwargs = {
        "content": f"## Observations\n\n- [finding] {observation}\n",
        "title": "Pump start inspection",
        "slug": "pump-start-inspection",
        "sources": [],
        "suggestions": False,
    }
    remember = next(c for c in commands.product_commands_for("mcp") if c.name == "remember")
    # This in-process caller owns only the invented fixture vault.
    with library_scope():
        validation = writer_lease.invoke_command(remember, vault, validate_only=True, **kwargs)
        result = writer_lease.invoke_command(
            remember,
            vault,
            draft_id=validation["draft_id"],
            draft_hash=validation["draft_hash"],
            draft_token=validation["draft_token"],
            relation_disposition="reviewed_none",
            relation_review_hash=validation["draft_hash"],
            relation_review_reason="The start count has no supported typed relation in this fixture.",
            **kwargs,
        )
        path = result["path"]
        assert observation in (vault / path).read_text(encoding="utf-8")
        readback = commands.op_read_memory(vault, path)
        assert observation in readback["body"]
        recalled = commands.op_find(vault, query="primingcheck", mode="keyword", graph=False)
        hits = [hit for hit in recalled if hit["path"] == path]
        assert hits, recalled
        assert observation in hits[0]["excerpt"]


@pytest.mark.query_deadline
def test_pressure_holds_upkeep_while_capture_recall_and_integrity_remain_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    ticks = dreamer.status()["ticks"]
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    monkeypatch.setenv("EXOMEM_AUTO_QUIET", "1")
    monkeypatch.setattr(
        resource_status, "gpu_headroom", lambda: {"status": "marginal", "usable": False}
    )
    monkeypatch.setattr(dreamer_policy, "POLL_SECONDS", 0.01)
    assert mode.resolve_mode() == "normal"

    offset = [0.0]

    def monotonic() -> float:
        return time.monotonic() + offset[0]

    # Advance fixture clocks past real idle/settle thresholds; keep process clocks intact.
    clock = dreamer.Clock
    monkeypatch.setattr(dreamer, "Clock", lambda: clock(monotonic=monotonic))
    monkeypatch.setattr(
        foreground_activity, "time", SimpleNamespace(monotonic=monotonic, sleep=time.sleep)
    )

    def await_pressure() -> dict:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            offset[0] += dreamer_policy.IDLE_SECONDS + dreamer_policy.SETTLE_FLOOR_SECONDS + 1.0
            signals = dreamer.gather_signals(vault)
            status = dreamer.status()
            if (
                signals.pressure is True
                and status["running"]
                and status["waiting_reason"] == "pressure"
            ):
                return status
            time.sleep(0.01)
        pytest.fail(f"worker did not hold for pressure: {dreamer.status()}")

    worker = dreamer.start(vault)
    assert worker is not None
    try:
        assert await_pressure()["ticks"] == ticks
        observation = "The pressurecheck inspection records eleven successful starts."
        kwargs = {
            "content": f"## Observations\n\n- [finding] {observation}\n",
            "title": "Pump pressure inspection",
            "slug": "pump-pressure-inspection",
            "sources": [],
            "suggestions": False,
        }
        remember = next(c for c in commands.product_commands_for("mcp") if c.name == "remember")
        # This in-process caller owns only the invented fixture vault.
        with library_scope():
            validation = writer_lease.invoke_command(remember, vault, validate_only=True, **kwargs)
            result = writer_lease.invoke_command(
                remember,
                vault,
                draft_id=validation["draft_id"],
                draft_hash=validation["draft_hash"],
                draft_token=validation["draft_token"],
                relation_disposition="reviewed_none",
                relation_review_hash=validation["draft_hash"],
                relation_review_reason="The start count has no supported typed relation in this fixture.",
                **kwargs,
            )
            path = result["path"]
            assert observation in (vault / path).read_text(encoding="utf-8")
            assert observation in commands.op_read_memory(vault, path)["body"]
            recalled = commands.op_find(vault, query="pressurecheck", mode="keyword", graph=False)
            hits = [hit for hit in recalled if hit["path"] == path]
            assert hits, recalled
            assert observation in hits[0]["excerpt"]
            surfaces = _surfaces(vault)
            due = json.loads(surfaces["due_state"])
            assert due["categories"]["supersession_integrity"][BROKEN]["open"]
            findings = json.loads(surfaces["audit"])["findings"]
            assert {"supersession_integrity", "forward_reference"} <= {
                finding["category"] for finding in findings if finding["path"] == BROKEN
            }
            items = json.loads(surfaces["attention"])["items"]
            assert any(
                item["path"] == BROKEN and "supersession_integrity" in item["categories"]
                for item in items
            )
        assert await_pressure()["ticks"] == ticks
    finally:
        dreamer.stop(timeout=5.0)
        worker.join(timeout=5.0)
        assert not worker.is_alive()
