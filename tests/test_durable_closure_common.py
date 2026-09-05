"""Unit coverage for the bounded public-MCP common-subset diagnostic."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "durable_closure_common.py"
spec = importlib.util.spec_from_file_location("durable_closure_common", MODULE_PATH)
common = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = common
spec.loader.exec_module(common)


def test_empty_disposable_roots_reject_nonempty_and_overlapping_paths(tmp_path: Path) -> None:
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    state.mkdir()
    vault.mkdir()
    (state / "old.json").write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        common.prepare_roots(state=state, vault=vault)
    with pytest.raises(ValueError, match="distinct"):
        common.prepare_roots(state=tmp_path / "same", vault=tmp_path / "same")


def test_deterministic_fixture_has_required_existing_pages_and_digest(tmp_path: Path) -> None:
    first = common.materialize_fixture(tmp_path / "one", pages=7)
    second = common.materialize_fixture(tmp_path / "two", pages=7)

    assert first["digest"] == second["digest"]
    assert first["page_count"] == 7
    assert {"tracker", "background", "stale_link"} <= set(first["named_pages"])
    assert all(entry["bytes"] > 0 and len(entry["sha256"]) == 64 for entry in first["pages"])


def test_fixture_size_is_bounded() -> None:
    with pytest.raises(ValueError, match="4"):
        common.fixture_pages(3)
    with pytest.raises(ValueError, match="8000"):
        common.fixture_pages(8001)


def test_malformed_or_failed_mcp_results_invalidate_instead_of_becoming_product_loss() -> None:
    with pytest.raises(common.AdapterFault, match="malformed"):
        common.decode_result({"structured_content": {"result": "not-an-object"}})
    with pytest.raises(common.AdapterFault, match="failed"):
        common.decode_result({"structured_content": {"result": {"success": False}}})


def test_exact_marker_and_search_verification_require_every_unique_marker() -> None:
    marker = "common-subset-marker-123"
    payload = {"content": f"# Result\n\n{marker}\n"}
    assert common.exact_marker_present(payload, marker) is True
    assert common.exact_marker_present({"content": "missing"}, marker) is False

    hits = {"results": [{"content": marker}, {"title": "other"}]}
    assert common.search_marker_present(hits, marker) is True
    assert common.search_marker_present({"results": [{"title": marker}]}, marker) is True
    assert common.search_marker_present({"results": []}, marker) is False


def test_equivalent_plan_uses_only_shared_markdown_operations() -> None:
    plan = common.common_plan("basic_memory")
    assert plan == common.common_plan("exomem")
    assert [step["operation"] for step in plan] == [
        "create_completed_chapter",
        "edit_tracker",
        "correct_stale_link",
        "capture_independent_note",
        "exact_read_changed_pages",
        "search_unique_markers",
    ]
    assert all(step["surface"] == "public_mcp" for step in plan)


def test_phase_clock_keeps_setup_teardown_and_timed_work_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 11.0, 13.5, 15.0))
    monkeypatch.setattr(common.time, "perf_counter", lambda: next(ticks))
    clock = common.PhaseClock()
    clock.start_timing()
    clock.finish_closure()
    clock.finish_teardown()

    assert clock.report() == {
        "pre_timing_ms": 1000.0,
        "wall_to_verified_closure_ms": 2500.0,
        "teardown_ms": 1500.0,
    }


def test_basic_memory_environment_is_fresh_and_only_has_basic_memory_prefixes(
    tmp_path: Path,
) -> None:
    env = common.basic_memory_environment(tmp_path / "state", tmp_path / "vault")

    assert env["BASIC_MEMORY_HOME"] == str(tmp_path / "state" / "home")
    assert env["BASIC_MEMORY_CONFIG_DIR"] == str(tmp_path / "state" / "config")
    assert env["BASIC_MEMORY_FORCE_LOCAL"] == "true"
    assert env["BASIC_MEMORY_EXPLICIT_ROUTING"] == "true"
    assert env["BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED"] == "false"
    assert env["BASIC_MEMORY_AUTO_UPDATE"] == "false"
    assert env["BASIC_MEMORY_NO_PROMOS"] == "1"
    assert not any(key.startswith("EXOMEM_") for key in env)


def test_percentiles_and_call_counts_only_include_public_calls() -> None:
    calls = [
        {"tool": "write", "elapsed_ms": 1.0, "ack": True},
        {"tool": "read", "elapsed_ms": 3.0, "ack": False},
        {"tool": "edit", "elapsed_ms": 2.0, "ack": True},
    ]
    assert common.call_measurements(calls) == {
        "public_call_count": 3,
        "ack_p50_ms": 1.0,
        "ack_p95_ms": 2.0,
    }
