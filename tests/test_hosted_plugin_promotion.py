from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import shutil
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem import hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]
_HOSTED_PLUGIN_SCRIPT = REPO_ROOT / "scripts" / "hosted-plugin.py"


def digest(value: str) -> str:
    return hmac.new(b"acceptance-pairing-key", value.encode(), hashlib.sha256).hexdigest()


def oauth_client_config_digest() -> str:
    config = {
        "platform": "claude",
        "admission_mode": "cimd",
        "client_id": "https://claude.example.com/oauth/client",
        "redirect_uris": sorted(
            [
                "https://claude.example.com/oauth/return",
                "https://claude.example.com/oauth/callback",
            ]
        ),
        "token_endpoint_auth_method": "none",
    }
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert canonical == (
        '{"admission_mode":"cimd","client_id":"https://claude.example.com/oauth/client",'
        '"platform":"claude","redirect_uris":["https://claude.example.com/oauth/callback",'
        '"https://claude.example.com/oauth/return"],"token_endpoint_auth_method":"none"}'
    )
    assert (
        hashlib.sha256(b"exomem-oauth-client-config:v1\0" + canonical.encode()).hexdigest()
        == "3c8bbd83906d29816f59d21b48a7e5a859379b124108b2abb1aa9a309ec3a339"
    )
    return "3c8bbd83906d29816f59d21b48a7e5a859379b124108b2abb1aa9a309ec3a339"


def test_oauth_client_config_digest_matches_shared_cimd_vector() -> None:
    assert (
        oauth_client_config_digest()
        == "3c8bbd83906d29816f59d21b48a7e5a859379b124108b2abb1aa9a309ec3a339"
    )


def copy_hosted_tree(destination: Path) -> Path:
    shutil.copytree(
        REPO_ROOT / "plugins" / "hosted",
        destination / "plugins" / "hosted",
        ignore=shutil.ignore_patterns("tmp*", ".exomem-hosted-render-*"),
    )
    return destination


def signed_evidence(
    root: Path, *, platform: str = "claude", secret: str = "operator-secret"
) -> dict[str, object]:
    compatibility = hosted_plugins.compatibility_manifest(root)
    definition = hosted_plugins.load_definition(root)
    generated = root / "plugins/hosted/generated"
    package_lock = json.loads((generated / f"{platform}.lock.json").read_text(encoding="utf-8"))
    archive_lock = json.loads((generated / f"{platform}.zip.lock.json").read_text(encoding="utf-8"))
    evidence: dict[str, object] = {
        "schema_version": 1,
        "platform": platform,
        "client_version": "1.0.0",
        "clean_client_identity_hmac_sha256": digest("clean-client-run"),
        "oauth_client_config_sha256": (
            oauth_client_config_digest() if platform == "claude" else digest("openai-client-config")
        ),
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "paired_run_hmac_sha256": digest("paired-run-1"),
        "test_identity": "hosted-client-plugins-v1",
        "exomem_identity_hmac_sha256": digest("identity-1"),
        "tenant_hmac_sha256": digest("tenant-1"),
        "entitlement_hmac_sha256": digest("entitlement-1"),
        "provisioning_operation_hmac_sha256": digest("operation-1"),
        "cell_hmac_sha256": digest("cell-1"),
        "identity_count": 1,
        "tenant_count": 1,
        "entitlement_count": 1,
        "operation_count": 1,
        "cell_count": 1,
        "volume_count": 1,
        "result_sha256": "1" * 64,
        "package_artifact_sha256": package_lock["artifact_sha256"],
        "archive_sha256": archive_lock["archive_sha256"],
        "compatibility_sha256": compatibility["compatibility_sha256"],
        "schema_contract_sha256": compatibility["schema_contract_sha256"],
        "command_surface_sha256": compatibility["command_surface_sha256"],
        "endpoint": compatibility["endpoint"],
        "plugin_version": definition.version,
        "profile": definition.profile,
        "operator_key_id": "operator-key",
        "native_install": True,
        "authorization": True,
        "tool_discovery": True,
        "content_recall": True,
        "citation": True,
        "durable_capture": True,
        "fresh_chat_recall": True,
    }
    if platform == "openai":
        evidence["registered_app_id_sha256"] = package_lock["registered_app_id_sha256"]
    evidence["operator_signature"] = hmac.new(
        secret.encode(), hosted_plugins._canonical_json(evidence), hashlib.sha256
    ).hexdigest()
    return evidence


