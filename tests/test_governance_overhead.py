"""Empty-policy fast path, corpus hygiene, and the internal read-leaf registry.

Zero enforcement this change: nothing here is wired into `find`/`get`/
`overview`/`graph`/`read_media`/query — this file only pins (a) the
empty-policy short circuit never touches the sidecar, (b) `_Governance/`
never surfaces as indexable content, and (c) the kernel's internal read
leaves are registry-level plumbing, not a shipped user-facing tool.
"""

from __future__ import annotations

import copy
import gc
import math
import os
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from exomem import commands, governance, lexstore
from exomem import find as find_module
from exomem.find_types import Hit, SemanticUnitHit
from exomem.governance import store
from exomem.governance.principal import RequestPrincipal, request_scope


def test_empty_policy_short_circuit(vault: Path) -> None:
    result = governance.decide_paths(
        vault, ["Knowledge Base/Notes/anything.md"], audience="external"
    )
    assert result["Knowledge Base/Notes/anything.md"].level == governance.DISCLOSURE_MAX
    assert not store.sidecar_path(vault).exists()


def test_blocked_policy_short_circuits_to_fail_closed_floor(vault: Path) -> None:
    """A cold-start compile refusal (no prior good compile) must resolve to a
    fail-closed L0 floor for every requested path — never the open fast path.
    Reproduces the reviewer's finding: a plain `ceiling: 9` typo on a fresh
    process previously made `decide_paths` return full disclosure (L6)."""
    scope = vault / "Knowledge Base" / "_Governance" / "scopes" / "acmeco.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "paths: [\"Projects/AcmeCo/**\"]\n",
        encoding="utf-8",
    )
    rule = vault / "Knowledge Base" / "_Governance" / "rules" / "acmeco-external.yaml"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\nceiling: 9\n",  # plain typo, out of range
        encoding="utf-8",
    )

    result = governance.decide_paths(
        vault, ["Knowledge Base/Notes/anything.md"], audience="external"
    )
    assert result["Knowledge Base/Notes/anything.md"].level == governance.DISCLOSURE_MIN
    assert not store.sidecar_path(vault).exists()


def test_governance_dir_never_surfaces_in_find(vault: Path) -> None:
    planted = vault / "Knowledge Base" / "_Governance" / "scopes" / "leaked-note.yaml"
    planted.parent.mkdir(parents=True, exist_ok=True)
    # Not even valid policy YAML — just a markdown-shaped file that COULD be
    # picked up as content if `_Governance/` weren't excluded from the walk.
    stray_md = vault / "Knowledge Base" / "_Governance" / "scopes" / "leaked-note.md"
    stray_md.write_text(
        "---\ntype: source\n---\nqzxvv-governance-leak-marker\n", encoding="utf-8"
    )
    find_module.clear_cache()
    hits = find_module.find(vault, query="qzxvv-governance-leak-marker")
    assert not any("leaked-note" in h.path for h in hits)


def test_kernel_leaves_are_registered_internally_only() -> None:
    expected = {
        "governance.load",
        "governance.evaluate_membership",
        "governance.decide",
        "governance.decide_paths",
    }
    assert set(governance.KERNEL_LEAVES) == expected
    for leaf in governance.KERNEL_LEAVES.values():
        assert callable(leaf)


def test_no_user_facing_command_ships_this_change() -> None:
    """No governance-named tool/operation is registered on the command
    surface — the kernel lands as inspection-only, internal plumbing."""
    command_names = {cmd.name for cmd in commands.COMMANDS}
    assert not any("governance" in name for name in command_names)


# ---------------------------------------------------------------------------
# Release-gate overhead micro-gate (add-release-gate, task 8.2)
#
# `tests/test_latency_gate.py` calls `find()` DIRECTLY, so the release plane —
# which sits in `op_find`, strictly after `find()` returns — is invisible to
# it. This gate measures at the `op_find` level over the same `gen_dense_vault`
# corpus, so the empty-policy fast path has a ceiling of its own without
# touching the existing thresholds.
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from synth_vault import gen_dense_vault  # noqa: E402

from exomem.governance import egress as egress_module  # noqa: E402
from exomem.governance import receipts as receipts_module  # noqa: E402
from exomem.governance import scrubber as scrubber_module  # noqa: E402

_OVERHEAD_N_NOTES = 2000
_OVERHEAD_QUERIES = (
    "retry backoff jitter",
    "cache stampede cold start",
    "autovacuum threshold bloat",
    "kill switch release",
)
#: Median added milliseconds `op_find` may spend in the release plane on a
#: vault with no `_Governance/` directory. The empty-policy fast path is a
#: bounded set of governance-marker, sidecar, and lifecycle probes plus the
#: projector's allow-list filter per hit.
_EMPTY_POLICY_BUDGET_MS = 5.0
#: Direct entry-point work must not grow with corpus size. This is deliberately
#: a ratio, not an absolute microsecond ceiling: shared runners can delay an
#: entire timing batch, but the same bounded probes should cost the same on a
#: 50-note and a 1,000-note vault. A 3x ceiling matches the other load-invariant
#: gates in this module while still catching any accidental corpus walk.
_ENTRY_POINT_SCALE_RATIO_CEILING = 3.0
_ENTRY_POINT_BATCH_CALLS = 1000
_ENTRY_POINT_SAMPLES = 5
#: Scrubber budget: < 2 ms per 100 KB of result text (design D7).
_SCRUBBER_BUDGET_MS_PER_100KB = 2.0

