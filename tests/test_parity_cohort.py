"""Serial current-source parity cohort collection contracts."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("parity_cohort", SCRIPTS / "parity_cohort.py")
cohort = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cohort
assert spec.loader is not None
spec.loader.exec_module(cohort)


def test_comparison_order_has_three_alternating_pairs() -> None:
    assert cohort.comparison_order() == [
        (1, "exomem"),
        (1, "basic_memory"),
        (2, "basic_memory"),
        (2, "exomem"),
        (3, "exomem"),
        (3, "basic_memory"),
    ]


@pytest.mark.parametrize(
    ("status", "exit_code", "accepted"),
    [
        ("pass", 0, True),
        ("fail", 1, True),
        ("pass", 1, False),
        ("fail", 0, False),
        ("invalid", 1, False),
    ],
)
def test_cohort_requires_exact_status_and_exit_code(
    status: str, exit_code: int, accepted: bool
) -> None:
    assert cohort.accepted_outcome(status, exit_code) is accepted


def test_basic_memory_revision_mismatch_refuses_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "basic-memory"
    source.mkdir()
    (source / "uv.lock").write_text("lock", encoding="utf-8")

    def git(_source: Path, *arguments: str) -> str:
        return "" if arguments[:2] == ("status", "--porcelain") else "different-revision"

    monkeypatch.setattr(cohort, "_git", git)

    with pytest.raises(ValueError, match="revision"):
        cohort.source_metadata(source, expected_revision="required-revision")


def test_total_budget_covers_six_outer_runs_and_overhead() -> None:
    assert cohort.MAX_RUNS == 6
    assert cohort.TOTAL_MAX_BUDGET_S == cohort.MAX_RUNS * cohort.OUTER_TIMEOUT_S + cohort.OVERHEAD_BUDGET_S
    assert cohort.TOTAL_MAX_BUDGET_S < 220 * 60


@pytest.mark.parametrize("product", ["exomem", "basic-memory"])
def test_cohort_refuses_output_inside_either_source(tmp_path, monkeypatch, product):
    source = tmp_path / "exomem"
    basic = tmp_path / "basic-memory"
    source.mkdir()
    basic.mkdir()
    monkeypatch.setattr(cohort, "ROOT", source)
    output = tmp_path / product / "new-run"
    with pytest.raises(ValueError, match="outside source"):
        cohort.main(["--output", str(output), "--basic-memory-source", str(basic),
                     "--basic-memory-python", str(basic / ".venv/bin/python")])
    assert not output.exists()


def test_cohort_retains_valid_failed_results_and_continues_all_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[tuple[str, Path]] = []
    exomem = tmp_path / "exomem"
    basic_memory = tmp_path / "basic-memory"
    exomem.mkdir()
    basic_memory.mkdir()

    monkeypatch.setattr(
        cohort,
        "selected_sources",
        lambda _source: {
            "exomem": {"source": str(exomem), "revision": "exo", "lock_sha256": "a" * 64},
            "basic_memory": {"source": str(basic_memory), "revision": "basic", "lock_sha256": "b" * 64},
        },
    )
    monkeypatch.setattr(cohort.os, "getloadavg", lambda: (0.0, 0.0, 0.0))

    class Process:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            self.product = command[command.index("--product") + 1]
            self.output = Path(command[command.index("--output") + 1])
            self.pid = 123
            launches.append((self.product, self.output))

        def wait(self, timeout: float | None = None) -> int:
            if len(launches) == 1:
                self.output.write_text('{"status": "fail", "calls": [{"outcome": "refused"}]}', encoding="utf-8")
                return 1
            self.output.write_text('{"status": "pass", "calls": [{"outcome": "ok"}]}', encoding="utf-8")
            return 0

    monkeypatch.setattr(cohort.subprocess, "Popen", Process)

    output = tmp_path / "cohort"
    assert cohort.main([
        "--output", str(output),
        "--basic-memory-source", str(basic_memory),
        "--basic-memory-python", str(tmp_path / "basic-python"),
    ]) == 0

    manifest = json.loads((output / "cohort.json").read_text(encoding="utf-8"))
    assert [product for product, _output in launches] == [product for _pair, product in cohort.comparison_order()]
    assert [run["status"] for run in manifest["runs"]] == ["fail", "pass", "pass", "pass", "pass", "pass"]
    assert manifest["collection_status"] == "complete"
    assert manifest["total_max_budget_s"] == cohort.TOTAL_MAX_BUDGET_S
    assert manifest["runs"][0]["result"]["calls"] == [{"outcome": "refused"}]


def test_workflow_runs_the_comparison_in_a_separate_guarded_job() -> None:
    workflow = (SCRIPTS.parent / ".github/workflows/mixed-load-benchmark.yml").read_text(encoding="utf-8")
    guard = "github.event.pull_request.head.repo.full_name == github.repository"
    assert "  compare:\n" in workflow
    assert workflow.count(guard) == 2
    assert "timeout-minutes: 220" in workflow
    assert "uv sync --frozen --no-dev --python 3.13.14 --project \"$basic_memory\"" in workflow
    assert "scripts/parity_cohort.py" in workflow
    assert "--basic-memory-source \"$basic_memory\"" in workflow
    assert "${{ runner.temp }}/parity-cohort/*/state/*-stdio.log" in workflow
    assert "${{ runner.temp }}/parity-cohort/*/state/config/basic-memory.log*" in workflow