def _sign_evidence(evidence: dict[str, object], *, secret: str = "operator-secret") -> None:
    evidence["operator_signature"] = hmac.new(
        secret.encode(),
        hosted_plugins._canonical_json(
            {key: value for key, value in evidence.items() if key != "operator_signature"}
        ),
        hashlib.sha256,
    ).hexdigest()


def v2_expectation(root: Path) -> dict[str, object]:
    cases = json.loads(
        (
            root / "plugins/hosted/candidates/hosted-alpha-agent-v2/selection-cases.json"
        ).read_text(encoding="utf-8")
    )
    return {
        "deployment_sha256": digest("deployed-v2"),
        "vault_purpose": "records-live-acceptance",
        "reset_epoch": "reset-v2",
        "principal_hmac_sha256": digest("principal-v2"),
        "audience_hmac_sha256": digest("audience-v2"),
        "client_contracts": {
            client: [
                contract["client"],
                contract["client_version"],
                contract["model_version"],
                contract["system_contract_version"],
            ]
            for client, contract in cases["client_contracts"].items()
        },
        "graph_proof_digest": digest("graph-v2"),
        "prompt_cases": {case["id"]: case["prompt_sha256"] for case in cases["cases"]},
        "selection_cases_sha256": hosted_plugins._sha256(hosted_plugins._canonical_json(cases)),
    }


