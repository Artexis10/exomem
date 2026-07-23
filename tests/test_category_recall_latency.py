"""Unit tests for scripts/category_recall_latency.py.

All calls/timers are mocked — no real vault, no real find() call, no real
sleep. These tests assert shape, math, and privacy of the harness itself
(OpenSpec change `restore-indexed-category-recall`, task 5.1), not real
workstation latency.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "category_recall_latency.py"
SPEC = importlib.util.spec_from_file_location("category_recall_latency", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
crl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = crl
SPEC.loader.exec_module(crl)

from exomem import cli_ops  # noqa: E402

SECRET_CATEGORY = "super-secret-real-category-xyz"
SECRET_VAULT = Path("/vaults/hugo-real-vault-secret-name")


def _envelope(total_ms: float, filter_eligibility_ms: float | None) -> dict:
    stages = {}
    if filter_eligibility_ms is not None:
        stages["filter_eligibility"] = {"ms": filter_eligibility_ms}
    return {"hits": [], "timings": {"total_ms": total_ms, "stages": stages}}


class FakeOpFind:
    """Records every call's kwargs and returns canned envelopes/lists."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, vault_root, **kwargs):
        self.calls.append({"vault_root": vault_root, **kwargs})
        resp = self.responses.pop(0) if self.responses else self.responses_default()
        if isinstance(resp, Exception):
            raise resp
        return resp

    def responses_default(self):
        return _envelope(1.0, 0.5)


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------


def test_page_request_shape_matches_spec():
    req = crl.build_request("cat-a", "page", limit=10, include_timings=True)
    assert req == {
        "query": "",
        "categories": ["cat-a"],
        "result_level": "page",
        "scope": "kb-only",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "pack": False,
        "graph_enrich": False,
        "limit": 10,
        "include_timings": True,
    }


def test_unit_request_shape_matches_spec():
    req = crl.build_request("cat-a", "unit", limit=10, include_timings=True)
    assert req["result_level"] == "unit"
    assert req["categories"] == ["cat-a"]
    assert req["query"] == ""
    assert req["scope"] == "kb-only"
    assert req["mode"] == "keyword"
    assert req["graph"] is False
    assert req["rerank"] is False
    assert req["pack"] is False


def test_invalid_result_level_rejected():
    with pytest.raises(ValueError):
        crl.build_request("cat-a", "mixed", limit=10, include_timings=True)


# --------------------------------------------------------------------------
# Nearest-rank percentile
# --------------------------------------------------------------------------


def test_nearest_rank_percentile_known_values():
    sv = list(range(1, 11))  # 1..10
    assert crl.nearest_rank_percentile(sv, 95) == 10
    assert crl.nearest_rank_percentile(sv, 90) == 9
    assert crl.nearest_rank_percentile(sv, 50) == 5


def test_nearest_rank_percentile_empty():
    assert crl.nearest_rank_percentile([], 95) == 0.0


def test_distribution_shape():
    dist = crl.distribution([3.0, 1.0, 2.0])
    assert dist["n"] == 3
    assert dist["min"] == 1.0
    assert dist["max"] == 3.0
    assert dist["p50"] == 2.0


def test_distribution_empty_is_none():
    assert crl.distribution([]) is None


# --------------------------------------------------------------------------
# 30 cold + 30 hot samples per level, exact request repeated
# --------------------------------------------------------------------------


def test_cold_lane_runs_30_samples_and_resets_before_each():
    op_find = FakeOpFind([_envelope(10.0, 5.0) for _ in range(30)])
    reset_calls = []

    def fake_reset():
        reset_calls.append(True)
        return {"pages": 1, "hot_find": 1}

    result = crl.run_lane(
        op_find, Path("/vault"), "cat-a", "page",
        cold=True, samples=30, reset_cache=fake_reset,
    )
    assert result["sample_count"] == 30
    assert len(reset_calls) == 30
    assert len(op_find.calls) == 30
    # Every call uses the exact same fixed request shape.
    first = {k: v for k, v in op_find.calls[0].items() if k != "vault_root"}
    for call in op_find.calls:
        assert {k: v for k, v in call.items() if k != "vault_root"} == first
    assert result["cache_reset"] == "before_each_sample"


