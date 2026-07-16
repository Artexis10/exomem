from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "graph_value_benchmark.py"
spec = importlib.util.spec_from_file_location("graph_value_benchmark", MODULE_PATH)
bench = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)


@pytest.fixture(scope="module")
def perfect_fixture(tmp_path_factory: pytest.TempPathFactory):
    manifest = bench.load_manifest()
    corpus = bench.render_exomem(manifest, tmp_path_factory.mktemp("graph-value") / "exomem")
    before = bench.corpus_hash(corpus.root)
    run = bench.run_exomem_fixture(manifest, corpus, revision="fixture-revision")
    return manifest, corpus, before, run


def _case(
    task: dict,
    *,
    reached: list[str] | None = None,
    edges: list | None = None,
) -> object:
    return bench.CaseResult(
        case_id=str(task["id"]),
        dimension=str(task["dimension"]),
        reached_nodes=reached or [],
        edges=edges or [],
        blocks=[],
        statuses={},
        response_bytes=0,
        latency_ms=0.0,
    )


def test_native_renderers_are_deterministic_and_preserve_neutral_relations(
    tmp_path: Path,
) -> None:
    manifest = bench.load_manifest()
    exomem_a = bench.render_exomem(manifest, tmp_path / "exomem-a")
    exomem_b = bench.render_exomem(manifest, tmp_path / "exomem-b")
    basic_a = bench.render_basic_memory(manifest, tmp_path / "basic-a")
    basic_b = bench.render_basic_memory(manifest, tmp_path / "basic-b")

    assert exomem_a.corpus_hash == exomem_b.corpus_hash
    assert basic_a.corpus_hash == basic_b.corpus_hash
    assert set(exomem_a.id_to_path) == set(basic_a.id_to_path)
    assert exomem_a.title_to_id == basic_a.title_to_id

    notes = {str(item["id"]): item for item in manifest["notes"]}
    for edge in manifest["relations"]:
        source = str(edge["from"])
        target = str(edge["to"])
        exomem_text = (exomem_a.root / exomem_a.id_to_path[source]).read_text()
        basic_text = (basic_a.root / basic_a.id_to_path[source]).read_text()
        canonical_target = exomem_a.id_to_path[target].removesuffix(".md")
        assert f"- {edge['type']} [[{canonical_target}]]" in exomem_text
        assert f"[[{exomem_a.id_to_path[target]}]]" not in exomem_text
        assert f"- {edge['type']} [[{notes[target]['title']}]]" in basic_text

    assert "origin" in exomem_a.parity["provenance"]
    assert "no origin" in basic_a.parity["provenance"]
    assert "semantic blocks" in exomem_a.parity["blocks"]
    assert "no relation-bearing block anchor" in basic_a.parity["blocks"]


def test_exomem_fixture_passes_every_dimension_without_mutating_markdown(
    perfect_fixture,
) -> None:
    manifest, corpus, before, run = perfect_fixture
    scores = bench.score_run(manifest, run)

    assert set(scores) == set(bench.ALL_DIMENSIONS)
    assert all(metric.supported for metric in scores.values())
    assert all(metric.ratio == 1.0 for metric in scores.values())
    assert run.mutation_safe is True
    assert before == bench.corpus_hash(corpus.root)
    assert run.renderer_parity == corpus.parity
    assert {case for metric in scores.values() for case in metric.case_ids} == {
        str(task["id"]) for task in manifest["tasks"]
    }


def test_graph_mistakes_remain_independent(perfect_fixture) -> None:
    manifest, _, _, _ = perfect_fixture
    tasks = {str(task["id"]): task for task in manifest["tasks"]}

    reachability = bench.score_case(
        tasks["common-one-hop"],
        _case(tasks["common-one-hop"], reached=["common-target"]),
    )
    wrong_type = bench.score_case(
        tasks["relation-type-fidelity"],
        _case(
            tasks["relation-type-fidelity"],
            edges=[bench.EdgeFact("filter-start", "filter-target", "relates_to")],
        ),
    )
    distracted = bench.score_case(
        tasks["typed-distractor-filter"],
        _case(
            tasks["typed-distractor-filter"],
            reached=["filter-target", "filter-distractor"],
        ),
    )
    wrong_direction = bench.score_case(
        tasks["outgoing-direction"],
        _case(
            tasks["outgoing-direction"],
            reached=["direction-target", "direction-source"],
            edges=[bench.EdgeFact("direction-center", "direction-target", "depends_on")],
        ),
    )

    assert reachability.ratio == 1.0
    assert wrong_type.ratio == 0.0
    assert distracted.ratio == 0.5
    assert distracted.unexpected == ["filter-distractor"]
    assert wrong_direction.ratio == 0.5
    assert wrong_direction.unexpected == ["direction-source"]


def test_basic_memory_normalization_preserves_public_typed_edges_and_marks_gaps(
    tmp_path: Path,
) -> None:
    manifest = bench.load_manifest()
    corpus = bench.render_basic_memory(manifest, tmp_path / "basic")
    tasks = {str(task["id"]): task for task in manifest["tasks"]}
    payload = {
        "results": [
            {
                "primary_result": {"title": "Filter Start"},
                "related_results": [
                    {"type": "entity", "title": "Filter Target"},
                    {
                        "type": "relation",
                        "from_entity": "Filter Start",
                        "to_entity": "Filter Target",
                        "relation_type": "depends_on",
                    },
                ],
            }
        ]
    }

    normalized = bench.normalize_basic_memory_context(
        tasks["relation-type-fidelity"], payload, corpus, elapsed_ms=1.0
    )
    governed = bench.normalize_basic_memory_context(
        tasks["provenance-trace"], payload, corpus, elapsed_ms=1.0
    )

    assert normalized.reached_nodes == ["filter-target"]
    assert normalized.edges == [bench.EdgeFact("filter-start", "filter-target", "depends_on")]
    assert governed.unsupported["provenance_traceability"].startswith(
        "build_context relations omit"
    )
    unsupported = bench.score_case(tasks["provenance-trace"], governed)
    assert unsupported.supported is False
    assert unsupported.ratio == 0.0
    assert unsupported.denominator == 1


