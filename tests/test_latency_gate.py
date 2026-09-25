"""Per-lane latency CEILING gate at realistic corpus scale (model-free).

Why this exists: a fixture-scale (10-file) latency benchmark once reported a
whole `find()` at ~5ms and HID a ~14s graph-lane cost on the owner's ~1700-note
vault. An aggregate over a toy corpus cannot catch a single-lane blow-up. This
gate closes that hole: it generates a realistic, densely-wikilinked 2000-note
vault (the same `scripts/synth_vault.py` generator the latency-curve harness and
the graph-lane regression test use), warms every lane, then asserts that NO lane
exceeds a sane per-lane ceiling — so a 14s-style regression fails CI loudly.

It is deliberately MODEL-FREE: the lane that regressed (graph) needs no model,
and neither do bm25/keyword/fusion. The vector/CLIP lanes are switched off so the
gate is deterministic and needs no GPU, model download, or embedding sidecar —
it runs in the lean CI matrix (like test_graph_lane_perf.py) AND is pinned in the
retrieval-eval job.

BASELINE — measured 2026-07-03 on the maintainer's box (AMD Ryzen 7 5800X3D /
RTX 5080 / 32 GB, Windows 11), model-free, over the 2000-note dense synthetic
vault via `scripts/latency_curve.py --sizes 2000` and a direct rebuild probe:

    warm graph lane   median ~222ms   p90 ~239ms
    warm end-to-end   median ~805ms   p90 ~1041ms
    bm25 / keyword    median ~243ms / ~268ms   (both O(N) full-corpus lanes)
    resolver REBUILD  ~1662ms  (a from-scratch WikilinkResolver over 2000 notes)

The regression this guards — the graph resolver reverting to a per-query rebuild
(read + YAML-parse every note) — would push the graph lane from ~222ms to
~1.9s+ (rebuild ~1662ms + resolution). The ceilings sit in the wide gap between
the warm baseline and that regression, with enough margin (~4.5x over the warm
median) that a slower CI runner does not flake but a rebuild regression cannot
hide. Re-measure (don't hand-tune) if the corpus generator or lane code changes.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import pytest
import yaml

from exomem import find as find_module
from exomem import freshness, lexstore
from exomem.vault import walk_vault_md

# Reuse the ONE synthetic-vault generator (scripts/synth_vault.py).
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from synth_vault import gen_dense_vault  # noqa: E402

# Corpus scale for the gate. 2000 notes: big enough that a per-query resolver
# rebuild (~1.7s here) stands clearly apart from the warm graph lane (~0.2s),
# yet small enough to generate + measure in a few seconds in CI.
N_NOTES = 2000

# Fixed query set, run over the warm corpus. Graph cost here is corpus-driven,
# not query-driven (verified: broad vs. selective queries cost the same), so a
# small spread of queries gives a stable median.
_QUERIES = (
    "topic prose paragraph related context",
    "note about synthetic dense graph",
    "related links between insight pattern notes",
)
_REPEAT = 3  # passes over the query set → ~9 samples per lane for a stable median

# --- Ceilings (see the module docstring for the measured baseline they derive
# from). Median-based; a rebuild regression trips them with room to spare while
# CI-speed variance over the warm baseline does not.
CEIL_GRAPH_MS = 1000.0  # warm ~222ms; a per-query resolver rebuild → ~1.9s trips this
CEIL_TOTAL_MS = 5000.0  # warm ~805ms; catastrophic-blowup backstop, CI-robust
CEIL_REFERENTS_MS = 1000.0  # warm median ~8ms at 2k notes + 500 entities
# The cold first call pays the one-time Entities/ walk (500 pages ≈ 256ms on the
# reference box) plus the cold graph-sidecar open: ≈474ms here, 1.3–1.4s on a CI
# runner. Warm calls never pay it again (registry keyed on the freshness
# checkpoint), so it gets the same catastrophic-blowup backstop as CEIL_TOTAL_MS
# rather than the warm ceiling; a per-query walk (~500ms × N) would trip it.
CEIL_REFERENTS_COLD_MS = 5000.0
# Relation-filtered recall adds one indexed sidecar lookup (two indexed edge
# queries + graph_nodes joins) to find(); it must not turn into an O(corpus)
# walk. This is a generous catastrophic-blowup backstop, not a tight bound —
# a full-scan regression would blow well past it. (Native-Windows dev machines
# cannot complete the bulk-corpus reads this fixture needs; the Linux CI matrix
# is the calibration authority, matching the graph ceilings above.)
CEIL_RELATION_FILTER_MS = 5000.0

# --- Warm-graph scaling bound (the anti-O(N) gate from the FTS5 change).
# MEASURED BASIS (2026-07-04, same box as the module baseline, registry LIVE):
# warm graph median 3.8ms @ 2k → 4.2ms @ 10k → 8.9ms @ 50k — flat, because the
# per-query cost is seed-capped expansion, not corpus size. The historical
# 226ms/1.1s/7.8s "graph wall" was a registry-COLD artifact: FreshnessSnapshot's
# O(N) stat-walk fallback billed to the graph span (the harness seeded the
# registry and then wiped it via clear_cache). A linear warm cost returning at
# N_NOTES_LARGE would add the walk back (~800ms at 8k on the reference box) and
# blow this bound by an order of magnitude; timing jitter on millisecond-scale
# medians is absorbed by the absolute slack. Re-measure, don't hand-tune.
N_NOTES_LARGE = 8000  # 4x the base corpus
CEIL_GRAPH_RATIO = 1.5  # warm graph median at 4x corpus must stay within 1.5x
GRAPH_RATIO_SLACK_MS = 25.0  # noise floor for ms-scale medians on shared CI
CEIL_REFERENTS_RATIO = 1.5
REFERENTS_RATIO_SLACK_MS = 25.0

# --- Context-compiler ceilings (add-context-activation, design D9).
# The metric includes the entire activation request. Excluding its retrieval
# stage previously allowed a fast compiler behind a many-second corpus search
# to pass. Stage timings diagnose regressions; total latency admits the request.
#
# The compiler's stages are index-backed and seed-capped, so they must be FLAT in
# corpus size: resolution reads a bounded candidate window out of the anchor
# catalogue, and each lane is capped before the budget pass. A linear-in-N cost
# reappearing here means a lane started walking the corpus, which is the
# regression class this gate names. The absolute ceiling is a
# catastrophic-blowup backstop sized like CEIL_REFERENTS_MS, not a tuned bound;
# re-measure rather than hand-tuning it.
CEIL_WORKING_SET_MS = 1000.0
CEIL_WORKING_SET_RATIO = 1.5
WORKING_SET_RATIO_SLACK_MS = 50.0
#: A unique authored alias isolates scale measurement from the deliberately
#: repetitive synthetic names. Resolver quality has separate acceptance cases;
#: this gate must run useful role lanes, never time an empty abstention.
WORKING_SET_TURN = (
    "I'm planning to meet Activation Scale Contact — what are the constraints?"
)


def _seed_freshness_live(vault: Path) -> None:
    """Seed the event-maintained freshness registry the way the watcher does, so
    the graph lane's resolver is live and warm (production shape) — not rebuilt."""
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


