"""Dense multilingual recall acceptance on the shipped encoder (make-recall-multilingual).

A personal server encodes recall with `BAAI/bge-m3` on ONNX Runtime int8. This
is the embeddings-job gate for that switch (design §17.6, bars from §17.9),
built through the product path: the fixture trees are rendered, their lexical
catalogues and embedding sidecars built by the real writers, and every query
goes through `find` (hybrid, rerank off).

* English (§17.6.1, §17.9): the golden fixture clears NDCG@10 >= 0.897 and
  recall@10 >= 0.9415, no query drops to recall 0, and no query loses its
  grade-3 page from the top 10. The golden tree padded with the German,
  Russian, Japanese and Estonian pages stays within 0.02 NDCG@10 of it.
* Multilingual hybrid arms (§17.6.2), per language: same-language recall@5
  >= 0.90, cross-language recall@10 >= 0.80, morphology (ru, et) recall@10
  >= 0.80.
* Language-bias twins (three per language): the English gold outranks the
  same-language poison in most of a language's twins in at least three of the
  four languages, and never loses more than one rank to it. In every Russian
  and Japanese twin it outranks it outright: the dense-lead guard's script
  rule decides those. German and Estonian twins share the English page's
  script, so the guard does not act there and the encoder decides.
* The Japanese-only vault: hybrid recall@10 >= 0.95.
* Short-query encode p95 <= 250 ms (§17.6.1).

Also printed, with the host load: query-encode p50/p95 (alone and during a
one-text-at-a-time build), build seconds per 1,000 chunks, and the resident
memory the one encoder instance adds.
"""

from __future__ import annotations

import os
import resource
import shutil
import statistics
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.embeddings
pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")
pytest.importorskip("huggingface_hub")

from epistemic.corpora import (  # noqa: E402
    recall_japanese_vault,
    recall_multilingual,
    recall_multilingual_twins,
)

from exomem import embeddings, lexstore, recall_space  # noqa: E402
from exomem import eval_metrics as metrics  # noqa: E402
from exomem import find as find_module  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_VAULT = _REPO_ROOT / "tests" / "fixtures"
_GOLDEN = _REPO_ROOT / "tests" / "golden" / "queries.yaml"
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import eval_retrieval  # noqa: E402

#: §17.9 English gate.
_GOLDEN_NDCG10 = 0.897
_GOLDEN_RECALL10 = 0.9415
_PADDED_NDCG_DELTA = 0.02
#: §17.6.2 hybrid bars.
_SAME_LANGUAGE_RECALL5 = 0.90
_CROSS_LANGUAGE_RECALL10 = 0.80
_MORPHOLOGY_RECALL10 = 0.80
_JAPANESE_VAULT_RECALL10 = 0.95
_GUARDED_TWIN_LANGUAGES = ("ru", "ja")
_TWIN_LANGUAGES_WON = 3
_QUERY_ENCODE_P95_MS = 250.0


def _load() -> str:
    try:
        return " ".join(f"{value:.2f}" for value in os.getloadavg())
    except OSError:
        return "unknown"


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _reset() -> None:
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    embeddings.clear_embedding_indexes()


def _prepare(vault: Path, *, label: str) -> dict:
    """Catalogue and sidecar for `vault`, through the product writers."""
    from conftest import initialize_vault_state_offline

    initialize_vault_state_offline(vault, source=f"dense multilingual acceptance: {label}")
    _reset()
    os.environ["EXOMEM_VAULT_PATH"] = str(vault)
    lexstore.ensure_fresh(vault)
    started = time.perf_counter()
    rows = embeddings.get_embedding_index(vault).rebuild_all()
    seconds = time.perf_counter() - started
    identity = embeddings.get_embedding_index(vault).identity
    return {"rows": rows, "seconds": seconds, "identity": identity, "load": _load()}