def test_dominance_flips_on_common_regression_and_names_the_case(perfect_fixture) -> None:
    manifest, _, _, run = perfect_fixture
    exomem_scores = bench.score_run(manifest, run)
    basic_scores = {
        dimension: bench.MetricResult(
            dimension,
            0 if dimension in bench.GOVERNED_DIMENSIONS else 1,
            1,
            0.0 if dimension in bench.GOVERNED_DIMENSIONS else 1.0,
            dimension not in bench.GOVERNED_DIMENSIONS,
            case_ids=list(metric.case_ids),
        )
        for dimension, metric in exomem_scores.items()
    }

    assert bench.dominance_report(exomem_scores, basic_scores)["dominant"] is True

    degraded = dict(exomem_scores)
    degraded["one_hop_reachability"] = bench.MetricResult(
        "one_hop_reachability",
        0,
        1,
        0.0,
        True,
        missing=["common-target"],
        case_ids=["common-one-hop"],
        failed_case_ids=["common-one-hop"],
    )
    report = bench.dominance_report(degraded, basic_scores)
    failed = next(
        item
        for item in report["checks"]
        if item["criterion"] == "no-regression:one_hop_reachability"
    )

    assert report["dominant"] is False
    assert "no-regression:one_hop_reachability" in report["failed_criteria"]
    assert failed["cases"] == ["common-one-hop"]


def test_reports_are_aggregate_reproducible_and_privacy_safe(perfect_fixture) -> None:
    manifest, corpus, _, run = perfect_fixture
    basic_corpus = bench.render_basic_memory(manifest, corpus.root.parent / "basic-report")
    basic = bench.unavailable_basic_memory(
        "direct comparison not requested; pass --direct with --basic-memory-root or "
        "--basic-memory-executable",
        basic_corpus,
    )
    report = bench.build_report(manifest, run, basic)
    markdown = bench.render_markdown_report(report)
    encoded = json.dumps(report, sort_keys=True)

    assert report["manifest_version"] == manifest["manifest_version"]
    assert report["fairness"]["weighted_aggregate"] is False
    assert "overall_score" not in encoded
    assert run.corpus_hash in encoded
    assert "renderer_parity" in encoded
    assert report["contenders"]["exomem"]["fact_parity"] == corpus.fact_parity
    assert report["contenders"]["basic_memory"]["fact_parity"] == basic_corpus.fact_parity
    assert str(corpus.root) not in encoded
    assert "/home/" not in encoded
    assert "C:\\" not in encoded
    assert "PRIVATE_API_KEY" not in encoded
    assert "The common graph path begins here." not in encoded
    assert "Manifest version: `2`" in markdown
    assert "## Fairness" in markdown
    assert "Corpus hash" in markdown
    assert "## Informational efficiency" in markdown
    assert "No weighted aggregate is used" in markdown
    assert "DIRECT CONTENDER NOT RUN" in markdown
    assert "pass --direct with --basic-memory-root or --basic-memory-executable" in markdown


def test_basic_memory_adapter_configuration_is_isolated_and_mutation_disabled(
    tmp_path: Path,
) -> None:
    corpus = bench.render_basic_memory(bench.load_manifest(), tmp_path / "basic")
    config = bench._basic_memory_config(corpus)

    assert config["projects"] == {"graph-benchmark": {"path": str(corpus.root), "mode": "local"}}
    assert config["semantic_search_enabled"] is False
    assert config["sync_changes"] is False
    assert config["disable_permalinks"] is True
    assert config["ensure_frontmatter_on_sync"] is False
    assert config["auto_update"] is False


def test_unpinned_basic_memory_executable_is_never_claim_eligible() -> None:
    manifest = bench.load_manifest()

    assert bench._basic_memory_pin_valid(None, manifest["contenders"]["basic_memory"]) is False