#: Load-invariant ceiling: how many times the anchor-free path may the
#: anchor-hit path cost. MEASURED, not guessed — on the same box, back to
#: back: per-alternative gating reads **1.45x**, and the all-or-nothing union
#: it replaced reads **9.27x**. A ceiling of 3.0 sits with roughly 2x margin
#: on either side of that 6.4x separation.
#:
#: This is the assertion that actually catches the regression, and the only
#: one worth trusting on a shared runner: contention inflates both sides
#: together, so the ratio holds while absolute milliseconds do not.
_ANCHOR_HIT_RATIO_CEILING = 3.0

#: When the absolute millisecond budget is meaningful at all. The anchor-free
#: control reads ~1.3 ms per 100 KB on this hardware when quiet, and the
#: anchor-hit path runs ~1.45x that. So the absolute budget can only be met by
#: non-regressed code while the control stays under 2.0 / 1.45 ~= 1.38 ms;
#: above that the box is busy and an absolute number measures the scheduler.
#: Derived from the ratio and the budget rather than chosen, so it moves only
#: if one of those moves.
_QUIET_MACHINE_CONTROL_MS = 1.35

#: Ceiling for a DIFFERENT ratio than `_ANCHOR_HIT_RATIO_CEILING` above: the
#: cost of `scrub_text` over ANCHOR-FREE prose against a same-process,
#: same-payload reference — the `_active_alternatives(text.lower())` prescan
#: call `scrub_text` itself makes before falling through to the unconditional
#: entropy pass. The anchor-free path IS the cheap path already, so there is
#: no slower/faster variant of the SAME corpus to ratio against the way the
#: anchor-hit test does above; the reference has to be a different,
#: comparably-sized operation over the SAME text instead.
#:
#: MEASURED, not guessed. A trivial non-matching literal regex
#: (`re.compile(r"...").sub(...)`) was tried first and rejected: at rest it
#: costs ~25 us per 100 KB against `scrub_text`'s ~1.0-1.8 ms, a stable ~40x —
#: but under artificial CPU contention (30 parallel busy loops on a 20-core
#: box) that ratio swung to 64x-264x, because a ~25 us operation is shorter
#: than a scheduler quantum and mostly dodges preemption, while the
#: millisecond-scale `scrub_text` call spans several quanta and gets
#: proportionally delayed. A reference that much cheaper than the thing it
#: calibrates is not load-invariant.
#:
#: `_active_alternatives(text.lower())` fixes that: it is comparable in
#: magnitude (~0.7-1.0 ms here, same order as `scrub_text`'s ~1.3-1.8 ms), so
#: contention inflates both together. Measured back to back, repeatedly,
#: including under the same 30-way contention above: 1.70x-1.91x, holding
#: steady even while a single `scrub_text` sample spiked to 17 ms under load
#: (the MINIMUM of repeated samples is what is compared — see `_best_ms`).
#: Planted-defect proof
#: (`test_the_entropy_pass_ratio_gate_catches_the_full_union_regression`):
#: forcing every alternative to run regardless of anchors measured a ~400x
#: ratio on this machine. A ceiling of 3.0 sits with ~1.6x margin over the
#: worst good-case observed and is several orders of magnitude below the bad
#: case.
_ENTROPY_PASS_RATIO_CEILING = 3.0


def _best_ms(fn, samples: int) -> float:
    """Minimum wall-clock cost of calling `fn()`, across `samples` repeats,
    after one untimed warm-up call. Pure CPU over a fixed in-memory string, so
    noise can only ADD time — the minimum is the least-contaminated estimate,
    the same reasoning `timeit` documents. Shared by every scrubber throughput
    test below so the sampling strategy lives in one place."""
    fn()
    elapsed = []
    for _ in range(samples):
        start = time.perf_counter()
        fn()
        elapsed.append((time.perf_counter() - start) * 1000.0)
    return min(elapsed)


def _interleaved_best_per_call_us(
    small_fn: Callable[[], Any], large_fn: Callable[[], Any]
) -> tuple[float, float]:
    """Return the least-contaminated per-call costs for two fixed-work paths.

    Each sample times a batch large enough to span scheduler quanta, and the
    order alternates so a transient load spike cannot systematically favor one
    corpus size. Scheduling noise only adds time, so the minimum batch for each
    side is the useful estimate, as with `_best_ms` above.
    """
    small_fn()
    large_fn()
    elapsed: dict[str, list[float]] = {"small": [], "large": []}
    functions = {"small": small_fn, "large": large_fn}
    for sample in range(_ENTRY_POINT_SAMPLES):
        order = ("small", "large") if sample % 2 == 0 else ("large", "small")
        for label in order:
            start = time.perf_counter()
            for _ in range(_ENTRY_POINT_BATCH_CALLS):
                functions[label]()
            elapsed[label].append(
                (time.perf_counter() - start) / _ENTRY_POINT_BATCH_CALLS * 1_000_000
            )
    return min(elapsed["small"]), min(elapsed["large"])


