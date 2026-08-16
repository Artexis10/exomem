"""Red-first behavioural contract for the MemoryBench guest export runner."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from protocol.models import DatasetIdentity


PIN = "118209a746d97d0d85e5a7234267f0b6962857e9"
TREE = "2ee25bdbcb6bfaaecb32f917920c53775a299b37"
LOCK = "561d761fd16f895a6597227c6fc1e46064779284317fd479e079e3c86b9857da"
BASIC_PIN = "816accaa9befe8281668ba8819eaf74d11ce2385"
BASIC_TREE = "4f0255a31c609cad90dbf3b50e3d14a517e4566e"
DATASET = Path("tests/fixtures/memorybench-export/longmemeval_s_cleaned.json")
CHECKPOINT = Path("tests/fixtures/memorybench-export/checkpoint.json")
RESULT = Path("tests/fixtures/memorybench-export/results/q-01.json")
RAW_QID = "q-01"
RAW_TAG = "private-container-tag"
RAW_GOLD = "answer-secret"
HMAC_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
MODEL_ARTIFACT_OBLIGATIONS = frozenset({
    "private-file-no-follow-owner-mode",
    "resolved-root-containment-and-disjointness",
    "registered-plan-and-provider-variant",
    "checkout-tree-lock-identity",
    "public-hmac-and-referenced-byte-digests",
    "dataset-bytes-and-case-count",
    "selection-membership-order-and-checkpoint-target-agreement",
    "cleanup-plan-run-plan-digest-identity-root-and-target-hmac",
    "checkpoint-dataset-result-reconciliation-and-case-completeness",
    "cleanup-discovery-union",
    "descriptor-process-namespace-config-corpus-work-root-absence",
    "cleanup-proof-source-agreement",
    "public-artifact-privacy",
})

EXPECTED_MODEL_ARTIFACT_OBLIGATIONS = frozenset({
    "private-file-no-follow-owner-mode",
    "resolved-root-containment-and-disjointness",
    "registered-plan-and-provider-variant",
    "checkout-tree-lock-identity",
    "public-hmac-and-referenced-byte-digests",
    "dataset-bytes-and-case-count",
    "selection-membership-order-and-checkpoint-target-agreement",
    "cleanup-plan-run-plan-digest-identity-root-and-target-hmac",
    "checkpoint-dataset-result-reconciliation-and-case-completeness",
    "cleanup-discovery-union",
    "descriptor-process-namespace-config-corpus-work-root-absence",
    "cleanup-proof-source-agreement",
    "public-artifact-privacy",
})


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hmac(domain: str, value: str) -> str:
    return hmac.new(
        bytes.fromhex(HMAC_KEY_HEX), domain.encode() + b"\0" + value.encode(), hashlib.sha256
    ).hexdigest()


def test_cross_language_privacy_hmac_vectors_are_frozen() -> None:
    from memorybench.export import privacy_hmac_sha256

    vectors = [
        ("case-id", "q-01_abs", "94e872ad0278c5e760d5ff4a7f170e513c148711365fc3d72bc45b12fc90f131"),
        ("container-tag", "q-01-run-01", "97b7ccef0e2c66cba51712bac76a50a19832e709b9534d1677d0872342e6f852"),
        ("artifact-path", "results/q-01_abs.json", "196854f5bf555f5f96463b1d1b04fe931a66c81376dac1ce82c23891458f2396"),
    ]
    for domain, raw, expected in vectors:
        assert privacy_hmac_sha256(HMAC_KEY_HEX, domain, raw) == expected
    assert MODEL_ARTIFACT_OBLIGATIONS == EXPECTED_MODEL_ARTIFACT_OBLIGATIONS


def _plan_payload(tmp_path: Path, *, fresh_runtime: bool = True) -> dict[str, Any]:
    home = tmp_path / "memorybench"
    dataset = home / "data/benchmarks/longmemeval/datasets/longmemeval_s_cleaned.json"
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(DATASET.read_bytes())
    if not fresh_runtime:
        _materialize_native_runtime(home)
    provider_checkout = tmp_path / "provider-checkout"
    provider_checkout.mkdir()
    output = tmp_path / "output"
    identity = DatasetIdentity(
        id="longmemeval",
        variant="cleaned",
        source="https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
        revision="fixture-pin",
        sha256=_sha(dataset),
        case_count=1,
    )
    return {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "memorybench-run-plan.v1",
        "run_id": "run-01",
        "upstream_run_id": "run-01",
        "provider": "exomem",
        "provider_variant": "exomem-source-only",
        "benchmark": "longmemeval",
        "selection": {"mode": "full", "target_question_ids": None},
        "harness": {
            "repository": "https://github.com/supermemoryai/memorybench",
            "commit": PIN,
            "tree": TREE,
            "bun_lock_sha256": LOCK,
        },
        "dataset": identity.model_dump(),
        "dataset_path": str(dataset),
        "provider_checkout": {
            "root": str(provider_checkout),
            "repository": "https://github.com/hugoa/exomem",
            "commit": "a" * 40,
            "tree": "b" * 40,
            "lock_sha256": "c" * 64,
        },
        "memorybench_home": str(home),
        "output_root": str(output),
        "guest_work_root": str(output / "guest-work"),
        "guest_evidence_root": str(output / "guest-evidence"),
        "contract_revision": "7cd15e6d6c67eb914e4f57bd943f98f7d1894b7f",
        "preregistration_sha256": "21aa5a8815038b82358336798b10afd8d3ffbd9739c8da597955bd14d8d962e3",
        "privacy_hmac_key_hex": HMAC_KEY_HEX,
    }


def _materialize_native_runtime(memorybench_home: Path) -> None:
    dataset = memorybench_home / "data/benchmarks/longmemeval/datasets/longmemeval_s_cleaned.json"
    questions = dataset.parent / "questions"
    questions.mkdir(parents=True, exist_ok=True)
    for row in json.loads(dataset.read_text(encoding="utf-8")):
        (questions / f"{row['question_id']}.json").write_text(
            json.dumps(row, sort_keys=True) + "\n", encoding="utf-8"
        )
    run = memorybench_home / "data/runs/run-01"
    (run / "results").mkdir(parents=True, exist_ok=True)
    (run / "checkpoint.json").write_bytes(CHECKPOINT.read_bytes())
    (run / "results/q-01.json").write_bytes(RESULT.read_bytes())


def _plan(tmp_path: Path, *, fresh_runtime: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = _plan_payload(tmp_path, fresh_runtime=fresh_runtime)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _fresh_plan(tmp_path: Path) -> Path:
    return _plan(tmp_path, fresh_runtime=True)


def _run(plan: Path, **kwargs: Any):
    from memorybench.export import run_export

    return run_export(plan, **kwargs)


def _stage(returncode: int = 0) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _materialize_native_runtime(Path(kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, returncode, "", "")

    return run


def _fresh_stage(returncode: int = 0) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _materialize_native_runtime(Path(kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, returncode, "", "")

    return run


def _artifact_ref(output: Path, relative: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return {"root": "output", "path": relative, "path_hmac_sha256": None, "sha256": _sha(path)}


def _full_cleanup_proof(
    output: Path,
    *,
    trigger: str = "success",
    provider: str = "exomem",
    all_absent: bool = True,
) -> dict[str, Any]:
    exomem = provider == "exomem"
    absence = {
        "namespace": all_absent,
        "corpus": None if exomem else all_absent,
        "config": None,
        "descriptor": all_absent if exomem else None,
        "process_group": all_absent if exomem else None,
        "work_root": all_absent if exomem else None,
    }
    target_ref = _artifact_ref(
        output,
        f"cleanup-evidence/target-{_hmac('container-tag', RAW_TAG)}.json",
        {
            "protocol_version": "1.0.0",
            "artifact_type": "guest-cleanup-observation.v1",
            "run_id": "run-01",
            "provider": provider,
            "provider_variant": "exomem-source-only" if exomem else "basic-memory-controlled",
            "scope": "target",
            "container_tag_hmac_sha256": _hmac("container-tag", RAW_TAG),
            "operation_result": "clear_succeeded",
            "operation_failure_code": None,
            "basic_finalization_calls": 0 if exomem else 1,
            "surfaces": {
                key: None if value is None else "absent" if value else "not_absent"
                for key, value in absence.items()
            },
            "process_binding": None,
        },
    )
    final_ref = _artifact_ref(
        output,
        "cleanup-evidence/final.json",
        {
            "protocol_version": "1.0.0",
            "artifact_type": "guest-cleanup-observation.v1",
            "run_id": "run-01",
            "provider": provider,
            "provider_variant": "exomem-source-only" if exomem else "basic-memory-controlled",
            "scope": "final",
            "container_tag_hmac_sha256": None,
            "operation_result": "final_probe",
            "operation_failure_code": None,
            "basic_finalization_calls": 0 if exomem else 1,
            "surfaces": {
                "namespace": None,
                "corpus": None,
                "config": "absent" if all_absent else "not_absent",
                "descriptor": "absent" if all_absent else "not_absent",
                "process_group": "absent" if all_absent else "not_absent",
                "work_root": "absent" if all_absent else "not_absent",
            },
            "process_binding": None,
        },
    )
    return {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "guest-cleanup.v1",
        "run_id": "run-01",
        "provider": provider,
        "provider_variant": "exomem-source-only" if exomem else "basic-memory-controlled",
        "trigger": trigger,
        "targets": [
            {
                "container_tag_hmac_sha256": _hmac("container-tag", RAW_TAG),
                "discovery_sources": ["checkpoint"],
                "outcome": "cleared" if all_absent else "absence_unproved",
                "failure_code": None if all_absent else "namespace_absence_unproved",
                "artifacts": [target_ref],
                "absence": absence,
            }
        ],
        "basic_public_cleanup_calls": 0 if exomem else 1,
        "failure_codes": [] if all_absent else ["namespace_absence_unproved"],
        "final_absence": {
            "config": all_absent,
            "descriptor": all_absent,
            "process_group": all_absent,
            "work_root": all_absent,
            "artifacts": [final_ref],
        },
        "all_absent": all_absent,
    }


def _cleanup_proof_for_targets(
    output: Path,
    cleanup_plan: dict[str, Any],
    *,
    provider: str,
    trigger: str,
) -> dict[str, Any]:
    exomem = provider == "exomem"
    targets = []
    for position, target in enumerate(cleanup_plan["targets"], start=1):
        reference = _artifact_ref(
            output,
            f"cleanup-evidence/feedback3-target-{position}.json",
            {"protocol_version": 1, "arbitrary_status": "absent"},
        )
        targets.append({
            "container_tag_hmac_sha256": target["container_tag_hmac_sha256"],
            "discovery_sources": target["discovery_sources"],
            "outcome": "cleared",
            "failure_code": None,
            "artifacts": [reference],
            "absence": {
                "namespace": True,
                "corpus": None if exomem else True,
                "config": None,
                "descriptor": True if exomem else None,
                "process_group": True if exomem else None,
                "work_root": True if exomem else None,
            },
        })
    final_reference = _artifact_ref(
        output,
        "cleanup-evidence/feedback3-final.json",
        {"protocol_version": 1, "arbitrary_status": "all_absent"},
    )
    return {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "guest-cleanup.v1",
        "run_id": cleanup_plan["run_id"],
        "provider": provider,
        "provider_variant": cleanup_plan["provider_variant"],
        "trigger": trigger,
        "targets": targets,
        "basic_public_cleanup_calls": 0 if exomem or not targets else 1,
        "failure_codes": [],
        "final_absence": {
            "config": True,
            "descriptor": True,
            "process_group": True,
            "work_root": True,
            "artifacts": [final_reference],
        },
        "all_absent": True,
    }


def _empty_cleanup_proof(output: Path, *, trigger: str = "success") -> dict[str, Any]:
    final_ref = _artifact_ref(
        output,
        "cleanup-evidence/final.json",
        {
            "protocol_version": "1.0.0",
            "artifact_type": "guest-cleanup-observation.v1",
            "run_id": "run-01",
            "provider": "exomem",
            "provider_variant": "exomem-source-only",
            "scope": "final",
            "container_tag_hmac_sha256": None,
            "operation_result": "final_probe",
            "operation_failure_code": None,
            "basic_finalization_calls": 0,
            "surfaces": {
                "namespace": None,
                "corpus": None,
                "config": "absent",
                "descriptor": "absent",
                "process_group": "absent",
                "work_root": "absent",
            },
            "process_binding": None,
        },
    )
    return {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "guest-cleanup.v1",
        "run_id": "run-01",
        "provider": "exomem",
        "provider_variant": "exomem-source-only",
        "trigger": trigger,
        "targets": [],
        "basic_public_cleanup_calls": 0,
        "failure_codes": [],
        "final_absence": {
            "config": True,
            "descriptor": True,
            "process_group": True,
            "work_root": True,
            "artifacts": [final_ref],
        },
        "all_absent": True,
    }


def _cleanup(output: Path, *, trigger: str = "success", all_absent: bool = True):
    def run(cleanup_plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert cleanup_plan["artifact_type"] == "guest-cleanup-plan.v1"
        expected = [
            {
                "container_tag": RAW_TAG,
                "container_tag_hmac_sha256": _hmac("container-tag", RAW_TAG),
                "discovery_sources": ["checkpoint"],
                "namespace_expected": True,
            }
        ]
        if cleanup_plan["targets"] == []:
            assert all_absent, "an unproved empty discovery union cannot claim success"
            return _empty_cleanup_proof(output, trigger=kwargs.get("trigger", trigger))
        assert cleanup_plan["targets"] == expected
        return _full_cleanup_proof(
            output, trigger=kwargs.get("trigger", trigger), all_absent=all_absent
        )

    return run


def _valid_dependencies(tmp_path: Path) -> dict[str, Any]:
    return {
        "checkout_verifier": lambda **_bound: "materialized",
        "provider_checkout_verifier": lambda _identity: None,
        "stage_runner": _stage(),
        "cleanup_runner": _cleanup(tmp_path / "output"),
    }


def test_secure_read_primitive_rejects_relative_symlink_wrong_owner_mode_and_writable_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memorybench.export import _secure_read

    plan = _plan(tmp_path)
    with pytest.raises(Exception):
        _secure_read(Path("relative-plan.json"), private=True)

    plan.chmod(0o644)
    with pytest.raises(Exception):
        _secure_read(plan, private=True)
    plan.chmod(0o600)

    link = tmp_path / "link.json"
    link.symlink_to(plan)
    with pytest.raises(Exception):
        _secure_read(link, private=True)

    import memorybench.export as export

    monkeypatch.setattr(export.os, "getuid", lambda: plan.stat().st_uid + 1)
    with pytest.raises(Exception, match="owner"):
        _secure_read(plan, private=True)
    monkeypatch.undo()

    tmp_path.chmod(0o770)
    try:
        with pytest.raises(Exception, match="parent|writable"):
            _secure_read(plan, private=True)
    finally:
        tmp_path.chmod(0o700)


def test_full_canonical_memorybench_plan_is_valid_and_unpinned(tmp_path: Path) -> None:
    from equivalence.selection import CANONICAL_LME_S_SOURCE
    from memorybench.export import _canonical_selection_pins
    from protocol.models import MemoryBenchRunPlan

    payload = _plan_payload(tmp_path)
    payload["selection"] = {"mode": "full", "target_question_ids": None}
    payload["dataset"] = {
        "id": "longmemeval",
        "variant": "LongMemEval-S cleaned September 2025",
        "source": CANONICAL_LME_S_SOURCE["repository"],
        "revision": CANONICAL_LME_S_SOURCE["revision"],
        "sha256": CANONICAL_LME_S_SOURCE["sha256"],
        "case_count": CANONICAL_LME_S_SOURCE["row_count"],
    }
    plan = MemoryBenchRunPlan.model_validate(payload)

    assert _canonical_selection_pins(plan, []) == {}


def test_noncanonical_explicit_twenty_five_case_memorybench_plan_is_refused(tmp_path: Path) -> None:
    from memorybench.export import _canonical_selection_pins
    from protocol.models import MemoryBenchRunPlan

    payload = _plan_payload(tmp_path)
    ids = [f"fixture-{index}" for index in range(25)]
    payload["selection"] = {"mode": "explicit", "target_question_ids": ids}
    plan = MemoryBenchRunPlan.model_validate(payload)

    with pytest.raises(ValueError, match="25-case comparative tier"):
        _canonical_selection_pins(
            plan,
            [{"question_id": question_id, "question_type": "multi-session"} for question_id in ids],
        )


def test_cli_has_strict_plan_only_surface_and_no_fixture_fault_switch(tmp_path: Path) -> None:
    from memorybench.export import main

    plan = _plan(tmp_path)
    with pytest.raises(SystemExit):
        main(["--plan", str(plan), "--export-failure"])
    with pytest.raises(SystemExit):
        main(["--plan", str(plan), "--stage", "answer"])


def test_preflight_binds_real_setup_provider_checkout_and_dataset_before_first_stage(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    payload = json.loads(plan.read_text())
    calls: dict[str, Any] = {}

    def checkout_verifier(**bound: Any) -> str:
        calls["checkout"] = bound
        return "materialized"

    def provider_checkout_verifier(identity: dict[str, Any]) -> None:
        calls["provider"] = identity

    def dataset_verifier(path: Path, identity: dict[str, Any]) -> None:
        calls["dataset"] = (path, identity)
        assert _sha(path) == identity["sha256"]
        assert len(json.loads(path.read_text())) == identity["case_count"]

    stage_calls: list[dict[str, Any]] = []

    def stage(argv: list[str], *, cwd: Path, env: dict[str, str], start_new_session: bool):
        _materialize_native_runtime(cwd)
        stage_calls.append(
            {"argv": argv, "cwd": cwd, "env": env, "start_new_session": start_new_session}
        )
        manifest = json.loads((tmp_path / "output/manifest.json").read_text())
        assert manifest["status"] == "started"
        assert manifest["schema_version"] == 2
        assert manifest["preregistration_identity"]["original"]["sha256"] == payload["preregistration_sha256"]
        assert (tmp_path / "output/ledger.jsonl").read_bytes() == b""
        return subprocess.CompletedProcess(argv, 0)

    result = _run(
        plan,
        checkout_verifier=checkout_verifier,
        provider_checkout_verifier=provider_checkout_verifier,
        dataset_verifier=dataset_verifier,
        stage_runner=stage,
        cleanup_runner=_cleanup(tmp_path / "output"),
    )

    assert result.status == "VALID" and result.exit_code == 0
    assert calls["checkout"] == {
        "memorybench_home": Path(payload["memorybench_home"]),
        "expected_commit": PIN,
        "expected_tree": TREE,
        "expected_bun_lock_sha256": LOCK,
    }
    assert calls["provider"] == payload["provider_checkout"]
    assert calls["dataset"] == (Path(payload["dataset_path"]), payload["dataset"])
    assert Path(stage_calls[0]["argv"][0]).is_absolute()
    assert [call["argv"][1:] for call in stage_calls] == [
        ["run", "src/cli/commands/competitive-ingest.ts", "--plan", str(plan), "--plan-sha256", _sha(plan)],
        ["run", "src/index.ts", "search", "-r", "run-01"],
    ]
    assert all(call["cwd"] == tmp_path / "memorybench" for call in stage_calls)
    assert all(call["start_new_session"] is True for call in stage_calls)
    assert all(call["env"]["MEMORYBENCH_GUEST_WORK_ROOT"] == str(tmp_path / "output/guest-work") for call in stage_calls)
    assert all(call["env"]["MEMORYBENCH_GUEST_EVIDENCE_ROOT"] == str(tmp_path / "output/guest-evidence") for call in stage_calls)
    assert all("OPENAI_API_KEY" not in call["env"] for call in stage_calls)
    terminal_manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert terminal_manifest["preregistration_identity"]["contract_revision"] == payload["contract_revision"]


def test_preregistration_plan_digest_is_only_an_assertion_against_derived_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from memorybench.export import main

    plan = _plan(tmp_path)
    payload = json.loads(plan.read_text())
    payload["preregistration_sha256"] = "d" * 64
    plan.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    plan.chmod(0o600)
    stages: list[object] = []

    result = _run(plan, stage_runner=lambda *args, **kwargs: stages.append((args, kwargs)))

    assert result.status == "BLOCKED" and result.exit_code == 2
    assert stages == []
    assert not Path(payload["output_root"]).exists()
    assert main(["--plan", str(plan)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_production_default_calls_the_real_setup_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memorybench.export as export

    plan = _plan(tmp_path)
    observed: list[tuple[Path, dict[str, Any]]] = []

    def verified(checkout: Path, **kwargs: Any) -> str:
        observed.append((checkout, kwargs))
        return "materialized"

    monkeypatch.setattr(export, "verify_checkout", verified)
    result = export.run_export(
        plan,
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_fresh_stage(),
        cleanup_runner=_cleanup(tmp_path / "output"),
    )
    assert result.status == "VALID"
    assert observed and observed[0][0] == tmp_path / "memorybench"


@pytest.mark.parametrize(
    "alter",
    ["case-count", "dataset-bytes", "harness-pin", "provider-checkout", "provider-variant", "pristine", "existing-output"],
)
def test_preflight_refuses_identity_drift_as_blocked_without_provider_work(
    tmp_path: Path, alter: str
) -> None:
    plan = _plan(tmp_path)
    payload = json.loads(plan.read_text())
    if alter == "case-count":
        payload["dataset"]["case_count"] = 2
    elif alter == "dataset-bytes":
        Path(payload["dataset_path"]).write_bytes(DATASET.read_bytes() + b"\n")
    elif alter == "harness-pin":
        payload["harness"]["tree"] = "0" * 40
    elif alter == "provider-variant":
        payload["provider_variant"] = "unregistered"
    elif alter == "existing-output":
        Path(payload["output_root"]).mkdir()
    plan.write_text(json.dumps(payload, sort_keys=True) + "\n")

    stage_calls: list[list[str]] = []

    def provider_verify(_identity: dict[str, Any]) -> None:
        if alter == "provider-checkout":
            raise RuntimeError("provider checkout binding refused")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "pristine" if alter == "pristine" else "materialized",
        provider_checkout_verifier=provider_verify,
        stage_runner=lambda argv, **_kwargs: stage_calls.append(argv),
    )
    assert result.status == "BLOCKED" and result.exit_code == 2
    assert stage_calls == []


def test_nonzero_stage_exit_is_not_failure_when_validated_artifacts_are_complete(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_stage(1),
        cleanup_runner=_cleanup(tmp_path / "output"),
    )
    assert result.status == "VALID" and result.exit_code == 0
    export = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert "stage_process_failed" not in export["failure_codes"]


def test_zero_stage_exit_does_not_override_missing_evidence(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    calls = 0

    def stage(argv: list[str], **_kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            _materialize_native_runtime(tmp_path / "memorybench")
        if calls == 2:
            (tmp_path / "memorybench/data/runs/run-01/results/q-01.json").unlink()
        return subprocess.CompletedProcess(argv, 0)

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=_cleanup(tmp_path / "output"),
    )
    assert result.status == "INVALID" and result.exit_code == 1
    export = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert export["status"] == "partial" and "result_missing" in export["failure_codes"]


def test_stage_exception_is_failure_even_when_fixture_artifacts_are_complete(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    def stage(_argv: list[str], **kwargs: Any) -> None:
        _materialize_native_runtime(Path(kwargs["cwd"]))
        raise RuntimeError("stage transport failed")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=_cleanup(tmp_path / "output", trigger="stage_failure"),
    )
    assert result.status == "INVALID" and result.exit_code == 1
    export = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert "stage_process_failed" in export["failure_codes"]


@pytest.mark.parametrize(
    ("mutation", "failure_code"),
    [
        ("missing-checkpoint", "checkpoint_missing"),
        ("missing-result", "result_missing"),
        ("duplicate-result", "result_duplicate"),
        ("extra-result", "case_set_mismatch"),
        ("inline-mismatch", "checkpoint_result_mismatch"),
        ("nonfinite", "result_invalid"),
        ("symlink-result", "result_outside_root"),
        ("malformed-checkpoint", "checkpoint_invalid"),
        ("malformed-result", "result_invalid"),
        ("outside-result-file", "result_outside_root"),
        ("question-mismatch", "result_identity_mismatch"),
        ("type-mismatch", "result_identity_mismatch"),
        ("gold-mismatch", "result_identity_mismatch"),
        ("container-mismatch", "checkpoint_result_mismatch"),
        ("phase-incomplete", "phase_incomplete"),
        ("phase-failed", "phase_failed"),
    ],
)
def test_corrupt_conflicting_or_outside_evidence_is_partial_invalid_without_precedence(
    tmp_path: Path, mutation: str, failure_code: str
) -> None:
    plan = _plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        run = home / "data/runs/run-01"
        checkpoint_path = run / "checkpoint.json"
        result_path = run / "results/q-01.json"
        if mutation == "missing-checkpoint":
            checkpoint_path.unlink()
        elif mutation == "missing-result":
            result_path.unlink()
        elif mutation == "duplicate-result":
            (run / "results/duplicate.json").write_bytes(RESULT.read_bytes())
        elif mutation == "extra-result":
            extra = json.loads(RESULT.read_text())
            extra["questionId"] = "extra"
            (run / "results/extra.json").write_text(json.dumps(extra))
        elif mutation == "inline-mismatch":
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["questions"][0]["results"] = [{"content": "different", "score": 1.0}]
            checkpoint_path.write_text(json.dumps(checkpoint))
        elif mutation == "nonfinite":
            result = json.loads(result_path.read_text())
            result["results"][0]["score"] = float("inf")
            result_path.write_text(json.dumps(result))
        elif mutation == "symlink-result":
            external = tmp_path / "outside-result.json"
            external.write_bytes(RESULT.read_bytes())
            result_path.unlink()
            result_path.symlink_to(external)
        elif mutation == "malformed-checkpoint":
            checkpoint_path.write_text("{")
        elif mutation == "malformed-result":
            result_path.write_text("{")
        elif mutation == "outside-result-file":
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["questions"][0]["resultFile"] = "../../outside.json"
            checkpoint_path.write_text(json.dumps(checkpoint))
        elif mutation in {"question-mismatch", "type-mismatch", "gold-mismatch", "container-mismatch"}:
            result = json.loads(result_path.read_text())
            key, value = {
                "question-mismatch": ("question", "different question"),
                "type-mismatch": ("questionType", "different-type"),
                "gold-mismatch": ("groundTruth", "different gold"),
                "container-mismatch": ("containerTag", "different-container"),
            }[mutation]
            result[key] = value
            result_path.write_text(json.dumps(result))
        else:
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["questions"][0]["phases"]["search"] = {
                "status": "pending" if mutation == "phase-incomplete" else "failed"
            }
            checkpoint_path.write_text(json.dumps(checkpoint))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "INVALID"
    public_text = (tmp_path / "output/memorybench-export.v1.json").read_text()
    public = json.loads(public_text)
    assert public["status"] == "partial"
    assert failure_code in public["failure_codes"]
    assert RAW_GOLD not in public_text and RAW_TAG not in public_text and RAW_QID not in public_text


def test_canonical_result_discovery_ignores_benign_checkpoint_path_strings(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _materialize_native_runtime(Path(kwargs["cwd"]))
        checkpoint_path = Path(kwargs["cwd"]) / "data/runs/run-01/checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint["questions"][0]["resultFile"] = "legacy/arbitrary-name.json"
        checkpoint_path.write_text(json.dumps(checkpoint))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "VALID"
    case = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())["cases"][0]
    assert case["hits"] == [{"content": "first", "score": 0.9}, {"content": "second", "score": 0.1}]


def test_checkpoint_top_level_status_is_not_completeness_evidence(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _materialize_native_runtime(Path(kwargs["cwd"]))
        checkpoint_path = Path(kwargs["cwd"]) / "data/runs/run-01/checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint["status"] = "failed"
        checkpoint_path.write_text(json.dumps(checkpoint))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "VALID"


def test_checkpoint_absence_uses_nullable_unobserved_evidence_and_never_infers_state(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _materialize_native_runtime(Path(kwargs["cwd"]))
        (Path(kwargs["cwd"]) / "data/runs/run-01/checkpoint.json").unlink()
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "INVALID"
    case = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())["cases"][0]
    assert case["checkpoint"] is None
    assert case["container_tag_hmac_sha256"] is None
    assert all(phase == {"status": "unobserved", "failure_code": None} for phase in case["phases"].values())
    assert case["hits"] == []


def test_cleanup_discovery_unions_nonpending_checkpoint_and_validated_basic_evidence(
    tmp_path: Path,
) -> None:
    import memorybench.export as export
    from protocol.models import MemoryBenchRunPlan

    payload = _plan_payload(tmp_path)
    payload["provider"] = "basic-memory"
    payload["provider_variant"] = "basic-memory-controlled"
    payload["provider_checkout"]["repository"] = "https://github.com/basicmachines-co/basic-memory"
    plan = MemoryBenchRunPlan.model_validate(payload)
    evidence = Path(plan.guest_evidence_root) / "basic-memory"
    evidence.mkdir(parents=True, mode=0o700)
    second_tag = "evidence-only-container"
    entries = [
        {
            "protocol_version": 1,
            "event": "request",
            "recorded_at_utc": "2026-01-02T00:00:00Z",
            "data": {
                "route": "/v1/search",
                "body": {
                    "protocol_version": 1,
                    "request_id": "request-1",
                    "container_tag": second_tag,
                    "query": "q",
                    "limit": 1,
                },
            },
        },
        {
            "protocol_version": 1,
            "event": "response",
            "recorded_at_utc": "2026-01-02T00:00:01Z",
            "data": {
                "route": "/v1/ingest",
                "response": {
                    "document_id": "doc-1",
                    "namespace": "ns-1",
                    "readiness": {
                        "protocol_version": 1,
                        "verified": True,
                        "container_tag": RAW_TAG,
                        "document_id": "doc-1",
                        "rendered_sha256": "a" * 64,
                        "fallback_detected": False,
                        "evidence_refs": [{"path": "receipt.json", "sha256": "b" * 64}],
                    },
                },
            },
        },
    ]
    for index, entry in enumerate(entries, start=1):
        path = evidence / f"operation-{index:06d}-{'a' * 12}.json"
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        path.chmod(0o600)
    checkpoint = json.loads(CHECKPOINT.read_text())
    targets = export._cleanup_target_union(
        plan, {RAW_QID: checkpoint["questions"][0]}
    )
    by_tag = {target["container_tag"]: target for target in targets}
    assert by_tag[RAW_TAG]["discovery_sources"] == ["checkpoint", "guest_evidence"]
    assert by_tag[RAW_TAG]["namespace_expected"] is True
    assert by_tag[second_tag]["discovery_sources"] == ["guest_evidence"]
    assert by_tag[second_tag]["namespace_expected"] is False
    assert [target["container_tag_hmac_sha256"] for target in targets] == sorted(
        target["container_tag_hmac_sha256"] for target in targets
    )


def test_cleanup_discovery_accepts_only_plan_bound_secure_exomem_descriptors(
    tmp_path: Path,
) -> None:
    import memorybench.export as export
    from protocol.models import MemoryBenchRunPlan

    plan = MemoryBenchRunPlan.model_validate(_plan_payload(tmp_path))
    raw_tag = "descriptor-only-container"
    directory = Path(plan.guest_work_root) / "services" / "exomem" / _sha_text(raw_tag)[:24]
    directory.mkdir(parents=True, mode=0o700)
    descriptor = {
        "protocol_version": 1,
        "provider": "exomem",
        "base_url": "http://127.0.0.1:12345",
        "bearer_token": "private-token",
        "pid": 123,
        "process_start_identity": "linux-proc-v1:123:456",
        "checkout_pin": plan.provider_checkout.commit,
        "checkout_root": plan.provider_checkout.root,
        "work_root": str(directory),
        "evidence_root": str(Path(plan.guest_evidence_root) / "exomem" / directory.name),
        "container_tag": raw_tag,
        "vault_root": str(directory / "vault"),
        "instance_id": "fixture-instance",
    }
    descriptor_path = directory / "service.v1.json"
    descriptor_path.write_text(json.dumps(descriptor) + "\n", encoding="utf-8")
    descriptor_path.chmod(0o600)
    targets = export._cleanup_target_union(plan, {})
    assert targets == [{
        "container_tag": raw_tag,
        "container_tag_hmac_sha256": _hmac("container-tag", raw_tag),
        "discovery_sources": ["secure_descriptor"],
        "namespace_expected": True,
    }]

    descriptor["checkout_pin"] = "0" * 40
    descriptor_path.write_text(json.dumps(descriptor) + "\n", encoding="utf-8")
    descriptor_path.chmod(0o600)
    with pytest.raises(Exception, match="descriptor|binding"):
        export._cleanup_target_union(plan, {})


def test_public_projection_keeps_original_hit_order_and_private_gold_is_exactly_protected(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    result = _run(plan, **_valid_dependencies(tmp_path))
    assert result.status == "VALID"
    public_path = tmp_path / "output/memorybench-export.v1.json"
    public_bytes = public_path.read_bytes()
    public_text = public_bytes.decode("utf-8")
    public = json.loads(public_text)
    case = public["cases"][0]

    for private_value in (RAW_QID, RAW_TAG, RAW_GOLD, str(tmp_path)):
        assert private_value not in public_text
    for oracle in (_sha_text(RAW_QID), _sha_text(RAW_TAG), _sha_text("results/q-01.json"), HMAC_KEY_HEX):
        assert oracle not in public_text
    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()

    assert not {"questionId", "containerTag", "groundTruth", "answer"} & keys(public)
    assert case["case_id_hmac_sha256"] == _hmac("case-id", RAW_QID)
    assert case["hits"] == [{"content": "first", "score": 0.9}, {"content": "second", "score": 0.1}]
    assert case["missing_fields"] == sorted(
        [
            "gold.answer_session_ids",
            "ingest.transmitted_payloads",
            "search.http_status",
            "search.normalized_hit_ids",
            "search.normalized_ranks",
            "search.normalized_scores",
            "search.options.limit",
            "search.options.threshold",
            "search.retry_attempts",
            "search.transmitted_query",
        ]
    )
    assert public["latency"] == {"publishable": False, "reason": "host_unvalidated"}

    gold_ref = case["private_gold"]
    expected_relative = f"private-gold/{_hmac('case-id', RAW_QID)}.json"
    assert gold_ref["root"] == "output" and gold_ref["path"] == expected_relative
    assert gold_ref["path_hmac_sha256"] is None
    gold = tmp_path / "output" / expected_relative
    assert gold_ref["sha256"] == _sha(gold)
    assert stat.S_IMODE(gold.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(gold.stat().st_mode) == 0o600
    private = json.loads(gold.read_text())
    assert private["question_id"] == RAW_QID
    assert private["container_tag"] == RAW_TAG
    assert private["ground_truth"] == RAW_GOLD
    assert private["answer_session_ids"] is None
    assert private["missing_fields"] == ["gold.answer_session_ids"]
    assert private["checkpoint_path"] == "checkpoint.json"
    assert private["canonical_result_path"] == "results/q-01.json"
    assert case["checkpoint"]["path"] is None
    assert case["checkpoint"]["path_hmac_sha256"] == _hmac("artifact-path", "checkpoint.json")
    assert case["canonical_result"]["path"] is None
    assert case["canonical_result"]["path_hmac_sha256"] == _hmac(
        "artifact-path", "results/q-01.json"
    )
    assert case["checkpoint"]["sha256"] == _sha(tmp_path / "memorybench/data/runs/run-01/checkpoint.json")
    assert case["canonical_result"]["sha256"] == _sha(tmp_path / "memorybench/data/runs/run-01/results/q-01.json")


@pytest.mark.parametrize(
    "forgery",
    [
        "hit", "private-ref-digest", "source-ref-digest", "source-path-hmac",
        "completeness", "privacy", "case-digest", "container-hmac",
    ],
)
def test_export_validator_rereads_plan_sources_and_private_members_to_reject_forgery(
    tmp_path: Path, forgery: str
) -> None:
    plan = _plan(tmp_path)
    _run(plan, **_valid_dependencies(tmp_path))
    from memorybench.export import validate_export

    public = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    if forgery == "hit":
        public["cases"][0]["hits"][0]["score"] = 99.0
    elif forgery == "private-ref-digest":
        public["cases"][0]["private_gold"]["sha256"] = "0" * 64
    elif forgery == "source-ref-digest":
        public["cases"][0]["canonical_result"]["sha256"] = "0" * 64
    elif forgery == "source-path-hmac":
        public["cases"][0]["canonical_result"]["path_hmac_sha256"] = "0" * 64
    elif forgery == "completeness":
        public["cases"][0]["canonical_result"] = None
    elif forgery == "privacy":
        public["privacy"]["contains_ground_truth"] = True
    elif forgery == "case-digest":
        public["cases"][0]["case_id_hmac_sha256"] = "0" * 64
    else:
        public["cases"][0]["container_tag_hmac_sha256"] = "0" * 64
    with pytest.raises(Exception):
        validate_export(public, run_plan_path=plan)


def _atomic_writer(events: list[str], fail_name: str | None = None):
    def write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
        events.append(f"write:{path.name}")
        if path.name == fail_name:
            raise OSError("injected writer secret must be sanitized")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(payload)
        path.chmod(mode)

    return write


def _manifest_finalizer(events: list[str], *, fail: bool = False):
    def finalize(path: Path, payload: dict[str, Any]) -> None:
        events.append("finalize-manifest")
        if fail:
            raise OSError("injected finalizer secret must be sanitized")
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)

    return finalize


def test_export_cleanup_proof_and_final_manifest_have_one_strict_ordering_path(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    events: list[str] = []

    def cleanup(plan_payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        events.append("cleanup")
        return _full_cleanup_proof(tmp_path / "output")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_stage(),
        cleanup_runner=cleanup,
        atomic_writer=_atomic_writer(events),
        manifest_finalizer=_manifest_finalizer(events),
    )
    assert result.status == "VALID" and result.exit_code == 0
    export_write = events.index("write:memorybench-export.v1.json")
    cleanup_call = events.index("cleanup")
    proof_write = events.index("write:guest-cleanup.v1.json")
    final_manifest = events.index("finalize-manifest")
    assert export_write < cleanup_call < proof_write < final_manifest


@pytest.mark.parametrize(
    ("fault", "expected_exit"),
    [("memorybench-export.v1.json", 1), ("guest-cleanup.v1.json", 3), ("finalizer", 1)],
)
def test_writer_and_finalizer_faults_never_fabricate_a_terminal_manifest(
    tmp_path: Path, fault: str, expected_exit: int
) -> None:
    plan = _plan(tmp_path)
    events: list[str] = []
    cleanup_calls: list[str] = []

    def cleanup(_plan: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        cleanup_calls.append("called")
        return _full_cleanup_proof(
            tmp_path / "output",
            trigger="export_failure" if fault == "memorybench-export.v1.json" else "success",
        )

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_stage(),
        cleanup_runner=cleanup,
        atomic_writer=_atomic_writer(events, None if fault == "finalizer" else fault),
        manifest_finalizer=_manifest_finalizer(events, fail=fault == "finalizer"),
    )
    assert cleanup_calls == ["called"]
    assert result.status == "INVALID" and result.exit_code == expected_exit
    durable = json.loads((tmp_path / "output/manifest.json").read_text())
    assert durable["status"] == "started" and durable["finalized_at"] is None


class _FakeSignals:
    def __init__(self) -> None:
        self.handler: Callable[[int, object | None], None] | None = None
        self.delivered: list[int] = []

    def install(self, handler: Callable[[int, object | None], None]) -> Callable[[], None]:
        self.handler = handler
        return lambda: None

    def deliver(self, signum: int) -> None:
        assert self.handler is not None
        self.delivered.append(signum)
        self.handler(signum, None)


@pytest.mark.parametrize(("first", "exit_code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_first_signal_wins_and_a_second_signal_cannot_interrupt_cleanup(
    tmp_path: Path, first: int, exit_code: int
) -> None:
    plan = _plan(tmp_path)
    signals = _FakeSignals()
    cleanup_completed: list[bool] = []

    def stage(argv: list[str], **kwargs: Any):
        _materialize_native_runtime(Path(kwargs["cwd"]))
        signals.deliver(first)
        return subprocess.CompletedProcess(argv, 0)

    def cleanup(_plan: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        signals.deliver(signal.SIGTERM if first == signal.SIGINT else signal.SIGINT)
        cleanup_completed.append(True)
        return _full_cleanup_proof(
            tmp_path / "output", trigger="SIGINT" if first == signal.SIGINT else "SIGTERM"
        )

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=cleanup,
        signal_installer=signals.install,
    )
    assert cleanup_completed == [True]
    assert len(signals.delivered) == 2
    assert result.status == "INVALID" and result.exit_code == exit_code
    assert (tmp_path / "output/guest-cleanup.v1.json").is_file()


def test_incomplete_target_mismatched_or_unproved_cleanup_proof_exits_three(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    incomplete = {"all_absent": True}
    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_stage(),
        cleanup_runner=lambda *_args, **_kwargs: incomplete,
    )
    assert result.status == "INVALID" and result.exit_code == 3

    second = _plan(tmp_path / "mismatched")

    def wrong_cleanup(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        wrong = _full_cleanup_proof(tmp_path / "mismatched/output")
        wrong["targets"][0]["container_tag_hmac_sha256"] = "0" * 64
        return wrong

    result = _run(
        second,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_stage(),
        cleanup_runner=wrong_cleanup,
    )
    assert result.status == "INVALID" and result.exit_code == 3

    third = _plan(tmp_path / "unproved")
    result = _run(
        third,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_stage(),
        cleanup_runner=_cleanup(tmp_path / "unproved/output", all_absent=False),
    )
    assert result.status == "INVALID" and result.exit_code == 3


def test_deterministic_replay_and_full_validation_recompute_every_digest(tmp_path: Path) -> None:
    outputs: list[tuple[bytes, bytes, bytes]] = []
    for name in ("first", "second"):
        root = tmp_path / name
        plan = _plan(root)
        result = _run(plan, **_valid_dependencies(root))
        assert result.status == "VALID"
        from memorybench.export import validate_export

        export_path = root / "output/memorybench-export.v1.json"
        public = json.loads(export_path.read_text())
        validate_export(public, run_plan_path=plan)
        private_path = root / "output" / public["cases"][0]["private_gold"]["path"]
        outputs.append(
            (
                export_path.read_bytes(),
                private_path.read_bytes(),
                (root / "output/guest-cleanup.v1.json").read_bytes(),
            )
        )
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "stale_state",
    ["outside-native-dataset", "native-byte-drift", "questions", "questions-symlink", "run-root"],
)
def test_native_dataset_and_fresh_runtime_state_block_before_any_stage(
    tmp_path: Path, stale_state: str,
) -> None:
    plan = _fresh_plan(tmp_path)
    payload = json.loads(plan.read_text(encoding="utf-8"))
    native = Path(payload["dataset_path"])
    if stale_state == "outside-native-dataset":
        outside = tmp_path / "outside-native.json"
        outside.write_bytes(native.read_bytes())
        payload["dataset_path"] = str(outside)
    elif stale_state == "native-byte-drift":
        native.write_bytes(native.read_bytes() + b"\n")
    elif stale_state == "questions":
        (native.parent / "questions").mkdir()
    elif stale_state == "questions-symlink":
        target = tmp_path / "outside-questions"
        target.mkdir()
        (native.parent / "questions").symlink_to(target, target_is_directory=True)
    else:
        (Path(payload["memorybench_home"]) / "data/runs/run-01").mkdir(parents=True)
    plan.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    stage_calls: list[list[str]] = []
    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=lambda argv, **_kwargs: stage_calls.append(argv),
    )
    assert result == type(result)("BLOCKED", 2)
    assert stage_calls == []


def test_fresh_question_shards_must_reconcile_to_the_raw_dataset_before_valid(
    tmp_path: Path,
) -> None:
    plan = _fresh_plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        shard = home / "data/benchmarks/longmemeval/datasets/questions/q-01.json"
        forged = json.loads(shard.read_text(encoding="utf-8"))
        forged["question"] = "shard differs from raw"
        shard.write_text(json.dumps(forged, sort_keys=True) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=_cleanup(tmp_path / "output"),
    )
    assert result.status == "INVALID" and result.exit_code == 1


@pytest.mark.parametrize(
    ("selection", "checkpoint_targets"),
    [
        ({"mode": "full", "target_question_ids": None}, None),
        (
            {"mode": "explicit", "target_question_ids": ["q-02", RAW_QID]},
            ["q-02", RAW_QID],
        ),
    ],
)
def test_selection_flows_through_additive_ingest_without_limit_or_sampling(
    tmp_path: Path,
    selection: dict[str, Any],
    checkpoint_targets: list[str] | None,
) -> None:
    plan = _fresh_plan(tmp_path)
    payload = json.loads(plan.read_text(encoding="utf-8"))
    payload["selection"] = selection
    raw_tags = {RAW_QID: RAW_TAG}
    if selection["mode"] == "explicit":
        dataset_path = Path(payload["dataset_path"])
        rows = json.loads(dataset_path.read_text(encoding="utf-8"))
        second = copy.deepcopy(rows[0])
        second.update({
            "question_id": "q-02",
            "question": "Where is the second receipt?",
            "answer": "answer-two",
        })
        second["sessions"][0]["session_id"] = "q-02-session-1"
        rows.append(second)
        dataset_path.write_text(json.dumps(rows, sort_keys=True) + "\n", encoding="utf-8")
        payload["dataset"]["sha256"] = _sha(dataset_path)
        payload["dataset"]["case_count"] = 2
        raw_tags["q-02"] = "private-container-two"
    plan.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    commands: list[list[str]] = []

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        _materialize_native_runtime(Path(kwargs["cwd"]))
        checkpoint_path = Path(kwargs["cwd"]) / "data/runs/run-01/checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_targets is not None:
            original = checkpoint["questions"][0]
            second_question = copy.deepcopy(original)
            second_question.update({
                "questionId": "q-02",
                "question": "Where is the second receipt?",
                "groundTruth": "answer-two",
                "containerTag": raw_tags["q-02"],
                "resultFile": "results/q-02.json",
            })
            checkpoint["questions"] = [second_question, original]
            checkpoint["targetQuestionIds"] = checkpoint_targets
            second_result = json.loads(RESULT.read_text(encoding="utf-8"))
            second_result.update({
                "questionId": "q-02",
                "question": "Where is the second receipt?",
                "groundTruth": "answer-two",
                "containerTag": raw_tags["q-02"],
            })
            (Path(kwargs["cwd"]) / "data/runs/run-01/results/q-02.json").write_text(
                json.dumps(second_result) + "\n", encoding="utf-8"
            )
            checkpoint_path.write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def cleanup(cleanup_plan: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        expected_ids = [RAW_QID] if selection["mode"] == "full" else selection["target_question_ids"]
        expected_targets = sorted(
            (
                {
                    "container_tag": raw_tags[question_id],
                    "container_tag_hmac_sha256": _hmac("container-tag", raw_tags[question_id]),
                    "discovery_sources": ["checkpoint"],
                    "namespace_expected": True,
                }
                for question_id in expected_ids
            ),
            key=lambda target: target["container_tag_hmac_sha256"],
        )
        assert cleanup_plan["targets"] == expected_targets
        targets = []
        for target in expected_targets:
            reference = _artifact_ref(
                tmp_path / "output",
                f"cleanup-evidence/target-{target['container_tag_hmac_sha256']}.json",
                {
                    "protocol_version": "1.0.0",
                    "artifact_type": "guest-cleanup-observation.v1",
                    "run_id": "run-01",
                    "provider": "exomem",
                    "provider_variant": "exomem-source-only",
                    "scope": "target",
                    "container_tag_hmac_sha256": target["container_tag_hmac_sha256"],
                    "operation_result": "clear_succeeded",
                    "operation_failure_code": None,
                    "basic_finalization_calls": 0,
                    "surfaces": {
                        "namespace": "absent", "corpus": None, "config": None,
                        "descriptor": "absent", "process_group": "absent",
                        "work_root": "absent",
                    },
                    "process_binding": None,
                },
            )
            targets.append({
                "container_tag_hmac_sha256": target["container_tag_hmac_sha256"],
                "discovery_sources": ["checkpoint"],
                "outcome": "cleared",
                "failure_code": None,
                "artifacts": [reference],
                "absence": {
                    "namespace": True, "corpus": None, "config": None,
                    "descriptor": True, "process_group": True, "work_root": True,
                },
            })
        final = _artifact_ref(
            tmp_path / "output", "cleanup-evidence/final.json",
            {
                "protocol_version": "1.0.0",
                "artifact_type": "guest-cleanup-observation.v1",
                "run_id": "run-01",
                "provider": "exomem",
                "provider_variant": "exomem-source-only",
                "scope": "final",
                "container_tag_hmac_sha256": None,
                "operation_result": "final_probe",
                "operation_failure_code": None,
                "basic_finalization_calls": 0,
                "surfaces": {
                    "namespace": None, "corpus": None, "config": "absent",
                    "descriptor": "absent", "process_group": "absent",
                    "work_root": "absent",
                },
                "process_binding": None,
            },
        )
        return {
            "protocol_version": "1.0.0", "schema_version": 1,
            "artifact_type": "guest-cleanup.v1", "run_id": "run-01",
            "provider": "exomem", "provider_variant": "exomem-source-only",
            "trigger": "success", "targets": targets,
            "basic_public_cleanup_calls": 0, "failure_codes": [],
            "final_absence": {
                "config": True, "descriptor": True, "process_group": True,
                "work_root": True, "artifacts": [final],
            },
            "all_absent": True,
        }

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=cleanup,
    )
    assert result.status == "VALID"
    assert len(commands) == 2
    ingest = commands[0]
    assert Path(ingest[0]).is_absolute()
    assert "src/cli/commands/competitive-ingest.ts" in ingest
    assert str(plan) in ingest and _sha(plan) in ingest
    joined = "\0".join(ingest).lower()
    assert all(flag not in joined for flag in ("--limit", "\0-l\0", "sample", "random"))
    public = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert [case["case_id_hmac_sha256"] for case in public["cases"]] == [
        _hmac("case-id", question_id)
        for question_id in ([RAW_QID] if selection["mode"] == "full" else selection["target_question_ids"])
    ]


@pytest.mark.parametrize("fault", ["checkpoint-target-order", "missing-selected-result"])
def test_selection_drives_checkpoint_agreement_and_export_completeness(
    tmp_path: Path, fault: str,
) -> None:
    plan = _fresh_plan(tmp_path)
    payload = json.loads(plan.read_text(encoding="utf-8"))
    payload["selection"] = {"mode": "explicit", "target_question_ids": [RAW_QID]}
    plan.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        checkpoint_path = home / "data/runs/run-01/checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint["targetQuestionIds"] = ["unselected-question"] if fault == "checkpoint-target-order" else [RAW_QID]
        checkpoint_path.write_text(json.dumps(checkpoint) + "\n")
        if fault == "missing-selected-result":
            (home / "data/runs/run-01/results/q-01.json").unlink()
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=_cleanup(tmp_path / "output"),
    )
    assert result.status == "INVALID" and result.exit_code == 1
    public = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert public["status"] == "partial"
    assert public["cases"][0]["hits"] == []


def _write_tool(path: Path, output: str) -> None:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n", encoding="utf-8")
    path.chmod(0o700)


@pytest.mark.parametrize("fault", [None, "missing-bun", "wrong-bun", "missing-uv"])
def test_preflight_resolves_and_verifies_bun_and_uv_outside_os_defpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    tools = tmp_path / "verified-tools"
    tools.mkdir()
    if fault != "missing-bun":
        _write_tool(tools / "bun", "1.2.0" if fault == "wrong-bun" else "1.3.14")
    if fault != "missing-uv":
        _write_tool(tools / "uv", "uv 0.8.2")
    monkeypatch.setenv("PATH", str(tools))
    plan = _fresh_plan(tmp_path / "run")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs["env"]))
        _materialize_native_runtime(Path(kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=_cleanup(tmp_path / "run/output"),
        cleanup_observer=lambda *_args, **_kwargs: True,
    )
    if fault is not None:
        assert result.status == "BLOCKED" and result.exit_code == 2
        assert calls == []
        return
    assert result.status == "VALID"
    assert calls and all(argv[0] == str((tools / "bun").resolve()) for argv, _env in calls)
    for _argv, env in calls:
        path_parts = env["PATH"].split(os.pathsep)
        assert path_parts[0] == str(tools.resolve())
        assert str(tools.resolve()) in path_parts
        assert env["PATH"] != os.defpath


def test_symlinked_results_directory_ancestor_cannot_supply_canonical_hits(
    tmp_path: Path,
) -> None:
    plan = _fresh_plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        results = home / "data/runs/run-01/results"
        outside = tmp_path / "outside-results"
        results.rename(outside)
        results.symlink_to(outside, target_is_directory=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "INVALID"
    public = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert "result_outside_root" in public["failure_codes"]
    assert public["cases"][0]["canonical_result"] is None
    assert public["cases"][0]["hits"] == []


@pytest.mark.parametrize(
    "private_value",
    [HMAC_KEY_HEX, RAW_QID, RAW_TAG, RAW_GOLD, "results/q-01.json", "groundTruth"],
)
def test_runtime_privacy_leak_in_an_otherwise_agreeing_hit_prevents_public_persistence(
    tmp_path: Path, private_value: str,
) -> None:
    plan = _fresh_plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        checkpoint_path = home / "data/runs/run-01/checkpoint.json"
        result_path = home / "data/runs/run-01/results/q-01.json"
        checkpoint = json.loads(checkpoint_path.read_text())
        canonical = json.loads(result_path.read_text())
        leak = {"content": f"leak:{private_value}", "score": 0.05}
        checkpoint["questions"][0]["results"].append(leak)
        canonical["results"].append(leak)
        checkpoint_path.write_text(json.dumps(checkpoint) + "\n")
        result_path.write_text(json.dumps(canonical) + "\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "INVALID" and result.exit_code == 1
    assert not (tmp_path / "output/memorybench-export.v1.json").exists()
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["status"] == "started" and manifest["finalized_at"] is None


def test_cleanup_retains_checkpoint_target_after_late_private_projection_write_failure(
    tmp_path: Path,
) -> None:
    plan = _fresh_plan(tmp_path)
    cleanup_targets: list[list[dict[str, Any]]] = []

    def writer(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
        if path.parent.name == "private-gold":
            raise OSError("late projection failure")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(payload)
        path.chmod(mode)

    def cleanup(cleanup_plan: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        cleanup_targets.append(cleanup_plan["targets"])
        return _full_cleanup_proof(tmp_path / "output", trigger="export_failure")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_fresh_stage(),
        cleanup_runner=cleanup,
        atomic_writer=writer,
    )
    assert result.status == "INVALID" and result.exit_code == 1
    assert cleanup_targets == [[{
        "container_tag": RAW_TAG,
        "container_tag_hmac_sha256": _hmac("container-tag", RAW_TAG),
        "discovery_sources": ["checkpoint"],
        "namespace_expected": True,
    }]]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX process groups")
def test_real_foreground_second_signal_cannot_kill_isolated_cleanup_group(
    tmp_path: Path,
) -> None:
    plan = _fresh_plan(tmp_path)
    stage_ready = tmp_path / "stage-ready"
    cleanup_ready = tmp_path / "cleanup-ready"
    cleanup_complete = tmp_path / "cleanup-complete"
    result_path = tmp_path / "coordinator-result.json"
    coordinator = os.fork()
    if coordinator == 0:  # pragma: no cover - assertions are made from the parent process
        try:
            os.setsid()

            class BlockingStage:
                stopped = False

                def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                    _materialize_native_runtime(Path(kwargs["cwd"]))
                    stage_ready.write_text("ready\n", encoding="utf-8")
                    while not self.stopped:
                        time.sleep(0.01)
                    return subprocess.CompletedProcess(argv, 130, "", "")

                def terminate(self) -> None:
                    self.stopped = True

            def isolated_cleanup(
                _cleanup_plan: dict[str, Any], *, trigger: str, start_new_session: bool,
            ) -> dict[str, Any]:
                assert start_new_session is True
                script = (
                    "import os,sys,time; "
                    "open(sys.argv[1],'w').write(str(os.getpid())); "
                    "time.sleep(0.35); open(sys.argv[2],'w').write('complete')"
                )
                helper = subprocess.Popen(
                    [sys.executable, "-c", script, str(cleanup_ready), str(cleanup_complete)],
                    start_new_session=start_new_session,
                )
                assert helper.wait(timeout=3) == 0
                return _full_cleanup_proof(tmp_path / "output", trigger=trigger)

            result = _run(
                plan,
                checkout_verifier=lambda **_kwargs: "materialized",
                provider_checkout_verifier=lambda _identity: None,
                stage_runner=BlockingStage(),
                cleanup_runner=isolated_cleanup,
            )
            result_path.write_text(
                json.dumps({"status": result.status, "exit_code": result.exit_code}) + "\n",
                encoding="utf-8",
            )
            os._exit(0)
        except BaseException:
            os._exit(90)

    helper_pid: int | None = None

    def wait_for(path: Path, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            waited, _status = os.waitpid(coordinator, os.WNOHANG)
            if waited == coordinator:
                return False
            time.sleep(0.01)
        return path.exists()

    try:
        assert wait_for(stage_ready, 3), "coordinator never entered the owned stage"
        os.kill(coordinator, signal.SIGINT)
        assert wait_for(cleanup_ready, 3), "cleanup never entered its isolated process group"
        helper_pid = int(cleanup_ready.read_text(encoding="utf-8"))
        os.killpg(coordinator, signal.SIGTERM)
        assert wait_for(cleanup_complete, 3), "foreground-group signal killed cleanup"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not result_path.exists():
            time.sleep(0.01)
        assert json.loads(result_path.read_text()) == {"status": "INVALID", "exit_code": 130}
        assert (tmp_path / "output/guest-cleanup.v1.json").is_file()
    finally:
        try:
            os.killpg(coordinator, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(coordinator, 0)
        except ChildProcessError:
            pass
        if helper_pid is not None:
            try:
                os.killpg(helper_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("conflict", ["inline-mismatch", "duplicate-canonical"])
def test_conflicting_result_sources_select_no_result_and_no_hits(
    tmp_path: Path, conflict: str,
) -> None:
    plan = _fresh_plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        run = home / "data/runs/run-01"
        if conflict == "inline-mismatch":
            checkpoint = json.loads((run / "checkpoint.json").read_text())
            checkpoint["questions"][0]["results"] = [{"content": "conflict", "score": 1.0}]
            (run / "checkpoint.json").write_text(json.dumps(checkpoint) + "\n")
        else:
            (run / "results/duplicate.json").write_bytes(RESULT.read_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "INVALID"
    case = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())["cases"][0]
    assert case["canonical_result"] is None
    assert case["hits"] == []


@pytest.mark.parametrize("description", [
    "run plan", "dataset", "checkpoint", "canonical result",
    "cleanup plan", "cleanup proof", "private gold", "persisted export",
])
@pytest.mark.parametrize("depth", ["top", "nested"])
def test_python_rejects_duplicate_json_members_at_every_source_and_depth(
    description: str, depth: str,
) -> None:
    from memorybench.export import _load_json_bytes

    raw = b'{"member":1,"member":1}' if depth == "top" else b'{"outer":{"member":1,"member":1}}'
    with pytest.raises(ValueError, match="duplicate|JSON"):
        _load_json_bytes(raw, description)


@pytest.mark.parametrize(
    "condition",
    ["missing", "symlink", "mode", "owner", "writable-parent", "malformed", "duplicate"],
)
def test_every_invalid_run_plan_is_a_quiet_blocked_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    condition: str,
) -> None:
    plan = _fresh_plan(tmp_path)
    candidate = plan
    if condition == "missing":
        candidate = tmp_path / "missing-secret-plan.json"
    elif condition == "symlink":
        candidate = tmp_path / "linked-secret-plan.json"
        candidate.symlink_to(plan)
    elif condition == "mode":
        plan.chmod(0o644)
    elif condition == "owner":
        import memorybench.export as export
        monkeypatch.setattr(export.os, "getuid", lambda: plan.stat().st_uid + 1)
    elif condition == "writable-parent":
        tmp_path.chmod(0o770)
    elif condition == "malformed":
        plan.write_text("{private-exception-text", encoding="utf-8")
    elif condition == "duplicate":
        plan.write_text('{"artifact_type":"memorybench-run-plan.v1","artifact_type":"memorybench-run-plan.v1"}\n')
    try:
        result = _run(candidate)
    finally:
        if condition == "writable-parent":
            tmp_path.chmod(0o700)
    assert result.status == "BLOCKED" and result.exit_code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert combined == ""
    assert "Traceback" not in combined
    assert str(tmp_path) not in combined
    assert "private-exception-text" not in combined


def _feedback3_provider_plan(tmp_path: Path, provider: str) -> Path:
    plan = _fresh_plan(tmp_path)
    payload = json.loads(plan.read_text(encoding="utf-8"))
    payload["provider"] = provider
    if provider == "basic-memory":
        payload["provider_variant"] = "basic-memory-controlled"
        payload["provider_checkout"]["repository"] = (
            "https://github.com/basicmachines-co/basic-memory"
        )
    plan.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return plan


@pytest.mark.parametrize("provider", ["basic-memory", "exomem"])
def test_feedback3_cleanup_proof_booleans_and_arbitrary_evidence_are_independently_reprobed(
    tmp_path: Path, provider: str,
) -> None:
    plan = _feedback3_provider_plan(tmp_path, provider)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        checkpoint_path = home / "data/runs/run-01/checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["provider"] = provider
        checkpoint_path.write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def forged_cleanup(cleanup_plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        service_root = tmp_path / f"output/guest-work/services/{provider}"
        service_root.mkdir(parents=True, mode=0o700)
        (service_root / "config.json").write_text("still present\n", encoding="utf-8")
        (service_root / "service.v1.json").write_text("still present\n", encoding="utf-8")
        return _cleanup_proof_for_targets(
            tmp_path / "output",
            cleanup_plan,
            provider=provider,
            trigger=kwargs["trigger"],
        )

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=forged_cleanup,
    )
    assert result.status == "INVALID" and result.exit_code == 3
    persisted = json.loads((tmp_path / "output/guest-cleanup.v1.json").read_text())
    assert persisted["all_absent"] is True, "the forged status must be persisted before reprobe"
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["status"] != "VALID"


@pytest.mark.parametrize("ancestor", ["data-runs", "run-id"])
def test_feedback3_every_run_ancestor_is_opened_no_follow(
    tmp_path: Path, ancestor: str,
) -> None:
    plan = _fresh_plan(tmp_path)
    mutated = False

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal mutated
        home = Path(kwargs["cwd"])
        if not mutated:
            _materialize_native_runtime(home)
            run = home / "data/runs/run-01"
            outside = tmp_path / f"outside-{ancestor}"
            if ancestor == "data-runs":
                runs = run.parent
                runs.rename(outside)
                runs.symlink_to(outside, target_is_directory=True)
            else:
                run.rename(outside)
                run.symlink_to(outside, target_is_directory=True)
            mutated = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "INVALID" and result.exit_code == 1
    public = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert public["status"] == "partial"
    assert "result_outside_root" in public["failure_codes"]
    assert public["cases"][0]["canonical_result"] is None
    assert public["cases"][0]["hits"] == []


@pytest.mark.parametrize(
    ("provider", "raw_tag", "source"),
    [
        ("basic-memory", "evidence-only-container", "guest_evidence"),
        ("exomem", "descriptor-only-container", "secure_descriptor"),
    ],
)
def test_feedback3_malformed_checkpoint_retains_each_independent_cleanup_target_source(
    tmp_path: Path, provider: str, raw_tag: str, source: str,
) -> None:
    plan = _feedback3_provider_plan(tmp_path, provider)
    payload = json.loads(plan.read_text(encoding="utf-8"))
    prepared = False
    observed_targets: list[list[dict[str, Any]]] = []
    attempted: list[str] = []

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal prepared
        home = Path(kwargs["cwd"])
        if not prepared:
            _materialize_native_runtime(home)
            if provider == "basic-memory":
                evidence = Path(payload["guest_evidence_root"]) / "basic-memory"
                evidence.mkdir(parents=True, mode=0o700)
                private = evidence / f"operation-000001-{'a' * 12}.json"
                private.write_text(json.dumps({
                    "protocol_version": 1,
                    "event": "request",
                    "recorded_at_utc": "2026-01-02T00:00:00Z",
                    "data": {
                        "route": "/v1/search",
                        "body": {
                            "protocol_version": 1,
                            "request_id": "request-1",
                            "container_tag": raw_tag,
                            "query": "fixture",
                            "limit": 1,
                        },
                    },
                }) + "\n", encoding="utf-8")
                private.chmod(0o600)
            else:
                directory = (
                    Path(payload["guest_work_root"])
                    / "services/exomem"
                    / _sha_text(raw_tag)[:24]
                )
                directory.mkdir(parents=True, mode=0o700)
                descriptor = directory / "service.v1.json"
                descriptor.write_text(json.dumps({
                    "protocol_version": 1,
                    "provider": "exomem",
                    "base_url": "http://127.0.0.1:1",
                    "bearer_token": "fixture-private-token",
                    "pid": os.getpid(),
                    "process_start_identity": f"linux-proc-v1:{os.getpid()}:fixture",
                    "checkout_pin": payload["provider_checkout"]["commit"],
                    "checkout_root": payload["provider_checkout"]["root"],
                    "work_root": str(directory),
                    "evidence_root": str(
                        Path(payload["guest_evidence_root"]) / "exomem" / directory.name
                    ),
                    "container_tag": raw_tag,
                    "vault_root": str(directory / "vault"),
                    "instance_id": "fixture-instance",
                }) + "\n", encoding="utf-8")
                descriptor.chmod(0o600)
            (home / "data/runs/run-01/checkpoint.json").write_text("{malformed", encoding="utf-8")
            prepared = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    def cleanup(cleanup_plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        observed_targets.append(cleanup_plan["targets"])
        attempted.extend(target["container_tag"] for target in cleanup_plan["targets"])
        return _cleanup_proof_for_targets(
            tmp_path / "output",
            cleanup_plan,
            provider=provider,
            trigger=kwargs["trigger"],
        )

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=cleanup,
    )
    assert result.status == "INVALID"
    expected = [{
        "container_tag": raw_tag,
        "container_tag_hmac_sha256": _hmac("container-tag", raw_tag),
        "discovery_sources": [source],
        "namespace_expected": source == "secure_descriptor",
    }]
    assert observed_targets == [expected]
    assert attempted == [raw_tag]


@pytest.mark.parametrize("corruption", ["alter", "remove", "add"])
def test_feedback3_shard_corruption_persists_partial_export_cleanup_and_final_manifest(
    tmp_path: Path, corruption: str,
) -> None:
    plan = _fresh_plan(tmp_path)
    corrupted = False

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal corrupted
        home = Path(kwargs["cwd"])
        if not corrupted:
            _materialize_native_runtime(home)
            questions = home / "data/benchmarks/longmemeval/datasets/questions"
            shard = questions / "q-01.json"
            if corruption == "alter":
                forged = json.loads(shard.read_text(encoding="utf-8"))
                forged["question"] = "shard differs from raw"
                shard.write_text(json.dumps(forged) + "\n", encoding="utf-8")
            elif corruption == "remove":
                shard.unlink()
            else:
                (questions / "unexpected.json").write_text("{}\n", encoding="utf-8")
            corrupted = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=_cleanup(tmp_path / "output"),
    )
    assert result.status == "INVALID" and result.exit_code == 1
    public = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert public["status"] == "partial"
    assert "case_set_mismatch" in public["failure_codes"]
    assert (tmp_path / "output/guest-cleanup.v1.json").is_file()
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["status"] == "INVALID"
    assert manifest["finalized_at"] is not None


def test_feedback3_manifest_timestamps_use_the_injected_aware_utc_clock_at_each_write(
    tmp_path: Path,
) -> None:
    plan = _fresh_plan(tmp_path)
    instants = [
        datetime(2026, 8, 10, 12, 0, 0, 123456, tzinfo=timezone.utc),
        datetime(2026, 8, 10, 12, 0, 1, 654321, tzinfo=timezone.utc),
    ]
    observed: list[datetime] = []

    def utc_now() -> datetime:
        value = instants[len(observed)]
        observed.append(value)
        return value

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=_stage(),
        cleanup_runner=_cleanup(tmp_path / "output"),
        utc_now=utc_now,
    )
    assert result.status == "VALID"
    assert observed == instants
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["started_at"] == "2026-08-10T12:00:00.123456Z"
    assert manifest["finalized_at"] == "2026-08-10T12:00:01.654321Z"
    assert datetime.fromisoformat(manifest["finalized_at"].replace("Z", "+00:00")) >= (
        datetime.fromisoformat(manifest["started_at"].replace("Z", "+00:00"))
    )


@pytest.mark.parametrize("provider", ["basic-memory", "exomem"])
def test_feedback4_digest_matching_arbitrary_cleanup_evidence_cannot_repeat_proof_claims(
    tmp_path: Path, provider: str,
) -> None:
    plan = _feedback3_provider_plan(tmp_path, provider)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        checkpoint_path = home / "data/runs/run-01/checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["provider"] = provider
        checkpoint_path.write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def forged_cleanup(cleanup_plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return _cleanup_proof_for_targets(
            tmp_path / "output",
            cleanup_plan,
            provider=provider,
            trigger=kwargs["trigger"],
        )

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=forged_cleanup,
    )
    assert result.status == "INVALID" and result.exit_code == 3
    proof = json.loads((tmp_path / "output/guest-cleanup.v1.json").read_text())
    assert proof["all_absent"] is True, "the forged model-valid claim is not operation evidence"
    evidence = json.loads(
        (tmp_path / "output/cleanup-evidence/feedback3-target-1.json").read_text()
    )
    assert evidence == {"protocol_version": 1, "arbitrary_status": "absent"}


@pytest.mark.parametrize(
    ("provider", "raw_tag", "source", "failure_code"),
    [
        ("basic-memory", "valid-evidence-target", "guest_evidence", "guest_evidence_invalid"),
        ("exomem", "valid-descriptor-target", "secure_descriptor", "secure_descriptor_invalid"),
    ],
)
def test_feedback4_malformed_discovery_sibling_retains_valid_target_and_stable_failures(
    tmp_path: Path,
    provider: str,
    raw_tag: str,
    source: str,
    failure_code: str,
) -> None:
    plan = _feedback3_provider_plan(tmp_path, provider)
    payload = json.loads(plan.read_text(encoding="utf-8"))
    observed_targets: list[list[dict[str, Any]]] = []

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        if not (home / "data/runs/run-01").exists():
            _materialize_native_runtime(home)
            if provider == "basic-memory":
                root = Path(payload["guest_evidence_root"]) / "basic-memory"
                root.mkdir(parents=True, mode=0o700)
                valid = root / f"operation-000001-{'a' * 12}.json"
                valid.write_text(json.dumps({
                    "protocol_version": 1,
                    "event": "request",
                    "recorded_at_utc": "2026-08-10T00:00:00Z",
                    "data": {
                        "route": "/v1/search",
                        "body": {
                            "protocol_version": 1,
                            "request_id": "feedback4-request",
                            "container_tag": raw_tag,
                            "query": "fixture",
                            "limit": 1,
                        },
                    },
                }) + "\n", encoding="utf-8")
                valid.chmod(0o600)
                malformed = root / f"operation-000002-{'b' * 12}.json"
                malformed.write_text("{malformed", encoding="utf-8")
                malformed.chmod(0o600)
            else:
                services = Path(payload["guest_work_root"]) / "services/exomem"
                directory = services / _sha_text(raw_tag)[:24]
                directory.mkdir(parents=True, mode=0o700)
                descriptor = directory / "service.v1.json"
                descriptor.write_text(json.dumps({
                    "protocol_version": 1,
                    "provider": "exomem",
                    "base_url": "http://127.0.0.1:1",
                    "bearer_token": "fixture-private-token",
                    "pid": os.getpid(),
                    "process_start_identity": f"linux-proc-v1:{os.getpid()}:fixture",
                    "checkout_pin": payload["provider_checkout"]["commit"],
                    "checkout_root": payload["provider_checkout"]["root"],
                    "work_root": str(directory),
                    "evidence_root": str(
                        Path(payload["guest_evidence_root"]) / "exomem" / directory.name
                    ),
                    "container_tag": raw_tag,
                    "vault_root": str(directory / "vault"),
                    "instance_id": "fixture-instance",
                }) + "\n", encoding="utf-8")
                descriptor.chmod(0o600)
                malformed_directory = services / ("f" * 24)
                malformed_directory.mkdir(mode=0o700)
                malformed = malformed_directory / "service.v1.json"
                malformed.write_text("{malformed", encoding="utf-8")
                malformed.chmod(0o600)
            (home / "data/runs/run-01/checkpoint.json").write_text(
                "{malformed", encoding="utf-8"
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def cleanup(cleanup_plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        observed_targets.append(cleanup_plan["targets"])
        return _cleanup_proof_for_targets(
            tmp_path / "output",
            cleanup_plan,
            provider=provider,
            trigger=kwargs["trigger"],
        )

    result = _run(
        plan,
        checkout_verifier=lambda **_kwargs: "materialized",
        provider_checkout_verifier=lambda _identity: None,
        stage_runner=stage,
        cleanup_runner=cleanup,
    )
    expected_target = {
        "container_tag": raw_tag,
        "container_tag_hmac_sha256": _hmac("container-tag", raw_tag),
        "discovery_sources": [source],
        "namespace_expected": source == "secure_descriptor",
    }
    assert observed_targets == [[expected_target]]
    public_path = tmp_path / "output/memorybench-export.v1.json"
    assert public_path.is_file(), "candidate errors must persist a partial export before cleanup"
    public = json.loads(public_path.read_text())
    assert public["status"] == "partial"
    assert {"checkpoint_invalid", failure_code} <= set(public["failure_codes"])
    assert result.status == "INVALID"


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-name",
        "duplicate-result",
        "extra-result",
        "inline-mismatch",
        "nonfinite",
        "symlink-result",
        "malformed-result",
        "outside-result-file",
        "question-mismatch",
        "type-mismatch",
        "gold-mismatch",
        "container-mismatch",
        "invalid-hit",
    ],
)
def test_feedback4_every_rejected_or_noncanonical_result_contributes_no_reader_hits(
    tmp_path: Path, mutation: str,
) -> None:
    plan = _fresh_plan(tmp_path)

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        run = home / "data/runs/run-01"
        checkpoint_path = run / "checkpoint.json"
        result_path = run / "results/q-01.json"
        if mutation == "wrong-name":
            result_path.rename(run / "results/wrong-name.json")
        elif mutation == "duplicate-result":
            (run / "results/duplicate.json").write_bytes(result_path.read_bytes())
        elif mutation == "extra-result":
            extra = json.loads(result_path.read_text())
            extra["questionId"] = "unexpected-question"
            (run / "results/unexpected-question.json").write_text(json.dumps(extra) + "\n")
        elif mutation == "inline-mismatch":
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["questions"][0]["results"] = [{"content": "different", "score": 1.0}]
            checkpoint_path.write_text(json.dumps(checkpoint) + "\n")
        elif mutation == "nonfinite":
            result = json.loads(result_path.read_text())
            result["results"][0]["score"] = float("inf")
            result_path.write_text(json.dumps(result) + "\n")
        elif mutation == "symlink-result":
            outside = tmp_path / "outside-result.json"
            outside.write_bytes(result_path.read_bytes())
            result_path.unlink()
            result_path.symlink_to(outside)
        elif mutation == "malformed-result":
            result_path.write_text("{malformed")
        elif mutation == "outside-result-file":
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["questions"][0]["resultFile"] = "../../outside.json"
            checkpoint_path.write_text(json.dumps(checkpoint) + "\n")
        elif mutation in {
            "question-mismatch", "type-mismatch", "gold-mismatch", "container-mismatch"
        }:
            result = json.loads(result_path.read_text())
            key, value = {
                "question-mismatch": ("question", "different question"),
                "type-mismatch": ("questionType", "different-type"),
                "gold-mismatch": ("groundTruth", "different gold"),
                "container-mismatch": ("containerTag", "different-container"),
            }[mutation]
            result[key] = value
            result_path.write_text(json.dumps(result) + "\n")
        else:
            result = json.loads(result_path.read_text())
            result["results"][0]["content"] = ""
            result_path.write_text(json.dumps(result) + "\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})
    assert result.status == "INVALID"
    case = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())["cases"][0]
    assert case["canonical_result"] is None
    assert case["hits"] == []


def test_feedback5_private_gold_parent_symlink_never_writes_or_chmods_outside_output(
    tmp_path: Path,
) -> None:
    plan = _fresh_plan(tmp_path)
    external = tmp_path / "external-private-gold"
    external.mkdir(mode=0o711)
    external.chmod(0o711)
    original_mode = stat.S_IMODE(external.stat().st_mode)
    linked = False

    def stage(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal linked
        home = Path(kwargs["cwd"])
        _materialize_native_runtime(home)
        if not linked:
            private_parent = tmp_path / "output/private-gold"
            assert not private_parent.exists()
            private_parent.symlink_to(external, target_is_directory=True)
            linked = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = _run(plan, **{**_valid_dependencies(tmp_path), "stage_runner": stage})

    outside_files = [path for path in external.rglob("*") if path.is_file()]
    assert outside_files == [], "protected writer followed private-gold parent outside output"
    assert stat.S_IMODE(external.stat().st_mode) == original_mode
    assert all(RAW_GOLD.encode() not in path.read_bytes() for path in outside_files)
    assert result.status == "INVALID" and result.exit_code == 1
    public = json.loads((tmp_path / "output/memorybench-export.v1.json").read_text())
    assert public["status"] == "partial"
    assert "private_gold_write_failed" in public["failure_codes"]
    assert (tmp_path / "output/guest-cleanup.v1.json").is_file()
    manifest = json.loads((tmp_path / "output/manifest.json").read_text())
    assert manifest["status"] == "INVALID" and manifest["finalized_at"] is not None


def _amended_identity(*, amended: bool):
    """A pre-registration identity with, or without, one acknowledged amendment.

    Built by hand rather than derived from the repository so the test states
    which case it is exercising instead of depending on whether the real
    contract happens to carry an amendment today.
    """
    from protocol.contracts import (
        AmendmentIdentity,
        ContractArtifactIdentity,
        PreregistrationIdentity,
        ReceiptIdentity,
    )

    original = ContractArtifactIdentity(
        path="benchmarks/epistemic/PREREGISTRATION.md",
        sha256="a" * 64,
        repository_revision="1" * 40,
    )
    ratification = ReceiptIdentity(
        receipt_path="benchmarks/epistemic/contracts/ratification.v1.json",
        receipt_sha256="b" * 64,
        introduction_revision="2" * 40,
    )
    if not amended:
        return PreregistrationIdentity(
            contract_revision="2" * 40,
            original=original,
            ratification=ratification,
            amendments=(),
            effective=original,
        )
    effective = ContractArtifactIdentity(
        path=original.path, sha256="c" * 64, repository_revision="3" * 40
    )
    amendment = AmendmentIdentity(
        sequence=1,
        receipt=ReceiptIdentity(
            receipt_path="benchmarks/epistemic/contracts/amendment-0001.v1.json",
            receipt_sha256="d" * 64,
            introduction_revision="4" * 40,
        ),
        parent_contract_sha256=original.sha256,
        contract=effective,
        affected_sections=("§7",),
        rationale="reason",
        effective_policy="after acknowledgment",
        acknowledgment_status="acknowledged",
        introduced_family_ids=(),
    )
    return PreregistrationIdentity(
        contract_revision="4" * 40,
        original=original,
        ratification=ratification,
        amendments=(amendment,),
        effective=effective,
    )


def test_started_manifest_carries_lineage_for_an_amended_preregistration(
    tmp_path: Path,
) -> None:
    """An amended pre-registration must reach the manifest as lineage.

    `RunManifest` refuses an amended identity that arrives without lineage, so
    omitting it here does not mislabel the run — it stops the guest lane from
    producing a manifest at all, before the first case is ingested. The direct
    lane derives lineage in `protocol.manifest`; this construction site is the
    second author of the same manifest and had to be told separately.
    """
    import memorybench.export as export
    from protocol.models import MemoryBenchRunPlan, PreregistrationLineage

    plan = MemoryBenchRunPlan.model_validate(_plan_payload(tmp_path))
    identity = _amended_identity(amended=True)

    manifest = export._started_manifest(plan, "2026-08-16T00:00:00Z", identity, {})

    assert manifest["preregistration_lineage"] == (
        PreregistrationLineage.from_identity(identity).model_dump(mode="json")
    )


def test_started_manifest_omits_lineage_for_an_unamended_preregistration(
    tmp_path: Path,
) -> None:
    """Lineage records amendments; a base-only run has none to record."""
    import memorybench.export as export
    from protocol.models import MemoryBenchRunPlan

    plan = MemoryBenchRunPlan.model_validate(_plan_payload(tmp_path))

    manifest = export._started_manifest(
        plan, "2026-08-16T00:00:00Z", _amended_identity(amended=False), {}
    )

    assert manifest["preregistration_lineage"] is None