@pytest.mark.parametrize(
    ("profile", "search_only", "expected_timeout"),
    (("lean", True, 60.0), ("full", False, 600.0)),
)
def test_index_command_keeps_frozen_launcher_prefix_and_builds_profile_indexes(
    monkeypatch,
    tmp_path: Path,
    profile: str,
    search_only: bool,
    expected_timeout: float,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    bench._index_basic_memory_corpus(
        command="uv",
        launcher_args=["run", "--frozen", "--project", "checkout", "basic-memory"],
        env={"HOME": str(tmp_path)},
        cwd=tmp_path,
        project="graph-benchmark",
        timeout=1.0,
        profile=profile,
    )

    expected = [
        "uv",
        "run",
        "--frozen",
        "--project",
        "checkout",
        "basic-memory",
        "reindex",
        "--full",
    ]
    if search_only:
        expected.append("--search")
    expected.extend(["--project", "graph-benchmark"])
    assert observed["command"] == expected
    assert observed["kwargs"]["timeout"] == expected_timeout


def test_exomem_renderer_is_stable_under_public_fix_maintenance(
    tmp_path: Path,
) -> None:
    from exomem.commands import op_maintain_memory

    corpus = bench.render_exomem(bench.load_manifest(), tmp_path / "exomem")
    before = bench.corpus_hash(corpus.root)

    report = op_maintain_memory(
        corpus.root,
        mode="fix",
        dry_run=False,
        rebuild_embeddings=False,
    )

    assert report["files_rewritten"] == 0
    assert bench.corpus_hash(corpus.root) == before


def test_full_exomem_index_uses_public_embedding_rebuild() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def call(self, probe_id: str, name: str, arguments: dict):
            self.calls.append((probe_id, name, arguments))
            return {"summary": {"embeddings_chunks": 17}, "files_rewritten": 0}

    full = Recorder()
    lean = Recorder()

    full_result = asyncio.run(bench.prepare_exomem_indexes(full, profile="full"))
    lean_result = asyncio.run(bench.prepare_exomem_indexes(lean, profile="lean"))

    assert full.calls == [
        (
            "full-index-setup",
            "maintain_memory",
            {"mode": "fix", "dry_run": False, "rebuild_embeddings": True},
        )
    ]
    assert full_result["summary"]["embeddings_chunks"] == 17
    assert lean.calls == []
    assert lean_result is None


def test_full_retrieval_lane_probe_requests_bm25_through_hybrid() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def call(self, probe_id: str, name: str, arguments: dict):
            self.calls.append((probe_id, name, arguments))
            query = arguments["query"]
            profile: dict = {"effective_mode": arguments["mode"]}
            ranking: dict = {"final_rank": 1}
            if query == "quasarneedle-7f3a":
                lanes = (
                    {
                        "bm25": {"raw_score": 4.2, "rank": 1},
                        "keyword": {"rank": 1},
                    }
                    if arguments["mode"] == "hybrid"
                    else {"keyword": {"rank": 1}}
                )
                path = "Knowledge Base/Notes/Insights/rare-token-note.md"
                profile["lanes"] = {
                    "bm25": {
                        "backend": "fts5:fixture",
                        "metric": {
                            "name": "raw_bm25_score",
                            "direction": "higher",
                            "range": "backend_dependent",
                        },
                        "status": "participated",
                    },
                    "keyword": {
                        "metric": {"name": "rank", "direction": "lower"},
                        "status": "participated",
                    },
                }
                if arguments["mode"] == "keyword":
                    profile["lanes"]["bm25"] = {
                        "status": "non_applicable",
                        "reason": "requested_mode_keyword",
                    }
            elif query.startswith("recent "):
                lanes = {
                    "keyword": {"rank": 1},
                    "temporal": {"rank": 1},
                }
                path = "Knowledge Base/Notes/Insights/rare-token-note.md"
                profile["lanes"] = {
                    "keyword": {"status": "participated"},
                    "temporal": {"status": "participated"},
                }
            elif query.startswith("retries should use"):
                if arguments["graph"]:
                    lanes = {
                        "graph": {
                            "rank": 1,
                            "provenance": {
                                "seed": "Knowledge Base/Notes/Insights/recall-vis-seed-1.md",
                                "relation_type": "supports",
                                "direction": "outbound",
                                "hop": 1,
                            },
                        },
                    }
                    path = "Knowledge Base/Notes/Insights/recall-vis-target-1.md"
                    profile["lanes"] = {"graph": {"status": "participated"}}
                else:
                    lanes = {"bm25": {"raw_score": 2.0, "rank": 1}}
                    path = "Knowledge Base/Notes/Insights/recall-vis-seed-1.md"
                    profile["lanes"] = {"bm25": {"status": "participated"}}
            else:
                lanes = {"vector": {"cosine": 0.8, "rank": 1}}
                path = "Knowledge Base/Notes/Insights/semantic-target.md"
                profile["lanes"] = {
                    "vector": {
                        "model": "BAAI/bge-base-en-v1.5",
                        "metric": {
                            "name": "cosine_similarity",
                            "direction": "higher",
                            "range": [-1.0, 1.0],
                        },
                        "status": "participated",
                    }
                }
            if arguments.get("rerank") is True:
                profile["rerank"] = {
                    "ran": True,
                    "model": "BAAI/bge-reranker-base",
                    "metric": {
                        "name": "cross_encoder_score",
                        "direction": "higher",
                    },
                }
                ranking["reranker"] = {
                    "raw_score": 0.9,
                    "adjusted_score": 0.9,
                    "multipliers": [
                        {
                            "name": "status",
                            "factor": 1.0,
                            "before": 0.9,
                            "after": 0.9,
                        }
                    ],
                }
            fused = arguments["mode"] == "hybrid" and len(lanes) >= 1
            if fused:
                weights = {}
                for lane_name, lane in lanes.items():
                    lane.setdefault("rrf_contribution", 0.01)
                    weights[lane_name] = 0.61
                profile["fusion"] = {"k": 60, "weights": weights}
            ranking["lanes"] = lanes
            if fused:
                ranking["fusion"] = {
                    "rrf_sum": sum(lane["rrf_contribution"] for lane in lanes.values())
                }
                ranking["multipliers"] = [
                    {
                        "name": "status",
                        "factor": 1.0,
                        "before": ranking["fusion"]["rrf_sum"],
                        "after": ranking["fusion"]["rrf_sum"],
                    }
                ]
                ranking["final_sort_tuple"] = [ranking["fusion"]["rrf_sum"], path]
            else:
                ranking["multipliers"] = []
                ranking["final_sort_tuple"] = [1.0, path]
            return {
                "hits": [
                    {
                        "path": path,
                        "ranking_explanation": ranking,
                    }
                ],
                "retrieval_profile": profile,
            }

        def probe_result(self, *, probe, checks, evidence=None, outcome=None, **kwargs):
            return bench.ProbeResult(
                probe_id=probe["id"],
                gate=probe["gate"],
                contender="exomem",
                surface="mcp",
                outcome=outcome or ("pass" if all(checks.values()) else "fail"),
                required=True,
                checks=checks,
                evidence=evidence or {},
            )

    probes = [
        {
            "id": "retrieval-lanes",
            "gate": "explanation_truth",
            "surface": "mcp",
            "required_profiles": ["full"],
        },
        *[
            {
                "id": probe_id,
                "gate": "exomem_extensions",
                "surface": "mcp",
                "required_profiles": ["full"],
            }
            for probe_id in ("media-pdf", "media-image", "media-audio", "media-video")
        ],
    ]
    manifest = {
        "profiles": {"full": {}},
        "probes": probes,
        "tolerances": {"score_absolute": 1e-9},
    }
    corpus = SimpleNamespace(
        id_to_path={
            "rare-token-note": "Knowledge Base/Notes/Insights/rare-token-note.md",
            "semantic-target": "Knowledge Base/Notes/Insights/semantic-target.md",
            "recall-vis-seed-1": "Knowledge Base/Notes/Insights/recall-vis-seed-1.md",
            "recall-vis-target-1": "Knowledge Base/Notes/Insights/recall-vis-target-1.md",
        }
    )
    recorder = Recorder()

    results = asyncio.run(
        bench.run_exomem_direct_probes(
            manifest,
            corpus,
            recorder,
            profile="full",
            graph_cases={},
            media_modules={},
        )
    )

    lexical_requests = [
        arguments
        for probe_id, name, arguments in recorder.calls
        if probe_id == "retrieval-lanes"
        and name == "ask_memory"
        and arguments["query"] == "quasarneedle-7f3a"
    ]
    assert {request["mode"] for request in lexical_requests} == {"keyword", "hybrid"}
    requests = [
        arguments
        for probe_id, name, arguments in recorder.calls
        if probe_id == "retrieval-lanes" and name == "ask_memory"
    ]
    assert any(arguments["query"].startswith("recent ") for arguments in requests)
    assert any(arguments["query"].startswith("retries should use") for arguments in requests)
    assert any(arguments.get("rerank") is True for arguments in requests)
    assert results["retrieval-lanes"].passed is True, {
        key: value for key, value in results["retrieval-lanes"].checks.items() if not value
    }


def test_full_media_probes_execute_public_process_search_and_read_paths() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []
            self.expected_by_filename = {
                "benchmark-pdf.pdf": "benchmark pdf semantic evidence",
                "benchmark-image.png": "quasar image",
                "benchmark-audio.ogg": "copper lantern audio benchmark",
                "benchmark-video.mp4": "ember video",
            }

        async def call(self, probe_id: str, name: str, arguments: dict):
            self.calls.append((probe_id, name, arguments))
            path = str(arguments.get("path") or "")
            if name == "process_media" and arguments.get("operation") == "process":
                kind = probe_id.removeprefix("media-")
                return {
                    "path": path,
                    "media_type": kind,
                    "state": "completed",
                    "sidecar_path": f"{path}.md",
                }
            if name == "process_media":
                return {"counts": {"completed": 4}}
            if name == "read_memory":
                expected = next(
                    text for filename, text in self.expected_by_filename.items() if filename in path
                )
                return {
                    "path": path,
                    "content": f"processing_state: completed\n\n{expected}",
                }
            if name == "ask_memory":
                expected = arguments["query"]
                filename = next(
                    filename
                    for filename, text in self.expected_by_filename.items()
                    if text == expected
                )
                payload = {
                    "hits": [{"path": (f"Knowledge Base/Sources/Media/benchmark/{filename}.md")}]
                }
                if probe_id == "media-image":
                    payload["retrieval_profile"] = {
                        "lanes": {
                            "clip": {
                                "status": "participated",
                                "metric": {
                                    "name": "cosine_similarity",
                                    "direction": "higher",
                                    "range": [-1.0, 1.0],
                                },
                            }
                        }
                    }
                    payload["hits"][0]["ranking_explanation"] = {
                        "lanes": {"clip": {"rank": 1, "cosine": 0.8}}
                    }
                return payload
            if name == "read_media":
                return {"path": path, "frame_count": 1, "frames": [{"index": 0}]}
            raise AssertionError((probe_id, name, arguments))

        def probe_result(self, *, probe, checks, evidence=None, outcome=None, **kwargs):
            return bench.ProbeResult(
                probe_id=probe["id"],
                gate=probe["gate"],
                contender="exomem",
                surface="mcp",
                outcome=outcome or ("pass" if all(checks.values()) else "fail"),
                required=True,
                checks=checks,
                evidence=evidence or {},
            )

    expected_text = {
        "pdf": "benchmark pdf semantic evidence",
        # Only require the high-contrast tokens fully visible inside the frame.
        # The trailing decorative word reaches the crop boundary in these tiny
        # deterministic fixtures and is not a reliable OCR contract.
        "image": "quasar image",
        "audio": "copper lantern audio benchmark",
        "video": "ember video",
    }
    media = [
        {
            "id": f"{kind}-sample",
            "kind": kind,
            "filename": f"benchmark-{kind}.{ext}",
            "expected_text": expected_text[kind],
        }
        for kind, ext in (
            ("pdf", "pdf"),
            ("image", "png"),
            ("audio", "ogg"),
            ("video", "mp4"),
        )
    ]
    manifest = {
        "profiles": {"full": {}},
        "probes": [
            {
                "id": f"media-{kind}",
                "gate": "exomem_extensions",
                "surface": "mcp",
                "fixture_ids": [f"{kind}-sample"],
                "required_profiles": ["full"],
            }
            for kind in ("pdf", "image", "audio", "video")
        ],
        "media": media,
        "tolerances": {"score_absolute": 1e-9},
    }
    modules = {
        name: {"available": True, "version": "fixture"}
        for name in ("fitz", "PIL", "pytesseract", "faster_whisper", "av")
    }
    recorder = Recorder()

    payloads = {kind: bench._media_payload(kind) for kind in expected_text}
    assert payloads["pdf"].startswith(b"%PDF-") and len(payloads["pdf"]) > 500
    assert payloads["image"].startswith(b"\x89PNG\r\n\x1a\n") and len(payloads["image"]) > 5_000
    assert payloads["audio"].startswith(b"OggS") and len(payloads["audio"]) > 5_000
    assert b"ftyp" in payloads["video"][:64] and len(payloads["video"]) > 20_000

    results = asyncio.run(
        bench.run_exomem_direct_probes(
            manifest,
            SimpleNamespace(id_to_path={}),
            recorder,
            profile="full",
            graph_cases={},
            media_modules=modules,
        )
    )

    assert set(results) == {"media-pdf", "media-image", "media-audio", "media-video"}
    assert all(result.passed for result in results.values())
    assert [name for _, name, _ in recorder.calls].count("process_media") == 8
    assert [name for _, name, _ in recorder.calls].count("ask_memory") == 5
    assert [name for _, name, _ in recorder.calls].count("read_memory") == 4
    assert [name for _, name, _ in recorder.calls].count("transfer_artifact") == 0
    assert [name for _, name, _ in recorder.calls].count("read_media") == 1


def test_python_module_inventory_uses_the_selected_interpreter(tmp_path: Path) -> None:
    inventory = bench.python_module_inventory(
        Path(sys.executable),
        ("json", "definitely_missing_benchmark_module"),
        cwd=tmp_path,
        env=bench._child_env(tmp_path),
    )

    assert inventory["json"]["available"] is True
    assert inventory["definitely_missing_benchmark_module"] == {
        "available": False,
        "version": None,
    }


def test_model_cache_fingerprint_records_revisions_and_artifact_hashes(
    tmp_path: Path,
) -> None:
    model = tmp_path / "models--vendor--fixture-model"
    (model / "refs").mkdir(parents=True)
    (model / "blobs").mkdir()
    (model / "refs" / "main").write_text("abc123\n")
    artifact = model / "blobs" / "weights"
    artifact.write_bytes(b"deterministic model bytes")

    fingerprint = bench.model_cache_fingerprint(
        tmp_path,
        backend="fixture-backend",
        device="cpu",
        dtype="float32",
        quantization="none",
    )
    encoded = json.dumps(fingerprint, sort_keys=True)

    assert fingerprint["backend"] == "fixture-backend"
    assert fingerprint["device"] == "cpu"
    assert fingerprint["models"] == [
        {
            "artifacts": [
                {
                    "name": "weights",
                    "sha256": bench.sha256_file(artifact),
                    "size_bytes": len(b"deterministic model bytes"),
                }
            ],
            "model": "vendor/fixture-model",
            "revision": "abc123",
        }
    ]
    assert str(tmp_path) not in encoded


def test_child_environment_does_not_forward_unrelated_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PRIVATE_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-forwarded")

    child = bench._child_env(tmp_path)

    assert child["HOME"] == str(tmp_path)
    assert "PRIVATE_API_KEY" not in child
    assert "UNRELATED_SECRET" not in child


def test_optional_rustup_home_timeout_does_not_abort_environment_setup(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bench.shutil, "which", lambda name: "/rustup" if name == "rustup" else None)

    def timeout(*args, **kwargs):
        raise bench.subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 10))

    monkeypatch.setattr(bench.subprocess, "run", timeout)

    assert bench._optional_rustup_home({"PATH": "/bin"}) is None


