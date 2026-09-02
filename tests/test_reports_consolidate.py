"""D3 reports/consolidate.py + D4: artifact-only, offline consolidation.

    python -m benchmarks.reports.consolidate --run <dir> [--run <dir>...] --out <dir>

Re-derives the pre-registration identity from the checked-in receipts (never
trusts a run's own claim), refuses non-terminal manifests and unknown
schema_versions, runs under offline_guard, writes consolidated.json then
report.md on success only, and never mutates a run directory. Also wires
9.1's cross-provider latency refusal end-to-end: a --run directory carrying
a memorybench-export.v1.json feeds a ProviderLatency observation into
render_all.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from unittest import mock

import pytest

FIXTURE = Path("benchmarks/lme/fixtures/mini.json")
REPO_ROOT = Path(__file__).resolve().parents[1]


def _lme_run(out: Path, run_id: str, provider: str = "hybrid-rag-control"):
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        return execute_run(
            RunConfig(dataset=FIXTURE, out=out, reader_name="stub", run_id=run_id, provider=provider),
            reader=StubReader(),
        )


def _started_manifest(run_dir: Path) -> None:
    from protocol.manifest import start_manifest

    start_manifest(
        run_dir, run_id="started",
        dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 0},
        started_at="2026-01-01T00:00:00Z",
    )


def _memorybench_export_dir(base: Path, *, provider: str, run_id: str) -> Path:
    """A directory carrying memorybench-export.v1.json, serialized exactly
    the way the real writer does it (memorybench/export.py:1675-1694:
    `_json_bytes` -> `json.dumps(payload, sort_keys=True,
    separators=(",", ":"), allow_nan=False) + "\\n"`, written to
    `<output_root>/memorybench-export.v1.json`).

    Built from the real `protocol.models.MemoryBenchExport` pydantic model
    rather than the full `_build_export` pipeline: that pipeline needs a live
    provider run under a real memorybench checkout (checkpoint.json,
    dataset rows, a `MemoryBenchRunPlan` pointing at an on-disk
    `memorybench_home`) -- heavier than a report-rendering fixture warrants.
    The case/harness/dataset shape below mirrors the codebase's own two
    existing fixtures for this exact model: tests/test_protocol_schema_
    conformance.py::_memorybench_payloads (the full export dict, validated
    against the committed JSON Schema) and tests/test_memorybench_export_
    observations.py::_case (the self-consistent case shape, both
    observations present, nothing declared missing) -- so this is the real
    pydantic model, serialized the way export.py serializes it, not an
    invented convenience shape.
    """

    from protocol.models import (
        MEMORYBENCH_BUN_LOCK_SHA256,
        MEMORYBENCH_COMMIT,
        MEMORYBENCH_REPOSITORY,
        MEMORYBENCH_TREE,
        MemoryBenchExport,
    )

    harness = {
        "repository": MEMORYBENCH_REPOSITORY, "commit": MEMORYBENCH_COMMIT,
        "tree": MEMORYBENCH_TREE, "bun_lock_sha256": MEMORYBENCH_BUN_LOCK_SHA256,
    }
    dataset = {
        "id": "longmemeval", "variant": "s", "source": "local", "revision": "pin",
        "sha256": "a" * 64, "case_count": 1,
    }
    phases = {
        "ingest": {"status": "completed", "failure_code": None},
        "indexing": {"status": "completed", "failure_code": None},
        "search": {"status": "completed", "failure_code": None},
    }
    case = {
        "case_ordinal": 1, "case_id_hmac_sha256": "a" * 64,
        "question": {"text": "which lantern?", "type": "knowledge-update", "date": None},
        "container_tag_hmac_sha256": "a" * 64,
        # Evidence references left null (never scored/compared here), which
        # is why `status` below is "partial" -- the model refuses "complete"
        # without them ("export status contradicts evidence completeness").
        "checkpoint": None, "canonical_result": None, "private_gold": None,
        "phases": phases, "hits": [{"content": "a hit", "score": 0.0}],
        "failure_codes": [], "missing_fields": [],
        "search": {
            "transmitted_query": "which lantern?", "options": {"limit": 10},
            "normalized_hit_ids": ["Knowledge Base/Notes/a.md"],
        },
        "ingest": {"transmitted_payload_sha256": ["a" * 64]},
    }
    export = {
        "protocol_version": "1.0.0", "schema_version": 1, "artifact_type": "memorybench-export.v1",
        "status": "partial", "run_id": run_id, "provider": provider,
        "provider_variant": f"{provider}-default", "benchmark": "longmemeval",
        "harness": harness, "dataset": dataset,
        "executed_stages": ["ingest", "indexing", "search"],
        "excluded_stages": ["answer", "evaluate", "report"],
        "privacy": {
            "classification": "provider_safe_reader_input", "contains_ground_truth": False,
            "source_results_contain_ground_truth": True,
        },
        "latency": {"publishable": False, "reason": "host_unvalidated"},
        "failure_codes": [], "cases": [case], "session_normalization": None, "readiness": None,
    }
    validated = MemoryBenchExport.model_validate(export).model_dump(mode="json")
    export_dir = base / f"export-{provider}"
    export_dir.mkdir(parents=True)
    payload = json.dumps(validated, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    (export_dir / "memorybench-export.v1.json").write_text(payload, encoding="utf-8")
    return export_dir


def _bogus_memorybench_export_dir(base: Path, *, mutate) -> Path:
    """A directory carrying a corrupted memorybench-export.v1.json: the same
    real, valid export dict `_memorybench_export_dir` builds and validates,
    but with `mutate` applied to the dict AFTER validation would have caught
    it and written WITHOUT re-validating -- a bogus artifact on disk, the
    same shape a real one could be corrupted into (mirrors
    test_r2_unknown_schema_version_exits_non_zero_and_writes_nothing's own
    technique of mutating a real writer's output), never a hand-invented
    shape.
    """

    from protocol.models import (
        MEMORYBENCH_BUN_LOCK_SHA256,
        MEMORYBENCH_COMMIT,
        MEMORYBENCH_REPOSITORY,
        MEMORYBENCH_TREE,
        MemoryBenchExport,
    )

    harness = {
        "repository": MEMORYBENCH_REPOSITORY, "commit": MEMORYBENCH_COMMIT,
        "tree": MEMORYBENCH_TREE, "bun_lock_sha256": MEMORYBENCH_BUN_LOCK_SHA256,
    }
    dataset = {
        "id": "longmemeval", "variant": "s", "source": "local", "revision": "pin",
        "sha256": "a" * 64, "case_count": 1,
    }
    phases = {
        "ingest": {"status": "completed", "failure_code": None},
        "indexing": {"status": "completed", "failure_code": None},
        "search": {"status": "completed", "failure_code": None},
    }
    case = {
        "case_ordinal": 1, "case_id_hmac_sha256": "a" * 64,
        "question": {"text": "which lantern?", "type": "knowledge-update", "date": None},
        "container_tag_hmac_sha256": "a" * 64,
        "checkpoint": None, "canonical_result": None, "private_gold": None,
        "phases": phases, "hits": [{"content": "a hit", "score": 0.0}],
        "failure_codes": [], "missing_fields": [],
        "search": {
            "transmitted_query": "which lantern?", "options": {"limit": 10},
            "normalized_hit_ids": ["Knowledge Base/Notes/a.md"],
        },
        "ingest": {"transmitted_payload_sha256": ["a" * 64]},
    }
    export = {
        "protocol_version": "1.0.0", "schema_version": 1, "artifact_type": "memorybench-export.v1",
        "status": "partial", "run_id": "bogus-run", "provider": "exomem",
        "provider_variant": "exomem-default", "benchmark": "longmemeval",
        "harness": harness, "dataset": dataset,
        "executed_stages": ["ingest", "indexing", "search"],
        "excluded_stages": ["answer", "evaluate", "report"],
        "privacy": {
            "classification": "provider_safe_reader_input", "contains_ground_truth": False,
            "source_results_contain_ground_truth": True,
        },
        "latency": {"publishable": False, "reason": "host_unvalidated"},
        "failure_codes": [], "cases": [case], "session_normalization": None, "readiness": None,
    }
    # Sanity: the dict is genuinely valid BEFORE the caller's mutation --
    # every red case below is a targeted corruption, not an accidentally
    # malformed baseline.
    MemoryBenchExport.model_validate(export)
    mutate(export)
    export_dir = base / "export-bogus"
    export_dir.mkdir(parents=True)
    payload = json.dumps(export, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    (export_dir / "memorybench-export.v1.json").write_text(payload, encoding="utf-8")
    return export_dir


def test_consolidate_writes_report_and_identity(tmp_path: Path) -> None:
    from reports.consolidate import consolidate

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-happy")
    out_dir = tmp_path / "out"

    consolidate([result.run_dir], out_dir, repo_root=REPO_ROOT)

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "| Ability | Variant | Questions |" in report
    assert "aggregate" not in report.lower()
    payload = json.loads((out_dir / "consolidated.json").read_text(encoding="utf-8"))
    assert payload["preregistration_identity"]["original"]["sha256"]
    assert payload["preregistration_identity"]["amendments"] == [] or isinstance(
        payload["preregistration_identity"]["amendments"], list
    )


def test_r4_identity_matches_derive_preregistration_identity_directly(tmp_path: Path) -> None:
    from protocol.contracts import derive_preregistration_identity
    from reports.consolidate import consolidate

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-r4")
    out_dir = tmp_path / "out"

    consolidate([result.run_dir], out_dir, repo_root=REPO_ROOT)
    payload = json.loads((out_dir / "consolidated.json").read_text(encoding="utf-8"))

    expected = derive_preregistration_identity(REPO_ROOT)
    assert payload["preregistration_identity"] == expected.model_dump(mode="json")
    assert payload["preregistration_identity"]["original"]["sha256"] == expected.original.sha256
    assert [a["sequence"] for a in payload["preregistration_identity"]["amendments"]] == [
        a.sequence for a in expected.amendments
    ]


def test_r1_non_terminal_manifest_exits_non_zero_and_writes_nothing(tmp_path: Path) -> None:
    from reports.consolidate import main

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _started_manifest(run_dir)
    out_dir = tmp_path / "out"

    exit_code = main(["--run", str(run_dir), "--out", str(out_dir)])

    assert exit_code != 0
    assert not out_dir.exists()


def test_r2_unknown_schema_version_exits_non_zero_and_writes_nothing(tmp_path: Path) -> None:
    from reports.consolidate import main

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-schema-drift")
    manifest_path = result.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_bytes = manifest_path.read_bytes()
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out_dir = tmp_path / "out"

    exit_code = main(["--run", str(result.run_dir), "--out", str(out_dir)])

    assert exit_code != 0
    assert not out_dir.exists()
    assert manifest_path.read_bytes() != original_bytes, "sanity: the mutation itself took"


def test_never_mutates_a_run_directory(tmp_path: Path) -> None:
    from reports.consolidate import consolidate

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-no-mutate")
    before = {
        path.relative_to(result.run_dir): path.read_bytes()
        for path in sorted(result.run_dir.rglob("*")) if path.is_file()
    }
    out_dir = tmp_path / "out"

    consolidate([result.run_dir], out_dir, repo_root=REPO_ROOT)

    after = {
        path.relative_to(result.run_dir): path.read_bytes()
        for path in sorted(result.run_dir.rglob("*")) if path.is_file()
    }
    assert before == after


def test_r7_consolidated_report_never_contains_aggregate(tmp_path: Path) -> None:
    from reports.consolidate import consolidate

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-no-aggregate")
    out_dir = tmp_path / "out"

    consolidate([result.run_dir], out_dir, repo_root=REPO_ROOT)

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "Aggregate" not in report
    assert "aggregate" not in report


def test_f4_a_manifest_and_a_cohort_in_the_same_directory_is_refused(tmp_path: Path) -> None:
    """F4: `_classify` must not silently pick one lane when a directory
    carries both an LME manifest and an epistemic cohort."""

    from reports.consolidate import consolidate

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-ambiguous")
    (result.run_dir / "validated-cohort.v1.json").write_text("{}", encoding="utf-8")
    out_dir = tmp_path / "out"

    with pytest.raises(ValueError, match="ambiguous"):
        consolidate([result.run_dir], out_dir, repo_root=REPO_ROOT)
    assert not out_dir.exists()


def test_f5_identity_is_written_before_the_report(tmp_path: Path) -> None:
    """F5: if a failure lands between the two writes, consolidated.json (the
    identity) must already be on disk -- never a report standing without it."""

    import reports.consolidate as consolidate_module

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-write-order")
    out_dir = tmp_path / "out"

    real_write_text = Path.write_text

    def _fail_on_report(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "report.md":
            raise OSError("simulated failure writing report.md")
        return real_write_text(self, *args, **kwargs)

    with mock.patch.object(Path, "write_text", _fail_on_report):
        with pytest.raises(OSError, match="simulated failure writing report.md"):
            consolidate_module.consolidate([result.run_dir], out_dir, repo_root=REPO_ROOT)

    assert (out_dir / "consolidated.json").is_file(), "identity must survive a later write failure"
    assert not (out_dir / "report.md").is_file()


def test_f6_a_blocked_row_renders_its_own_status_never_a_fabricated_score(tmp_path: Path) -> None:
    """F6/D3: "a blocked row renders `blocked: <reason>`, never a loss".

    Produced from the real writer's own output: `lme.runner.execute_run`
    writes one gold-evidence-ceiling.jsonl line per question; removing one
    line (rather than hand-writing a manifest) is the same minimal-mutation
    technique tests/test_report_offline.py already uses on `contamination`/
    `status` -- the row for that question's ability is then genuinely
    "blocked: missing gold-evidence ceiling" per lme/report.py:108, the one
    place in the reused renderers that emits the literal word "blocked".
    """

    from reports.consolidate import consolidate

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-blocked-row")
    ceiling_path = result.run_dir / "bounds" / "gold-evidence-ceiling.jsonl"
    kept = [
        line for line in ceiling_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["question_id"] != "mini-single-user"
    ]
    ceiling_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    consolidate([result.run_dir], out_dir, repo_root=REPO_ROOT)

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    row = next(line for line in report.splitlines() if line.startswith("| single-session-user"))
    assert row.strip().endswith("| blocked: missing gold-evidence ceiling |"), row
    assert not re.search(r"\d+/\d+", row), f"a blocked row must never carry a fabricated score: {row}"


def test_h3_two_providers_carrying_host_unvalidated_latency_end_to_end(tmp_path: Path) -> None:
    """H3 end-to-end: two --run directories each carrying a real
    memorybench-export.v1.json with host_unvalidated latency -> no
    comparative column, but neither provider's own indicative row is
    dropped (F1's correction to the original, wrong, "no numbers at all"
    pin); H1: the two never share a `latency_ms` column/table."""

    from reports.consolidate import consolidate

    export_a = _memorybench_export_dir(tmp_path, provider="exomem", run_id="run-a")
    export_b = _memorybench_export_dir(tmp_path, provider="basic-memory", run_id="run-b")
    out_dir = tmp_path / "out"

    consolidate([export_a, export_b], out_dir, repo_root=REPO_ROOT)

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "## Latency" in report
    assert "### exomem" in report
    assert "### basic-memory" in report
    assert "| n/a | indicative (host_unvalidated) |" in report
    assert "withheld: transport asymmetry (4b.40)" in report
    # H1: no shared column -- each provider's own table/header appears
    # exactly once, never one table both providers' rows land in.
    assert report.count("| latency_ms | disposition |") == 2
    exomem_block = report.split("### exomem", 1)[1].split("### basic-memory", 1)[0]
    basic_memory_block = report.split("### basic-memory", 1)[1]
    assert "| latency_ms | disposition |" in exomem_block
    assert "| latency_ms | disposition |" in basic_memory_block
    assert "aggregate" not in report.lower()


def test_h3_a_single_provider_export_renders_without_the_withheld_marker(tmp_path: Path) -> None:
    from reports.consolidate import consolidate

    export_dir = _memorybench_export_dir(tmp_path, provider="exomem", run_id="run-solo")
    out_dir = tmp_path / "out"

    consolidate([export_dir], out_dir, repo_root=REPO_ROOT)

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "### exomem" in report
    assert "| n/a | indicative (host_unvalidated) |" in report
    assert "withheld: transport asymmetry (4b.40)" not in report


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda export: export.__setitem__("schema_version", 99), "schema_version"),
        (lambda export: export.__setitem__("artifact_type", "not-an-export"), "artifact_type|literal|Input should be"),
        (lambda export: export.__delitem__("provider"), "provider|Field required"),
    ],
    ids=["bogus-schema-version", "bogus-artifact-type", "truncated-missing-provider"],
)
def test_a_malformed_memorybench_export_is_refused_not_consumed(
    tmp_path: Path, mutate, match: str
) -> None:
    """The HIGH finding this micro-round fixes: `_latency_observation` used
    to validate only the `latency` sub-object, so a corrupted export
    (unknown schema_version, wrong artifact_type, or missing `provider`)
    was consumed anyway and rendered a latency row. It must refuse exactly
    like an unknown-schema_version LME manifest does -- exit non-zero,
    nothing written -- never a raw KeyError."""

    from reports.consolidate import main

    export_dir = _bogus_memorybench_export_dir(tmp_path, mutate=mutate)
    out_dir = tmp_path / "out"

    exit_code = main(["--run", str(export_dir), "--out", str(out_dir)])

    assert exit_code != 0
    assert not out_dir.exists()


def test_a_malformed_memorybench_export_raises_a_validation_error_not_a_keyerror(
    tmp_path: Path,
) -> None:
    """Calling `consolidate()` directly (below the CLI's broad except)
    proves the refusal is a real pydantic ValidationError, not a KeyError
    that a differently-shaped corruption could slip past."""

    from pydantic import ValidationError
    from reports.consolidate import consolidate

    export_dir = _bogus_memorybench_export_dir(
        tmp_path, mutate=lambda export: export.__delitem__("provider")
    )
    out_dir = tmp_path / "out"

    with pytest.raises(ValidationError):
        consolidate([export_dir], out_dir, repo_root=REPO_ROOT)
    assert not out_dir.exists()


def test_r5_consolidate_runs_under_its_own_offline_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """H2: the injected render_all must actually be reached -- a real run
    directory (built by the real writer), not an empty one _classify would
    refuse before offline_guard is ever entered."""

    import reports.consolidate as consolidate_module

    def _sneaky(inputs, *, latency=()):  # type: ignore[no-untyped-def]
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        with probe:
            probe.connect(("127.0.0.1", 9))
        return "unreachable"

    monkeypatch.setattr(consolidate_module, "render_all", _sneaky)
    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-offline-guard")
    out_dir = tmp_path / "out"

    exit_code = consolidate_module.main(["--run", str(result.run_dir), "--out", str(out_dir)])

    assert exit_code != 0
    assert not out_dir.exists()
    stderr = capsys.readouterr().err
    assert "offline report generation forbids socket.connect" in stderr, stderr


def test_the_cli_module_is_runnable_as_python_dash_m(tmp_path: Path) -> None:
    import subprocess
    import sys

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "consolidate-cli")
    out_dir = tmp_path / "out"

    completed = subprocess.run(
        [
            sys.executable, "-m", "benchmarks.reports.consolidate",
            "--run", str(result.run_dir), "--out", str(out_dir),
        ],
        cwd=REPO_ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert (out_dir / "report.md").is_file()
    assert (out_dir / "consolidated.json").is_file()
