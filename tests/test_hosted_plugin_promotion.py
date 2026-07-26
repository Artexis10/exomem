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


def copy_hosted_tree(destination: Path) -> Path:
    shutil.copytree(
        REPO_ROOT / "plugins" / "hosted",
        destination / "plugins" / "hosted",
        ignore=shutil.ignore_patterns("tmp*", ".exomem-hosted-render-*"),
    )
    return destination


def signed_evidence(root: Path, *, secret: str = "operator-secret") -> dict[str, object]:
    compatibility = hosted_plugins.compatibility_manifest(root)
    definition = hosted_plugins.load_definition(root)
    generated = root / "plugins/hosted/generated"
    package_lock = json.loads((generated / "claude.lock.json").read_text(encoding="utf-8"))
    archive_lock = json.loads((generated / "claude.zip.lock.json").read_text(encoding="utf-8"))
    evidence: dict[str, object] = {
        "schema_version": 1,
        "platform": "claude",
        "client_version": "1.0.0",
        "clean_client_identity": "clean-client-run",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "paired_run_id": "paired-run-1",
        "test_identity": "hosted-client-plugins-v1",
        "exomem_identity": "identity-1",
        "tenant": "tenant-1",
        "entitlement": "entitlement-1",
        "provisioning_operation": "operation-1",
        "cell": "cell-1",
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
    evidence["operator_signature"] = hmac.new(
        secret.encode(), hosted_plugins._canonical_json(evidence), hashlib.sha256
    ).hexdigest()
    return evidence


def test_promotion_rejects_discovery_only_or_mocked_evidence() -> None:
    with pytest.raises(ValueError, match="content-bearing"):
        hosted_plugins.promote(REPO_ROOT, "claude", {"mocked": True})


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
        "client regression",
        expected_state="live",
        expected_record_sha256=live_digest,
    )
    assert hosted_plugins.distribution_manifest(root) == {
        "live_platforms": [],
        "cross_client_ready": False,
    }


def test_promotion_rejects_wrongly_typed_success_and_duplicate_counts(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.regenerate_claude(root)
    evidence = signed_evidence(root)

    with pytest.raises(ValueError, match="successful"):
        hosted_plugins.promote(
            root,
            "claude",
            {**evidence, "authorization": "failed"},
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
        )
    with pytest.raises(ValueError, match="resource counts"):
        hosted_plugins.promote(
            root,
            "claude",
            {**evidence, "tenant_count": 2},
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
        )


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
    (hosted / "acceptance-fixture-v1.json").write_text(
        '{"access_token":"private"}', encoding="utf-8"
    )
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="unsafe"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)


def test_public_gate_rejects_private_tokens_in_archive_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = tmp_path / "plugins" / "hosted"
    (hosted / "generated").mkdir(parents=True)
    (hosted / "definition.json").write_text("{}", encoding="utf-8")
    (hosted / "behavior-fixtures-v1.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(hosted / "generated" / "claude.zip", "w") as archive:
        archive.writestr("skills/exomem/SKILL.md", "api_secret=private")
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="unsafe"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)