def test_direct_mode_requires_an_explicit_basic_memory_target(tmp_path: Path) -> None:
    args = bench.parse_args(["--direct"])

    with pytest.raises(ValueError, match="--basic-memory-root"):
        bench._execute(args, bench.load_manifest(), tmp_path)


def test_profile_defaults_to_lean_and_accepts_full() -> None:
    assert bench.parse_args([]).profile == "lean"
    assert bench.parse_args(["--profile", "full"]).profile == "full"


def test_recall_visibility_registered_and_excluded_from_dominance_comparison() -> None:
    """recall_visibility is a must-pass Exomem-only invariant: present in
    ALL_DIMENSIONS (so load_manifest accepts its tasks and score_run/
    dominance_report's fixture_failures check covers it) but absent from
    COMMON_DIMENSIONS/GOVERNED_DIMENSIONS (so it never enters the
    dominance_report Basic-Memory comparison `checks` loop — it does not
    touch the Basic Memory comparison path)."""
    assert "recall_visibility" in bench.ALL_DIMENSIONS
    assert "recall_visibility" not in bench.COMMON_DIMENSIONS
    assert "recall_visibility" not in bench.GOVERNED_DIMENSIONS


def test_recall_visibility_clears_perfect_fixture(perfect_fixture) -> None:
    """The two recall-visibility tasks (find(), lexical+graph lanes, no
    embeddings) surface their typed neighbour WITH a graph-provenance
    annotation matching the authored relation type."""
    manifest, _, _, run = perfect_fixture
    scores = bench.score_run(manifest, run)

    assert "recall_visibility" in scores
    metric = scores["recall_visibility"]
    assert metric.supported
    assert metric.ratio == 1.0, f"recall_visibility did not clear: {metric.as_dict()}"
    assert not metric.missing
    assert set(metric.case_ids) == {"recall-visibility-1", "recall-visibility-2"}


