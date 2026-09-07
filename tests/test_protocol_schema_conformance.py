"""RM3: every artifact this lane emits validates against its COMMITTED schema.

The drift gate proves the committed schemas match the models. This proves the
files the lane actually writes match the committed schemas — the other half,
and the one that catches an emitter that quietly stopped conforming.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest import mock

import pytest
from benchmark_capabilities import require_posix_host_paths
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

SCHEMA_DIR = Path("benchmarks/protocol/schema")
FIXTURE = Path("benchmarks/lme/fixtures/mini.json")
TWIN = Path("benchmarks/equivalence/fixtures/perturbed-twin")


def _validator(name: str, version: int = 1) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{name}.v{version}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def emitted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    from equivalence.differ import compare_runs
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run
    from protocol.contracts import RATIFICATION_REPOSITORY_REVISION
    from protocol.manifest import start_manifest

    root = tmp_path_factory.mktemp("conformance")
    pinned_start_manifest = lambda *args, **kwargs: start_manifest(  # noqa: E731
        *args, contract_revision=RATIFICATION_REPOSITORY_REVISION, **kwargs
    )
    with (
        mock.patch.dict(
            os.environ,
            {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"},
        ),
        mock.patch("lme.runner.start_manifest", side_effect=pinned_start_manifest),
    ):
        result = execute_run(
            RunConfig(dataset=FIXTURE, out=root, reader_name="stub", run_id="conformance", provider="hybrid-rag-control"),
            reader=StubReader(),
        )
    diff_out = root / "diff"
    compare_runs(TWIN / "left", TWIN / "right", mode="blocking", out=diff_out)
    return {"run": result.run_dir, "diff": diff_out}


def test_the_run_manifest_validates_against_its_committed_schema(emitted) -> None:
    _validator("run-manifest", 2).validate(json.loads((emitted["run"] / "manifest.json").read_text(encoding="utf-8")))


def test_committed_lme_selection_validates_against_its_closed_schema() -> None:
    validator = _validator("lme-selection")
    payload = json.loads(Path("benchmarks/equivalence/subsets/lme-s-25.json").read_text(encoding="utf-8"))
    validator.validate(payload)
    assert len(payload["target_question_ids"]) == 25


def test_lifecycle_enabled_direct_traces_validate_against_case_trace_v2(emitted) -> None:
    validator = _validator("case-trace", 2)
    traces = sorted((emitted["run"] / "traces").glob("*.jsonl"))
    assert traces, "no traces were written"
    for trace in traces:
        entries = _rows(trace)
        assert entries
        validator.validate({
            "protocol_version": "1.0.0", "schema_version": 2, "case_id": trace.stem, "entries": entries,
        })


def test_standalone_v1_traces_remain_accepted_but_mixed_versions_are_refused(tmp_path: Path) -> None:
    from protocol.trace import CaseTraceReader, CaseTraceWriter, TraceError

    CaseTraceWriter(tmp_path, "legacy").append({"record": "timing", "phase": "legacy", "ms": 1})
    assert _validator("case-trace").is_valid({
        "protocol_version": "1.0.0", "schema_version": 1, "case_id": "legacy",
        "entries": [{"record": "timing", "phase": "legacy", "ms": 1}],
    })
    with pytest.raises(TraceError, match="mixed"):
        CaseTraceWriter(tmp_path, "legacy", schema_version=2).append({
            "record": "timing", "phase": "new", "ms": 1,
        })
    assert [record.record for record in CaseTraceReader(tmp_path, "legacy")] == ["timing"]


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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


HMAC_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"


def _hmac(domain: str, value: str) -> str:
    message = domain.encode() + b"\0" + value.encode()
    return hmac.new(bytes.fromhex(HMAC_KEY_HEX), message, hashlib.sha256).hexdigest()


def _reference(root: str, path: str, fill: str) -> dict[str, str | None]:
    if root == "output":
        return {"root": root, "path": path, "path_hmac_sha256": None, "sha256": fill * 64}
    return {
        "root": root,
        "path": None,
        "path_hmac_sha256": _hmac("artifact-path", path),
        "sha256": fill * 64,
    }


def _memorybench_payloads() -> dict[str, dict]:
    harness = {
        "repository": "https://github.com/supermemoryai/memorybench",
        "commit": "118209a746d97d0d85e5a7234267f0b6962857e9",
        "tree": "2ee25bdbcb6bfaaecb32f917920c53775a299b37",
        "bun_lock_sha256": "561d761fd16f895a6597227c6fc1e46064779284317fd479e079e3c86b9857da",
    }
    dataset = {
        "id": "longmemeval",
        "variant": "cleaned",
        "source": "xiaowu0162/longmemeval-cleaned",
        "revision": "fixture-pin",
        "sha256": "4" * 64,
        "case_count": 1,
    }
    case_digest = _hmac("case-id", "q-01")
    tag_digest = _hmac("container-tag", "private-container-tag")
    missing = sorted(
        [
            "gold.answer_session_ids",
            "ingest.transmitted_payloads",
            "search.transmitted_query",
            "search.options.limit",
            "search.options.threshold",
            "search.normalized_hit_ids",
            "search.normalized_scores",
            "search.normalized_ranks",
            "search.retry_attempts",
            "search.http_status",
        ]
    )
    run_plan = {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "memorybench-run-plan.v1",
        "run_id": "run-01",
        "upstream_run_id": "run-01",
        "provider": "exomem",
        "provider_variant": "exomem-source-only",
        "benchmark": "longmemeval",
        "selection": {"mode": "full", "target_question_ids": None},
        "harness": harness,
        "dataset": dataset,
        "dataset_path": "/owned/memorybench/data/benchmarks/longmemeval/datasets/longmemeval_s_cleaned.json",
        "provider_checkout": {
            "root": "/owned/provider",
            "repository": "https://github.com/example/provider",
            "commit": "5" * 40,
            "tree": "6" * 40,
            "lock_sha256": "7" * 64,
        },
        "memorybench_home": "/owned/memorybench",
        "output_root": "/owned/output",
        "guest_work_root": "/owned/output/work",
        "guest_evidence_root": "/owned/output/evidence",
        "contract_revision": "7cd15e6d6c67eb914e4f57bd943f98f7d1894b7f",
        "preregistration_sha256": "8" * 64,
        "privacy_hmac_key_hex": HMAC_KEY_HEX,
    }
    export = {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "memorybench-export.v1",
        "status": "complete",
        "run_id": "run-01",
        "provider": "exomem",
        "provider_variant": "exomem-source-only",
        "benchmark": "longmemeval",
        "harness": harness,
        "dataset": dataset,
        "executed_stages": ["ingest", "indexing", "search"],
        "excluded_stages": ["answer", "evaluate", "report"],
        "privacy": {
            "classification": "provider_safe_reader_input",
            "contains_ground_truth": False,
            "source_results_contain_ground_truth": True,
        },
        "latency": {"publishable": False, "reason": "host_unvalidated"},
        "failure_codes": [],
        "cases": [
            {
                "case_ordinal": 1,
                "case_id_hmac_sha256": case_digest,
                "question": {"text": "Where?", "type": "single-session-user", "date": "2026-01-01"},
                "container_tag_hmac_sha256": tag_digest,
                "checkpoint": _reference("memorybench_run", "checkpoint.json", "a"),
                "canonical_result": _reference("memorybench_run", "results/q-01.json", "b"),
                "private_gold": _reference("output", f"private-gold/{case_digest}.json", "c"),
                "phases": {
                    "ingest": {"status": "completed", "failure_code": None},
                    "indexing": {"status": "completed", "failure_code": None},
                    "search": {"status": "completed", "failure_code": None},
                },
                "hits": [{"content": "first", "score": 0.9}],
                "failure_codes": [],
                "missing_fields": missing,
                # This fixture declares every observation missing, so both
                # optional blocks stay null and their labels stay in `missing`.
                "search": None,
                "ingest": None,
                "namespace_pattern": None,
                "readiness": None,
            }
        ],
        "session_normalization": None,
        "readiness": None,
    }
    private_gold = {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "memorybench-private-gold.v1",
        "case_id_hmac_sha256": case_digest,
        "question_id": "q-01",
        "container_tag": "private-container-tag",
        "question": "Where?",
        "question_type": "single-session-user",
        "ground_truth": "answer-secret",
        "answer_session_ids": None,
        "checkpoint_path": "checkpoint.json",
        "checkpoint_sha256": "a" * 64,
        "canonical_result_path": "results/q-01.json",
        "canonical_result_sha256": "b" * 64,
        "missing_fields": ["gold.answer_session_ids"],
    }
    cleanup_plan = {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "guest-cleanup-plan.v1",
        "run_id": "run-01",
        "provider": "exomem",
        "provider_variant": "exomem-source-only",
        "guest_work_root": "/owned/output/work",
        "guest_evidence_root": "/owned/output/evidence",
        "run_plan_path": "/owned/run-plan.json",
        "run_plan_sha256": "d" * 64,
        "targets": [
            {
                "container_tag": "private-container-tag",
                "container_tag_hmac_sha256": tag_digest,
                "discovery_sources": ["checkpoint", "guest_evidence"],
                "namespace_expected": True,
            }
        ],
    }
    cleanup = {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "guest-cleanup.v1",
        "run_id": "run-01",
        "provider": "exomem",
        "provider_variant": "exomem-source-only",
        "trigger": "success",
        "targets": [
            {
                "container_tag_hmac_sha256": tag_digest,
                "discovery_sources": ["checkpoint", "guest_evidence"],
                "outcome": "cleared",
                "failure_code": None,
                "artifacts": [_reference("output", "cleanup-evidence/target.json", "e")],
                "absence": {
                    "namespace": True,
                    "corpus": None,
                    "config": None,
                    "descriptor": True,
                    "process_group": True,
                    "work_root": True,
                },
            }
        ],
        "basic_public_cleanup_calls": 0,
        "failure_codes": [],
        "final_absence": {
            "config": True,
            "descriptor": True,
            "process_group": True,
            "work_root": True,
            "artifacts": [_reference("output", "cleanup-evidence/final.json", "f")],
        },
        "all_absent": True,
    }
    return {
        "MemoryBenchRunPlan": run_plan,
        "MemoryBenchExport": export,
        "MemoryBenchPrivateGold": private_gold,
        "GuestCleanupPlan": cleanup_plan,
        "GuestCleanup": cleanup,
    }


SCHEMA_BY_MODEL = {
    "MemoryBenchRunPlan": "memorybench-run-plan",
    "MemoryBenchExport": "memorybench-export",
    "MemoryBenchPrivateGold": "memorybench-private-gold",
    "GuestCleanupPlan": "guest-cleanup-plan",
    "GuestCleanup": "guest-cleanup",
}

SCHEMA_MODEL_EXCEPTIONS = frozenset({
    "MemoryBenchRunPlan.output_root|guest_work_root|guest_evidence_root:relation",
    "MemoryBenchExport.failure_codes:ordering",
    "MemoryBenchExport.cases.case_ordinal:ordering",
    "MemoryBenchExport.cases.case_ordinal:keyed-uniqueness",
    "MemoryBenchExport.cases.failure_codes:ordering",
    "MemoryBenchExport.cases.missing_fields:ordering",
    "MemoryBenchPrivateGold.missing_fields:ordering",
    "GuestCleanupPlan.targets:ordering",
    "GuestCleanupPlan.targets.container_tag_hmac_sha256:keyed-uniqueness",
    "GuestCleanupPlan.targets.discovery_sources:ordering",
    "GuestCleanup.targets.container_tag_hmac_sha256:keyed-uniqueness",
    "GuestCleanup.targets.discovery_sources:ordering",
    "GuestCleanup.targets.artifacts:ordering",
    "GuestCleanup.failure_codes:ordering",
    "GuestCleanup.final_absence.artifacts:ordering",
})

EXPECTED_SCHEMA_MODEL_EXCEPTIONS = frozenset({
    "MemoryBenchRunPlan.output_root|guest_work_root|guest_evidence_root:relation",
    "MemoryBenchExport.failure_codes:ordering",
    "MemoryBenchExport.cases.case_ordinal:ordering",
    "MemoryBenchExport.cases.case_ordinal:keyed-uniqueness",
    "MemoryBenchExport.cases.failure_codes:ordering",
    "MemoryBenchExport.cases.missing_fields:ordering",
    "MemoryBenchPrivateGold.missing_fields:ordering",
    "GuestCleanupPlan.targets:ordering",
    "GuestCleanupPlan.targets.container_tag_hmac_sha256:keyed-uniqueness",
    "GuestCleanupPlan.targets.discovery_sources:ordering",
    "GuestCleanup.targets.container_tag_hmac_sha256:keyed-uniqueness",
    "GuestCleanup.targets.discovery_sources:ordering",
    "GuestCleanup.targets.artifacts:ordering",
    "GuestCleanup.failure_codes:ordering",
    "GuestCleanup.final_absence.artifacts:ordering",
})


def _accepts_both(model_name: str, payload: dict) -> None:
    import protocol.models as models

    rendered = getattr(models, model_name).model_validate(payload).model_dump(mode="json")
    assert rendered == payload, "full evidence payload must not rely on model defaults"
    _validator(SCHEMA_BY_MODEL[model_name]).validate(payload)


def _rejects_both(model_name: str, payload: dict) -> None:
    import protocol.models as models

    with pytest.raises(PydanticValidationError):
        getattr(models, model_name).model_validate(payload)
    with pytest.raises(JsonSchemaValidationError):
        _validator(SCHEMA_BY_MODEL[model_name]).validate(payload)


def _rejects_model_only(model_name: str, payload: dict, exception: str) -> None:
    assert exception in SCHEMA_MODEL_EXCEPTIONS
    import protocol.models as models

    with pytest.raises(PydanticValidationError):
        getattr(models, model_name).model_validate(payload)
    _validator(SCHEMA_BY_MODEL[model_name]).validate(payload)


@pytest.mark.parametrize("model_name", list(SCHEMA_BY_MODEL))
def test_memorybench_full_payloads_validate_under_strict_model_and_committed_schema(
    model_name: str,
) -> None:
    # The committed payloads carry `/owned/...`, which `protocol/models.py`
    # validates as an absolute *host* path -- true on Linux, not on Windows.
    require_posix_host_paths()
    _accepts_both(model_name, _memorybench_payloads()[model_name])


@pytest.mark.parametrize("model_name", list(SCHEMA_BY_MODEL))
def test_memorybench_models_and_schemas_both_reject_unknown_fields_and_wrong_literals(
    model_name: str,
) -> None:
    base = _memorybench_payloads()[model_name]
    unknown = copy.deepcopy(base)
    unknown["unknown"] = True
    _rejects_both(model_name, unknown)
    wrong = copy.deepcopy(base)
    wrong["artifact_type"] = "wrong.v1"
    _rejects_both(model_name, wrong)


@pytest.mark.parametrize(
    "source",
    ["/" + "local/dataset.json", "file:" + "///local/dataset.json", "C:" + "\\data\\set.json"],
)
def test_run_plan_model_and_schema_reject_local_dataset_sources(source: str) -> None:
    payload = _memorybench_payloads()["MemoryBenchRunPlan"]
    payload["dataset"]["source"] = source
    _rejects_both("MemoryBenchRunPlan", payload)


@pytest.mark.parametrize("mutation", ["work-outside", "evidence-equals-work", "wrong-pin"])
def test_run_plan_model_and_schema_reject_root_and_exact_harness_contradictions(
    mutation: str,
) -> None:
    payload = _memorybench_payloads()["MemoryBenchRunPlan"]
    if mutation == "work-outside":
        payload["guest_work_root"] = "/owned/outside/work"
        exception = "MemoryBenchRunPlan.output_root|guest_work_root|guest_evidence_root:relation"
    elif mutation == "evidence-equals-work":
        payload["guest_evidence_root"] = payload["guest_work_root"]
        exception = "MemoryBenchRunPlan.output_root|guest_work_root|guest_evidence_root:relation"
    else:
        payload["harness"]["commit"] = "0" * 40
        _rejects_both("MemoryBenchRunPlan", payload)
        return
    _rejects_model_only("MemoryBenchRunPlan", payload, exception)


@pytest.mark.parametrize("mutation", ["bad-root", "traversal", "unsorted", "duplicate"])
def test_export_model_and_schema_reject_bad_refs_and_noncanonical_arrays(mutation: str) -> None:
    payload = _memorybench_payloads()["MemoryBenchExport"]
    case = payload["cases"][0]
    if mutation == "bad-root":
        case["canonical_result"]["root"] = "filesystem"
    elif mutation == "traversal":
        case["private_gold"]["path"] = "private-gold/../secret.json"
    elif mutation == "unsorted":
        case["missing_fields"] = list(reversed(case["missing_fields"]))
    else:
        case["missing_fields"] = [*case["missing_fields"], case["missing_fields"][-1]]
    if mutation == "unsorted":
        _rejects_model_only(
            "MemoryBenchExport", payload, "MemoryBenchExport.cases.missing_fields:ordering"
        )
    else:
        _rejects_both("MemoryBenchExport", payload)


@pytest.mark.parametrize("mutation", ["phase-failure-code", "missing-result", "partial-without-failure"])
def test_export_model_and_schema_reject_phase_and_status_contradictions(mutation: str) -> None:
    payload = _memorybench_payloads()["MemoryBenchExport"]
    case = payload["cases"][0]
    if mutation == "phase-failure-code":
        case["phases"]["search"]["failure_code"] = "phase_failed"
    elif mutation == "missing-result":
        case["canonical_result"] = None
    else:
        payload["status"] = "partial"
    _rejects_both("MemoryBenchExport", payload)


@pytest.mark.parametrize("mutation", ["null-not-missing", "present-but-missing", "duplicate-ids"])
def test_private_gold_model_and_schema_reject_answer_session_null_matrix(mutation: str) -> None:
    payload = _memorybench_payloads()["MemoryBenchPrivateGold"]
    if mutation == "null-not-missing":
        payload["missing_fields"] = []
    elif mutation == "present-but-missing":
        payload["answer_session_ids"] = ["session-a"]
    else:
        payload["answer_session_ids"] = ["session-a", "session-a"]
        payload["missing_fields"] = []
    _rejects_both("MemoryBenchPrivateGold", payload)


@pytest.mark.parametrize("mutation", ["unsorted-targets", "duplicate-target", "unsorted-sources"])
def test_cleanup_plan_model_and_schema_reject_noncanonical_or_conflicting_targets(
    mutation: str,
) -> None:
    payload = _memorybench_payloads()["GuestCleanupPlan"]
    target = payload["targets"][0]
    if mutation == "unsorted-targets":
        other = copy.deepcopy(target)
        other["container_tag"] = "sorts-first"
        other["container_tag_hmac_sha256"] = _hmac("container-tag", "sorts-first")
        payload["targets"] = [other, target]
    elif mutation == "duplicate-target":
        payload["targets"] = [target, copy.deepcopy(target)]
    elif mutation == "unsorted-sources":
        target["discovery_sources"] = ["guest_evidence", "checkpoint"]
    exception = {
        "unsorted-targets": "GuestCleanupPlan.targets:ordering",
        "unsorted-sources": "GuestCleanupPlan.targets.discovery_sources:ordering",
    }.get(mutation)
    if exception:
        _rejects_model_only("GuestCleanupPlan", payload, exception)
    else:
        _rejects_both("GuestCleanupPlan", payload)


@pytest.mark.parametrize("mutation", ["forged-all-absent", "illegal-null-matrix", "already-absent-unproved"])
def test_cleanup_proof_model_and_schema_recompute_absence_and_applicability(mutation: str) -> None:
    payload = _memorybench_payloads()["GuestCleanup"]
    target = payload["targets"][0]
    if mutation == "forged-all-absent":
        target["outcome"] = "clear_failed"
        target["failure_code"] = "clear_failed"
        payload["failure_codes"] = ["clear_failed"]
    elif mutation == "illegal-null-matrix":
        target["absence"]["corpus"] = True
    else:
        target["outcome"] = "already_absent"
        target["absence"]["descriptor"] = False
    _rejects_both("GuestCleanup", payload)


def test_basic_cleanup_full_payload_validates_but_count_and_shared_surface_contradictions_do_not() -> None:
    payload = _memorybench_payloads()["GuestCleanup"]
    payload["provider"] = "basic-memory"
    payload["provider_variant"] = "basic-memory-controlled"
    target = payload["targets"][0]
    target["absence"] = {
        "namespace": True,
        "corpus": True,
        "config": None,
        "descriptor": None,
        "process_group": None,
        "work_root": None,
    }
    payload["basic_public_cleanup_calls"] = 1
    _accepts_both("GuestCleanup", payload)

    wrong_count = copy.deepcopy(payload)
    wrong_count["basic_public_cleanup_calls"] = 0
    wrong_count["all_absent"] = False
    _rejects_both("GuestCleanup", wrong_count)
    wrong_matrix = copy.deepcopy(payload)
    wrong_matrix["targets"][0]["absence"]["descriptor"] = True
    _rejects_both("GuestCleanup", wrong_matrix)


def test_schema_to_model_exception_registry_is_closed_to_genuine_in_document_relations() -> None:
    assert SCHEMA_MODEL_EXCEPTIONS == EXPECTED_SCHEMA_MODEL_EXCEPTIONS


@pytest.mark.parametrize(
    ("mode", "target_question_ids"),
    [("full", None), ("explicit", ["question-z", "question-a"])],
)
def test_run_plan_selection_union_accepts_full_or_ordered_explicit_selection(
    mode: str, target_question_ids: list[str] | None,
) -> None:
    # The committed payloads carry `/owned/...`, which `protocol/models.py`
    # validates as an absolute *host* path -- true on Linux, not on Windows.
    require_posix_host_paths()
    payload = _memorybench_payloads()["MemoryBenchRunPlan"]
    payload["selection"] = {"mode": mode, "target_question_ids": target_question_ids}
    _accepts_both("MemoryBenchRunPlan", payload)


@pytest.mark.parametrize(
    ("mode", "target_question_ids"),
    [
        ("full", []),
        ("full", ["question-a"]),
        ("explicit", None),
        ("explicit", []),
        ("explicit", ["question-a", "question-a"]),
        ("sample", ["question-a"]),
    ],
)
def test_run_plan_selection_union_rejects_null_empty_duplicate_and_unknown_combinations(
    mode: str, target_question_ids: list[str] | None,
) -> None:
    payload = _memorybench_payloads()["MemoryBenchRunPlan"]
    payload["selection"] = {"mode": mode, "target_question_ids": target_question_ids}
    _rejects_both("MemoryBenchRunPlan", payload)


@pytest.mark.parametrize(
    ("model_name", "field_path"),
    [
        ("MemoryBenchRunPlan", ("dataset_path",)),
        ("MemoryBenchRunPlan", ("provider_checkout", "root")),
        ("MemoryBenchRunPlan", ("memorybench_home",)),
        ("MemoryBenchRunPlan", ("output_root",)),
        ("MemoryBenchRunPlan", ("guest_work_root",)),
        ("MemoryBenchRunPlan", ("guest_evidence_root",)),
        ("GuestCleanupPlan", ("guest_work_root",)),
        ("GuestCleanupPlan", ("guest_evidence_root",)),
        ("GuestCleanupPlan", ("run_plan_path",)),
    ],
)
def test_model_and_schema_reject_every_relative_or_non_normalized_absolute_path_field(
    model_name: str, field_path: tuple[str, ...],
) -> None:
    for invalid in ("relative/path", "/owned/../escape"):
        payload = _memorybench_payloads()[model_name]
        owner = payload
        for part in field_path[:-1]:
            owner = owner[part]
        owner[field_path[-1]] = invalid
        _rejects_both(model_name, payload)


def test_private_gold_preserves_original_order_for_unique_answer_session_ids() -> None:
    payload = _memorybench_payloads()["MemoryBenchPrivateGold"]
    payload["answer_session_ids"] = ["session-z", "session-a"]
    payload["missing_fields"] = []
    _accepts_both("MemoryBenchPrivateGold", payload)


def test_private_gold_preserves_canonical_numeric_ground_truth() -> None:
    payload = _memorybench_payloads()["MemoryBenchPrivateGold"]
    payload["ground_truth"] = 2026
    _accepts_both("MemoryBenchPrivateGold", payload)


@pytest.mark.parametrize("invalid", [None, True, 1.5, ["answer"]])
def test_private_gold_rejects_noncanonical_ground_truth_types(invalid: object) -> None:
    payload = _memorybench_payloads()["MemoryBenchPrivateGold"]
    payload["ground_truth"] = invalid
    _rejects_both("MemoryBenchPrivateGold", payload)


def test_output_artifact_reference_rejects_an_empty_path_in_model_and_schema() -> None:
    payload = _memorybench_payloads()["MemoryBenchExport"]
    payload["cases"][0]["private_gold"]["path"] = ""
    _rejects_both("MemoryBenchExport", payload)


def test_cleanup_artifact_identity_and_order_include_path_hmac_as_the_fourth_closed_field() -> None:
    payload = _memorybench_payloads()["GuestCleanup"]
    first = {
        "root": "memorybench_run", "path": None,
        "path_hmac_sha256": "1" * 64, "sha256": "9" * 64,
    }
    second = {**first, "path_hmac_sha256": "2" * 64}
    payload["targets"][0]["artifacts"] = [first, second]
    _accepts_both("GuestCleanup", payload)

    reversed_payload = copy.deepcopy(payload)
    reversed_payload["targets"][0]["artifacts"] = [second, first]
    _rejects_model_only(
        "GuestCleanup", reversed_payload, "GuestCleanup.targets.artifacts:ordering"
    )


def _basic_cleanup_failure(*, calls: int) -> dict:
    payload = _memorybench_payloads()["GuestCleanup"]
    payload["provider"] = "basic-memory"
    payload["provider_variant"] = "basic-memory-controlled"
    target = payload["targets"][0]
    target["outcome"] = "absence_unproved"
    target["failure_code"] = "namespace_absence_unproved"
    target["absence"] = {
        "namespace": False,
        "corpus": False,
        "config": None,
        "descriptor": None,
        "process_group": None,
        "work_root": None,
    }
    payload["basic_public_cleanup_calls"] = calls
    payload["failure_codes"] = ["namespace_absence_unproved"]
    payload["all_absent"] = False
    return payload


@pytest.mark.parametrize("calls", [0, 1])
def test_basic_failed_cleanup_proofs_accept_the_observed_zero_or_one_finalization_count(
    calls: int,
) -> None:
    _accepts_both("GuestCleanup", _basic_cleanup_failure(calls=calls))


def test_basic_already_absent_uses_namespace_corpus_and_final_shared_surfaces() -> None:
    payload = _memorybench_payloads()["GuestCleanup"]
    payload["provider"] = "basic-memory"
    payload["provider_variant"] = "basic-memory-controlled"
    target = payload["targets"][0]
    target["outcome"] = "already_absent"
    target["absence"] = {
        "namespace": True,
        "corpus": True,
        "config": None,
        "descriptor": None,
        "process_group": None,
        "work_root": None,
    }
    payload["basic_public_cleanup_calls"] = 0
    _accepts_both("GuestCleanup", payload)


def test_basic_successful_cleared_and_all_already_absent_proofs_enforce_exact_counts() -> None:
    cleared = _memorybench_payloads()["GuestCleanup"]
    cleared["provider"] = "basic-memory"
    cleared["provider_variant"] = "basic-memory-controlled"
    cleared["targets"][0]["absence"] = {
        "namespace": True,
        "corpus": True,
        "config": None,
        "descriptor": None,
        "process_group": None,
        "work_root": None,
    }
    cleared["basic_public_cleanup_calls"] = 1
    _accepts_both("GuestCleanup", cleared)
    wrong_cleared = copy.deepcopy(cleared)
    wrong_cleared["basic_public_cleanup_calls"] = 0
    _rejects_both("GuestCleanup", wrong_cleared)

    already = copy.deepcopy(cleared)
    already["targets"][0]["outcome"] = "already_absent"
    already["basic_public_cleanup_calls"] = 0
    _accepts_both("GuestCleanup", already)
    wrong_already = copy.deepcopy(already)
    wrong_already["basic_public_cleanup_calls"] = 1
    _rejects_both("GuestCleanup", wrong_already)