def test_hot_lane_runs_30_samples_and_never_resets():
    op_find = FakeOpFind([_envelope(0.5, 0.0) for _ in range(30)])
    reset_calls = []

    def fake_reset():
        reset_calls.append(True)

    result = crl.run_lane(
        op_find, Path("/vault"), "cat-a", "unit",
        cold=False, samples=30, reset_cache=fake_reset,
    )
    assert result["sample_count"] == 30
    assert len(reset_calls) == 0
    assert len(op_find.calls) == 30
    assert result["cache_reset"] == "none"
    # Unchanged request every time (the live-cache contract for a real hot hit).
    first = op_find.calls[0]
    for call in op_find.calls:
        assert call == first


def test_unit_hot_lane_missing_filter_eligibility_fails_closed():
    op_find = FakeOpFind([_envelope(3.0, None) for _ in range(30)])
    with pytest.raises(RuntimeError, match="unit_hot.*filter_eligibility"):
        crl.run_lane(
            op_find, Path("/vault"), "cat-a", "unit",
            cold=False, samples=30, reset_cache=lambda: None,
        )


# --------------------------------------------------------------------------
# Selective cache reset seam
# --------------------------------------------------------------------------


class _FakeFindModuleWithSeam:
    def __init__(self):
        self.reset_calls = 0
        self.clear_cache_calls = 0
        self.unload_ram_caches_calls = 0

    def reset_page_and_result_caches(self):
        self.reset_calls += 1
        return {"pages": 3, "hot_find": 2}

    def clear_cache(self):
        self.clear_cache_calls += 1

    def unload_ram_caches(self):
        self.unload_ram_caches_calls += 1
        return {"pages": 0, "resolvers": 0, "hot_find": 0}


def test_reset_cold_sample_caches_calls_only_the_named_seam():
    fake = _FakeFindModuleWithSeam()
    out = crl.reset_cold_sample_caches(fake)
    assert fake.reset_calls == 1
    assert fake.clear_cache_calls == 0
    assert fake.unload_ram_caches_calls == 0
    assert out == {"pages": 3, "hot_find": 2}


class _FakeFindModuleWithoutSeam:
    def __init__(self):
        self.clear_cache_calls = 0
        self.unload_ram_caches_calls = 0

    def clear_cache(self):
        self.clear_cache_calls += 1

    def unload_ram_caches(self):
        self.unload_ram_caches_calls += 1


def test_reset_cold_sample_caches_raises_when_seam_missing_and_never_falls_back():
    fake = _FakeFindModuleWithoutSeam()
    with pytest.raises(RuntimeError, match="reset_page_and_result_caches"):
        crl.reset_cold_sample_caches(fake)
    assert fake.clear_cache_calls == 0
    assert fake.unload_ram_caches_calls == 0


# --------------------------------------------------------------------------
# Cardinality preflight
# --------------------------------------------------------------------------


def test_check_cardinality_exactly_two_required():
    assert crl.check_cardinality(2) is True
    assert crl.check_cardinality(1) is False
    assert crl.check_cardinality(3) is False
    assert crl.check_cardinality(0) is False
    assert crl.check_cardinality(None) is False


def test_preflight_once_reports_candidate_count():
    op_find = FakeOpFind([[{"path": "a.md"}, {"path": "b.md"}]])
    result = crl.preflight_once(op_find, Path("/vault"), "cat-a")
    assert result == {"catalog_status": "ready", "candidate_count": 2}


def test_preflight_once_reports_wrong_cardinality():
    op_find = FakeOpFind([[{"path": "a.md"}, {"path": "b.md"}, {"path": "c.md"}]])
    result = crl.preflight_once(op_find, Path("/vault"), "cat-a")
    assert result["candidate_count"] == 3
    assert crl.check_cardinality(result["candidate_count"]) is False