def test_recall_visibility_failure_is_independent_and_skips_dominance_checks(
    perfect_fixture,
) -> None:
    """A broken/missing graph annotation fails ONLY the fixture gate (must-pass
    Exomem invariant) — it must never surface as a dominance_report `checks`
    criterion, since that loop is scoped to COMMON_DIMENSIONS/
    GOVERNED_DIMENSIONS only (the Basic Memory comparison path)."""
    manifest, _, _, run = perfect_fixture
    tasks = {str(task["id"]): task for task in manifest["tasks"]}

    broken = bench.score_case(
        tasks["recall-visibility-1"],
        _case(tasks["recall-visibility-1"], edges=[]),
    )
    assert broken.ratio == 0.0

    exomem_scores = dict(bench.score_run(manifest, run))
    exomem_scores["recall_visibility"] = broken

    # No Basic Memory comparison available (fixture-only mode): the failing
    # dimension is a bare name in failed_criteria, not "fixture:"-prefixed —
    # that prefix only applies once a real basic_scores comparison exists.
    fixture_only_report = bench.dominance_report(exomem_scores, None)
    assert fixture_only_report["fixture_passed"] is False
    assert "recall_visibility" in fixture_only_report["failed_criteria"]

    # With a (stand-in) Basic Memory comparison present, the SAME failure is
    # reported as "fixture:recall_visibility" and never as a `checks` entry —
    # that loop is scoped to COMMON_DIMENSIONS/GOVERNED_DIMENSIONS only.
    basic_scores = dict(exomem_scores)
    report = bench.dominance_report(exomem_scores, basic_scores)
    assert report["fixture_passed"] is False
    assert "fixture:recall_visibility" in report["failed_criteria"]
    assert not any("recall_visibility" in item["criterion"] for item in report["checks"])


def test_recall_visibility_unsupported_for_basic_memory(tmp_path: Path) -> None:
    """find()/hit-envelope graph-provenance has no build_context equivalent —
    Basic Memory's score for this dimension must always be unsupported, never
    a real comparison (the dominance `checks` loop never looks it up anyway,
    since it iterates COMMON_DIMENSIONS/GOVERNED_DIMENSIONS only)."""
    manifest = bench.load_manifest()
    corpus = bench.render_basic_memory(manifest, tmp_path / "basic")
    tasks = {str(task["id"]): task for task in manifest["tasks"]}

    normalized = bench.normalize_basic_memory_context(
        tasks["recall-visibility-1"], {"results": []}, corpus, elapsed_ms=1.0
    )
    assert normalized.unsupported.get("recall_visibility")

    scored = bench.score_case(tasks["recall-visibility-1"], normalized)
    assert scored.supported is False
    assert scored.ratio == 0.0


