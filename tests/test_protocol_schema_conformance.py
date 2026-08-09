"""RM3: every artifact this lane emits validates against its COMMITTED schema.

The drift gate proves the committed schemas match the models. This proves the
files the lane actually writes match the committed schemas — the other half,
and the one that catches an emitter that quietly stopped conforming.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from unittest import mock

import pytest
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path("benchmarks/protocol/schema")
FIXTURE = Path("benchmarks/lme/fixtures/mini.json")
TWIN = Path("benchmarks/equivalence/fixtures/perturbed-twin")


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{name}.v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def emitted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    from equivalence.differ import compare_runs
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    root = tmp_path_factory.mktemp("conformance")
    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        result = execute_run(
            RunConfig(dataset=FIXTURE, out=root, reader_name="stub", run_id="conformance", provider="hybrid-rag-control"),
            reader=StubReader(),
        )
    diff_out = root / "diff"
    compare_runs(TWIN / "left", TWIN / "right", mode="blocking", out=diff_out)
    return {"run": result.run_dir, "diff": diff_out}


def test_the_run_manifest_validates_against_its_committed_schema(emitted) -> None:
    _validator("run-manifest").validate(json.loads((emitted["run"] / "manifest.json").read_text(encoding="utf-8")))


def test_every_trace_record_validates_against_the_case_trace_schema(emitted) -> None:
    validator = _validator("case-trace")
    traces = sorted((emitted["run"] / "traces").glob("*.jsonl"))
    assert traces, "no traces were written"
    for trace in traces:
        entries = _rows(trace)
        assert entries
        validator.validate({
            "protocol_version": "1.0.0", "schema_version": 1, "case_id": trace.stem, "entries": entries,
        })


def test_every_probe_result_validates_against_its_committed_schema(emitted) -> None:
    validator = _validator("probe-result")
    rows = _rows(emitted["run"] / "probes.jsonl")
    assert len(rows) == 3
    for row in rows:
        validator.validate(row)


def test_every_budget_ledger_entry_validates_against_its_committed_schema(emitted) -> None:
    validator = _validator("budget-ledger")
    rows = _rows(emitted["run"] / "ledger.jsonl")
    assert rows
    for row in rows:
        validator.validate(row)


def test_the_equivalence_diff_artifact_validates_against_its_committed_schema(emitted) -> None:
    validator = _validator("equivalence-diff")
    artifact = json.loads((emitted["diff"] / "equivalence-diff.v1.json").read_text(encoding="utf-8"))
    validator.validate(artifact)
    assert artifact["kind"] == "equivalence-diff.v1"
    assert artifact["diffs"], "the perturbed twin must produce differences to validate"
    assert any(diff["compare_as"] is None for diff in artifact["diffs"])


def test_every_exception_register_entry_validates_against_its_committed_schema(tmp_path: Path) -> None:
    import yaml
    from equivalence.exceptions import load_exceptions

    validator = _validator("equivalence-exception")
    register = tmp_path / "exceptions.yaml"
    register.write_text(
        yaml.safe_dump([{
            "case_id": "twin-all-keys", "field": "retrieved_text", "compare_as": "set-membership",
            "evidence": "upstream ordering is not part of the contract", "approver": "benchmark-owner",
            "expires_at": "2099-01-01",
        }]),
        encoding="utf-8",
    )
    entries = load_exceptions(register)
    assert entries and entries[0].active(dt.date(2026, 8, 9))
    for entry in entries:
        validator.validate({"protocol_version": "1.0.0", "schema_version": 1, **entry.__dict__})


def test_the_emitted_equivalence_input_carries_every_differ_key(emitted) -> None:
    from equivalence.differ import EQUIVALENCE_KEYS

    payload = json.loads((emitted["run"] / "equivalence.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "equivalence-input.v1"
    for case in payload["cases"]:
        assert set(EQUIVALENCE_KEYS) <= set(case)
        assert case["namespace"] == "hybrid-run-24hex"
        assert all(len(sha) == 64 for sha in case["ingestion_payloads"])
