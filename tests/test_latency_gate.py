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

import statistics
import sys
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import freshness
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