def _skip_unless_quiet(control_ms: float, ratio: float, ratio_ceiling: float) -> None:
    """Skip the informational absolute-ms assertion when the machine is too
    busy for an absolute number to mean anything. The load-invariant ratio
    gate stays on regardless; this only guards the reported ABSOLUTE budget,
    and is shared across both scrubber throughput tests below."""
    if control_ms >= _QUIET_MACHINE_CONTROL_MS:
        pytest.skip(
            f"machine is contended (control {control_ms:.2f} ms, over the "
            f"{_QUIET_MACHINE_CONTROL_MS} ms quiet threshold); the "
            f"{_SCRUBBER_BUDGET_MS_PER_100KB} ms ABSOLUTE budget measures the "
            f"scheduler here. The load-invariant ratio gate passed at "
            f"{ratio:.1f}x against a {ratio_ceiling}x ceiling."
        )


def _seed_freshness_live(vault: Path) -> None:
    """Seed the freshness registry the way the watcher does, so `op_find` runs
    in its production steady state instead of rebuilding the resolver on every
    call — mirrors `tests/test_latency_gate.py::_seed_freshness_live`."""
    from exomem import freshness
    from exomem.vault import walk_vault_md

    freshness.seed(
        vault,
        "vault",
        ((str(p), freshness.stat_signature(p)) for p in walk_vault_md(vault)),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(kb)),
    )


def _time_op_find_ms(vault: Path, query: str) -> float:
    start = time.perf_counter()
    commands.op_find(vault, query=query, limit=10, mode="keyword", graph=False)
    return (time.perf_counter() - start) * 1000.0


def test_empty_policy_op_find_overhead_under_budget(tmp_path: Path, monkeypatch) -> None:
    """An ungoverned vault must not pay for the release plane.

    Measured as an INTERLEAVED A/B: each round times one gated `op_find` and
    one with the gate's two entry points stubbed to identity, back to back on
    the same query, and the gate reads the median of the per-round deltas.
    `op_find` over a 2k dense corpus drifts by tens of milliseconds between
    passes, so differencing two sequential medians cannot resolve a
    sub-millisecond effect — pairing is what makes the measurement mean what
    the budget says it means.
    """
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    vault = tmp_path / "dense"
    gen_dense_vault(vault, _OVERHEAD_N_NOTES)
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    # Warm every lane before measuring: the first call builds the BM25 corpus
    # and the resolver, which would swamp a millisecond-scale delta.
    for query in _OVERHEAD_QUERIES:
        commands.op_find(vault, query=query, limit=10, mode="keyword", graph=False)

    real_annotate = egress_module.annotate_hits
    real_gate_state = egress_module.gate_state

    def _stub() -> None:
        monkeypatch.setattr(
            egress_module,
            "annotate_hits",
            lambda vault_root, hits, **kw: egress_module.AnnotatedHits(hits=hits),
        )
        monkeypatch.setattr(
            egress_module, "gate_state", lambda vault_root: (governance.EMPTY_POLICY, False)
        )

    def _restore() -> None:
        monkeypatch.setattr(egress_module, "annotate_hits", real_annotate)
        monkeypatch.setattr(egress_module, "gate_state", real_gate_state)

    deltas: list[float] = []
    for _ in range(5):
        for query in _OVERHEAD_QUERIES:
            _restore()
            gated = _time_op_find_ms(vault, query)
            _stub()
            bare = _time_op_find_ms(vault, query)
            deltas.append(gated - bare)
    _restore()

    delta = statistics.median(deltas)
    assert delta < _EMPTY_POLICY_BUDGET_MS, (
        f"empty-policy release-gate overhead {delta:.2f} ms exceeds the "
        f"{_EMPTY_POLICY_BUDGET_MS} ms budget (paired deltas: "
        f"min={min(deltas):.2f} max={max(deltas):.2f})"
    )


def test_empty_policy_gate_entry_points_are_constant_time(tmp_path: Path) -> None:
    """The two entry points the release plane adds to `op_find` on an
    ungoverned vault: a bounded policy/lifecycle probe and an `annotate_hits`
    call that returns its input. Their cost must not grow with the corpus.

    The end-to-end paired A/B above owns the absolute 5 ms release-plane
    budget. This direct gate protects the constant-work invariant with an
    interleaved ratio so scheduler contention cannot turn a healthy path into
    a false release failure.
    """
    small_vault = tmp_path / "dense-small"
    large_vault = tmp_path / "dense-large"
    gen_dense_vault(small_vault, 50, links_per_note=0)
    gen_dense_vault(large_vault, 1000, links_per_note=0)

    small_gate_us, large_gate_us = _interleaved_best_per_call_us(
        lambda: egress_module.gate_state(small_vault),
        lambda: egress_module.gate_state(large_vault),
    )
    gate_ratio = max(small_gate_us, large_gate_us) / min(small_gate_us, large_gate_us)
    assert gate_ratio < _ENTRY_POINT_SCALE_RATIO_CEILING, (
        f"gate_state scaled {gate_ratio:.1f}x with corpus size "
        f"(small={small_gate_us:.1f} us, large={large_gate_us:.1f} us)"
    )

    small_annotate_us, large_annotate_us = _interleaved_best_per_call_us(
        lambda: egress_module.annotate_hits(small_vault, [], limit=10),
        lambda: egress_module.annotate_hits(large_vault, [], limit=10),
    )
    annotate_ratio = max(small_annotate_us, large_annotate_us) / min(
        small_annotate_us, large_annotate_us
    )
    assert annotate_ratio < _ENTRY_POINT_SCALE_RATIO_CEILING, (
        f"annotate_hits scaled {annotate_ratio:.1f}x with corpus size "
        f"(small={small_annotate_us:.1f} us, large={large_annotate_us:.1f} us)"
    )


