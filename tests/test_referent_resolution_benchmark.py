from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "referent_resolution_benchmark.py"
MANIFEST = ROOT / "tests" / "fixtures" / "referent_resolution" / "manifest.json"


@pytest.fixture(autouse=True)
def _model_free_benchmark(monkeypatch: pytest.MonkeyPatch):
    from exomem import find as find_module

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    find_module.clear_cache()
    yield
    find_module.clear_cache()


def _module():
    spec = importlib.util.spec_from_file_location("referent_resolution_benchmark", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_render_is_deterministic_for_seed(tmp_path: Path) -> None:
    module = _module()
    manifest = module.load_manifest(MANIFEST)
    first = module.render_fixture(manifest, tmp_path / "a")
    second = module.render_fixture(manifest, tmp_path / "b")
    assert first.corpus_hash == second.corpus_hash
    assert first.id_to_path == second.id_to_path


def test_fixture_passes_public_artifact_privacy_scan(tmp_path: Path) -> None:
    module = _module()
    rendered = module.render_fixture(module.load_manifest(MANIFEST), tmp_path / "vault")
    assert module.scan_public_artifacts(rendered.root) == []


def test_every_case_meets_expected_outcome_with_graph_on(tmp_path: Path) -> None:
    report = _module().run_benchmark(MANIFEST, work_root=tmp_path)
    assert [case["case_id"] for case in report["_case_results"]] == [
        "A",
        "A2",
        "B",
        "C",
        "D",
        "D2",
        "E",
        "F",
        "G",
        "H",
        "I",
        "J",
        "K",
        "L",
        "M",
        "N",
        "O",
    ]
    assert all(case["graph_on"]["expected"] for case in report["_case_results"])


def test_graph_only_cases_fail_without_graph_and_pass_with_graph(tmp_path: Path) -> None:
    report = _module().run_benchmark(MANIFEST, work_root=tmp_path)
    cases = [case for case in report["_case_results"] if case["graph_required"]]
    assert cases
    assert all(case["graph_on"]["expected"] and not case["graph_off"]["expected"] for case in cases)


def test_negative_control_and_ambiguity_cases_abstain(tmp_path: Path) -> None:
    report = _module().run_benchmark(MANIFEST, work_root=tmp_path)
    cases = {case["case_id"]: case for case in report["_case_results"]}
    assert cases["F"]["graph_on"]["status"] == "ambiguous"
    assert cases["G"]["graph_on"]["status"] == "unresolved"
    assert cases["I"]["graph_on"]["status"] == "unresolved"
    assert cases["O"]["graph_on"]["status"] == "unresolved"
    assert all(
        not Path(path).stem.startswith("o-person-")
        for path in cases["O"]["graph_on"]["candidates"]
    )
    # O abstains because the persons fail the cue type, not because nothing matched.
    assert cases["O"]["graph_on"]["reasons"]["type_mismatch"] >= 2
    assert cases["N"]["graph_on"]["status"] == "partial"
    assert cases["N"]["graph_on"]["unresolved_count"] == 1
    assert [Path(path).stem for path in cases["N"]["graph_on"]["candidates"]] == ["n-noise"]


def test_metric_floors_hold(tmp_path: Path) -> None:
    report = _module().run_benchmark(MANIFEST, work_root=tmp_path)
    metrics = report["metrics"]
    assert metrics["set_accuracy"] >= 0.9
    assert metrics["false_resolution_rate"] == 0.0
    assert metrics["abstention_accuracy"] == 1.0
    assert metrics["partial_accuracy"] == 1.0
    assert metrics["graph_incremental_value"] >= 1


def test_report_is_aggregate_only_and_reproducible(tmp_path: Path) -> None:
    module = _module()
    first = module.run_benchmark(MANIFEST, work_root=tmp_path / "one")
    second = module.run_benchmark(MANIFEST, work_root=tmp_path / "two")
    public_first = module.public_report(first)
    public_second = module.public_report(second)
    first_timing = public_first.pop("referents_stage")
    second_timing = public_second.pop("referents_stage")
    assert public_first == public_second
    assert set(first_timing) == set(second_timing) == {"median_ms", "p95_ms"}
    encoded = json.dumps(public_first, sort_keys=True)
    assert "_case_results" not in encoded
    assert str(tmp_path) not in encoded