def _ranked(vault: Path, query: str, *, scope: str = "kb-only") -> list[str]:
    os.environ["EXOMEM_VAULT_PATH"] = str(vault)
    hits = find_module.find(
        vault,
        query=query,
        limit=10,
        mode="hybrid",
        scope=scope,
        widen_outside_kb=scope == "kb",
        rerank=False,
    )
    return [eval_retrieval._canon(hit.path) for hit in hits]


def _rank(ranked: list[str], path: str) -> int:
    return ranked.index(path) + 1 if path in ranked else len(ranked) + 1


def _golden(vault: Path) -> dict:
    """The golden set's rankings and the harness's metrics over them."""
    _reset()
    rows = []
    for entry in eval_retrieval._load_golden(_GOLDEN):
        ranked = _ranked(vault, entry["query"], scope=entry.get("scope", "kb-only"))
        best = max(entry["relevance"].values())
        rows.append(
            {
                "query": entry["query"],
                "ranked": ranked,
                "ndcg10": metrics.ndcg_at_k(ranked, entry["relevance"], 10),
                "mrr": metrics.mrr(ranked, entry["relevant"]),
                "recall10": metrics.recall_at_k(ranked, entry["relevant"], 10),
                "best_found": any(
                    path in ranked[:10]
                    for path, grade in entry["relevance"].items()
                    if grade == best
                ),
            }
        )
    return {
        "ndcg10": metrics.mean(row["ndcg10"] for row in rows),
        "mrr": metrics.mean(row["mrr"] for row in rows),
        "recall10": metrics.mean(row["recall10"] for row in rows),
        "rows": rows,
    }


def _rows(vault: Path, queries: list[dict], resolve) -> list[dict]:
    _reset()
    out = []
    for row in queries:
        ranked = _ranked(vault, row["query"])
        gold = {resolve(key) for key in row["gold"]}
        poison = {resolve(key) for key in row.get("poison", ())}
        out.append(
            {
                **row,
                "recall5": metrics.recall_at_k(ranked, gold, 5),
                "recall10": metrics.recall_at_k(ranked, gold, 10),
                "gold_rank": min((_rank(ranked, path) for path in gold), default=11),
                "poison_rank": min((_rank(ranked, path) for path in poison), default=11),
            }
        )
    return out


def _resolve_any(key: str) -> str:
    for module in (recall_multilingual, recall_multilingual_twins):
        try:
            return module.resolve_key(key)
        except KeyError:
            continue
    raise KeyError(key)


def _encode_latency(queries: list[str]) -> tuple[float, float]:
    timings = []
    for query in queries:
        started = time.perf_counter()
        embeddings.embed_texts([query], is_query=True)
        timings.append((time.perf_counter() - started) * 1000.0)
    timings.sort()
    p95 = timings[min(len(timings) - 1, int(round(0.95 * (len(timings) - 1))))]
    return statistics.median(timings), p95


def _encode_latency_during_a_build(queries: list[str], passages: list[str]) -> tuple[float, float]:
    """Query-encode latency while another thread encodes passages one at a time,
    as the re-embed job does."""
    stop = threading.Event()

    def build() -> None:
        index = 0
        while not stop.is_set():
            embeddings.embed_texts([passages[index % len(passages)]], is_query=False)
            index += 1

    thread = threading.Thread(target=build, name="dense-acceptance-build")
    thread.start()
    try:
        return _encode_latency(queries)
    finally:
        stop.set()
        thread.join()