# ---------------------------------------------------------------------------
# Governed receipt append overhead micro-gate (add-disclosure-receipts 5.2)
# ---------------------------------------------------------------------------

_RECEIPT_WARMUP_PAIRS = 30
_RECEIPT_MEASURED_PAIRS = 200
_RECEIPT_MEDIAN_CEILING_MS = 3.0
_RECEIPT_P95_CEILING_MS = 8.0
_RECEIPT_PRINCIPAL = RequestPrincipal(audience_id="external", surface="test")
_RECEIPT_MICROGATE_CHILD_ENV = "EXOMEM_RECEIPT_MICROGATE_CHILD"


def _write_receipt_perf_policy(vault: Path) -> None:
    scopes = (
        (
            "01ARZ3NDEKTSV4RRFFQ69G5A01",
            "Knowledge Base/Search/l5-*.md",
            5,
            "01ARZ3NDEKTSV4RRFFQ69G5B01",
        ),
        (
            "01ARZ3NDEKTSV4RRFFQ69G5A02",
            "Knowledge Base/Search/l6-*.md",
            6,
            "01ARZ3NDEKTSV4RRFFQ69G5B02",
        ),
        (
            "01ARZ3NDEKTSV4RRFFQ69G5A03",
            "Knowledge Base/Structure/blocked-*.md",
            0,
            "01ARZ3NDEKTSV4RRFFQ69G5B03",
        ),
        (
            "01ARZ3NDEKTSV4RRFFQ69G5A04",
            "Knowledge Base/Structure/released-*.md",
            6,
            "01ARZ3NDEKTSV4RRFFQ69G5B04",
        ),
    )
    governance_root = vault / "Knowledge Base" / "_Governance"
    for scope_id, path_glob, ceiling, rule_id in scopes:
        scope = governance_root / "scopes" / f"perf-{scope_id[-1]}.yaml"
        scope.parent.mkdir(parents=True, exist_ok=True)
        scope.write_text(
            "governance_version: 1\n"
            f"id: {scope_id}\n"
            f'paths: ["{path_glob}"]\n',
            encoding="utf-8",
        )
        rule = governance_root / "rules" / f"perf-{rule_id[-1]}.yaml"
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text(
            "governance_version: 1\n"
            f"id: {rule_id}\n"
            f'scope_ids: ["{scope_id}"]\n'
            "audience: external\n"
            f"ceiling: {ceiling}\n",
            encoding="utf-8",
        )


def _receipt_perf_hit(path: str, index: int) -> Hit | SemanticUnitHit:
    if index % 2 == 0:
        return Hit(
            path=path,
            type="insight",
            scope="perf",
            title=f"Receipt performance page {index}",
            updated="2026-07-27",
            excerpt="bounded receipt performance excerpt",
            bm25_rank=index + 1,
        )
    return SemanticUnitHit(
        unit_ref=f"unit-{index}",
        form="compact",
        category_raw="config",
        category_key="config",
        category="config",
        kind="observation",
        content="bounded receipt performance unit",
        excerpt="bounded receipt performance unit",
        tags=[],
        context=None,
        source_anchor=None,
        source_span={},
        source_hash=f"{index + 1:064x}",
        parent_path=path,
        parent_ref=None,
        parent_title=f"Receipt performance page {index}",
        parent_type="insight",
        parent_status="active",
        parent_updated="2026-07-27",
        bm25_rank=index + 1,
    )


def _seed_receipt_perf_vault(vault: Path) -> list[Hit | SemanticUnitHit]:
    _write_receipt_perf_policy(vault)
    hits: list[Hit | SemanticUnitHit] = []
    for index in range(10):
        level = 5 if index < 5 else 6
        path = f"Knowledge Base/Search/l{level}-{index:02d}.md"
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\ntype: insight\n---\n"
            f"# Receipt performance page {index}\n\n"
            "Bounded content for the governed mixed search.\n",
            encoding="utf-8",
        )
        hits.append(_receipt_perf_hit(path, index))
    structure = vault / "Knowledge Base" / "Structure"
    structure.mkdir(parents=True, exist_ok=True)
    for prefix in ("blocked", "released"):
        for index in range(50):
            (structure / f"{prefix}-{index:02d}.md").write_text(
                "---\ntype: insight\n---\n# Structure entry\n",
                encoding="utf-8",
            )
    return hits


