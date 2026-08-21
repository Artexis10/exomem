from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "infra/contracts/exomem-hosted-runtime-upgrade-execution-v1.schema.json"
SCRIPT = ROOT / "infra/scripts/hosted_runtime_upgrade.py"


def _module():
    assert SCRIPT.is_file(), "the governed upgrade CLI must be committed"
    spec = importlib.util.spec_from_file_location("hosted_runtime_upgrade", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _target() -> dict[str, object]:
    return {
        "releaseVersion": "0.57.2",
        "sourceCommit": "a" * 40,
        "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{'b' * 64}",
        "runtimeCandidateSha256": "c" * 64,
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "d" * 64,
        "commandFingerprint": "e" * 64,
        "schemaDigest": "f" * 64,
        "compatibilityDigest": "1" * 64,
    }


def test_execution_schema_is_committed_strict_and_closed() -> None:
    assert SCHEMA.is_file(), "the upgrade execution schema must be committed"
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False


def test_new_execution_validates_and_contains_only_redacted_authority() -> None:
    module = _module()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )

    Draft202012Validator(schema).validate(record)
    assert record["phase"] == "selected"
    assert record["result"] == {
        "code": "in_progress",
        "nextSafeAction": "trust_target",
    }
    encoded = json.dumps(record, sort_keys=True).casefold()
    for forbidden in ("token", "credential", "note title", "vault path", "browser"):
        assert forbidden not in encoded


def test_trusted_transition_binds_consumer_and_release_evidence() -> None:
    module = _module()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )

    trusted = module.advance_execution(
        record,
        expected_sha256=module.canonical_sha256(record),
        next_phase="trusted",
        updated_at="2026-08-21T12:01:00Z",
        facts={
            "substrateConsumerCommit": "2" * 40,
            "releaseEvidenceSha256": "3" * 64,
        },
    )

    Draft202012Validator(schema).validate(trusted)
    assert trusted["phase"] == "trusted"
    assert trusted["target"] == record["target"]
    assert trusted["repositories"]["substrateConsumerCommit"] == "2" * 40
    assert trusted["evidence"]["release"] == ["3" * 64]
    assert trusted["result"]["nextSafeAction"] == "deploy_expand"


def test_retrying_the_same_committed_phase_is_idempotent() -> None:
    module = _module()
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    facts = {
        "substrateConsumerCommit": "2" * 40,
        "releaseEvidenceSha256": "3" * 64,
    }
    trusted = module.advance_execution(
        record,
        expected_sha256=module.canonical_sha256(record),
        next_phase="trusted",
        updated_at="2026-08-21T12:01:00Z",
        facts=facts,
    )

    replay = module.advance_execution(
        trusted,
        expected_sha256=module.canonical_sha256(trusted),
        next_phase="trusted",
        updated_at="2026-08-21T12:01:00Z",
        facts=facts,
    )

    assert replay == trusted


def test_retrying_any_committed_phase_requires_identical_facts() -> None:
    module = _module()
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    trusted_facts = {
        "substrateConsumerCommit": "2" * 40,
        "releaseEvidenceSha256": "3" * 64,
    }
    trusted = module.advance_execution(
        record,
        expected_sha256=module.canonical_sha256(record),
        next_phase="trusted",
        updated_at="2026-08-21T12:01:00Z",
        facts=trusted_facts,
    )
    expanded_facts = {
        "exomemCommit": "4" * 40,
        "pairSha256": "5" * 64,
        "expandSha256": "6" * 64,
        "contractSha256": "7" * 64,
        "expandEvidenceSha256": "8" * 64,
    }
    expanded = module.advance_execution(
        trusted,
        expected_sha256=module.canonical_sha256(trusted),
        next_phase="expanded",
        updated_at="2026-08-21T12:02:00Z",
        facts=expanded_facts,
    )

    replay = module.advance_execution(
        expanded,
        expected_sha256=module.canonical_sha256(expanded),
        next_phase="expanded",
        updated_at="2026-08-21T12:02:00Z",
        facts=expanded_facts,
    )
    assert replay == expanded

    changed = dict(expanded_facts, contractSha256="0" * 64)
    with pytest.raises(module.UpgradeExecutionError, match="retry differs"):
        module.advance_execution(
            expanded,
            expected_sha256=module.canonical_sha256(expanded),
            next_phase="expanded",
            updated_at="2026-08-21T12:02:00Z",
            facts=changed,
        )


