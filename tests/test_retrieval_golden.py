"""Golden-set retrieval REGRESSION GATE (embeddings-only CI job).

exomem's differentiated ranking (BM25 + local vectors fused via RRF, reranked)
is the whole value proposition, but the fast CI matrix runs embeddings-OFF
(lexical/BM25 only), so a vector/fusion/ranking regression would sail straight
through it. This test is the quality gate: it builds the embedding sidecar over
the bundled fixture KB, runs the SAME golden-set evaluation the offline harness
uses (`scripts/eval_retrieval.py`), and asserts the measured hybrid ranking
clears hard floors — mean NDCG@10 / MRR / recall@10 with margin, plus a
per-query guard that no golden query silently drops to recall@10 == 0.

Heavy: loads BAAI/bge-base-en-v1.5. It runs only in the dedicated `retrieval-eval`
CI job (`pytest -m embeddings`, embeddings extra installed) and locally. The
module is import-skipped where torch / sentence-transformers are absent, so the
lean 3-version matrix collects nothing here.

BASELINE — how the floors were produced (do NOT hand-tune; re-measure):
    Model rev BAAI/bge-base-en-v1.5 (frozen), exomem 0.4.1, measured 2026-07-03
    on a sidecar-built copy of tests/fixtures (198 chunk vectors) via:
        EXOMEM_VAULT_PATH=<tmp copy of tests/fixtures> \
          uv run --extra embeddings python scripts/eval_retrieval.py --report markdown
    (the copy's sidecar built first with get_embedding_index(vault).rebuild_all()).
    Measured hybrid, rerank OFF (exactly what this test evaluates), 26 golden queries:
        NDCG@5=0.9142  NDCG@10=0.9270  MRR=0.9154  recall@10=0.9615
        per-query recall@10 minimum = 0.5 — two entries deliberately grade a
        marginal page that prefer_compiled / prefer_active DEMOTES out of top-10
        (the compiled-over-source and supersession pins); their grade-3 ideal is
        always found, so no query drops to recall@10 == 0.
        vs keyword-only NDCG@10=0.3430 — the gap this gate protects.
    Floors sit ~0.08-0.14 below the measured means: a genuine ranking regression
    trips them, but run-to-run / CPU-vs-GPU fp noise does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Skip the whole module wherever the vector stack isn't installed (the lean CI
# matrix), so `pytest -q` there collects nothing heavy from here.
pytest.importorskip("sentence_transformers")
pytest.importorskip("torch")

from exomem import embeddings as embeddings_module  # noqa: E402
from exomem import find as find_module  # noqa: E402

# Reuse the offline eval harness verbatim — the gate must score identically to
# `scripts/eval_retrieval.py` (same _canon, same _evaluate, same golden loader).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import eval_retrieval  # noqa: E402

_GOLDEN = _REPO_ROOT / "tests" / "golden" / "queries.yaml"

# --- Floors, WITH MARGIN. See the module docstring for how these were measured.
_MEASURED = {"ndcg10": 0.9270, "mrr": 0.9154, "recall10": 0.9615}  # 2026-07-03, 26 queries
_MEAN_NDCG10_FLOOR = 0.85
_MEAN_MRR_FLOOR = 0.80
_MEAN_RECALL10_FLOOR = 0.88


@pytest.mark.embeddings
@pytest.mark.parametrize(
    ("vec_quant", "lexical_backend"),
    [
        ("off", "fts5"),     # the shipped default: numpy scan + FTS5 lexical lanes
        ("off", "python"),   # lexical kill switch: yesterday's ranking wholesale
        ("binary", "fts5"),  # promotion gate for opt-in binary quantization (vec0)
    ],
)
def test_golden_hybrid_ranking_clears_floors(
    vault: Path, monkeypatch: pytest.MonkeyPatch, vec_quant: str, lexical_backend: str
) -> None:
    """Hybrid ranking over the golden set must clear the measured floors.

    Parametrized over the vector backend's quantization mode AND the lexical
    backend: `off` exercises the numpy scan (the shipped default; exact, so the
    floors are the same regression gate they always were); `binary` opts into the
    vec0 backend and is the PROMOTION GATE for its quantized mode. `fts5` is the
    PROMOTION GATE for the FTS5
    lexical backend — its bm25() scorer differs from BM25Okapi, so it is
    floors-gated (including the stemming pin), not rank-identical; `python`
    proves the kill switch still clears the same floors. The gates compose.

    `vault` (conftest) copies tests/fixtures → a tmp dir and points
    EXOMEM_VAULT_PATH at it; the repo fixtures are never mutated and the sidecar
    lands in the throwaway copy.
    """
    from exomem import lexstore

    if vec_quant == "binary":
        pytest.importorskip("sqlite_vec")
        # numpy is now the default backend; binary quantization lives ONLY in the
        # vec0 backend, so this promotion gate must opt into it explicitly — else
        # the QUANT flag would be silently ignored and the case would re-run numpy.
        monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
        monkeypatch.setenv("EXOMEM_VEC_QUANT", "binary")
    else:
        # `off` cases run the shipped default (numpy) — unset both so no stray
        # process env forces a backend.
        monkeypatch.delenv("EXOMEM_VEC_BACKEND", raising=False)
        monkeypatch.delenv("EXOMEM_VEC_QUANT", raising=False)
    if lexical_backend == "fts5" and not lexstore.fts5_available():
        pytest.skip("this SQLite build lacks FTS5/trigram")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", lexical_backend)
    lexstore.reset_memo()
    lexstore.clear_stores()
    # Live vectors: lift the suite-wide disable (conftest autouse) and any
    # KB_MCP_ alias; leave CLIP off — the golden targets are all text notes.
    for var in ("EXOMEM_DISABLE_EMBEDDINGS", "KB_MCP_DISABLE_EMBEDDINGS"):
        monkeypatch.delenv(var, raising=False)
    embeddings_module._IMPORT_FAILED = False
    # The `vault` fixture already cleared these, but the delenv above widened the
    # world — drop any index instance memoized before embeddings were enabled.
    embeddings_module.clear_embedding_indexes()
    find_module.clear_cache()

    # Build the sidecar the eval reads: the offline script assumes a prebuilt one
    # (a real vault's server maintains it); the fixture ships without one.
    rows = embeddings_module.get_embedding_index(vault).rebuild_all()
    assert rows > 0, "fixture KB produced no embedding chunks — nothing to rank"

    golden = eval_retrieval._load_golden(_GOLDEN)
    assert golden, f"golden set failed to load from {_GOLDEN}"

    result = eval_retrieval._evaluate(
        vault, golden, find_module.DEFAULT_RANKING, rerank=False
    )

    # --- Aggregate floors (mean over the golden set), with margin.
    assert result["ndcg10"] >= _MEAN_NDCG10_FLOOR, (
        f"mean NDCG@10 {result['ndcg10']:.4f} < floor {_MEAN_NDCG10_FLOOR} "
        f"(baseline {_MEASURED['ndcg10']}) — hybrid ranking regressed"
    )
    assert result["mrr"] >= _MEAN_MRR_FLOOR, (
        f"mean MRR {result['mrr']:.4f} < floor {_MEAN_MRR_FLOOR} "
        f"(baseline {_MEASURED['mrr']}) — hybrid ranking regressed"
    )
    assert result["recall10"] >= _MEAN_RECALL10_FLOOR, (
        f"mean recall@10 {result['recall10']:.4f} < floor {_MEAN_RECALL10_FLOOR} "
        f"(baseline {_MEASURED['recall10']}) — hybrid recall regressed"
    )

    # --- Per-query guard: NO golden query may silently vanish (recall@10 == 0).
    # A mean can stay high while one query collapses to zero recall; this catches
    # the single-query cliff the aggregate floors would mask.
    dropped = [r["query"] for r in result["rows"] if r["recall10"] == 0.0]
    assert not dropped, (
        f"{len(dropped)} golden query(ies) dropped to recall@10 == 0 "
        f"(target absent from top-10): {dropped}"
    )