def _serializing_receipt_sink() -> Callable[..., dict[str, Any]]:
    """Pure append_event drop-in: validate and serialize, but touch no storage."""
    state: dict[str, Any] = {"seq": 0, "prev": receipts_module.GENESIS_HASH}
    instance_id = "f" * 32

    def append_event(
        _vault_root: Path,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        phase: str = "recorded",
        event_id: str | None = None,
        timestamp: str | None = None,
        critical: bool = False,
    ) -> dict[str, Any]:
        receipts_module._validate_event(event_type, phase, payload)
        if event_id is not None and not receipts_module._valid_event_id(
            event_type, phase, event_id
        ):
            raise receipts_module.ReceiptError(
                "receipt event id is not opaque for its event phase"
            )
        if event_type == "critical" and event_id is None:
            raise receipts_module.ReceiptError(
                "critical receipts require a deterministic event id"
            )
        normalized_timestamp, _parsed = receipts_module._timestamp(
            timestamp or receipts_module._now()
        )
        record: dict[str, Any] = {
            "schema": receipts_module.SCHEMA,
            "event_id": event_id or uuid.uuid4().hex,
            "event_type": event_type,
            "phase": phase,
            "timestamp": normalized_timestamp,
            "instance_id": instance_id,
            "seq": state["seq"] + 1,
            "prev": state["prev"],
            "durable": critical,
            **dict(payload),
        }
        record["hash"] = receipts_module._record_hash(record)
        encoded = receipts_module._canonical_json(record)
        if len(encoded) + 1 > receipts_module.MAX_RECORD_BYTES:
            raise receipts_module.ReceiptError(
                "receipt record exceeds the bounded tail window"
            )
        state.update(seq=record["seq"], prev=record["hash"])
        return record

    return append_event


def _receipt_perf_invoke(
    vault: Path,
    command_name: str,
    leaf: Callable[[], Any],
) -> tuple[Any, tuple[dict[str, Any], ...]]:
    with request_scope(_RECEIPT_PRINCIPAL):
        with egress_module.disclosure_boundary(vault, command_name) as collector:
            projection = leaf()
            projection = egress_module.postfilter(command_name, projection, vault)
            egress_module.emit_boundary_receipt(collector)
    return projection, tuple(outcome.value for outcome in collector.outcomes)


def _nearest_rank(samples: list[float], percentile: int) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * percentile / 100) - 1)]


def _receipt_distribution(samples: list[float]) -> dict[str, float | int]:
    return {
        "n": len(samples),
        "min": min(samples),
        "p50": statistics.median(samples),
        "p90": _nearest_rank(samples, 90),
        "p95": _nearest_rank(samples, 95),
        "max": max(samples),
    }


def _format_receipt_distribution(label: str, samples: list[float]) -> str:
    distribution = _receipt_distribution(samples)
    return (
        f"{label}: n={distribution['n']} min={distribution['min']:.3f} "
        f"p50={distribution['p50']:.3f} p90={distribution['p90']:.3f} "
        f"p95={distribution['p95']:.3f} max={distribution['max']:.3f} ms"
    )


def _measure_receipt_case(
    monkeypatch: pytest.MonkeyPatch,
    *,
    case_name: str,
    invoke: Callable[[], tuple[Any, tuple[dict[str, Any], ...]]],
) -> tuple[list[float], list[float], list[float]]:
    real_append = receipts_module.append_event
    sink_append = _serializing_receipt_sink()

    def call(append: Callable[..., dict[str, Any]]) -> tuple[int, Any]:
        monkeypatch.setattr(receipts_module, "append_event", append)
        started = time.perf_counter_ns()
        result = invoke()
        return time.perf_counter_ns() - started, result

    # Like timeit, exclude cyclic-collector scheduling from the timed region.
    # Both arms still allocate, validate, hash, and serialize the same receipt;
    # refcount cleanup remains in-band. A collection inherited from hundreds of
    # earlier full-suite tests would otherwise land arbitrarily in one adjacent
    # arm and measure process history rather than incremental receipt storage.
    gc_was_enabled = gc.isenabled()
    gc.collect()
    if gc_was_enabled:
        gc.disable()
    try:
        _real_preflight_ns, real_preflight = call(real_append)
        _sink_preflight_ns, sink_preflight = call(sink_append)
        assert real_preflight == sink_preflight, (
            f"{case_name} real/sink final projections or outcome sets differ"
        )
        assert real_preflight[1], f"{case_name} preflight recorded no governed outcomes"

        for pair in range(_RECEIPT_WARMUP_PAIRS):
            order = (
                (real_append, sink_append)
                if pair % 2 == 0
                else (sink_append, real_append)
            )
            for append in order:
                call(append)

        real_ms: list[float] = []
        sink_ms: list[float] = []
        overhead_ms: list[float] = []
        for pair in range(_RECEIPT_MEASURED_PAIRS):
            order = (
                (real_append, sink_append)
                if pair % 2 == 0
                else (sink_append, real_append)
            )
            measured = {append: call(append)[0] / 1_000_000 for append in order}
            real = measured[real_append]
            sink = measured[sink_append]
            real_ms.append(real)
            sink_ms.append(sink)
            overhead_ms.append(real - sink)
        return real_ms, sink_ms, overhead_ms
    finally:
        monkeypatch.setattr(receipts_module, "append_event", real_append)
        if gc_was_enabled:
            gc.enable()