def test_preflight_once_catalog_warming_is_not_ready():
    op_find = FakeOpFind([cli_ops.OpError("RETRIEVAL_INDEX_WARMING", "still warming")])
    result = crl.preflight_once(op_find, Path("/vault"), "cat-a")
    assert result == {"catalog_status": "RETRIEVAL_INDEX_WARMING", "candidate_count": None}


def test_wait_for_catalog_ready_retries_until_ready():
    op_find = FakeOpFind([
        cli_ops.OpError("RETRIEVAL_INDEX_WARMING", "still warming"),
        cli_ops.OpError("RETRIEVAL_INDEX_WARMING", "still warming"),
        [{"path": "a.md"}, {"path": "b.md"}],
    ])
    sleeps = []
    result = crl.wait_for_catalog_ready(
        op_find, Path("/vault"), "cat-a", max_wait_s=10.0, sleep=sleeps.append,
    )
    assert result == {"catalog_status": "ready", "candidate_count": 2}
    assert len(sleeps) == 2


def test_wait_for_catalog_ready_bounded_when_never_ready():
    op_find = FakeOpFind([cli_ops.OpError("RETRIEVAL_INDEX_WARMING", "still warming")] * 5)
    result = crl.wait_for_catalog_ready(
        op_find, Path("/vault"), "cat-a", max_wait_s=0.0, sleep=lambda _s: None,
    )
    assert result["catalog_status"] == "RETRIEVAL_INDEX_WARMING"
    assert result["candidate_count"] is None


def test_run_harness_seeds_live_freshness_once_before_preflight():
    events: list[str] = []
    responses = (
        [[{"path": "a.md"}, {"path": "b.md"}]]
        + [_envelope(5.0, 1.0) for _ in range(4)]
    )

    class OrderedFind(FakeOpFind):
        def __call__(self, vault_root, **kwargs):
            assert events and events[0] == "seeded"
            events.append("find")
            return super().__call__(vault_root, **kwargs)

    def prepare(_vault: Path) -> dict[str, bool]:
        assert events == []
        events.append("seeded")
        return {"kb": True, "vault": True}

    report = crl.run_harness(
        Path("/vault"),
        "cat-a",
        op_find=OrderedFind(responses),
        reset_cache=lambda: None,
        samples=1,
        count_pages=lambda _vault: 2500,
        prepare_freshness=prepare,
    )

    assert report["gates"]["pass"] is True
    assert events[0] == "seeded"
    assert events.count("seeded") == 1


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------


def test_round_to_bucket_corpus_size():
    assert crl.round_to_bucket(2410, 500) == 2500
    assert crl.round_to_bucket(1234, 500) == 1000
    assert crl.round_to_bucket(2760, 500) == 3000


def test_bucket_candidate_count():
    assert crl.bucket_candidate_count(0) == 5
    assert crl.bucket_candidate_count(2) == 5
    assert crl.bucket_candidate_count(5) == 5
    assert crl.bucket_candidate_count(6) == 10


# --------------------------------------------------------------------------
# Threshold pass/fail logic
# --------------------------------------------------------------------------


def _lane(total: dict | None, elig: dict | None) -> dict:
    return {"total_ms": total, "filter_eligibility_ms": elig}


def test_evaluate_gates_all_pass():
    lanes = {
        "page_cold": _lane({"p95": 200.0}, {"p95": 50.0}),
        "page_hot": _lane({"p95": 5.0}, {"p95": 0.0}),
        "unit_cold": _lane({"p95": 220.0}, {"p95": 90.0}),
        "unit_hot": _lane({"p95": 9.0}, {"p95": 0.0}),
    }
    gates = crl.evaluate_gates(lanes)
    assert gates["pass"] is True
    assert gates["page_cold_filter_eligibility_p95_lt_100ms"] is True
    assert gates["page_cold_total_p95_lt_250ms"] is True
    assert gates["page_hot_total_p95_lt_10ms"] is True