def test_execution_advances_through_every_reviewed_phase() -> None:
    module = _module()
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    steps = [
        (
            "trusted",
            {"substrateConsumerCommit": "2" * 40, "releaseEvidenceSha256": "3" * 64},
            "deploy_expand",
        ),
        (
            "expanded",
            {
                "exomemCommit": "4" * 40,
                "pairSha256": "5" * 64,
                "expandSha256": "6" * 64,
                "contractSha256": "7" * 64,
                "expandEvidenceSha256": "8" * 64,
            },
            "inventory_fleet",
        ),
        (
            "inventoried",
            {
                "inventoryStatus": "empty",
                "inventorySha256": "9" * 64,
                "cellCount": 0,
                "legacyCellCount": 0,
                "inventoryEvidenceSha256": "a" * 64,
                "cells": [],
            },
            "begin_rollforward",
        ),
        ("rolling", {"rolloutEvidenceSha256": "b" * 64}, "prove_zero_legacy"),
        (
            "drained",
            {
                "inventorySha256": "c" * 64,
                "inventoryEvidenceSha256": "d" * 64,
                "cellCount": 0,
            },
            "deploy_contract",
        ),
        ("contracted", {"contractEvidenceSha256": "e" * 64}, "run_promotion"),
        ("promoted", {"promotionEvidenceSha256": "f" * 64}, "run_acceptance"),
        ("accepted", {"acceptanceEvidenceSha256": "1" * 64}, "finalize"),
        ("complete", {"finalEvidenceSha256": "0" * 64}, "none"),
    ]

    for minute, (phase, facts, next_action) in enumerate(steps, start=1):
        record = module.advance_execution(
            record,
            expected_sha256=module.canonical_sha256(record),
            next_phase=phase,
            updated_at=f"2026-08-21T12:{minute:02d}:00Z",
            facts=facts,
        )
        validator.validate(record)
        assert record["phase"] == phase
        assert record["result"]["nextSafeAction"] == next_action

    assert record["result"]["code"] == "complete"


@pytest.mark.parametrize("location", ["root", "target"])
def test_validation_rejects_unknown_content_or_secret_fields(location: str) -> None:
    module = _module()
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    forged = copy.deepcopy(record)
    if location == "root":
        forged["credential"] = "must-never-land"
    else:
        forged["target"]["vaultPath"] = "/must/never/land"

    with pytest.raises(module.UpgradeExecutionError, match="fields"):
        module.validate_execution(forged)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda record: record["repositories"].update({"credential": "secret"}),
            "repository fields",
        ),
        (
            lambda record: record["inventory"].update({"cellCount": True}),
            "cellCount",
        ),
        (
            lambda record: record["evidence"]["release"].extend(["2" * 64] * 2),
            "duplicate",
        ),
        (
            lambda record: record["result"].update({"nextSafeAction": "finalize"}),
            "result",
        ),
        (
            lambda record: record.update({"updatedAt": "2026-08-21T11:59:59Z"}),
            "earlier",
        ),
    ],
)
def test_validation_rejects_malformed_nested_or_incoherent_state(mutation, message: str) -> None:
    module = _module()
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    mutation(record)

    with pytest.raises(module.UpgradeExecutionError, match=message):
        module.validate_execution(record)


def test_validation_rejects_phase_without_required_authority() -> None:
    module = _module()
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    record["phase"] = "expanded"
    record["result"]["nextSafeAction"] = "inventory_fleet"

    with pytest.raises(module.UpgradeExecutionError, match="expanded"):
        module.validate_execution(record)


def test_phase_advance_refuses_a_stale_record_hash() -> None:
    module = _module()
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    expected = module.canonical_sha256(record)
    record["result"]["code"] = "tampered"

    with pytest.raises(module.UpgradeExecutionError, match="changed"):
        module.advance_execution(
            record,
            expected_sha256=expected,
            next_phase="trusted",
            updated_at="2026-08-21T12:01:00Z",
            facts={
                "substrateConsumerCommit": "2" * 40,
                "releaseEvidenceSha256": "3" * 64,
            },
        )


