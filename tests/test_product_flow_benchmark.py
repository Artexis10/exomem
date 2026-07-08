from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "product_flow_benchmark.py"
spec = importlib.util.spec_from_file_location("product_flow_benchmark", MODULE_PATH)
bench = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)


def test_json_payload_reads_last_json_line() -> None:
    payload = bench._json_payload("noise\n{\"success\": true, \"data\": {\"x\": 1}}\n")
    assert payload == {"success": True, "data": {"x": 1}}


def test_status_from_checks() -> None:
    assert bench._status_from_checks([]) == "not_measured"
    assert bench._status_from_checks([bench.Check("a", True, "ok")]) == "pass"
    assert bench._status_from_checks([bench.Check("a", False, "bad")]) == "fail"
    assert bench._status_from_checks([
        bench.Check("a", True, "ok"),
        bench.Check("b", False, "bad"),
    ]) == "partial"


def test_summarize_flows_counts_statuses_and_ratings() -> None:
    flows = [
        bench.FlowResult("fresh_setup", "Fresh", "pass", "behind"),
        bench.FlowResult("write_remember", "Write", "pass", "ahead"),
        bench.FlowResult("schema", "Schema", "not_measured", "missing"),
    ]

    summary = bench.summarize_flows(flows)

    assert summary["total"] == 3
    assert summary["by_rating"]["ahead"] == 1
    assert summary["by_rating"]["behind"] == 1
    assert summary["by_rating"]["missing"] == 1
    assert summary["by_status"]["pass"] == 2
    assert summary["by_status"]["not_measured"] == 1


def test_render_text_report_includes_flow_and_basic_memory_surface() -> None:
    report = {
        "generated_at": "2026-07-08T00:00:00+00:00",
        "repo": "repo",
        "summary": {"by_rating": {rating: 0 for rating in bench.RATINGS}},
        "flows": [
            bench.FlowResult(
                "fresh_setup",
                "Fresh setup",
                "pass",
                "behind",
                checks=[bench.Check("setup", True, "ok")],
                evidence=["evidence"],
                gaps=["gap"],
            ).as_dict()
        ],
        "basic_memory_reference": {"observed": ["schema tools: schema_infer"]},
    }

    rendered = bench.render_text_report(report)

    assert "fresh_setup: pass / behind" in rendered
    assert "OK: setup - ok" in rendered
    assert "schema tools: schema_infer" in rendered


def test_basic_memory_reference_detects_public_surface(tmp_path: Path) -> None:
    root = tmp_path / "basic-memory"
    docs = root / "docs" / "specs"
    docs.mkdir(parents=True)
    (root / "README.md").write_text(
        "Start free trial\nuv tool install basic-memory\n"
        "write_note read_note search_notes\n"
        "build_context canvas\n"
        "schema_infer schema_validate schema_diff\n"
        "import claude conversations\nimport chatgpt\nimport memory-json\n",
        encoding="utf-8",
    )
    (root / "docs" / "ai-assistant-guide-extended.md").write_text(
        "recent_activity list_memory_projects project set-cloud\n",
        encoding="utf-8",
    )
    (docs / "SPEC-SCHEMA.md").write_text("schema_infer schema_validate\n", encoding="utf-8")

    reference = bench.basic_memory_reference(root)

    observed = "\n".join(reference["observed"])
    assert "cloud/local onboarding" in observed
    assert "write/read/search MCP tools" in observed
    assert "context graph tools" in observed
    assert "schema tools" in observed
    assert "importers" in observed
    assert reference["missing"] == []