def test_evaluate_gates_fails_on_slow_hot_total():
    lanes = {
        "page_cold": _lane({"p95": 200.0}, {"p95": 50.0}),
        "page_hot": _lane({"p95": 15.0}, {"p95": 0.0}),  # over the 10ms gate
        "unit_cold": _lane({"p95": 220.0}, {"p95": 90.0}),
        "unit_hot": _lane({"p95": 9.0}, {"p95": 0.0}),
    }
    gates = crl.evaluate_gates(lanes)
    assert gates["pass"] is False
    assert gates["page_hot_total_p95_lt_10ms"] is False


def test_evaluate_gates_missing_filter_eligibility_fails_closed():
    lanes = {
        "page_cold": _lane({"p95": 200.0}, {"p95": 50.0}),
        "page_hot": _lane({"p95": 5.0}, {"p95": 0.0}),
        "unit_cold": _lane({"p95": 220.0}, None),
        "unit_hot": _lane({"p95": 9.0}, {"p95": 0.0}),
    }
    gates = crl.evaluate_gates(lanes)
    assert gates["unit_cold_filter_eligibility_p95_lt_100ms"] is False
    assert gates["pass"] is False


# --------------------------------------------------------------------------
# Anonymization / bucketing sentinel
# --------------------------------------------------------------------------


def test_report_never_leaks_category_query_path_or_exact_counts():
    op_find = FakeOpFind(
        [[{"path": "a.md"}, {"path": "b.md"}]]  # preflight
        + [_envelope(50.0, 20.0) for _ in range(30)]  # page cold
        + [_envelope(2.0, 0.0) for _ in range(30)]  # page hot
        + [_envelope(60.0, 30.0) for _ in range(30)]  # unit cold
        + [_envelope(3.0, 0.0) for _ in range(30)]  # unit hot
    )
    report = crl.run_harness(
        SECRET_VAULT,
        SECRET_CATEGORY,
        op_find=op_find,
        reset_cache=lambda: None,
        count_pages=lambda _vault: 2413,
        run_id="fixed-test-run-id",
    )
    blob = json.dumps(report)
    assert SECRET_CATEGORY not in blob
    assert "hugo-real-vault-secret-name" not in blob
    assert str(SECRET_VAULT) not in blob
    assert "a.md" not in blob and "b.md" not in blob

    assert set(report.keys()) == {
        "run_id", "corpus_size_bucket", "sample_count", "percentile_method",
        "cache_policy", "preflight", "lanes", "gates",
    }
    assert report["corpus_size_bucket"] == 2500  # bucketed, never the exact 2413
    assert "candidate_count" not in report["preflight"]
    assert report["preflight"]["candidate_count_bucket"] == 5  # bucketed, never the exact 2
    assert report["preflight"]["cardinality_ok"] is True
    assert report["run_id"] == "fixed-test-run-id"


def test_report_halts_lanes_when_cardinality_wrong():
    op_find = FakeOpFind([[{"path": "a.md"}]])  # only one candidate
    report = crl.run_harness(
        SECRET_VAULT,
        SECRET_CATEGORY,
        op_find=op_find,
        reset_cache=lambda: None,
        count_pages=lambda _vault: 100,
    )
    assert report["preflight"]["cardinality_ok"] is False
    assert report["lanes"] == {}
    assert report["gates"]["pass"] is False
    assert report["gates"]["reason"] == "preflight_failed"
    # No lane sampling happened beyond the single preflight call.
    assert len(op_find.calls) == 1


def test_report_halts_lanes_when_catalog_not_ready():
    op_find = FakeOpFind([cli_ops.OpError("RETRIEVAL_INDEX_WARMING", "still warming")])
    report = crl.run_harness(
        SECRET_VAULT,
        SECRET_CATEGORY,
        op_find=op_find,
        reset_cache=lambda: None,
        count_pages=lambda _vault: 100,
        warmup_timeout_s=0.0,
    )
    assert report["preflight"]["catalog_status"] == "RETRIEVAL_INDEX_WARMING"
    assert report["lanes"] == {}
    assert report["gates"]["pass"] is False