def test_local_core_manifest_is_versioned_and_declares_full_neutral_corpus() -> None:
    manifest = bench.load_manifest()

    assert manifest["manifest_version"] == 2
    assert manifest["inventory_version"] == 1
    assert set(manifest["scope"]["excluded"]) == {
        "accounts",
        "billing",
        "cloud_sync",
        "deployment",
        "graphical_interfaces",
        "hosting",
        "teams",
    }
    assert manifest["performance"] == {
        "counterbalanced": True,
        "index_duration_ratio_max": 2.0,
        "order_tolerance": 0,
        "query_median_ratio_max": 2.0,
        "query_p95_ratio_max": 2.5,
        "repetitions": 5,
        "response_bytes_ratio_max": 2.0,
        "seeds": [1729],
        "timeout_seconds": 30.0,
        "warmups": 1,
    }
    assert manifest["observations"]
    assert manifest["schemas"]
    assert manifest["mutations"]
    assert manifest["datasets"]
    assert {item["kind"] for item in manifest["media"]} == {
        "audio",
        "image",
        "pdf",
        "video",
    }
    assert {item["gate"] for item in manifest["probes"]} == set(bench.GATE_NAMES)


def test_inventory_reconciliation_names_unclassified_runtime_operations() -> None:
    manifest = bench.load_manifest()
    declared = set(manifest["operation_inventory"]["exomem"]["mcp"])

    result = bench.reconcile_operation_inventory(
        manifest,
        contender="exomem",
        surface="mcp",
        discovered=sorted({*declared, "new_public_tool"}),
    )

    assert result["valid"] is False
    assert result["unclassified"] == ["new_public_tool"]
    assert result["missing"] == []


def test_inventory_requires_probe_execution_or_explicit_boundary_reason() -> None:
    manifest = bench.load_manifest()
    graph_probe = next(item for item in manifest["probes"] if item["id"] == "graph-context")

    missing = bench.validate_probe_coverage(manifest, executed_probe_ids=set())
    covered = bench.validate_probe_coverage(
        manifest,
        executed_probe_ids={str(item["id"]) for item in manifest["probes"]},
    )

    assert "graph-context" in missing["missing_required_probes"]
    assert graph_probe["fixture_ids"]
    assert missing["valid"] is False
    assert covered["valid"] is True


def test_inventory_requires_each_probe_operation_on_its_declared_probe() -> None:
    manifest = bench.load_manifest()
    declared = manifest["operation_inventory"]["exomem"]["mcp"]
    observed = {
        operation: {str(classification["probe"])}
        for operation, classification in declared.items()
        if classification["classification"] == "probe"
    }

    observed.pop("browse_memory")
    observed["read_memory"] = {"authoring-read-update"}
    missing = bench.validate_operation_execution(
        manifest,
        contender="exomem",
        surface="mcp",
        observed_operation_probes=observed,
    )

    assert missing["valid"] is False
    assert missing["missing_operations"] == ["browse_memory"]
    assert missing["wrong_probe_operations"] == {
        "read_memory": {
            "expected_probe": "exact-lookup",
            "observed_probes": ["authoring-read-update"],
        }
    }

    observed["browse_memory"] = {"retrieval-matrix"}
    observed["read_memory"] = {"exact-lookup", "authoring-read-update"}
    covered = bench.validate_operation_execution(
        manifest,
        contender="exomem",
        surface="mcp",
        observed_operation_probes=observed,
    )

    assert covered["valid"] is True
    assert covered["executed_operation_count"] == len(observed)


def test_native_renderers_preserve_extended_neutral_facts(tmp_path: Path) -> None:
    manifest = bench.load_manifest()
    exomem = bench.render_exomem(manifest, tmp_path / "extended-exomem")
    basic = bench.render_basic_memory(manifest, tmp_path / "extended-basic")

    assert set(exomem.artifact_paths) == set(basic.artifact_paths)
    assert set(exomem.fact_parity) == set(basic.fact_parity)
    assert all(
        value["status"] in {"native", "closest_native", "unsupported"}
        for value in exomem.fact_parity.values()
    )
    assert all(
        value["status"] in {"native", "closest_native", "unsupported"}
        for value in basic.fact_parity.values()
    )
    observation_note = next(
        item for item in manifest["observations"] if item["id"] == "decision-unit"
    )
    exomem_text = (exomem.root / exomem.id_to_path[str(observation_note["note"])]).read_text()
    basic_text = (basic.root / basic.id_to_path[str(observation_note["note"])]).read_text()
    assert "[decision]" in exomem_text
    assert "[decision]" in basic_text
    assert exomem.fixture_hash == bench.fixture_hash(exomem.root)
    assert basic.fixture_hash == bench.fixture_hash(basic.root)


def _probe_result(
    probe_id: str,
    gate: str,
    contender: str,
    outcome: str = "pass",
) -> object:
    return bench.ProbeResult(
        probe_id=probe_id,
        gate=gate,
        contender=contender,
        surface="mcp",
        outcome=outcome,
        required=True,
        checks={"expected": outcome == "pass"},
    )


def _perfect_probe_results(manifest: dict) -> tuple[dict[str, object], dict[str, object]]:
    exomem: dict[str, object] = {}
    basic: dict[str, object] = {}
    for probe in manifest["probes"]:
        probe_id = str(probe["id"])
        gate = str(probe["gate"])
        exomem[probe_id] = _probe_result(probe_id, gate, "exomem")
        basic_outcome = "unsupported" if probe.get("basic_memory") == "unsupported" else "pass"
        basic[probe_id] = _probe_result(probe_id, gate, "basic-memory", basic_outcome)
    return exomem, basic


def test_five_gates_are_independent_and_have_no_weighted_aggregate() -> None:
    manifest = bench.load_manifest()
    exomem, basic = _perfect_probe_results(manifest)

    evaluation = bench.evaluate_local_core_gates(
        manifest,
        exomem_results=exomem,
        basic_results=basic,
        preflight_valid=True,
        profile="full",
    )

    assert set(evaluation["gates"]) == set(bench.GATE_NAMES)
    assert all(item["passed"] for item in evaluation["gates"].values())
    assert evaluation["local_core_advantage"] is True
    assert "weighted_aggregate" not in json.dumps(evaluation)