def _build_dense_vault(root: Path, n: int) -> Path:
    """Generate, freshness-seed, and lane-warm an n-note dense vault."""
    vault = root / f"vault-{n}"
    gen_dense_vault(vault, n)
    _seed_freshness_live(vault)
    # Startup owns the potentially unbounded catalog build.  Interactive find
    # calls intentionally refuse to become that build for a large corpus, so
    # this steady-state latency fixture must perform the explicit warmup first.
    lexstore.ensure_fresh(vault)
    # Warm every lane once so the measured passes reflect steady state, not the
    # first-touch lexical-sidecar / bm25-corpus / resolver build.
    for q in _QUERIES:
        find_module.find(vault, query=q, limit=10, mode="hybrid", graph=True)
    return vault


@pytest.fixture
def model_free(monkeypatch: pytest.MonkeyPatch):
    """Model lanes OFF + caches clean — the deterministic lean-CI shape.

    The vector/CLIP lanes are forced off (CLIP via env, vector by making the
    embedding getter raise ImportError — find() treats that as a lean-deployment
    shape and falls back to BM25/keyword without recording a failure), so the
    gate measures the model-free lanes deterministically whether or not torch is
    installed on the host.
    """
    find_module.clear_cache()
    freshness.clear()
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")  # every call runs all lanes

    from exomem import embeddings as embeddings_module

    def _raise(*_a, **_k):
        raise ImportError("model-free latency gate: vector lane disabled")

    monkeypatch.setattr(embeddings_module, "get_embedding_index", _raise)
    yield
    find_module.clear_cache()
    freshness.clear()


@pytest.fixture
def dense_vault_2k(tmp_path: Path, model_free) -> Path:
    """A warm, freshness-seeded 2000-note dense vault with model lanes OFF."""
    return _build_dense_vault(tmp_path, N_NOTES)


def _measure(vault: Path) -> tuple[dict[str, float], float]:
    """Return (per-lane median ms, total median ms) over the warm query set."""
    lane_samples: dict[str, list[float]] = {}
    total_samples: list[float] = []
    for _ in range(_REPEAT):
        for q in _QUERIES:
            t = find_module.FindTimings()
            find_module.find(vault, query=q, limit=10, mode="hybrid", graph=True, timings=t)
            d = t.as_dict()
            total_samples.append(d["total_ms"])
            for lane, stage in d["stages"].items():
                # A lane's span records `ms` even when its body raised (the
                # model-free vector ImportError), so skip errored/skipped lanes.
                if "ms" in stage and "error" not in stage and "skipped" not in stage:
                    lane_samples.setdefault(lane, []).append(stage["ms"])
    medians = {lane: statistics.median(v) for lane, v in lane_samples.items()}
    return medians, statistics.median(total_samples)