def v2_signed_evidence(root: Path, expectation: dict[str, object]) -> dict[str, object]:
    evidence = signed_evidence(root)
    compatibility = hosted_plugins.compatibility_manifest(
        root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    )
    definition = hosted_plugins.load_definition(root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE)
    generated = root / "plugins/hosted/generated/candidates/hosted-alpha-agent-v2"
    lock = json.loads((generated / "claude.lock.json").read_text(encoding="utf-8"))
    archive_lock = json.loads((generated / "claude.zip.lock.json").read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    actions = [
        {"action": action, "outcome": outcome}
        for action, outcome in (
            ("describe", "completed"),
            ("validate", "completed"),
            ("create", "committed"),
            ("inspect", "completed"),
            ("query", "completed"),
            ("append", "committed"),
            ("update", "committed"),
            ("revise", "committed"),
            ("rebaseline", "committed"),
        )
    ]
    evidence.update(
        {
            "plugin_version": definition.version,
            "profile": definition.profile,
            "compatibility_sha256": compatibility["compatibility_sha256"],
            "schema_contract_sha256": compatibility["schema_contract_sha256"],
            "command_surface_sha256": compatibility["command_surface_sha256"],
            "package_artifact_sha256": lock["artifact_sha256"],
            "archive_sha256": archive_lock["archive_sha256"],
            "records_acceptance": {
                "schema_version": 1,
                "deployment": {"sha256": expectation["deployment_sha256"]},
                "release": {
                    "package": "exomem",
                    "version": definition.version,
                    "profile": hosted_plugins.LIFECYCLE_CANDIDATE,
                    "minimum_records_reader_version": 2,
                },
                "surface": {"mcp_digest": compatibility["schema_contract_sha256"]},
                "run": {
                    "nonce": "records-v2-run-20260812",
                    "timestamp": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "expires_at": (now + timedelta(hours=1))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                },
                "vault": {
                    "purpose": expectation["vault_purpose"],
                    "reset_epoch": expectation["reset_epoch"],
                },
                "identity": {
                    "principal_hmac_sha256": expectation["principal_hmac_sha256"],
                    "audience_hmac_sha256": expectation["audience_hmac_sha256"],
                },
                "client_contracts": {
                    client: dict(contract)
                    for client, contract in json.loads(
                        (
                            root
                            / "plugins/hosted/candidates/hosted-alpha-agent-v2/selection-cases.json"
                        ).read_text(encoding="utf-8")
                    )["client_contracts"].items()
                },
                "actions": actions,
                "mutations": [
                    {
                        "action": action,
                        "request_id": f"request-{action}",
                        "receipt_id": f"receipt-{action}",
                        "terminal_outcome": "committed",
                        "before_readback_sha256": digest(f"before-{action}"),
                        "after_readback_sha256": digest(f"after-{action}"),
                    }
                    for action in ("create", "append", "update", "revise", "rebaseline")
                ],
                "restart": {"outcome": "completed", "readback_sha256": digest("restart-v2")},
                "prompt_cases": [
                    {
                        "id": case["id"],
                        "sha256": case["prompt_sha256"],
                        "client": case["client"],
                        "action": "append" if case["expected"] == "append" else "proposal",
                        "outcome": "committed" if case["expected"] == "append" else "completed",
                        "mutation": case["expected"] == "append",
                    }
                    for case in json.loads(
                        (
                            root
                            / "plugins/hosted/candidates/hosted-alpha-agent-v2/selection-cases.json"
                        ).read_text(encoding="utf-8")
                    )["cases"]
                ],
                "graph_availability": {"proof_digest": expectation["graph_proof_digest"]},
            },
        }
    )
    _sign_evidence(evidence)
    return evidence


def test_promotion_rejects_discovery_only_or_mocked_evidence() -> None:
    with pytest.raises(ValueError, match="content-bearing"):
        hosted_plugins.promote(
            REPO_ROOT,
            "claude",
            {"mocked": True},
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(REPO_ROOT, "claude"),
        )


def test_pending_records_are_not_distributed() -> None:
    distribution = hosted_plugins.distribution_manifest(REPO_ROOT)

    assert distribution == {"live_platforms": [], "cross_client_ready": False}


def test_v2_selection_cases_are_fixed_and_content_free() -> None:
    cases = json.loads(
        (
            REPO_ROOT
            / "plugins/hosted/candidates/hosted-alpha-agent-v2/selection-cases.json"
        ).read_text(encoding="utf-8")
    )

    assert cases["schema_version"] == 1
    assert set(cases["client_contracts"]) == {"codex", "claude-code"}
    assert {(case["client"], case["expected"]) for case in cases["cases"]} == {
        ("codex", "append"),
        ("claude-code", "append"),
        ("codex", "proposal"),
        ("claude-code", "proposal"),
    }
    assert all(set(case) == {"id", "client", "expected", "prompt_sha256"} for case in cases["cases"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", case["prompt_sha256"]) for case in cases["cases"])


def test_v2_promotion_refuses_unsigned_or_incomplete_records_evidence(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(root, candidate="hosted-alpha-agent-v2", platform="claude")
    evidence = signed_evidence(root)
    compatibility = hosted_plugins.compatibility_manifest(root, candidate="hosted-alpha-agent-v2")
    generated = root / "plugins/hosted/generated/candidates/hosted-alpha-agent-v2"
    lock = json.loads((generated / "claude.lock.json").read_text(encoding="utf-8"))
    archive_lock = json.loads((generated / "claude.zip.lock.json").read_text(encoding="utf-8"))
    evidence["profile"] = "hosted-alpha-agent-v2"
    evidence["plugin_version"] = "0.2.0"
    evidence["compatibility_sha256"] = compatibility["compatibility_sha256"]
    evidence["schema_contract_sha256"] = compatibility["schema_contract_sha256"]
    evidence["command_surface_sha256"] = compatibility["command_surface_sha256"]
    evidence["package_artifact_sha256"] = lock["artifact_sha256"]
    evidence["archive_sha256"] = archive_lock["archive_sha256"]
    evidence["records_acceptance"] = {"prose": "trust me"}
    evidence["operator_signature"] = hmac.new(
        b"operator-secret",
        hosted_plugins._canonical_json(
            {key: value for key, value in evidence.items() if key != "operator_signature"}
        ),
        hashlib.sha256,
    ).hexdigest()

    with pytest.raises(ValueError, match="lifecycle evidence"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            candidate="hosted-alpha-agent-v2",
                records_expectation=v2_expectation(root),
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate="hosted-alpha-agent-v2"
            ),
        )


def test_v2_promotion_binds_candidate_lock_and_replays_exact_evidence_only(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE, platform="claude")
    expectation = v2_expectation(root)
    evidence = v2_signed_evidence(root, expectation)
    v1_bytes = {
        path.relative_to(root): path.read_bytes()
        for path in (root / "plugins/hosted/promotion").glob("*.json")
    }
    pending_digest = hosted_plugins.promotion_record_sha256(
        root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    )

    hosted_plugins.promote(
        root,
        "claude",
        evidence,
        candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
        records_expectation=expectation,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="pending",
        expected_record_sha256=pending_digest,
    )

    record_path = hosted_plugins.promotion_record(
        root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    )
    first_bytes = record_path.read_bytes()
    record = json.loads(first_bytes)
    expected_lock = json.loads(
        (
            root
            / "plugins/hosted/generated/candidates/hosted-alpha-agent-v2/claude.lock.json"
        ).read_text(encoding="utf-8")
    )
    assert record["state"] == "live"
    assert record["candidate"] == hosted_plugins.LIFECYCLE_CANDIDATE
    assert record["package_lock"] == expected_lock
    assert record["evidence"] == evidence
    assert {
        path.relative_to(root): path.read_bytes()
        for path in (root / "plugins/hosted/promotion").glob("*.json")
    } == v1_bytes

    live_digest = hosted_plugins.promotion_record_sha256(
        root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    )
    hosted_plugins.promote(
        root,
        "claude",
        evidence,
        candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
        records_expectation=expectation,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="live",
        expected_record_sha256=live_digest,
    )
    assert record_path.read_bytes() == first_bytes

    durable = json.loads(first_bytes)
    durable["evidence"]["records_acceptance"]["run"] = {
        "nonce": "records-v2-run-20200101",
        "timestamp": "2020-01-01T00:00:00Z",
        "expires_at": "2020-01-01T01:00:00Z",
    }
    _sign_evidence(durable["evidence"])
    record_path.write_bytes(hosted_plugins._canonical_json(durable) + b"\n")
    assert hosted_plugins.distribution_manifest(
        root,
        candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
        records_expectation=expectation,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
    ) == {"live_platforms": ["claude"], "cross_client_ready": False}

    replay = json.loads(json.dumps(durable))
    replay["evidence"]["timestamp"] = "2020-01-01T00:00:00Z"
    _sign_evidence(replay["evidence"])
    record_path.write_bytes(hosted_plugins._canonical_json(replay) + b"\n")
    stale_live_digest = hosted_plugins.promotion_record_sha256(
        root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    )
    hosted_plugins.promote(
        root,
        "claude",
        replay["evidence"],
        candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
        records_expectation=expectation,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="live",
        expected_record_sha256=stale_live_digest,
    )
    stale_changed = json.loads(json.dumps(replay["evidence"]))
    stale_changed["result_sha256"] = "0" * 64
    _sign_evidence(stale_changed)
    with pytest.raises(ValueError, match="stale"):
        hosted_plugins.promote(
            root,
            "claude",
            stale_changed,
            candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
            records_expectation=expectation,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="live",
            expected_record_sha256=stale_live_digest,
        )

    changed_evidence = json.loads(json.dumps(evidence))
    changed_evidence["result_sha256"] = "0" * 64
    _sign_evidence(changed_evidence)
    for candidate, expected_state, expected_digest in (
        (hosted_plugins.LIFECYCLE_CANDIDATE, "live", live_digest),
        (hosted_plugins.LIFECYCLE_CANDIDATE, "live", pending_digest),
        (hosted_plugins.DEFAULT_CANDIDATE, "pending", live_digest),
    ):
        with pytest.raises(ValueError, match="changed"):
            hosted_plugins.promote(
                root,
                "claude",
                changed_evidence,
                candidate=candidate,
                records_expectation=expectation,
                trusted_key_id="operator-key",
                trusted_secret="operator-secret",
                expected_state=expected_state,
                expected_record_sha256=expected_digest,
            )

    cases_path = root / "plugins/hosted/candidates/hosted-alpha-agent-v2/selection-cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases["cases"][0]["prompt_sha256"] = "0" * 64
    cases_path.write_text(json.dumps(cases), encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        hosted_plugins.promote(
            root,
            "claude",
            replay["evidence"],
            candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
            records_expectation=expectation,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="live",
            expected_record_sha256=stale_live_digest,
        )


def test_promote_cli_preserves_v2_candidate_and_operator_expectation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_path = tmp_path / "evidence.json"
    expectation_path = tmp_path / "expectation.json"
    evidence_path.write_text("{}", encoding="utf-8")
    expectation_path.write_text("{}", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("hosted_plugin_script", _HOSTED_PLUGIN_SCRIPT)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    captured: dict[str, object] = {}
    monkeypatch.setattr(script.hosted_plugins, "promote", lambda *args, **kwargs: captured.update(kwargs))
    monkeypatch.setenv("EXOMEM_HOSTED_PROMOTION_SECRET", "operator-secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hosted-plugin.py",
            "promote",
            "--platform",
            "claude",
            "--candidate",
            hosted_plugins.LIFECYCLE_CANDIDATE,
            "--evidence",
            str(evidence_path),
            "--records-expectation",
            str(expectation_path),
            "--operator-key-id",
            "operator-key",
            "--expected-state",
            "pending",
            "--expected-record-sha256",
            "0" * 64,
        ],
    )

    assert script.main() == 0
    assert captured["candidate"] == hosted_plugins.LIFECYCLE_CANDIDATE
    assert captured["records_expectation"] == {}


def test_v2_archive_and_distribution_are_candidate_scoped(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE, platform="claude")

    archived = hosted_plugins.archive(
        root,
        tmp_path / "archive",
        platform="claude",
        candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
    )

    assert (archived / "claude.zip").read_bytes() == (
        root / "plugins/hosted/generated/candidates/hosted-alpha-agent-v2/claude.zip"
    ).read_bytes()
    assert hosted_plugins.distribution_manifest(
        root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    ) == {"live_platforms": [], "cross_client_ready": False}
    assert hosted_plugins.distribution_manifest(root) == {
        "live_platforms": [],
        "cross_client_ready": False,
    }


@pytest.mark.parametrize("command", ["archive", "demote"])
def test_cli_forwards_v2_candidate_to_archive_and_demote(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("hosted_plugin_candidate_script", _HOSTED_PLUGIN_SCRIPT)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        script.hosted_plugins,
        "archive" if command == "archive" else "demote",
        lambda *args, **kwargs: captured.update(kwargs),
    )
    argv = ["hosted-plugin.py", command, "--candidate", hosted_plugins.LIFECYCLE_CANDIDATE]
    if command == "archive":
        argv.extend(("--platform", "claude"))
    else:
        argv.extend(
            (
                "--platform", "claude", "--reason", "client-regression", "--expected-state", "live",
                "--expected-record-sha256", "0" * 64,
            )
        )
    monkeypatch.setattr(sys, "argv", argv)

    assert script.main() == 0
    assert captured["candidate"] == hosted_plugins.LIFECYCLE_CANDIDATE


def test_cli_status_reads_v2_pending_records_without_v1_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("hosted_plugin_status_script", _HOSTED_PLUGIN_SCRIPT)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(
        script.hosted_plugins,
        "check_compatibility_descriptor",
        lambda *args: pytest.fail("v2 status must not inspect the v1 descriptor"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["hosted-plugin.py", "status", "--candidate", hosted_plugins.LIFECYCLE_CANDIDATE],
    )

    assert script.main() == 0
    status = json.loads(capsys.readouterr().out)
    assert status["records"]["claude"] == hosted_plugins.promotion_record_sha256(
        REPO_ROOT, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item["records_acceptance"].pop("run"),
        lambda item: item["records_acceptance"].__setitem__("prose", "trust me"),
        lambda item: item.__setitem__("operator_signature", "0" * 64),
        lambda item: item["records_acceptance"].__setitem__("prose", "trust me"),
        lambda item: item.__setitem__("timestamp", "2020-01-01T00:00:00Z"),
        lambda item: item["records_acceptance"]["run"].__setitem__(
            "expires_at", "2020-01-01T00:00:00Z"
        ),
        lambda item: item["records_acceptance"]["vault"].__setitem__("reset_epoch", "wrong"),
        lambda item: item["records_acceptance"]["client_contracts"]["codex"].__setitem__("client", "wrong"),
        lambda item: item["records_acceptance"]["client_contracts"]["codex"].__setitem__(
            "model_version", "wrong"
        ),
        lambda item: item["records_acceptance"]["client_contracts"]["codex"].__setitem__(
            "system_contract_version", "wrong"
        ),
        lambda item: item["records_acceptance"]["release"].__setitem__("profile", "wrong"),
        lambda item: item["records_acceptance"]["release"].__setitem__("version", "0.0.0"),
        lambda item: item["records_acceptance"]["release"].__setitem__(
            "minimum_records_reader_version", 1
        ),
        lambda item: item["records_acceptance"]["surface"].__setitem__("mcp_digest", "0" * 64),
        lambda item: item["records_acceptance"]["mutations"][0].__setitem__(
            "after_readback_sha256", item["records_acceptance"]["mutations"][0]["before_readback_sha256"]
        ),
        lambda item: item["records_acceptance"]["actions"].pop(),
        lambda item: item["records_acceptance"]["restart"].__setitem__("outcome", "failed"),
        lambda item: item["records_acceptance"]["graph_availability"].__setitem__(
            "proof_digest", "0" * 64
        ),
        lambda item: item["records_acceptance"]["prompt_cases"][0].__setitem__("sha256", "0" * 64),
        lambda item: item["records_acceptance"]["prompt_cases"][0].__setitem__("outcome", "completed"),
        lambda item: item["records_acceptance"]["prompt_cases"][0].__setitem__("mutation", False),
        lambda item: item["records_acceptance"]["prompt_cases"].append(
            {
                "id": "invented-case",
                "sha256": "0" * 64,
                "client": "codex",
                "action": "append",
                "outcome": "committed",
                "mutation": True,
            }
        ),
    ],
)
def test_v2_promotion_refuses_unbound_or_incomplete_live_evidence(
    tmp_path: Path, mutate: object
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE, platform="claude")
    expectation = v2_expectation(root)
    evidence = v2_signed_evidence(root, expectation)
    mutate(evidence)  # type: ignore[operator]
    if evidence["operator_signature"] != "0" * 64:
        _sign_evidence(evidence)

    with pytest.raises(ValueError):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
            records_expectation=expectation,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
            ),
        )


def test_v2_promotion_refuses_operator_selection_case_substitution(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE, platform="claude")
    expectation = v2_expectation(root)
    expectation["selection_cases_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="selection cases"):
        hosted_plugins.promote(
            root,
            "claude",
            v2_signed_evidence(root, v2_expectation(root)),
            candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
            records_expectation=expectation,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
            ),
        )


def test_v2_promotion_refuses_missing_or_swapped_client_contracts(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(root, candidate=hosted_plugins.LIFECYCLE_CANDIDATE, platform="claude")
    expectation = v2_expectation(root)
    expectation["client_contracts"].pop("claude-code")  # type: ignore[index]
    with pytest.raises(ValueError, match="expectations"):
        hosted_plugins.promote(
            root,
            "claude",
            v2_signed_evidence(root, v2_expectation(root)),
            candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
            records_expectation=expectation,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
            ),
        )

    evidence = v2_signed_evidence(root, v2_expectation(root))
    evidence["records_acceptance"]["client_contracts"]["codex"] = evidence["records_acceptance"]["client_contracts"]["claude-code"]  # type: ignore[index]
    _sign_evidence(evidence)
    with pytest.raises(ValueError, match="client contracts"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
            records_expectation=v2_expectation(root),
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=hosted_plugins.LIFECYCLE_CANDIDATE
            ),
        )


def test_promotion_requires_exact_signed_evidence_and_compare_and_swap(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.regenerate_claude(root)
    evidence = signed_evidence(root)
    pending_digest = hosted_plugins.promotion_record_sha256(root, "claude")

    hosted_plugins.promote(
        root,
        "claude",
        evidence,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="pending",
        expected_record_sha256=pending_digest,
    )

    assert hosted_plugins.distribution_manifest(
        root, trusted_key_id="operator-key", trusted_secret="operator-secret"
    ) == {"live_platforms": ["claude"], "cross_client_ready": False}
    with pytest.raises(ValueError, match="changed"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=pending_digest,
        )

    live_digest = hosted_plugins.promotion_record_sha256(root, "claude")
    hosted_plugins.demote(
        root,
        "claude",
        "client-regression",
        expected_state="live",
        expected_record_sha256=live_digest,
    )
    assert hosted_plugins.distribution_manifest(root) == {
        "live_platforms": [],
        "cross_client_ready": False,
    }


def test_promotion_requires_and_persists_signed_oauth_client_config_digest(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    evidence = signed_evidence(root)
    missing_digest = {
        key: value for key, value in evidence.items() if key != "oauth_client_config_sha256"
    }
    missing_digest["operator_signature"] = hmac.new(
        b"operator-secret",
        hosted_plugins._canonical_json(
            {key: value for key, value in missing_digest.items() if key != "operator_signature"}
        ),
        hashlib.sha256,
    ).hexdigest()

    with pytest.raises(ValueError, match="content-bearing"):
        hosted_plugins.promote(
            root,
            "claude",
            missing_digest,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude"),
        )

    malformed_digest = {**evidence, "oauth_client_config_sha256": "not-a-digest"}
    malformed_digest["operator_signature"] = hmac.new(
        b"operator-secret",
        hosted_plugins._canonical_json(
            {key: value for key, value in malformed_digest.items() if key != "operator_signature"}
        ),
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(ValueError, match="digests"):
        hosted_plugins.promote(
            root,
            "claude",
            malformed_digest,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude"),
        )

    hosted_plugins.promote(
        root,
        "claude",
        evidence,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="pending",
        expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude"),
    )

    record = json.loads(hosted_plugins.promotion_record(root, "claude").read_text(encoding="utf-8"))
    assert record["evidence"]["oauth_client_config_sha256"] == oauth_client_config_digest()


def test_openai_promotion_binds_the_registered_app_identity_and_claude_rejects_it(
    tmp_path: Path,
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(
        root,
        platform="openai",
        openai_app_id="plugin_asdk_app_releaseinput123",
    )
    evidence = signed_evidence(root, platform="openai")
    missing_identity = {
        key: value for key, value in evidence.items() if key != "registered_app_id_sha256"
    }
    missing_identity["operator_signature"] = hmac.new(
        b"operator-secret",
        hosted_plugins._canonical_json(
            {key: value for key, value in missing_identity.items() if key != "operator_signature"}
        ),
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(ValueError, match="content-bearing"):
        hosted_plugins.promote(
            root,
            "openai",
            missing_identity,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "openai"),
        )

    wrong_identity = {**evidence, "registered_app_id_sha256": "0" * 64}
    wrong_identity["operator_signature"] = hmac.new(
        b"operator-secret",
        hosted_plugins._canonical_json(
            {key: value for key, value in wrong_identity.items() if key != "operator_signature"}
        ),
        hashlib.sha256,
    ).hexdigest()

    with pytest.raises(ValueError, match="registered app identity"):
        hosted_plugins.promote(
            root,
            "openai",
            wrong_identity,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "openai"),
        )

    hosted_plugins.promote(
        root,
        "openai",
        evidence,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="pending",
        expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "openai"),
    )
    record = json.loads(hosted_plugins.promotion_record(root, "openai").read_text(encoding="utf-8"))
    assert record["evidence"]["registered_app_id_sha256"] == evidence["registered_app_id_sha256"]

    claude_with_registered_app = {
        **signed_evidence(root),
        "registered_app_id_sha256": evidence["registered_app_id_sha256"],
    }
    claude_with_registered_app["operator_signature"] = hmac.new(
        b"operator-secret",
        hosted_plugins._canonical_json(
            {
                key: value
                for key, value in claude_with_registered_app.items()
                if key != "operator_signature"
            }
        ),
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(ValueError, match="content-bearing"):
        hosted_plugins.promote(
            root,
            "claude",
            claude_with_registered_app,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude"),
        )


def test_demotion_rejects_free_form_public_reason(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    record_path = hosted_plugins.promotion_record(root, "claude")
    record_path.write_text(
        json.dumps({"schema_version": 1, "platform": "claude", "state": "live"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stable reason code"):
        hosted_plugins.demote(
            root,
            "claude",
            "Customer tenant failed in a private region",
            expected_state="live",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude"),
        )


def test_promotion_rejects_wrongly_typed_success_and_duplicate_counts(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.regenerate_claude(root)
    evidence = signed_evidence(root)
    pending_digest = hosted_plugins.promotion_record_sha256(root, "claude")

    with pytest.raises(ValueError, match="successful"):
        hosted_plugins.promote(
            root,
            "claude",
            {**evidence, "authorization": "failed"},
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=pending_digest,
        )
    with pytest.raises(ValueError, match="resource counts"):
        hosted_plugins.promote(
            root,
            "claude",
            {**evidence, "tenant_count": 2},
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=pending_digest,
        )


def test_live_distribution_does_not_expire_with_acceptance_timestamp(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.regenerate_claude(root)
    evidence = signed_evidence(root)
    hosted_plugins.promote(
        root,
        "claude",
        evidence,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="pending",
        expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude"),
    )
    record_path = hosted_plugins.promotion_record(root, "claude")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["evidence"]["timestamp"] = "2026-01-01T00:00:00Z"
    unsigned = {
        key: value for key, value in record["evidence"].items() if key != "operator_signature"
    }
    record["evidence"]["operator_signature"] = hmac.new(
        b"operator-secret", hosted_plugins._canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    record_path.write_text(json.dumps(record), encoding="utf-8")

    assert hosted_plugins.distribution_manifest(
        root, trusted_key_id="operator-key", trusted_secret="operator-secret"
    ) == {"live_platforms": ["claude"], "cross_client_ready": False}


def test_promotion_recomputes_archive_instead_of_trusting_its_lock(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.regenerate_claude(root)
    evidence = signed_evidence(root)
    archive = root / "plugins/hosted/generated/claude.zip"
    with zipfile.ZipFile(archive, "a") as package:
        package.comment = b"tampered"
    lock = root / "plugins/hosted/generated/claude.zip.lock.json"
    lock.write_text(
        json.dumps(
            {"platform": "claude", "archive_sha256": hosted_plugins._sha256(archive.read_bytes())}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stale"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude"),
        )


def test_promotion_never_accepts_a_trust_key_from_evidence() -> None:
    with pytest.raises(ValueError, match="operator-trusted signing key"):
        hosted_plugins.promote(
            REPO_ROOT,
            "claude",
            {"operator_key_id": "evidence-key", "operator_signature": "0" * 64},
        )


def test_public_gate_rejects_private_tokens_in_source_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = tmp_path / "plugins" / "hosted"
    (hosted / "assets").mkdir(parents=True)
    (hosted / "definition.json").write_text("{}", encoding="utf-8")
    (hosted / "behavior-fixtures-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "acceptance-fixture-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "assets" / "icon.svg").write_text("api_secret=private", encoding="utf-8")
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="unsafe"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)


def test_public_gate_rejects_quoted_json_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = tmp_path / "plugins" / "hosted"
    hosted.mkdir(parents=True)
    (hosted / "definition.json").write_text("{}", encoding="utf-8")
    (hosted / "behavior-fixtures-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "acceptance-fixture-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "acceptance-fixture-v1.json").write_text(
        '{"access_token":"private"}', encoding="utf-8"
    )
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="credential value"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)


def test_public_gate_rejects_raw_promotion_resource_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = tmp_path / "plugins" / "hosted"
    (hosted / "promotion").mkdir(parents=True)
    (hosted / "definition.json").write_text("{}", encoding="utf-8")
    (hosted / "behavior-fixtures-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "acceptance-fixture-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "promotion/claude.json").write_text(
        '{"state":"live","evidence":{"tenant":"production-tenant"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="raw private identifier"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)


def test_public_gate_rejects_private_tokens_in_archive_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = tmp_path / "plugins" / "hosted"
    (hosted / "generated").mkdir(parents=True)
    (hosted / "definition.json").write_text("{}", encoding="utf-8")
    (hosted / "behavior-fixtures-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "acceptance-fixture-v1.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(hosted / "generated" / "claude.zip", "w") as archive:
        archive.writestr("skills/exomem/SKILL.md", "api_secret=private")
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="unsafe"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)