def test_case_level_shared_regression_and_mutual_failure_block_claim() -> None:
    manifest = bench.load_manifest()
    exomem, basic = _perfect_probe_results(manifest)
    exomem["retrieval-matrix"] = _probe_result("retrieval-matrix", "shared_core", "exomem", "fail")

    regression = bench.evaluate_local_core_gates(
        manifest,
        exomem_results=exomem,
        basic_results=basic,
        preflight_valid=True,
        profile="full",
    )
    basic["retrieval-matrix"] = _probe_result(
        "retrieval-matrix", "shared_core", "basic-memory", "fail"
    )
    mutual = bench.evaluate_local_core_gates(
        manifest,
        exomem_results=exomem,
        basic_results=basic,
        preflight_valid=True,
        profile="full",
    )

    assert regression["gates"]["shared_core"]["passed"] is False
    assert "retrieval-matrix" in regression["paired_regressions"]
    assert regression["local_core_advantage"] is False
    assert mutual["gates"]["shared_core"]["passed"] is False
    assert mutual["local_core_advantage"] is False


def test_invalid_preflight_and_lean_profile_emit_no_full_claim() -> None:
    manifest = bench.load_manifest()
    exomem, basic = _perfect_probe_results(manifest)

    invalid = bench.evaluate_local_core_gates(
        manifest,
        exomem_results=exomem,
        basic_results=basic,
        preflight_valid=False,
        profile="full",
    )
    lean = bench.evaluate_local_core_gates(
        manifest,
        exomem_results=exomem,
        basic_results=basic,
        preflight_valid=True,
        profile="lean",
    )

    assert invalid["local_core_advantage"] is None
    assert invalid["claim_valid"] is False
    assert lean["local_core_advantage"] is None
    assert lean["claim_valid"] is False


def test_performance_summary_and_gate_use_predeclared_paired_bands() -> None:
    manifest = bench.load_manifest()
    order = bench.counterbalanced_order(5)
    exomem = bench.PerformanceEvidence(
        query_ms=[10.0, 12.0, 11.0, 13.0, 10.0],
        index_ms=100.0,
        response_bytes=[100, 110, 90, 100, 100],
        timeouts=0,
    )
    basic = bench.PerformanceEvidence(
        query_ms=[8.0, 9.0, 10.0, 9.0, 9.0],
        index_ms=80.0,
        response_bytes=[80, 90, 100, 90, 90],
        timeouts=0,
    )

    gate = bench.evaluate_performance_envelope(manifest, exomem, basic)

    assert order == [
        ("exomem", "basic-memory"),
        ("basic-memory", "exomem"),
        ("exomem", "basic-memory"),
        ("basic-memory", "exomem"),
        ("exomem", "basic-memory"),
    ]
    assert gate["passed"] is True
    assert gate["exomem"]["median_query_ms"] == 11.0
    assert gate["exomem"]["p95_query_ms"] == 13.0
    assert gate["bands"]["query_median"]["ratio"] == pytest.approx(11 / 9)


def test_paired_performance_coordinator_executes_counterbalanced_order() -> None:
    manifest = bench.load_manifest()
    observed: list[str] = []
    coordinator = bench.PairedPerformanceCoordinator(manifest, timeout=1.0)

    async def participate(name: str):
        async def sample():
            observed.append(name)
            return {"contender": name}

        return await coordinator.participate(name, sample, index_ms=10.0)

    async def run_pair():
        return await asyncio.gather(participate("exomem"), participate("basic-memory"))

    results = asyncio.run(run_pair())
    warmups = int(manifest["performance"]["warmups"])
    expected = [name for pair in bench.counterbalanced_order(5) for name in pair]

    assert observed[(warmups + 1) * 2 :] == expected
    assert all(result[0].query_ms for result in results)
    assert all(result[0].cold_query_ms is not None for result in results)
    assert all(len(result[0].warmup_query_ms) == warmups for result in results)
    assert all(result[0].requested_seeds == (1729,) for result in results)
    assert all(result[0].seed_control_supported is False for result in results)
    assert all(result[1]["sample_complete"] for result in results)


def test_direct_preparation_is_serial_and_finishes_before_either_contender_proceeds() -> None:
    observed: list[str] = []
    coordinator = bench.SerialPreparationCoordinator(("basic-memory", "exomem"))

    async def participate(name: str):
        async def prepare():
            observed.append(f"start:{name}")
            await asyncio.sleep(0)
            observed.append(f"finish:{name}")
            return name

        result = await coordinator.participate(name, prepare)
        observed.append(f"proceed:{name}")
        return result

    async def run_pair():
        return await asyncio.gather(
            participate("exomem"),
            participate("basic-memory"),
        )

    assert asyncio.run(run_pair()) == ["exomem", "basic-memory"]
    assert observed[:4] == [
        "start:basic-memory",
        "finish:basic-memory",
        "start:exomem",
        "finish:exomem",
    ]
    assert set(observed[4:]) == {"proceed:basic-memory", "proceed:exomem"}


def test_direct_preparation_failure_releases_waiting_contender() -> None:
    coordinator = bench.SerialPreparationCoordinator(("basic-memory", "exomem"))

    async def participate(name: str):
        async def prepare():
            if name == "basic-memory":
                raise ValueError("index failed")
            return name

        return await coordinator.participate(name, prepare)

    async def run_pair():
        return await asyncio.wait_for(
            asyncio.gather(
                participate("exomem"),
                participate("basic-memory"),
                return_exceptions=True,
            ),
            timeout=1.0,
        )

    waiting, failing = asyncio.run(run_pair())

    assert isinstance(waiting, RuntimeError)
    assert "basic-memory preparation failed" in str(waiting)
    assert isinstance(failing, ValueError)


def test_exomem_generated_registry_is_fully_classified() -> None:
    manifest = bench.load_manifest()
    discovered = bench.exomem_registry_inventory()

    mcp = bench.reconcile_operation_inventory(
        manifest,
        contender="exomem",
        surface="mcp",
        discovered=discovered["mcp"],
    )
    cli = bench.reconcile_operation_inventory(
        manifest,
        contender="exomem",
        surface="cli",
        discovered=discovered["cli"],
    )

    assert mcp["valid"] is True
    assert cli["valid"] is True