@pytest.mark.timeout(240)
def test_governed_receipt_append_overhead_under_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get(_RECEIPT_MICROGATE_CHILD_ENV) != "1":
        # Performance is process-history-sensitive: the full suite reaches this
        # test after thousands of threads/subprocesses and otherwise measures
        # that accumulated interpreter state. Run exactly one fresh worker; the
        # guarded child executes both paired cases together in one process.
        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env[_RECEIPT_MICROGATE_CHILD_ENV] = "1"
        env["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(repo_root / "src") + (
            f"{os.pathsep}{existing_pythonpath}" if existing_pythonpath else ""
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-s",
                f"{Path(__file__).resolve()}::"
                "test_governed_receipt_append_overhead_under_budget",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        output = completed.stdout + completed.stderr
        if output:
            print(output, end="")
        assert completed.returncode == 0, (
            "fresh receipt microgate worker failed\n" + output
        )
        return

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "runtime-state"))
    vault = tmp_path / "receipt-overhead"
    hits = _seed_receipt_perf_vault(vault)
    monkeypatch.setattr(
        find_module,
        "find",
        lambda *_args, **_kwargs: copy.deepcopy(hits),
    )
    monkeypatch.setattr(commands.query_log, "log_find_call", lambda **_kwargs: None)

    cases: tuple[
        tuple[str, str, Callable[[], Any], int, dict[int, int]], ...
    ] = (
        (
            "mixed-search",
            "find",
            lambda: commands.op_find(
                vault,
                query="receipt performance",
                result_level="mixed",
                limit=10,
                mode="keyword",
                graph=False,
                rerank=False,
            ),
            10,
            {5: 5, 6: 5},
        ),
        (
            "structure-reduction",
            "list_directory",
            lambda: commands.op_list_directory(
                vault,
                path="Knowledge Base/Structure",
            ),
            100,
            {0: 50, 6: 50},
        ),
    )
    violations: list[str] = []
    for case_name, command_name, leaf, expected_outcomes, expected_levels in cases:
        def invoke(
            leaf: Callable[[], Any] = leaf,
            command_name: str = command_name,
        ) -> tuple[Any, tuple[dict[str, Any], ...]]:
            return _receipt_perf_invoke(vault, command_name, leaf)

        projection, outcomes = invoke()
        assert len(outcomes) == expected_outcomes
        assert {
            level: sum(outcome.get("level") == level for outcome in outcomes)
            for level in expected_levels
        } == expected_levels
        if case_name == "mixed-search":
            assert len(projection) == 10
            assert {
                "semantic_unit" if item.get("result_type") == "semantic_unit" else "page"
                for item in projection
            } == {"page", "semantic_unit"}
        else:
            assert len(projection["entries"]) == 50

        real_ms, sink_ms, overhead_ms = _measure_receipt_case(
            monkeypatch,
            case_name=case_name,
            invoke=invoke,
        )
        report = "\n".join(
            (
                _format_receipt_distribution("real", real_ms),
                _format_receipt_distribution("sink", sink_ms),
                _format_receipt_distribution("overhead", overhead_ms),
            )
        )
        print(f"{case_name}\n{report}")
        median = statistics.median(overhead_ms)
        p95 = _nearest_rank(overhead_ms, 95)
        if median > _RECEIPT_MEDIAN_CEILING_MS or p95 > _RECEIPT_P95_CEILING_MS:
            violations.append(
                f"{case_name} receipt overhead exceeded median "
                f"{_RECEIPT_MEDIAN_CEILING_MS:.1f} ms / p95 "
                f"{_RECEIPT_P95_CEILING_MS:.1f} ms ceilings\n{report}"
            )
    assert not violations, "\n\n".join(violations)