def test_recovery_decision_tracks_the_last_tenant_safe_phase() -> None:
    module = _module()
    selected = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    assert module.recovery_decision(selected) == "discard_target_artifacts"

    trusted = module.advance_execution(
        selected,
        expected_sha256=module.canonical_sha256(selected),
        next_phase="trusted",
        updated_at="2026-08-21T12:01:00Z",
        facts={
            "substrateConsumerCommit": "2" * 40,
            "releaseEvidenceSha256": "3" * 64,
        },
    )
    expanded = module.advance_execution(
        trusted,
        expected_sha256=module.canonical_sha256(trusted),
        next_phase="expanded",
        updated_at="2026-08-21T12:02:00Z",
        facts={
            "exomemCommit": "4" * 40,
            "pairSha256": "5" * 64,
            "expandSha256": "6" * 64,
            "contractSha256": "7" * 64,
            "expandEvidenceSha256": "8" * 64,
        },
    )
    assert module.recovery_decision(expanded) == "restore_prior_platform_lock"

    rolling = copy.deepcopy(expanded)
    rolling["phase"] = "rolling"
    rolling["cells"] = [
        {
            "cellId": "cell_1809ce5c",
            "class": "legacy",
            "releaseVersion": "0.54.1",
            "assignmentId": "assignment_1",
            "operationId": "operation_1",
            "status": "rolling",
            "beforeVaultSha256": "9" * 64,
            "afterVaultSha256": None,
            "evidenceSha256": None,
        }
    ]
    assert module.recovery_decision(rolling) == "retry_cell_atomically"

    rolling["cells"][0]["status"] = "complete"
    rolling["cells"][0]["afterVaultSha256"] = "9" * 64
    rolling["cells"][0]["evidenceSha256"] = "a" * 64
    assert module.recovery_decision(rolling) == "hold_expand_and_recover"


def test_execution_file_round_trip_is_canonical_private_and_fenced(
    tmp_path: Path,
) -> None:
    module = _module()
    path = tmp_path / "runtime-0.57.2.json"
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )

    digest = module.write_execution(path, record)

    assert module.load_execution(path) == record
    assert digest == module.canonical_sha256(record)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        path.read_bytes()
        == (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    changed = copy.deepcopy(record)
    changed["updatedAt"] = "2026-08-21T12:01:00Z"
    with pytest.raises(module.UpgradeExecutionError, match="fence"):
        module.write_execution(path, changed, expected_sha256="0" * 64)
    assert module.load_execution(path) == record

    new_digest = module.write_execution(path, changed, expected_sha256=digest)
    assert new_digest == module.canonical_sha256(changed)
    assert module.load_execution(path) == changed


def test_execution_loader_rejects_symlinks_duplicate_keys_and_noncanonical_bytes(
    tmp_path: Path,
) -> None:
    module = _module()
    record = module.new_execution(
        execution_id="runtime-0-57-2-20260821t120000z",
        target=_target(),
        created_at="2026-08-21T12:00:00Z",
    )
    canonical = tmp_path / "canonical.json"
    module.write_execution(canonical, record)

    linked = tmp_path / "linked.json"
    linked.symlink_to(canonical)
    with pytest.raises(module.UpgradeExecutionError, match="symlink"):
        module.load_execution(linked)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"artifact":"one","artifact":"two"}\n', encoding="utf-8")
    with pytest.raises(module.UpgradeExecutionError, match="duplicate"):
        module.load_execution(duplicate)

    noncanonical = tmp_path / "pretty.json"
    noncanonical.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(module.UpgradeExecutionError, match="canonical"):
        module.load_execution(noncanonical)


def test_operator_cli_creates_inspects_revalidates_and_advances(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_target()), encoding="utf-8")
    execution = tmp_path / "runtime-0.57.2.json"

    assert (
        module.main(
            [
                "create",
                "--execution",
                os.fspath(execution),
                "--execution-id",
                "runtime-0-57-2-20260821t120000z",
                "--target",
                os.fspath(target),
                "--at",
                "2026-08-21T12:00:00Z",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["phase"] == "selected"
    assert created["nextSafeAction"] == "trust_target"

    assert module.main(["inspect", "--execution", os.fspath(execution)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected == created

    assert module.main(["validate", "--execution", os.fspath(execution)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated == {"executionSha256": created["executionSha256"], "valid": True}

    facts = tmp_path / "trusted-facts.json"
    facts.write_text(
        json.dumps(
            {
                "substrateConsumerCommit": "2" * 40,
                "releaseEvidenceSha256": "3" * 64,
            }
        ),
        encoding="utf-8",
    )
    assert (
        module.main(
            [
                "advance",
                "--execution",
                os.fspath(execution),
                "--expected-sha256",
                created["executionSha256"],
                "--phase",
                "trusted",
                "--facts",
                os.fspath(facts),
                "--at",
                "2026-08-21T12:01:00Z",
            ]
        )
        == 0
    )
    advanced = json.loads(capsys.readouterr().out)
    assert advanced["phase"] == "trusted"
    assert advanced["nextSafeAction"] == "deploy_expand"
    assert module.load_execution(execution)["phase"] == "trusted"