def test_environment_fingerprint_records_pins_without_leaking_absolute_paths(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    state = tmp_path / "run" / "state"
    checkout.mkdir()
    state.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (checkout / "uv.lock").write_text("version = 1\n")
    config = state / "config.json"
    config.write_text('{"project":"fixture"}\n')

    fingerprint = bench.environment_fingerprint(
        contender="fixture",
        checkout=checkout,
        state_root=state,
        config_path=config,
        python=Path(sys.executable),
        model_metadata={"embeddings": {"status": "disabled"}},
    )
    encoded = json.dumps(fingerprint, sort_keys=True)

    assert fingerprint["pyproject_sha256"] == bench.sha256_file(checkout / "pyproject.toml")
    assert fingerprint["lock_sha256"] == bench.sha256_file(checkout / "uv.lock")
    assert fingerprint["config_sha256"] == bench.sha256_file(config)
    assert fingerprint["state_isolated"] is True
    assert fingerprint["python_version"]
    assert fingerprint["models"]["embeddings"]["status"] == "disabled"
    assert str(tmp_path) not in encoded


def test_exomem_version_comes_from_the_source_runtime() -> None:
    from exomem import __version__

    assert bench._exomem_version() == __version__


def test_raw_artifact_store_records_scrubbed_envelopes(tmp_path: Path) -> None:
    store = bench.RawArtifactStore(tmp_path / "raw")

    evidence = store.record(
        contender="exomem",
        probe_id="retrieval-matrix",
        request={"query": "quasarneedle-7f3a", "authorization": "secret-token"},
        response={"hits": ["rare-token-note"], "api_key": "secret-key"},
    )

    request_path = tmp_path / "raw" / evidence["request"]
    response_path = tmp_path / "raw" / evidence["response"]
    assert request_path.is_file()
    assert response_path.is_file()
    assert "secret-token" not in request_path.read_text()
    assert "secret-key" not in response_path.read_text()
    assert "[redacted]" in request_path.read_text()
    assert evidence["request_sha256"] == bench.sha256_file(request_path)
    assert evidence["response_sha256"] == bench.sha256_file(response_path)


def test_raw_artifact_store_normalizes_nested_model_payloads(tmp_path: Path) -> None:
    class Root:
        def model_dump(self, *, mode: str):
            assert mode == "json"
            return {"hits": [{"path": "Knowledge Base/example.md"}]}

    class LegacyRoot:
        def __init__(self, root):
            self.root = root

    class AttrRoot:
        def __init__(self, result):
            self.result = result

    store = bench.RawArtifactStore(tmp_path / "raw")
    evidence = store.record(
        contender="exomem",
        probe_id="typed-response",
        request={"query": "example"},
        response=AttrRoot(LegacyRoot(Root())),
    )

    response = json.loads((tmp_path / "raw" / evidence["response"]).read_text())
    assert response == {"result": {"hits": [{"path": "Knowledge Base/example.md"}]}}


def test_recorded_mcp_client_preserves_each_call_and_measurement(tmp_path: Path) -> None:
    class FakeClient:
        async def call_tool(self, name: str, arguments: dict):
            return SimpleNamespace(
                is_error=False,
                data={"result": {"tool": name, "arguments": arguments}},
            )

    recorder = bench.RecordedMCPClient(
        FakeClient(),
        contender="exomem",
        timeout=1.0,
        artifacts=bench.RawArtifactStore(tmp_path / "raw"),
    )

    first = asyncio.run(recorder.call("retrieval-matrix", "ask_memory", {"query": "rare"}))
    second = asyncio.run(recorder.call("retrieval-matrix", "ask_memory", {"query": "phrase"}))
    result = recorder.probe_result(
        probe={"id": "retrieval-matrix", "gate": "shared_core", "surface": "mcp"},
        checks={
            "rare": first["arguments"]["query"] == "rare",
            "phrase": second["arguments"]["query"] == "phrase",
        },
    )

    assert result.passed is True
    assert len(result.latency_ms) == 2
    assert len(result.response_bytes) == 2
    assert len(result.evidence["artifacts"]) == 2
    assert result.evidence["artifacts"][0]["request"].endswith("-01.request.json")
    assert result.evidence["artifacts"][1]["request"].endswith("-02.request.json")
    assert result.raw_request == result.evidence["artifacts"][0]["request"]
    assert result.raw_response == result.evidence["artifacts"][0]["response"]
    assert recorder.observed_operation_probes == {"ask_memory": {"retrieval-matrix"}}


def test_recorded_cli_preserves_command_envelope(tmp_path: Path) -> None:
    recorder = bench.RecordedCLI(
        contender="basic-memory",
        command=sys.executable,
        launcher_args=[],
        cwd=tmp_path,
        env={},
        timeout=5.0,
        artifacts=bench.RawArtifactStore(tmp_path / "raw"),
    )

    completed = recorder.call("maintenance-cli", ["-c", "print('healthy')"])
    result = recorder.probe_result(
        probe={"id": "maintenance-cli", "gate": "lifecycle_integrity", "surface": "cli"},
        checks={"healthy": completed.returncode == 0 and "healthy" in completed.stdout},
    )

    assert result.passed is True
    assert result.surface == "cli"
    assert recorder.observed_operation_probes == {"-c": {"maintenance-cli"}}
    assert result.evidence["artifacts"][0]["request"].endswith("-01.request.json")
    assert result.response_bytes


def test_fast_local_core_fixture_executes_every_required_lean_probe(tmp_path: Path) -> None:
    manifest = bench.load_manifest()

    result = bench.run_exomem_local_core_fixture(manifest, tmp_path / "local-core")
    required = {
        str(item["id"]) for item in manifest["probes"] if "lean" in item["required_profiles"]
    }

    assert set(result) == required
    assert all(item.passed for item in result.values()), {
        probe_id: item.as_dict() for probe_id, item in result.items() if not item.passed
    }
    assert (
        bench.validate_probe_coverage(
            {
                **manifest,
                "probes": [
                    item for item in manifest["probes"] if "lean" in item["required_profiles"]
                ],
            },
            executed_probe_ids=set(result),
        )["valid"]
        is True
    )
