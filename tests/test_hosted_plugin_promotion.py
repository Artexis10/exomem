from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exomem import hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        openai_app_id="asdk_app_releaseinput123",
    )
    hosted_plugins.archive(
        root,
        platform="openai",
        openai_app_id="asdk_app_releaseinput123",
        output=root / "plugins/hosted/generated",
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