def test_no_lane_exceeds_ceiling_at_scale(dense_vault_2k: Path) -> None:
    """No lane exceeds its ceiling at 2000 notes — the anti-hidden-14s gate.

    Asserts two things over one warm measurement (the vault is generated once):

    1. The GRAPH lane stays under CEIL_GRAPH_MS. This is the direct guard for the
       ~14s regression: if the resolver reverts to a per-query full rebuild, the
       graph lane jumps from ~0.2s to ~1.9s+ and trips the ceiling. The graph
       stage must also actually run — a silent skip is its own regression.
    2. End-to-end find() stays under CEIL_TOTAL_MS — a CI-robust backstop for any
       single lane blowing into the seconds range. The failure message names the
       dominant lane so triage starts from evidence, not a bisect.
    """
    medians, total_ms = _measure(dense_vault_2k)
    rounded = {k: round(v, 1) for k, v in medians.items()}

    assert "graph" in medians, f"graph lane did not run at {N_NOTES} notes: {rounded}"
    graph_ms = medians["graph"]
    assert graph_ms < CEIL_GRAPH_MS, (
        f"graph lane median {graph_ms:.0f}ms >= ceiling {CEIL_GRAPH_MS:.0f}ms at "
        f"{N_NOTES} notes — the resolver is likely being rebuilt per query again "
        f"(warm baseline ~222ms; a full rebuild is ~1.7s). all medians: {rounded}"
    )

    worst = max(medians.items(), key=lambda kv: kv[1]) if medians else ("<none>", 0.0)
    assert total_ms < CEIL_TOTAL_MS, (
        f"total find() median {total_ms:.0f}ms >= ceiling {CEIL_TOTAL_MS:.0f}ms at "
        f"{N_NOTES} notes (warm baseline ~805ms). Dominant lane: {worst[0]} "
        f"({worst[1]:.0f}ms). all medians: {rounded}"
    )


# This case builds the 2000-note fixture AND a full graph sidecar rebuild on top
# (the fixture leaves the graph lane in wikilink-fallback), so it lands near the
# repo's global 60s timeout; give it explicit headroom so CI is deterministic. The
# assertion it guards (relation-filtered find() < CEIL_RELATION_FILTER_MS) is what
# matters, not the one-time fixture+rebuild setup cost.
@pytest.mark.timeout(300)
def test_relation_filtered_recall_stays_bounded(dense_vault_2k: Path) -> None:
    """A relation filter resolves participants from the indexed sidecar, not an
    O(corpus) walk — end-to-end find() with `relations=[...]` stays under the
    catastrophic-blowup backstop at 2000 notes.

    The dense fixture leaves the graph lane in wikilink-fallback (no sidecar), so
    the sidecar is built once here; `links_to` is the broadest relation (every
    wikilink), exercising the largest participant set the corpus can produce.
    """
    from exomem import epistemic_graph

    epistemic_graph.EpistemicGraphIndex(dense_vault_2k).rebuild_all()
    samples: list[float] = []
    for _ in range(_REPEAT):
        for q in _QUERIES:
            t = find_module.FindTimings()
            find_module.find(
                dense_vault_2k,
                query=q,
                limit=10,
                mode="hybrid",
                graph=True,
                relations=["links_to"],
                timings=t,
            )
            samples.append(t.as_dict()["total_ms"])
    total_ms = statistics.median(samples)
    assert total_ms < CEIL_RELATION_FILTER_MS, (
        f"relation-filtered find() median {total_ms:.0f}ms >= ceiling "
        f"{CEIL_RELATION_FILTER_MS:.0f}ms at {N_NOTES} notes — the participant "
        f"lookup is likely walking the corpus instead of the indexed sidecar."
    )


