from __future__ import annotations

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
        assert f"- {edge['type']} [[{exomem_a.id_to_path[target]}]]" in exomem_text
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
    assert str(corpus.root) not in encoded
    assert "/home/" not in encoded
    assert "C:\\" not in encoded
    assert "PRIVATE_API_KEY" not in encoded
    assert "The common graph path begins here." not in encoded
    assert "Manifest version: `1`" in markdown
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


def test_index_command_keeps_frozen_launcher_prefix(monkeypatch, tmp_path: Path) -> None:
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
    )

    assert observed["command"] == [
        "uv",
        "run",
        "--frozen",
        "--project",
        "checkout",
        "basic-memory",
        "reindex",
        "--full",
        "--search",
        "--project",
        "graph-benchmark",
    ]


def test_child_environment_does_not_forward_unrelated_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PRIVATE_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-forwarded")

    child = bench._child_env(tmp_path)

    assert child["HOME"] == str(tmp_path)
    assert "PRIVATE_API_KEY" not in child
    assert "UNRELATED_SECRET" not in child


def test_direct_mode_requires_an_explicit_basic_memory_target(tmp_path: Path) -> None:
    args = bench.parse_args(["--direct"])

    with pytest.raises(ValueError, match="--basic-memory-root"):
        bench._execute(args, bench.load_manifest(), tmp_path)