def _fixture_passages(limit: int = 64) -> list[str]:
    passages: list[str] = []
    for page in sorted((_FIXTURE_VAULT / "Knowledge Base").rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        passages.extend(embeddings.chunk_text(page.stem, text.split("\n---\n", 1)[-1]))
        if len(passages) >= limit:
            break
    return passages[:limit]


@pytest.fixture(scope="module")
def dense(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("dense-multilingual")
    try:
        for name in ("EXOMEM_DISABLE_EMBEDDINGS", "KB_MCP_DISABLE_EMBEDDINGS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
        before = _rss_mib()
        load_started = time.perf_counter()
        embeddings.get_model()
        loaded = {"seconds": time.perf_counter() - load_started, "rss_mib": _rss_mib() - before}

        golden_tree = root / "golden"
        shutil.copytree(_FIXTURE_VAULT, golden_tree)
        golden_build = _prepare(golden_tree, label="golden")
        golden = _golden(golden_tree)

        padded_tree = root / "padded"
        shutil.copytree(_FIXTURE_VAULT, padded_tree)
        recall_multilingual.render(padded_tree)
        recall_multilingual_twins.render(padded_tree)
        padded_build = _prepare(padded_tree, label="padded")
        padded = _golden(padded_tree)
        queries = [*recall_multilingual.load_queries(), *recall_multilingual_twins.load_queries()]
        multilingual = _rows(padded_tree, queries, _resolve_any)

        japanese_tree = root / "japanese"
        keys = recall_japanese_vault.build_corpus(japanese_tree)
        japanese_build = _prepare(japanese_tree, label="japanese")
        japanese = _rows(
            japanese_tree,
            [{"query": q.query, "gold": [q.gold]} for q in recall_japanese_vault.QUERIES],
            lambda key: eval_retrieval._canon(keys[key]),
        )

        latency_queries = [entry["query"] for entry in eval_retrieval._load_golden(_GOLDEN)]
        latency_queries += [row["query"] for row in queries]
        latency_load = _load()
        p50, p95 = _encode_latency(latency_queries)
        contended_load = _load()
        contended_p50, contended_p95 = _encode_latency_during_a_build(
            latency_queries, _fixture_passages()
        )
        yield {
            "loaded": loaded,
            "golden": golden,
            "golden_build": golden_build,
            "padded": padded,
            "padded_build": padded_build,
            "multilingual": multilingual,
            "japanese": japanese,
            "japanese_build": japanese_build,
            "latency": {"p50_ms": p50, "p95_ms": p95, "n": len(latency_queries), "load": latency_load},
            "contended": {"p50_ms": contended_p50, "p95_ms": contended_p95, "load": contended_load},
        }
    finally:
        _reset()
        monkeypatch.undo()


def test_recall_encodes_with_the_multilingual_model(dense) -> None:
    identity = dense["golden_build"]["identity"]
    assert embeddings.MODEL_NAME == recall_space.PERSONAL_MODEL
    assert identity.model == recall_space.PERSONAL_MODEL
    assert identity.dim == 1024
    assert identity.fingerprint and identity.fingerprint.startswith("BAAI/bge-m3|cls|l2|")


def test_the_english_golden_set_clears_the_switch_bars(dense) -> None:
    golden = dense["golden"]
    print(
        f"[dense] golden NDCG@10={golden['ndcg10']:.4f} recall@10={golden['recall10']:.4f} "
        f"MRR={golden['mrr']:.4f} (build load {dense['golden_build']['load']})"
    )
    assert golden["ndcg10"] >= _GOLDEN_NDCG10
    assert golden["recall10"] >= _GOLDEN_RECALL10
    assert [row["query"] for row in golden["rows"] if row["recall10"] == 0.0] == []
    assert [row["query"] for row in golden["rows"] if not row["best_found"]] == []


def test_multilingual_padding_keeps_the_golden_set_within_its_bar(dense) -> None:
    delta = dense["padded"]["ndcg10"] - dense["golden"]["ndcg10"]
    print(f"[dense] padded golden NDCG@10={dense['padded']['ndcg10']:.4f} (delta {delta:+.4f})")
    assert abs(delta) <= _PADDED_NDCG_DELTA


def _cells(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    cells: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        cells.setdefault((row["language"], row["kind"]), []).append(row)
    return cells


def test_the_hybrid_arms_clear_their_bars_in_every_language(dense) -> None:
    cells = _cells(dense["multilingual"])
    for (language, kind), rows in sorted(cells.items()):
        print(
            f"[dense] {language} {kind}: n={len(rows)} "
            f"recall@5={metrics.mean(r['recall5'] for r in rows):.2f} "
            f"recall@10={metrics.mean(r['recall10'] for r in rows):.2f} "
            f"gold ranks={[r['gold_rank'] for r in rows]}"
        )
    failing = {}
    for (language, kind), rows in cells.items():
        if kind == "same_language":
            value, bar = metrics.mean(r["recall5"] for r in rows), _SAME_LANGUAGE_RECALL5
        elif kind == "cross_language":
            value, bar = metrics.mean(r["recall10"] for r in rows), _CROSS_LANGUAGE_RECALL10
        elif kind == "morphology":
            value, bar = metrics.mean(r["recall10"] for r in rows), _MORPHOLOGY_RECALL10
        else:
            continue
        if value < bar:
            failing[language, kind] = value
    assert failing == {}


def test_the_english_gold_outranks_its_same_language_twins(dense) -> None:
    twins = [row for row in dense["multilingual"] if row["kind"] == "language_bias"]
    for row in twins:
        print(
            f"[dense] twin {row['language']}: gold {row['gold_rank']} "
            f"poison {row['poison_rank']} | {row['query']}"
        )
    by_language: dict[str, list[dict]] = {}
    for row in twins:
        by_language.setdefault(row["language"], []).append(row)
    won = sorted(
        language
        for language, rows in by_language.items()
        if 2 * sum(row["gold_rank"] < row["poison_rank"] for row in rows) > len(rows)
    )
    print(f"[dense] twins won (most of a language's twins): {won} of {sorted(by_language)}")
    assert len(by_language) == 4 and all(len(rows) >= 3 for rows in by_language.values())
    assert len(won) >= _TWIN_LANGUAGES_WON
    guarded = [row for row in twins if row["language"] in _GUARDED_TWIN_LANGUAGES]
    assert guarded
    assert [
        (row["language"], row["query"], row["gold_rank"], row["poison_rank"])
        for row in guarded
        if row["gold_rank"] > row["poison_rank"]
    ] == []
    # In every language the gold never loses more than one rank to its poison.
    assert [
        (row["language"], row["query"], row["gold_rank"], row["poison_rank"])
        for row in twins
        if row["gold_rank"] > row["poison_rank"] + 1
    ] == []


def test_the_japanese_only_vault_is_found_by_hybrid_recall(dense) -> None:
    rows = dense["japanese"]
    recall10 = metrics.mean(row["recall10"] for row in rows)
    print(f"[dense] japanese vault: n={len(rows)} recall@10={recall10:.2f}")
    assert recall10 >= _JAPANESE_VAULT_RECALL10


def test_a_short_query_encodes_within_its_latency_bar(dense) -> None:
    latency = dense["latency"]
    assert latency["p95_ms"] <= _QUERY_ENCODE_P95_MS, latency


def test_report_cost_with_the_load(dense) -> None:
    latency = dense["latency"]
    builds = {
        name: dense[f"{name}_build"] for name in ("golden", "padded", "japanese")
    }
    print(
        f"[dense] model load {dense['loaded']['seconds']:.1f} s, "
        f"+{dense['loaded']['rss_mib']:.0f} MiB max RSS for the one instance"
    )
    print(
        f"[dense] query encode p50 {latency['p50_ms']:.0f} ms p95 {latency['p95_ms']:.0f} ms "
        f"over {latency['n']} queries (load {latency['load']})"
    )
    contended = dense["contended"]
    print(
        f"[dense] query encode during a one-text-at-a-time build: p50 {contended['p50_ms']:.0f} ms "
        f"p95 {contended['p95_ms']:.0f} ms (load {contended['load']})"
    )
    for name, build in builds.items():
        per_1k = 1000.0 * build["seconds"] / max(1, build["rows"])
        print(
            f"[dense] {name} build: {build['rows']} chunks in {build['seconds']:.1f} s = "
            f"{per_1k:.0f} s per 1k chunks (load {build['load']})"
        )
    assert latency["n"] > 0