def test_warm_candidate_admission_reuses_checkpointed_projection(
    tmp_path: Path, model_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lane filtering uses the request's exact projection instead of re-statting
    every ranked path.  One live check before page hydration remains allowed."""
    from exomem import recall_policy

    note_count = 200
    vault = _build_dense_vault(tmp_path, note_count)
    original = recall_policy.is_recall_candidate
    calls = 0

    def counted(root: Path, path: Path | str) -> bool:
        nonlocal calls
        calls += 1
        return original(root, path)

    monkeypatch.setattr(recall_policy, "is_recall_candidate", counted)

    find_module.find(
        vault,
        query=_QUERIES[0],
        limit=10,
        mode="hybrid",
        graph=True,
    )

    assert calls <= note_count + 100, (
        f"candidate admission revalidated {calls} paths for {note_count} notes; "
        "the checkpoint-bound projection should filter lanes before bounded live hydration"
    )


def test_warm_graph_lane_does_not_scale_linearly(tmp_path: Path, model_free) -> None:
    """Warm graph median at 4x the corpus stays within CEIL_GRAPH_RATIO (plus an
    absolute ms-scale noise floor) — so a linear-in-N warm cost cannot return
    silently, whatever its mechanism (the last one was FreshnessSnapshot's
    O(N) stat-walk fallback billed to the graph span; see the bound's comment).

    A ceiling alone cannot catch this: 8.9ms at 50k passes CEIL_GRAPH_MS with
    two orders of magnitude to spare, so an O(N) regression would hide under
    the ceiling for years of corpus growth. The RATIO pins the shape.
    """
    small = _build_dense_vault(tmp_path, N_NOTES)
    small_medians, _ = _measure(small)
    large = _build_dense_vault(tmp_path, N_NOTES_LARGE)
    large_medians, _ = _measure(large)

    assert "graph" in small_medians and "graph" in large_medians, (
        f"graph lane did not run at both sizes: {small_medians.keys()} / {large_medians.keys()}"
    )
    g_small, g_large = small_medians["graph"], large_medians["graph"]
    bound = max(g_small * CEIL_GRAPH_RATIO, g_small + GRAPH_RATIO_SLACK_MS)
    assert g_large < bound, (
        f"warm graph median scaled {g_small:.1f}ms @ {N_NOTES} → {g_large:.1f}ms "
        f"@ {N_NOTES_LARGE} notes (bound {bound:.1f}ms): a linear-in-N per-query "
        f"cost is back in the graph lane. Sub-spans: "
        f"{ {k: round(v, 1) for k, v in large_medians.items() if k.startswith('graph')} }"
    )


def _measure_referent_stage(vault: Path) -> tuple[float, float, dict]:
    from exomem import commands

    def call() -> dict:
        result = commands.op_find(
            vault,
            query="my two synthetic friends",
            limit=10,
            mode="hybrid",
            graph=True,
            rerank=False,
            include_timings=True,
        )
        assert isinstance(result, dict)
        return result

    first = call()
    first_stage = first["timings"]["stages"].get("referents")
    assert first_stage is not None and "ms" in first_stage
    samples: list[float] = []
    for _ in range(3):
        result = call()
        stage = result["timings"]["stages"].get("referents")
        assert stage is not None and "ms" in stage
        samples.append(stage["ms"])
    return first_stage["ms"], statistics.median(samples), first["referents"]


@pytest.mark.timeout(300)
def test_referent_stage_stays_bounded_at_scale(
    tmp_path: Path, model_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synth_vault import gen_entity_overlay

    from exomem import commands, referent_runtime

    vault = _build_dense_vault(tmp_path, N_NOTES)
    gen_entity_overlay(vault, 500, seed=19)
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    cold_referents_ms, referents_ms, block = _measure_referent_stage(vault)
    assert cold_referents_ms < CEIL_REFERENTS_COLD_MS
    assert referents_ms < CEIL_REFERENTS_MS
    assert len(json.dumps(block)) < 16_000

    resolver_calls = 0
    original_resolver = referent_runtime.resolve_for_find

    def counted_resolver(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(*args, **kwargs)

    monkeypatch.setattr(referent_runtime, "resolve_for_find", counted_resolver)

    non_cue = commands.op_find(
        vault,
        query="synthetic retrieval topic",
        mode="hybrid",
        graph=True,
        rerank=False,
        include_timings=True,
    )
    assert "referents" not in non_cue["timings"]["stages"]
    assert resolver_calls == 0

    monkeypatch.setenv("EXOMEM_DISABLE_REFERENTS", "1")
    disabled = commands.op_find(
        vault,
        query="my two synthetic friends",
        mode="hybrid",
        graph=True,
        rerank=False,
        include_timings=True,
    )
    assert "referents" not in disabled["timings"]["stages"]


@pytest.mark.timeout(600)
def test_referent_stage_does_not_scale_linearly(tmp_path: Path, model_free) -> None:
    from synth_vault import gen_entity_overlay

    small = _build_dense_vault(tmp_path, N_NOTES)
    gen_entity_overlay(small, 125, seed=23)
    _seed_freshness_live(small)
    lexstore.ensure_fresh(small)
    _, small_ms, _ = _measure_referent_stage(small)

    large = _build_dense_vault(tmp_path, N_NOTES_LARGE)
    gen_entity_overlay(large, 500, seed=23)
    _seed_freshness_live(large)
    lexstore.ensure_fresh(large)
    _, large_ms, _ = _measure_referent_stage(large)

    bound = max(
        small_ms * CEIL_REFERENTS_RATIO,
        small_ms + REFERENTS_RATIO_SLACK_MS,
    )
    assert large_ms < bound, (
        f"referents stage scaled {small_ms:.1f}ms @ {N_NOTES} to "
        f"{large_ms:.1f}ms @ {N_NOTES_LARGE} (bound {bound:.1f}ms)"
    )


@pytest.mark.timeout(600)
def test_entity_type_registry_load_is_bounded_at_scale(
    tmp_path: Path,
    model_free,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import commands, entity_types

    original_parse = entity_types._parse_extension_data
    vault = _build_dense_vault(tmp_path, N_NOTES_LARGE)
    registry_path = vault / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_text = yaml.safe_dump(
        {
            "schema_version": 1,
            "entity_types": {
                f"place-{index:02d}": {
                    "folder": f"Places{index:02d}",
                    "label": f"Place {index:02d}",
                    "aliases": [f"location{index:02d}"],
                    "cue_nouns": [f"venue{index:02d}"],
                    "capture_guidance": "A stable synthetic place identity.",
                    "parent": "concept",
                }
                for index in range(50)
            },
        },
        sort_keys=False,
    )

    def cold_find_ms() -> float:
        find_module.clear_cache()
        entity_types._CACHE.clear()
        _seed_freshness_live(vault)
        started = time.perf_counter()
        commands.op_find(
            vault,
            query="which venue00",
            mode="hybrid",
            graph=False,
            rerank=False,
        )
        return (time.perf_counter() - started) * 1000

    cold_deltas_ms: list[float] = []
    without_registry_ms: list[float] = []
    with_registry_ms: list[float] = []
    measurement_order: list[str] = []

    def measure(*, with_registry: bool) -> float:
        if with_registry:
            registry_path.write_text(registry_text, encoding="utf-8")
            measurement_order.append("with")
        else:
            registry_path.unlink(missing_ok=True)
            measurement_order.append("without")
        return cold_find_ms()

    for index in range(_REPEAT):
        if index % 2 == 0:
            without_ms = measure(with_registry=False)
            with_ms = measure(with_registry=True)
        else:
            with_ms = measure(with_registry=True)
            without_ms = measure(with_registry=False)
        without_registry_ms.append(without_ms)
        with_registry_ms.append(with_ms)
        cold_deltas_ms.append(with_ms - without_ms)
    assert measurement_order == [
        value
        for _index in range(_REPEAT)
        for value in (("without", "with") if _index % 2 == 0 else ("with", "without"))
    ]
    registry_path.write_text(registry_text, encoding="utf-8")
    # Whole-find wall time is dominated by BM25/lexstore cold cost (~300ms here,
    # ~800ms on a CI runner), so a fixed millisecond delta over three paired
    # samples measures runner noise, not the registry (CI read 103ms on an 817ms
    # baseline). Bound the whole-find delta as a catastrophic backstop — a quarter
    # of the baseline or 50ms, whichever is larger — and bound the registry's
    # attributable cost directly below.
    without_median_ms = statistics.median(without_registry_ms)
    cold_delta_ms = statistics.median(cold_deltas_ms)
    cold_delta_ceiling_ms = max(50.0, 0.25 * without_median_ms)
    assert cold_delta_ms < cold_delta_ceiling_ms, (
        f"50-type registry added {cold_delta_ms:.1f}ms to cold op_find "
        f"(without={without_median_ms:.1f}ms, "
        f"with={statistics.median(with_registry_ms):.1f}ms, "
        f"ceiling={cold_delta_ceiling_ms:.1f}ms)"
    )

    # Attributable cold cost, each from a cleared cache: (a) registry parse +
    # cue-noun build, bounded directly; (b) the entity-folder enumeration, as a
    # paired delta with vs without the fifty extension folders — the dense
    # fixture's own entity pages cost the same with or without a registry and
    # are bounded by the referents gate, not here.
    from exomem import entity_registry, referent_resolution

    for index in range(50):
        (vault / "Knowledge Base" / "Entities" / f"Places{index:02d}").mkdir(
            parents=True, exist_ok=True
        )

    def attributable(*, with_registry: bool) -> tuple[float, float]:
        if with_registry:
            registry_path.write_text(registry_text, encoding="utf-8")
        else:
            registry_path.unlink(missing_ok=True)
        entity_types._CACHE.clear()
        referent_resolution._CUE_NOUN_CACHE.clear()
        entity_registry.clear_entity_registry_cache()
        started = time.perf_counter()
        registry = entity_types.load_entity_types(vault)
        referent_resolution.cue_nouns_for(registry)
        parsed = (time.perf_counter() - started) * 1000
        assert len(registry.extensions) == (50 if with_registry else 0)
        started = time.perf_counter()
        entity_registry.load_entity_registry(
            vault, freshness_key=("registry-latency", with_registry), type_registry=registry
        )
        return parsed, (time.perf_counter() - started) * 1000

    parse_with_ms: list[float] = []
    walk_with_ms: list[float] = []
    walk_without_ms: list[float] = []
    for index in range(_REPEAT):
        order = (False, True) if index % 2 == 0 else (True, False)
        for arm in order:
            parsed, walked = attributable(with_registry=arm)
            if arm:
                parse_with_ms.append(parsed)
                walk_with_ms.append(walked)
            else:
                walk_without_ms.append(walked)
    registry_path.write_text(registry_text, encoding="utf-8")
    parse_median_ms = statistics.median(parse_with_ms)
    walk_without_median_ms = statistics.median(walk_without_ms)
    walk_delta_ms = statistics.median(walk_with_ms) - walk_without_median_ms
    assert parse_median_ms < 50.0, (
        f"50-type registry parse + cue-noun build took {parse_median_ms:.1f}ms cold"
    )
    assert walk_delta_ms < max(50.0, 0.25 * walk_without_median_ms), (
        f"fifty extension folders added {walk_delta_ms:.1f}ms to the cold entity walk "
        f"(without={walk_without_median_ms:.1f}ms)"
    )

    parse_ms: list[float] = []

    def timed_parse(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_parse(*args, **kwargs)
        finally:
            parse_ms.append((time.perf_counter() - started) * 1000)

    monkeypatch.setattr(entity_types, "_parse_extension_data", timed_parse)
    entity_types._CACHE.clear()

    commands.op_find(
        vault,
        query="which venue00",
        mode="hybrid",
        graph=False,
        rerank=False,
    )
    assert len(parse_ms) == 1
    assert parse_ms[0] < 50.0

    commands.op_find(
        vault,
        query="which venue00",
        mode="hybrid",
        graph=False,
        rerank=False,
    )
    assert len(parse_ms) == 1, "warm registry load reparsed instead of costing 0 ms"


# --------------------------------------------------------------------------- #
# Context compiler (add-context-activation task 5.4)
# --------------------------------------------------------------------------- #


def _compiler_ms(timings: dict) -> float:
    """Measure the whole operation, including work outside compiler spans."""
    return float(timings["total_ms"])


def _measure_working_set(vault: Path) -> tuple[float, dict]:
    """Return (warm median compiler ms, one packet) over repeated activations.

    The index is built explicitly first, for the same reason `lexstore.ensure_fresh`
    is: startup owns a potentially unbounded derived build, and an interactive call
    must never become that build.
    """
    from exomem import commands, working_set_index, working_set_runtime

    # The generated entities otherwise all share the words "Synthetic Person",
    # which can trigger disambiguation before any role work runs.
    person = next((vault / "Knowledge Base/Entities/People").glob("synthetic-person-00007-*.md"))
    person.write_text(
        person.read_text(encoding="utf-8").replace(
            "entity_type: person\n",
            "entity_type: person\naliases: [Activation Scale Contact]\n",
            1,
        ),
        encoding="utf-8",
    )
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()

    def call() -> dict:
        # The packet cache is keyed on the turn, so vary it to measure the
        # compiler rather than a cache hit.
        working_set_runtime.reset_caches_for_tests()
        return commands.op_activate_context(
            vault, turn=WORKING_SET_TURN, include_timings=True
        )

    packet = call()
    samples = [_compiler_ms(call()["timings"]) for _ in range(3)]
    return statistics.median(samples), packet


@pytest.mark.timeout(300)
def test_working_set_compiler_stays_bounded_at_scale(tmp_path: Path, model_free) -> None:
    from synth_vault import gen_entity_overlay

    vault = _build_dense_vault(tmp_path, N_NOTES)
    gen_entity_overlay(vault, 500, seed=19)
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)

    compiler_ms, packet = _measure_working_set(vault)

    assert packet["abstained"] is False, packet.get("abstention")
    assert compiler_ms < CEIL_WORKING_SET_MS, (
        f"context compiler took {compiler_ms:.1f}ms @ {N_NOTES} notes "
        f"(ceiling {CEIL_WORKING_SET_MS:.1f}ms)"
    )
    # The packet is bounded by construction; assert it, so a budget regression
    # shows up as a gate failure rather than as a context-window surprise.
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]
    assert len(json.dumps(packet)) < 24_000


@pytest.mark.timeout(600)
def test_working_set_compiler_does_not_scale_linearly(tmp_path: Path, model_free) -> None:
    from synth_vault import gen_entity_overlay

    small = _build_dense_vault(tmp_path, N_NOTES)
    gen_entity_overlay(small, 125, seed=23)
    _seed_freshness_live(small)
    lexstore.ensure_fresh(small)
    small_ms, _ = _measure_working_set(small)

    large = _build_dense_vault(tmp_path, N_NOTES_LARGE)
    gen_entity_overlay(large, 500, seed=23)
    _seed_freshness_live(large)
    lexstore.ensure_fresh(large)
    large_ms, _ = _measure_working_set(large)

    bound = max(
        small_ms * CEIL_WORKING_SET_RATIO,
        small_ms + WORKING_SET_RATIO_SLACK_MS,
    )
    assert large_ms < bound, (
        f"context compiler scaled {small_ms:.1f}ms @ {N_NOTES} to "
        f"{large_ms:.1f}ms @ {N_NOTES_LARGE} (bound {bound:.1f}ms)"
    )


#: Distinct pages the full ring's events fall on, the same at every corpus
#: size, so the ratio measures the corpus and not a differently shaped ring.
RING_PAGES = 512


def _measure_referential_working_set(vault: Path) -> tuple[float, dict]:
    """Return (warm median ms, one packet) for "continue" against a heat ring
    at `RING_MAX` events over `RING_PAGES` pages, the scaled contact's page
    the newest work.

    One read lands before every measured call, so each one rebuilds the
    profile from the full ring rather than serving a cached aggregation: the
    steady state of a session that reads between turns.
    """
    from exomem import commands, working_set_heat, working_set_index, working_set_runtime

    person = next((vault / "Knowledge Base/Entities/People").glob("synthetic-person-00007-*.md"))
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    # Seeds the projection: the registry is copied once per sidecar.
    commands.op_activate_context(vault, turn=WORKING_SET_TURN)

    pages = sorted(
        path.relative_to(vault).as_posix()
        for path in (vault / "Knowledge Base" / "Notes").rglob("*.md")
    )[:RING_PAGES]
    assert len(pages) == RING_PAGES
    now_ns = time.time_ns()
    ring = [
        working_set_heat.HeatEvent(
            now_ns - 3_600 * 10**9 + index * 10**6,
            pages[index % len(pages)],
            "read" if index % 3 else "work",
            origin="ring",
        )
        for index in range(working_set_heat.RING_MAX)
    ]
    working_set_heat.append(vault, ring)
    target = person.relative_to(vault).as_posix()
    working_set_heat.append(
        vault, [working_set_heat.HeatEvent(now_ns - 60 * 10**9, target, "work", origin="edit_memory")]
    )

    def call(index: int) -> dict:
        working_set_heat.append(
            vault,
            [
                working_set_heat.HeatEvent(
                    now_ns - 1_800 * 10**9 + index, pages[index % len(pages)], "read", origin="read"
                )
            ],
        )
        working_set_runtime.reset_caches_for_tests()
        return commands.op_activate_context(vault, turn="continue", include_timings=True)

    packet = call(0)
    samples = [_compiler_ms(call(index)["timings"]) for index in range(1, 4)]
    return statistics.median(samples), packet


@pytest.mark.timeout(900)
def test_referential_working_set_stays_under_ceiling_with_a_full_ring(
    tmp_path: Path, model_free
) -> None:
    """Step 5's projection at its bound: a full ring is aggregated and ranked
    on every referential turn, so "continue" must stay under the compiler
    ceiling at 2k and 8k notes and must not scale with the corpus."""
    from synth_vault import gen_entity_overlay

    measured: dict[int, tuple[float, dict]] = {}
    for notes, overlay in ((N_NOTES, 125), (N_NOTES_LARGE, 500)):
        vault = _build_dense_vault(tmp_path, notes)
        gen_entity_overlay(vault, overlay, seed=29)
        measured[notes] = _measure_referential_working_set(vault)

    for notes, (compiler_ms, packet) in measured.items():
        assert packet["abstained"] is False, (notes, packet.get("abstention"))
        assert any("recency" in item["evidence"] for item in packet["anchors"]), notes
        assert compiler_ms < CEIL_WORKING_SET_MS, (
            f"referential activation took {compiler_ms:.1f}ms @ {notes} notes with a full "
            f"ring (ceiling {CEIL_WORKING_SET_MS:.1f}ms)"
        )
    small_ms, large_ms = measured[N_NOTES][0], measured[N_NOTES_LARGE][0]
    bound = max(small_ms * CEIL_WORKING_SET_RATIO, small_ms + WORKING_SET_RATIO_SLACK_MS)
    assert large_ms < bound, (
        f"referential activation scaled {small_ms:.1f}ms @ {N_NOTES} to "
        f"{large_ms:.1f}ms @ {N_NOTES_LARGE} with a full ring (bound {bound:.1f}ms)"
    )
    sys.stderr.write(
        f"\nreferential working set, full ring: {small_ms:.1f}ms @ {N_NOTES}, "
        f"{large_ms:.1f}ms @ {N_NOTES_LARGE} (bound {bound:.1f}ms)\n"
    )


# --- Task 5.3 (make-activation-conventions-vault-owned): the same ceiling
# holds with a vault-owned registry in effect. `test_working_set_compiler_
# does_not_scale_linearly` above already measures 8k notes but only asserts
# the SCALING ratio against the 2k measurement; this pins the absolute
# ceiling at 8k too, the way the 2k test above already pins it.


def _conventions_override_at_caps() -> dict:
    """An override sized to exactly every cap `activation_conventions` names,
    with rules that match no real page -- the caps exist to bound per-request
    evaluation cost, not the number of pages one rule admits (design.md
    decision 3's own point), so this measures the cost of the RULE COUNT.
    """
    from exomem import activation_conventions as ac_module

    return {
        "schema_version": 1,
        "anchors": {
            "resource": {
                "add_folders": [f"CustomResource{i}" for i in range(ac_module.MAX_FOLDERS_PER_KIND)],
                "add_tags": [f"resource-tag-{i}" for i in range(ac_module.MAX_TAGS_PER_KIND)],
                "add_types": [f"resource-type-{i}" for i in range(ac_module.MAX_TYPES_PER_KIND)],
            },
            "hub": {
                "add_folders": [f"CustomHub{i}" for i in range(ac_module.MAX_FOLDERS_PER_KIND)],
                "add_tags": [f"hub-tag-{i}" for i in range(ac_module.MAX_TAGS_PER_KIND)],
                "add_types": [f"hub-type-{i}" for i in range(ac_module.MAX_TYPES_PER_KIND)],
            },
            "add_skip_folders": [f"CustomSkip{i}" for i in range(ac_module.MAX_SKIP_FOLDERS)],
        },
        "state": {
            "prefer_state_fields": [f"custom_state_field_{i}" for i in range(ac_module.MAX_STATE_FIELDS)]
        },
        "stopwords": {"add": [f"customstopword{i}" for i in range(ac_module.MAX_STOPWORDS)]},
        "resolution": {"rare_term_max_anchors": 1},
    }


def _roles_override_at_caps() -> dict:
    """An override sized to exactly every cap `context_roles` names: the
    roles-per-override, cues-per-role and evidence-categories-per-role caps.
    """
    from exomem import context_roles as roles_module

    categories = [
        "decision", "fact", "finding", "insight",
        "constraint", "requirement", "assumption", "risk",
    ]
    assert len(categories) == roles_module.MAX_EVIDENCE_CATEGORIES_PER_ROLE
    cues = [f"custom cue number {i}" for i in range(roles_module.MAX_CUES_PER_ROLE)]
    return {
        "schema_version": 1,
        "roles": {
            f"custom_latency_role_{i}": {
                "lane": "evidence",
                "description": "synthetic role for the latency gate",
                "cues": cues,
                "evidence_cues": cues,
                "evidence_categories": categories,
            }
            for i in range(roles_module.MAX_ROLES_PER_OVERRIDE)
        },
    }


def _write_overrides_at_caps(vault: Path) -> None:
    import yaml

    from exomem import activation_conventions as ac_module
    from exomem import context_roles as roles_module

    roles_module.override_path(vault).parent.mkdir(parents=True, exist_ok=True)
    roles_module.override_path(vault).write_text(
        yaml.safe_dump(_roles_override_at_caps(), sort_keys=True), encoding="utf-8"
    )
    ac_module.override_path(vault).write_text(
        yaml.safe_dump(_conventions_override_at_caps(), sort_keys=True), encoding="utf-8"
    )
    roles_module.clear_cache()
    ac_module.clear_cache()


@pytest.mark.timeout(600)
def test_working_set_compiler_stays_bounded_at_8k_notes_with_the_shipped_registry(
    tmp_path: Path, model_free
) -> None:
    from synth_vault import gen_entity_overlay

    vault = _build_dense_vault(tmp_path, N_NOTES_LARGE)
    gen_entity_overlay(vault, 500, seed=19)
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)

    compiler_ms, packet = _measure_working_set(vault)

    assert packet["abstained"] is False, packet.get("abstention")
    assert compiler_ms < CEIL_WORKING_SET_MS, (
        f"context compiler took {compiler_ms:.1f}ms @ {N_NOTES_LARGE} notes "
        f"(ceiling {CEIL_WORKING_SET_MS:.1f}ms)"
    )


@pytest.mark.timeout(300)
def test_working_set_compiler_stays_bounded_with_an_override_at_the_caps(
    tmp_path: Path, model_free
) -> None:
    from synth_vault import gen_entity_overlay

    vault = _build_dense_vault(tmp_path, N_NOTES)
    gen_entity_overlay(vault, 500, seed=19)
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    _write_overrides_at_caps(vault)

    compiler_ms, packet = _measure_working_set(vault)

    assert packet["abstained"] is False, packet.get("abstention")
    assert packet["generation"]["roles_source"] == "vault"
    assert packet["generation"]["conventions_source"] == "vault"
    assert compiler_ms < CEIL_WORKING_SET_MS, (
        f"context compiler took {compiler_ms:.1f}ms @ {N_NOTES} notes with an "
        f"override at every cap (ceiling {CEIL_WORKING_SET_MS:.1f}ms)"
    )


@pytest.mark.timeout(300)
def test_a_rule_admitting_a_large_folder_is_reported_not_gated(
    tmp_path: Path, model_free
) -> None:
    """design.md's Risks section: 'An owner admits everything ... The first is
    slow; the latency gate reports it.' `Notes/` holds three of
    `gen_dense_vault`'s seven folders, so naming it as a resource rule makes
    most of the corpus an anchor candidate -- legitimately slow, not unsound
    (more anchors only tightens `rare_term`), so this measures and reports
    the cost instead of gating it against `CEIL_WORKING_SET_MS`. The
    `pytest.mark.timeout` above is the actual backstop, against a hang rather
    than against a slow-but-correct compile.
    """
    import yaml

    from exomem import activation_conventions as ac_module
    from synth_vault import gen_entity_overlay

    vault = _build_dense_vault(tmp_path, N_NOTES)
    gen_entity_overlay(vault, 500, seed=19)
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)

    override_path = ac_module.override_path(vault)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "anchors": {"resource": {"add_folders": ["Notes"]}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    ac_module.clear_cache()

    compiler_ms, packet = _measure_working_set(vault)

    print(
        f"\n[reported, not gated] compiler took {compiler_ms:.1f}ms @ {N_NOTES} notes "
        f"with 'Notes/' admitted whole as a resource rule "
        f"(ordinary ceiling {CEIL_WORKING_SET_MS:.1f}ms)"
    )
    assert packet["abstained"] is False, packet.get("abstention")
    assert packet["generation"]["conventions_source"] == "vault"