def test_scrubber_throughput_under_budget() -> None:
    """< 2 ms per 100 KB (design D7) — the documented design target stays
    exactly that. But a bare wall-clock assertion calibrated on one dev box
    cannot hold on a CI runner that is ~2-2.5x slower: this exact corpus
    measured 2.31 ms on GitHub Actions (both py3.11 and py3.13) against the
    2.0 ms budget while reading ~1.0-2.1 ms locally. The scrubber runs on
    EVERY result, including the empty-policy fast path, so its cost is
    unconditional — and this corpus is anchor-free prose, so it only
    exercises the prescan-MISS path plus the unconditional entropy
    `_TOKEN_PATTERN.sub`.

    Fixed the same way as `test_scrubber_throughput_on_an_anchor_hit_corpus`
    below: a load-invariant ratio against a same-process, same-payload
    control is the assertion that is always on, and the absolute ms budget is
    asserted only when that control shows the machine is quiet enough for an
    absolute number to mean anything. See `_ENTROPY_PASS_RATIO_CEILING` for
    why the control here is `_active_alternatives(text.lower())` — the
    prescan call `scrub_text` itself makes — rather than a trivial no-op
    regex, and for the measured derivation of the ratio ceiling.
    """
    chunk = (
        "Retry with full jitter backoff avoids thundering herds when a "
        "downstream dependency recovers. The circuit breaker opens after "
        "consecutive failures and half-opens on a timer. "
    )
    payload = chunk * (100_000 // len(chunk) + 1)
    payload = payload[:100_000]
    assert len(payload) == 100_000
    assert not scrubber_module._may_contain_credential(payload), (
        "corpus must stay anchor-free for this measurement to mean the "
        "prescan-MISS path, not the union"
    )

    best_ms = _best_ms(lambda: scrubber_module.scrub_text(payload), samples=9)
    control_ms = _best_ms(
        lambda: scrubber_module._active_alternatives(payload.lower()), samples=9
    )
    ratio = best_ms / control_ms
    print(
        f"\nanchor-free scrubber throughput: {best_ms:.2f} ms / 100 KB "
        f"(prescan-only control {control_ms:.2f} ms, ratio {ratio:.2f}x)"
    )

    # Load-invariant: always on, regardless of machine speed. See
    # `_ENTROPY_PASS_RATIO_CEILING` for the measured good-case (~1.7x-1.9x)
    # and planted-defect (~400x) numbers behind the 3.0x ceiling.
    assert ratio < _ENTROPY_PASS_RATIO_CEILING, (
        f"scrub_text is {ratio:.2f}x its own prescan-only cost (ceiling "
        f"{_ENTROPY_PASS_RATIO_CEILING}x) on anchor-free text — the "
        "unconditional entropy pass regressed"
    )

    _skip_unless_quiet(best_ms, ratio, _ENTROPY_PASS_RATIO_CEILING)
    assert best_ms < _SCRUBBER_BUDGET_MS_PER_100KB, (
        f"scrubber took {best_ms:.2f} ms per 100 KB at its FASTEST on a quiet "
        f"machine, over the {_SCRUBBER_BUDGET_MS_PER_100KB} ms budget"
    )


def test_empty_policy_gate_state_opens_no_sidecar(vault: Path) -> None:
    """The pre-`find()` probe must stay a single `is_dir()` on an ungoverned
    vault: no page parsed, no sidecar opened, no over-fetch."""
    policy, needs_overfetch = egress_module.gate_state(vault)
    assert policy.empty is True
    assert needs_overfetch is False
    assert egress_module.pool_limit(15) == 30
    assert not store.sidecar_path(vault).exists()


def test_scrubber_throughput_on_an_anchor_hit_corpus() -> None:
    """The honest number, on text that actually reaches the alternation.

    `test_scrubber_throughput_under_budget` uses anchor-free prose, so it only
    measures the prescan MISS path — the cheap case. Real vault content
    contains the words `token`, `secret`, `password` and `bearer` in ordinary
    prose (runbooks, incident notes, API documentation), and every one of them
    is a prescan anchor, so those results used to pay the full
    compiled-alternation cost: ~8 ms per 100 KB against a 2 ms budget.

    Fixed by gating the union PER ALTERNATIVE instead of all-or-nothing. Text
    whose only anchors are English words cannot match the PEM, AWS, GitHub,
    JWT, Slack or Google shapes, and the assignment-operator second gate
    (`CredentialPattern.also_requires`) rejects the labelled-secret shape on
    prose that has no `=` or `:`. Same superset argument the whole-union
    prescan already rested on, applied where it pays. The budget was never
    moved.

    Measured two ways, because an absolute wall-clock number at this margin is
    not by itself a trustworthy gate. The fixed code costs ~1.45 ms against a
    2.0 ms budget — real headroom, but only ~27%, and a full-suite run on a
    contended machine measured 2.03 ms for code that reads 1.42 ms when the
    box is quiet. An assertion that flakes under load gets muted, and a muted
    gate catches nothing.

    So the budget is asserted against the MINIMUM of several passes (this is
    pure CPU over a fixed in-memory string, so noise can only ADD time — the
    minimum is the least-contaminated estimate, the same reasoning `timeit`
    documents), and it is asserted only when a same-process CONTROL says the
    machine is quiet enough for an absolute number to mean anything. The
    control is the identical function over the anchor-free corpus, so load
    inflates both together.

    The load-INVARIANT assertion — the ratio of the anchor-hit path to the
    anchor-miss path — is always on, and it is the one that actually catches
    this regression class: before the per-alternative gating that ratio was
    ~20x, and it is ~3.6x now. The budget itself was never moved.
    """
    chunk = (
        "The deploy runbook says to rotate the token before release, and the "
        "secret is stored in the vault rather than in the environment. A bearer "
        "credential is issued per session; the password rotation cadence is "
        "quarterly and the api_key is scoped to one service. "
    )
    payload = (chunk * (100_000 // len(chunk) + 1))[:100_000]
    assert scrubber_module._may_contain_credential(payload), (
        "corpus must actually hit an anchor for this measurement to mean anything"
    )
    control_chunk = (
        "Retry with full jitter backoff avoids thundering herds when a "
        "downstream dependency recovers. The circuit breaker opens after "
        "consecutive failures and half-opens on a timer. "
    )
    control = (control_chunk * (100_000 // len(control_chunk) + 1))[:100_000]
    assert not scrubber_module._may_contain_credential(control)

    best_ms = _best_ms(lambda: scrubber_module.scrub_text(payload), samples=9)
    control_ms = _best_ms(lambda: scrubber_module.scrub_text(control), samples=9)
    ratio = best_ms / control_ms
    print(
        f"\nanchor-hit scrubber throughput: {best_ms:.2f} ms / 100 KB "
        f"(anchor-free control {control_ms:.2f} ms, ratio {ratio:.1f}x)"
    )

    # Load-invariant: the anchor-hit path must stay within a small multiple of
    # the anchor-miss path. All-or-nothing union gating put this at ~20x.
    assert ratio < _ANCHOR_HIT_RATIO_CEILING, (
        f"anchor-hit path is {ratio:.1f}x the anchor-free path (ceiling "
        f"{_ANCHOR_HIT_RATIO_CEILING}x) — the per-alternative gating regressed"
    )

    _skip_unless_quiet(control_ms, ratio, _ANCHOR_HIT_RATIO_CEILING)
    assert best_ms < _SCRUBBER_BUDGET_MS_PER_100KB, (
        f"anchor-hit scrubber took {best_ms:.2f} ms per 100 KB at its FASTEST "
        f"on a quiet machine, over the {_SCRUBBER_BUDGET_MS_PER_100KB} ms budget"
    )


def test_per_alternative_gating_is_what_keeps_the_anchor_hit_path_cheap() -> None:
    """The mechanism behind the number above, pinned directly so a refactor
    that quietly restores the all-or-nothing union is caught by something
    sturdier than a timing measurement on a shared machine."""
    prose = (
        "Rotate the token before release; the secret is stored in the vault "
        "and the password cadence is quarterly."
    )
    active = scrubber_module._active_alternatives(prose.lower())
    names = {scrubber_module.CREDENTIAL_PATTERNS[i].name for i in active}
    # English anchors alone must not drag in shapes that cannot match.
    assert not names & {
        "pem_private_key",
        "pgp_private_key",
        "aws_access_key_id",
        "github_token",
        "compact_jwt",
        "slack_token",
        "google_api_key",
    }
    # No assignment operator -> the expensive labelled-secret shape is skipped.
    assert "labelled_secret_assignment" not in names
    # …but the moment one appears, it comes back.
    with_operator = scrubber_module._active_alternatives(
        f"{prose} api_key=Zm9vYmFyYmF6cXV4MTIzNDU2".lower()
    )
    assert "labelled_secret_assignment" in {
        scrubber_module.CREDENTIAL_PATTERNS[i].name for i in with_operator
    }


def test_the_ratio_gate_catches_the_all_or_nothing_regression(
    monkeypatch,
) -> None:
    """Planted defect for the throughput gate itself.

    A timing assertion nobody has seen fail is not a gate. Restore the
    all-or-nothing behavior — every alternative runs whenever ANY anchor hits —
    and the load-invariant ratio must break its ceiling. Measured back to back
    on one box: per-alternative gating 1.45x, all-or-nothing 9.27x.
    """
    hit_chunk = (
        "The deploy runbook says to rotate the token before release, and the "
        "secret is stored in the vault rather than in the environment. A bearer "
        "credential is issued per session; the password rotation cadence is "
        "quarterly and the api_key is scoped to one service. "
    )
    ctl_chunk = (
        "Retry with full jitter backoff avoids thundering herds when a "
        "downstream dependency recovers. The circuit breaker opens after "
        "consecutive failures and half-opens on a timer. "
    )
    hit = (hit_chunk * (100_000 // len(hit_chunk) + 1))[:100_000]
    ctl = (ctl_chunk * (100_000 // len(ctl_chunk) + 1))[:100_000]

    every = tuple(range(len(scrubber_module.CREDENTIAL_PATTERNS)))
    monkeypatch.setattr(
        scrubber_module,
        "_active_alternatives",
        lambda lowered: every if any(a in lowered for a in scrubber_module._ANCHORS) else (),
    )

    ratio = _best_ms(lambda: scrubber_module.scrub_text(hit), samples=5) / _best_ms(
        lambda: scrubber_module.scrub_text(ctl), samples=5
    )
    assert ratio > _ANCHOR_HIT_RATIO_CEILING, (
        f"all-or-nothing gating measured only {ratio:.1f}x the anchor-free path, "
        f"under the {_ANCHOR_HIT_RATIO_CEILING}x ceiling — the ratio gate would "
        "not have caught the regression it exists to catch"
    )


def test_the_entropy_pass_ratio_gate_catches_the_full_union_regression(
    monkeypatch,
) -> None:
    """Planted defect for `test_scrubber_throughput_under_budget`'s ratio gate.

    That gate's corpus is anchor-free, so the sibling planted defect above
    (all-or-nothing gated on ANY anchor hit) does not touch it: with zero
    anchors present, `any(a in lowered for a in scrubber_module._ANCHORS)` is
    still `False` and the prescan still reports no active alternatives. The
    failure mode this test proves instead is the prescan being skipped
    entirely — `_active_alternatives` unconditionally reporting every
    alternative regardless of anchors — which forces `scrub_text` to run the
    full 12-pattern union even over prose that cannot match any of it.

    The control call the throughput test makes also goes through
    `_active_alternatives` directly, but as a hardcoded constant return it
    costs nothing, while the numerator (`scrub_text`) now pays for the whole
    union. Measured on this machine: ~400x, several orders of magnitude past
    the 3.0x ceiling.
    """
    chunk = (
        "Retry with full jitter backoff avoids thundering herds when a "
        "downstream dependency recovers. The circuit breaker opens after "
        "consecutive failures and half-opens on a timer. "
    )
    payload = (chunk * (100_000 // len(chunk) + 1))[:100_000]

    every = tuple(range(len(scrubber_module.CREDENTIAL_PATTERNS)))
    monkeypatch.setattr(scrubber_module, "_active_alternatives", lambda lowered: every)

    best_ms = _best_ms(lambda: scrubber_module.scrub_text(payload), samples=5)
    control_ms = _best_ms(
        lambda: scrubber_module._active_alternatives(payload.lower()), samples=5
    )
    ratio = best_ms / control_ms
    assert ratio > _ENTROPY_PASS_RATIO_CEILING, (
        f"forcing the full union unconditionally measured only {ratio:.1f}x "
        f"the prescan-only cost, under the {_ENTROPY_PASS_RATIO_CEILING}x "
        "ceiling — the ratio gate would not have caught the regression it "
        "exists to catch"
    )
