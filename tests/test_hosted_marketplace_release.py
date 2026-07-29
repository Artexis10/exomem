from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem import hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]


def copy_hosted_tree(destination: Path) -> Path:
    shutil.copytree(REPO_ROOT / "plugins" / "hosted", destination / "plugins" / "hosted")
    return destination


def signed_evidence(
    value: dict[str, object], secret: str = "directory-test-secret"
) -> dict[str, object]:
    value = {key: nested for key, nested in value.items() if key != "operator_signature"}
    value["operator_key_id"] = "directory-test-key"
    value["operator_signature"] = hosted_plugins.hmac.new(
        secret.encode("utf-8"),
        hosted_plugins._canonical_json(value),
        hosted_plugins.hashlib.sha256,
    ).hexdigest()
    return value


def ready_directory_evidence(root: Path) -> tuple[str, str]:
    secret = "directory-test-secret"
    checked_at = datetime.now(UTC).replace(microsecond=0)
    expires_at = checked_at + timedelta(hours=1)

    def timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    surfaces = {
        name: {"ok": True, "content_sha256": "a" * 64}
        for name in (
            "website_url",
            "documentation_url",
            "setup_url",
            "privacy_url",
            "terms_url",
            "support_url",
            "oauth_discovery",
            "mcp_authorization",
            "mcp_initialize",
            "tool_discovery",
        )
    }
    evidence_root = root / "plugins/hosted/directory"
    compatibility = hosted_plugins.compatibility_manifest(root)
    common = {
        "schema_version": 1,
        "deployment_sha256": "b" * 64,
        "checked_at": timestamp(checked_at),
        "expires_at": timestamp(expires_at),
    }
    (evidence_root / "production-evidence.json").write_text(
        json.dumps(
            signed_evidence(
                {
                    **common,
                    "evidence_type": "directory-production-probes",
                    "surfaces": surfaces,
                    "compatibility_sha256": compatibility["compatibility_sha256"],
                    "command_surface_sha256": compatibility["command_surface_sha256"],
                    "schema_contract_sha256": compatibility["schema_contract_sha256"],
                    "full_tool_contract_sha256": hosted_plugins._full_tool_contract_sha256(
                        compatibility
                    ),
                    "origin_rejection": True,
                    "response_minimization": True,
                    "sampled_output_sale_free": True,
                },
                secret,
            )
        ),
        encoding="utf-8",
    )
    channels = {
        channel: {
            "provider_registration": True,
            "publisher_verified": True,
            "policy_approved": True,
            "reviewer_seeded": True,
            **({"domain_verified": True} if channel == "openai-plugin" else {}),
        }
        for channel in hosted_plugins.DIRECTORY_CHANNELS
    }
    (evidence_root / "prerequisite-evidence.json").write_text(
        json.dumps(
            signed_evidence(
                {**common, "evidence_type": "directory-prerequisites", "channels": channels}, secret
            )
        ),
        encoding="utf-8",
    )
    admission = {
        key: True
        for key in (
            "ordinary_acquisition",
            "capacity",
            "quotas",
            "abuse_controls",
            "spend_alarms",
            "support_coverage",
            "pricing_decision",
        )
    }
    (evidence_root / "public-admission-evidence.json").write_text(
        json.dumps(
            signed_evidence(
                {**common, "evidence_type": "directory-public-admission", "admission": admission},
                secret,
            )
        ),
        encoding="utf-8",
    )
    bindings = hosted_plugins._directory_bindings(root, "claude-connector", openai_app_id=None)
    lock = json.loads(
        (root / "plugins/hosted/generated/claude.lock.json").read_text(encoding="utf-8")
    )
    (root / "plugins/hosted/promotion/claude.json").write_text(
        json.dumps(
            {
                "state": "live",
                "compatibility_sha256": bindings["compatibility_sha256"],
                "package_lock": lock,
            }
        ),
        encoding="utf-8",
    )
    return "directory-test-key", secret


def load_hosted_plugin_cli() -> object:
    spec = importlib.util.spec_from_file_location(
        "hosted_plugin_cli", REPO_ROOT / "scripts" / "hosted-plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_marketplace_packets_are_deterministic_and_provider_shaped() -> None:
    first = hosted_plugins.directory_packets(REPO_ROOT, openai_app_id="asdk_app_releaseinput123")
    second = hosted_plugins.directory_packets(REPO_ROOT, openai_app_id="asdk_app_releaseinput123")

    assert first == second
    assert set(first) == set(hosted_plugins.DIRECTORY_CHANNELS)
    openai = json.loads(first["openai-plugin"])
    assert openai["channel"] == "openai-plugin"
    assert openai["acceptance_surfaces"] == ["chatgpt", "codex"]
    assert openai["screenshots"] == {"status": "not_applicable", "reason": "no MCP App UI"}
    assert len(openai["review_cases"]["positive"]) >= 5
    assert len(openai["review_cases"]["negative"]) >= 3
    assert all(
        {"title", "readOnlyHint", "destructiveHint", "openWorldHint"} <= set(tool["annotations"])
        for tool in openai["tools"]
    )
    assert all(
        set(tool["annotation_explanations"])
        == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        and all(explanation.strip() for explanation in tool["annotation_explanations"].values())
        for tool in openai["tools"]
    )
    assert openai["review_recording"] == {
        "required": True,
        "operator_supplied": True,
    }
    assert "recording_url" not in json.dumps(openai)
    assert all("_" not in tool["annotations"]["title"] for tool in openai["tools"])
    assert "asdk_app_releaseinput123" not in first["openai-plugin"].decode("utf-8")
    assert openai["brand_asset"] == {
        "path": "assets/icon.svg",
        "sha256": hosted_plugins._sha256(
            (REPO_ROOT / "plugins/hosted/assets/icon.svg").read_bytes()
        ),
    }
    assert openai["categories"] == ["Productivity"]
    assert openai["documentation_url"] == "https://substratesystems.io/exomem/setup"
    assert openai["setup_url"] == "https://substratesystems.io/exomem/setup"
    assert set(openai["capabilities"]) == {"read", "write"}
    assert len(openai["use_cases"]) >= 2


def test_marketplace_packet_preserves_the_complete_live_tool_contract() -> None:
    packet = json.loads(
        hosted_plugins.directory_packets(
            REPO_ROOT, channel="openai-plugin", openai_app_id="asdk_app_releaseinput123"
        )["openai-plugin"]
    )
    compatibility = hosted_plugins.compatibility_manifest(REPO_ROOT)
    expected_tools = [
        {
            "name": entry["name"],
            "description": entry["mcp_tool"]["description"],
            "input_schema": entry["mcp_tool"]["inputSchema"],
            "output_schema": entry["mcp_tool"].get("outputSchema"),
            "retry_semantics": (
                "idempotent"
                if entry["mcp_tool"]["annotations"].get("idempotentHint")
                else "do_not_retry"
            ),
            "annotations": {
                key: entry["mcp_tool"]["annotations"][key]
                for key in (
                    "title",
                    "readOnlyHint",
                    "destructiveHint",
                    "idempotentHint",
                    "openWorldHint",
                )
            },
        }
        for entry in compatibility["agent_contract"]["commands"]
    ]

    assert [
        {key: value for key, value in tool.items() if key != "annotation_explanations"}
        for tool in packet["tools"]
    ] == [
        tool for tool in expected_tools
    ]
    assert all(
        set(tool["annotation_explanations"])
        == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        and all(isinstance(value, str) and value.strip() for value in tool["annotation_explanations"].values())
        for tool in packet["tools"]
    )
    tools = {tool["name"]: tool for tool in packet["tools"]}
    assert "draft_token" in tools["remember"]["input_schema"]["properties"]
    assert "transition_token" in tools["observe_memory"]["input_schema"]["properties"]


def test_openai_packet_rejects_missing_boolean_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    original_manifest = hosted_plugins.compatibility_manifest

    def incomplete_annotations(repo_root: Path | None = None) -> dict[str, object]:
        manifest = json.loads(json.dumps(original_manifest(repo_root)))
        del manifest["agent_contract"]["commands"][0]["mcp_tool"]["annotations"]["idempotentHint"]
        return manifest

    monkeypatch.setattr(hosted_plugins, "compatibility_manifest", incomplete_annotations)
    with pytest.raises(ValueError, match="tool annotations are incomplete"):
        hosted_plugins.directory_packets(
            root, channel="openai-plugin", openai_app_id="asdk_app_releaseinput123"
        )


@pytest.mark.parametrize("unsafe_key", ["default", "example", "const", "value"])
def test_public_schema_scan_rejects_credential_values_but_allows_property_names(
    tmp_path: Path, unsafe_key: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/generated/compatibility.json"
    compatibility = json.loads(path.read_text(encoding="utf-8"))
    schema = compatibility["agent_contract"]["commands"][0]["mcp_tool"]["inputSchema"]
    schema["properties"]["api_token"] = {"type": "string", unsafe_key: "actual-secret"}
    path.write_text(json.dumps(compatibility), encoding="utf-8")

    with pytest.raises(ValueError, match="credential value"):
        hosted_plugins.validate_hosted_public_inputs(root)


def test_marketplace_definition_rejects_public_url_and_publisher_drift(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["common"]["publisher"] = "Another publisher"
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match="publisher"):
        hosted_plugins.load_marketplace_definition(root)

    definition["common"]["publisher"] = "Substrate Systems OÜ"
    definition["common"]["support_url"] = "http://localhost/support"
    path.write_text(json.dumps(definition), encoding="utf-8")
    with pytest.raises(ValueError, match="public HTTPS"):
        hosted_plugins.load_marketplace_definition(root)


def test_openai_listing_limits_allow_exact_boundaries_and_reject_next_character(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["channels"]["openai-plugin"]["short_description"] = "x" * 30
    definition["channels"]["openai-plugin"]["starter_prompts"][0] = "x" * 128
    path.write_text(json.dumps(definition), encoding="utf-8")

    hosted_plugins.load_marketplace_definition(root)

    definition["channels"]["openai-plugin"]["short_description"] = "x" * 31
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match="short_description"):
        hosted_plugins.load_marketplace_definition(root)

    definition["channels"]["openai-plugin"]["short_description"] = "x" * 30
    definition["channels"]["openai-plugin"]["starter_prompts"][0] = "x" * 129
    path.write_text(json.dumps(definition), encoding="utf-8")
    with pytest.raises(ValueError, match="starter_prompts"):
        hosted_plugins.load_marketplace_definition(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (("common", "release_notes"), "Private alpha access is available."),
        (("common", "description"), "A trial service for governed knowledge."),
        (("channels", "openai-plugin", "title"), "Exomem demo"),
        (("channels", "openai-plugin", "short_description"), "Hypothetical knowledge."),
        (("channels", "openai-plugin", "starter_prompts", 0), "Show what is not yet built."),
    ],
)
def test_openai_listing_rejects_release_stage_claims(
    tmp_path: Path, field: tuple[object, ...], value: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    target = definition
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = value
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match="release-stage"):
        hosted_plugins.load_marketplace_definition(root)


def test_openai_listing_does_not_reject_words_containing_forbidden_terms(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["channels"]["openai-plugin"]["short_description"] = "Demonstrate cited project work"
    path.write_text(json.dumps(definition), encoding="utf-8")

    hosted_plugins.load_marketplace_definition(root)


@pytest.mark.parametrize(
    "claim",
    [
        "This service has not yet been built.",
        "This service has not-yet-been-built.",
        "This service has not  yet  been  built.",
    ],
)
def test_openai_listing_rejects_not_yet_been_built_variants(
    tmp_path: Path, claim: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["common"]["release_notes"] = claim
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match="release-stage"):
        hosted_plugins.load_marketplace_definition(root)


def test_marketplace_review_cases_bind_the_versioned_generic_fixture() -> None:
    fixture_path = REPO_ROOT / "plugins/hosted/marketplace-review-fixture-v1.json"
    assert fixture_path.is_file(), "the canonical generic reviewer fixture must be checked in"

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    cases = hosted_plugins.load_marketplace_review_cases(REPO_ROOT)
    assert fixture["payload_sha256"] == hosted_plugins._sha256(
        hosted_plugins._canonical_json(fixture["payload"])
    )
    assert cases["fixture"] == {
        "fixture_version": fixture["fixture_version"],
        "payload_sha256": fixture["payload_sha256"],
    }
    references = {note["reference"] for note in fixture["payload"]["notes"]}
    assert all(
        case["fixture_version"] == fixture["fixture_version"]
        and set(case["fixture_references"]).issubset(references)
        for case in cases["positive"]
    )
    write_cases = [case for case in cases["positive"] if "remember" in case["expected_tools"]]
    assert write_cases
    assert all(case["fixture_reset"] == fixture["reset"] for case in write_cases)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda root: _mutate_review_cases(
                root, lambda cases: cases["fixture"].update(fixture_version="stale-v1")
            ),
            "fixture version",
        ),
        (
            lambda root: _mutate_review_cases(
                root,
                lambda cases: cases["positive"][0].update(fixture_references=["unknown-reference"]),
            ),
            "fixture reference",
        ),
        (
            lambda root: _mutate_fixture(
                root,
                lambda fixture: fixture["payload"]["notes"][0].update(content="Drifted content."),
            ),
            "fixture payload digest",
        ),
    ],
)
def test_marketplace_review_cases_reject_stale_fixture_bindings(
    tmp_path: Path, mutate: object, match: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    assert (root / "plugins/hosted/marketplace-review-fixture-v1.json").is_file()
    mutate(root)

    with pytest.raises(ValueError, match=match):
        hosted_plugins.load_marketplace_review_cases(root)


@pytest.mark.parametrize("tool", ["observe_memory", "capture_source", "triage_memory", "connect_memory"])
def test_marketplace_review_cases_require_reset_for_every_write_capable_tool(
    tmp_path: Path, tool: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    write_tools = {
        entry["name"]
        for entry in hosted_plugins.compatibility_manifest(root)["agent_contract"]["commands"]
        if not entry["mcp_tool"]["annotations"]["readOnlyHint"]
    }
    assert tool in write_tools
    path = root / "plugins/hosted/marketplace-review-cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    cases["positive"][0]["expected_tools"] = [tool]
    path.write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(ValueError, match="fixture reset"):
        hosted_plugins.load_marketplace_review_cases(root)


def test_marketplace_review_fixture_rejects_mismatched_reset_reference_and_key(
    tmp_path: Path,
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    fixture_path = root / "plugins/hosted/marketplace-review-fixture-v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["reset"]["disposable_reference"] = "project-brief"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    cases_path = root / "plugins/hosted/marketplace-review-cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases["positive"][2]["fixture_reset"] = fixture["reset"]
    cases_path.write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(ValueError, match="review fixture reset is invalid"):
        hosted_plugins.load_marketplace_review_cases(root)


def _mutate_review_cases(root: Path, mutate: object) -> None:
    path = root / "plugins/hosted/marketplace-review-cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    mutate(cases)
    path.write_text(json.dumps(cases), encoding="utf-8")


def _mutate_fixture(root: Path, mutate: object) -> None:
    path = root / "plugins/hosted/marketplace-review-fixture-v1.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    mutate(fixture)
    path.write_text(json.dumps(fixture), encoding="utf-8")


def test_openai_packet_renders_every_boolean_annotation() -> None:
    packet = json.loads(
        hosted_plugins.directory_packets(
            REPO_ROOT, channel="openai-plugin", openai_app_id="asdk_app_releaseinput123"
        )["openai-plugin"]
    )
    assert all(
        set(tool["annotations"])
        == {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        and all(isinstance(tool["annotations"][key], bool) for key in tool["annotation_explanations"])
        for tool in packet["tools"]
    )


def test_openai_read_only_non_idempotent_explanation_describes_state_variability() -> None:
    packet = json.loads(
        hosted_plugins.directory_packets(
            REPO_ROOT, channel="openai-plugin", openai_app_id="asdk_app_releaseinput123"
        )["openai-plugin"]
    )
    tool = next(item for item in packet["tools"] if item["name"] == "ask_memory")
    explanation = tool["annotation_explanations"]["idempotentHint"].lower()

    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["idempotentHint"] is False
    assert "state" in explanation
    assert "create" not in explanation


@pytest.mark.parametrize("channel", ["claude-connector", "claude-plugin"])
def test_claude_directory_packets_exclude_openai_review_only_fields(channel: str) -> None:
    packet = json.loads(hosted_plugins.directory_packets(REPO_ROOT, channel=channel)[channel])

    assert "review_recording" not in packet
    assert all("annotation_explanations" not in tool for tool in packet["tools"])
    assert all(
        set(tool["annotations"])
        == {"title", "readOnlyHint", "destructiveHint", "openWorldHint"}
        for tool in packet["tools"]
    )


def test_marketplace_definition_rejects_tampered_brand_asset_digest(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["common"]["brand_asset"]["sha256"] = "0" * 64
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match="brand asset"):
        hosted_plugins.load_marketplace_definition(root)


def test_claude_listing_accepts_current_form_limits(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["channels"]["claude-connector"]["title"] = "x" * 90
    definition["common"]["description"] = "x" * 1500
    path.write_text(json.dumps(definition), encoding="utf-8")

    hosted_plugins.load_marketplace_definition(root)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        (("channels", "claude-connector", "title"), "x" * 101, "title"),
        (("channels", "claude-connector", "short_description"), "x" * 56, "short_description"),
        (("common", "description"), "x" * 2001, "description"),
        (("common", "description"), {"bad": True}, "string"),
        (("common", "release_notes"), 7, "string"),
        (("common", "capabilities", "read"), ["bad"], "string"),
    ],
)
def test_marketplace_definition_rejects_invalid_form_field_types_and_limits(
    tmp_path: Path, field: tuple[str, ...], value: object, match: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    target = definition
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = value
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        hosted_plugins.load_marketplace_definition(root)


@pytest.mark.parametrize("channel", hosted_plugins.DIRECTORY_CHANNELS)
def test_directory_check_rejects_missing_selected_packet(tmp_path: Path, channel: str) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / f"plugins/hosted/directory/generated/{channel}.json"
    path.unlink()

    with pytest.raises(ValueError, match="generated directory packet is missing"):
        hosted_plugins.directory_check(
            root,
            channel=channel,
            openai_app_id="asdk_app_releaseinput123" if channel == "openai-plugin" else None,
        )


def test_openai_draft_packet_requires_registered_package_identity() -> None:
    with pytest.raises(ValueError, match="registered OpenAI app"):
        hosted_plugins.directory_packets(REPO_ROOT, channel="openai-plugin")


def test_directory_status_is_fail_closed_while_promotions_and_probes_are_pending() -> None:
    status = hosted_plugins.directory_status(REPO_ROOT)

    assert status["public_channels"] == []
    assert all(not channel["ready"] for channel in status["channels"].values())
    assert all(channel["state"] == "draft" for channel in status["channels"].values())
    assert "promotion is not live" in status["channels"]["claude-connector"]["blockers"]


def test_expired_signed_production_evidence_blocks_directory_readiness(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    checked_at = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    compatibility = hosted_plugins.compatibility_manifest(root)
    evidence = signed_evidence(
        {
            "schema_version": 1,
            "evidence_type": "directory-production-probes",
            "deployment_sha256": "b" * 64,
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "expires_at": (checked_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "surfaces": {
                name: {"ok": True, "content_sha256": "a" * 64}
                for name in (
                    "website_url",
                    "documentation_url",
                    "setup_url",
                    "privacy_url",
                    "terms_url",
                    "support_url",
                    "oauth_discovery",
                    "mcp_authorization",
                    "mcp_initialize",
                    "tool_discovery",
                )
            },
            "compatibility_sha256": compatibility["compatibility_sha256"],
            "command_surface_sha256": compatibility["command_surface_sha256"],
            "schema_contract_sha256": compatibility["schema_contract_sha256"],
            "full_tool_contract_sha256": hosted_plugins._full_tool_contract_sha256(compatibility),
            "origin_rejection": True,
            "response_minimization": True,
            "sampled_output_sale_free": True,
        }
    )
    (root / "plugins/hosted/directory/production-evidence.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )

    status = hosted_plugins.directory_status(
        root,
        trusted_key_id="directory-test-key",
        trusted_secret="directory-test-secret",
        deployment_sha256="b" * 64,
    )
    assert (
        "production-evidence is expired or exceeds its TTL"
        in status["channels"]["claude-connector"]["blockers"]
    )


def test_production_evidence_requires_sale_free_sampled_output(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    key_id, secret = ready_directory_evidence(root)
    path = root / "plugins/hosted/directory/production-evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["sampled_output_sale_free"] = False
    path.write_text(json.dumps(signed_evidence(evidence, secret)), encoding="utf-8")

    status = hosted_plugins.directory_status(
        root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
    )
    assert (
        "production probe sampled output sale-freedom is unhealthy"
        in status["channels"]["claude-connector"]["blockers"]
    )


def test_directory_submissions_are_append_only_and_preserve_runtime_promotion(
    tmp_path: Path,
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    original_promotion = (root / "plugins/hosted/promotion/claude.json").read_bytes()
    digest = hosted_plugins.directory_record_sha256(root, "claude-connector")
    submissions = root / "plugins/hosted/directory/submissions/claude-connector"
    before = {path.name: path.read_bytes() for path in submissions.glob("*.json")}

    hosted_plugins.record_directory_state(
        root,
        "claude-connector",
        "rejected",
        expected_state="draft",
        expected_record_sha256=digest,
    )

    with pytest.raises(ValueError, match="changed"):
        hosted_plugins.record_directory_state(
            root,
            "claude-connector",
            "draft",
            expected_state="rejected",
            expected_record_sha256=digest,
        )
    after = {path.name: path.read_bytes() for path in submissions.glob("*.json")}
    assert before.items() <= after.items()
    assert len(after) == len(before) + 1
    assert (root / "plugins/hosted/promotion/claude.json").read_bytes() == original_promotion


def test_directory_requires_signed_evidence_and_keeps_active_revision_public_during_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    key_id, secret = ready_directory_evidence(root)
    status = hosted_plugins.directory_status(
        root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
    )
    assert status["channels"]["claude-connector"]["ready"]
    packet = json.loads(
        hosted_plugins.directory_packets(root, channel="claude-connector")["claude-connector"]
    )
    receipt = {
        "channel": "claude-connector",
        "state": "submitted",
        "listing_version": "v1",
        "listing_sha256": packet["listing_sha256"],
        "compatibility_sha256": packet["bindings"]["compatibility_sha256"],
        "package_lock_sha256": packet["bindings"]["package_lock_sha256"],
        "archive_lock_sha256": packet["bindings"]["archive_lock_sha256"],
        "promotion_record_sha256": hosted_plugins.promotion_record_sha256(root, "claude"),
        "provider_directory_id_sha256": "d" * 64,
        "recorded_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "public_url": "https://example.com/exomem",
    }
    digest = hosted_plugins.directory_record_sha256(root, "claude-connector")
    with pytest.raises(ValueError, match="unsupported|unsigned|signature"):
        hosted_plugins.record_directory_state(
            root,
            "claude-connector",
            "submitted",
            expected_state="draft",
            expected_record_sha256=digest,
            receipt=receipt,
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )
    receipt = signed_evidence(
        {**receipt, "schema_version": 1, "deployment_sha256": "b" * 64}, secret
    )
    hosted_plugins.record_directory_state(
        root,
        "claude-connector",
        "submitted",
        expected_state="draft",
        expected_record_sha256=digest,
        receipt=receipt,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    submitted = hosted_plugins.directory_record_sha256(root, "claude-connector")
    receipt = signed_evidence({**receipt, "state": "in_review"}, secret)
    hosted_plugins.record_directory_state(
        root,
        "claude-connector",
        "in_review",
        expected_state="submitted",
        expected_record_sha256=submitted,
        receipt=receipt,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    in_review = hosted_plugins.directory_record_sha256(root, "claude-connector")
    receipt = signed_evidence({**receipt, "state": "approved"}, secret)
    hosted_plugins.record_directory_state(
        root,
        "claude-connector",
        "approved",
        expected_state="in_review",
        expected_record_sha256=in_review,
        receipt=receipt,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    approved = hosted_plugins.directory_record_sha256(root, "claude-connector")
    assert (
        hosted_plugins.directory_status(
            root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
        )["channels"]["claude-connector"]["active_submission_sha256"]
        is None
    )
    receipt = signed_evidence({**receipt, "state": "published"}, secret)
    hosted_plugins.record_directory_state(
        root,
        "claude-connector",
        "published",
        expected_state="approved",
        expected_record_sha256=approved,
        receipt=receipt,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    published = hosted_plugins.directory_record_sha256(root, "claude-connector")
    status = hosted_plugins.directory_status(
        root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
    )
    assert status["channels"]["claude-connector"]["active_submission_sha256"] is None
    assert not status["channels"]["claude-connector"]["public"]
    post_checked_at = datetime.now(UTC).replace(microsecond=0)
    post_install = signed_evidence(
        {
            "schema_version": 1,
            "evidence_type": "directory-post-install",
            "deployment_sha256": "b" * 64,
            "checked_at": post_checked_at.isoformat().replace("+00:00", "Z"),
            "expires_at": (post_checked_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "channel": "claude-connector",
            "submission_sha256": published,
            "listing_sha256": receipt["listing_sha256"],
            "package_lock_sha256": receipt["package_lock_sha256"],
            "public_url": receipt["public_url"],
            "sampled_output_sale_free": True,
            "checks": {
                "fresh_non_reviewer_oauth": True,
                "tool_and_skill_discovery": True,
                "governed_recall_with_citation": True,
                "durable_capture": True,
                "fresh_chat_recall": True,
                "do_not_capture": True,
                "revocation": True,
            },
        },
        secret,
    )
    path = (
        root / f"plugins/hosted/directory/post-install-evidence/claude-connector/{published}.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(post_install), encoding="utf-8")
    published_record = hosted_plugins._directory_submissions(root, "claude-connector")[published]
    receipt_mutations = [
        (
            "signature",
            {"operator_signature": "0" * 64},
            "persisted directory receipt has an invalid operator signature",
        ),
        ("schema", {"unexpected": True}, "persisted directory receipt has unsupported fields"),
        (
            "deployment",
            {"deployment_sha256": "c" * 64},
            "persisted directory receipt has stale bindings",
        ),
        (
            "promotion",
            {"promotion_record_sha256": "c" * 64},
            "persisted directory receipt does not bind the current publication state",
        ),
    ]
    for _name, changes, expected in receipt_mutations:
        altered = json.loads(json.dumps(published_record))
        altered["receipt"].update(changes)
        if _name != "signature":
            altered["receipt"] = signed_evidence(altered["receipt"], secret)
        blockers = hosted_plugins._post_install_blockers(
            root,
            "claude-connector",
            published,
            altered,
            openai_app_id=None,
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )
        assert blockers == [expected]
    unsafe_post_install = signed_evidence(
        {**post_install, "sampled_output_sale_free": False}, secret
    )
    path.write_text(json.dumps(unsafe_post_install), encoding="utf-8")
    with pytest.raises(ValueError, match="post-install evidence does not bind"):
        hosted_plugins.activate_directory_submission(
            root,
            "claude-connector",
            target_submission_sha256=published,
            expected_active_submission_sha256=None,
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )
    path.write_text(json.dumps(post_install), encoding="utf-8")
    hosted_plugins.activate_directory_submission(
        root,
        "claude-connector",
        target_submission_sha256=published,
        expected_active_submission_sha256=None,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    assert hosted_plugins.directory_status(
        root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
    )["channels"]["claude-connector"]["public"]
    definition_path = root / "plugins/hosted/marketplace-definition.json"
    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    definition["channels"]["claude-connector"]["short_description"] = (
        "Cited retrieval and durable capture for project work."
    )
    definition_path.write_text(json.dumps(definition), encoding="utf-8")
    hosted_plugins.record_directory_state(
        root,
        "claude-connector",
        "draft",
        expected_state="published",
        expected_record_sha256=published,
        expected_active_submission_sha256=published,
        listing_version="v2",
        deployment_sha256="b" * 64,
    )
    draft_v2 = hosted_plugins.directory_record_sha256(root, "claude-connector")
    status = hosted_plugins.directory_status(
        root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
    )
    assert status["channels"]["claude-connector"]["public"]
    assert (
        status["channels"]["claude-connector"]["listing_versions"]["v2"]["record_sha256"]
        == draft_v2
    )
    hosted_plugins.activate_directory_submission(
        root,
        "claude-connector",
        target_submission_sha256=published,
        expected_active_submission_sha256=None,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    original_write = hosted_plugins._write_json_atomic

    def crash_after_withdrawal_record(path: Path, value: object) -> None:
        original_write(path, value)
        if (
            "submissions" in path.parts
            and isinstance(value, dict)
            and value.get("state") == "withdrawn"
        ):
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(hosted_plugins, "_write_json_atomic", crash_after_withdrawal_record)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        hosted_plugins.record_directory_state(
            root,
            "claude-connector",
            "withdrawn",
            expected_state="published",
            expected_record_sha256=published,
            expected_active_submission_sha256=published,
            target_submission_sha256=published,
            deployment_sha256="b" * 64,
        )
    monkeypatch.setattr(hosted_plugins, "_write_json_atomic", original_write)
    interrupted = hosted_plugins.directory_status(
        root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
    )["channels"]["claude-connector"]
    assert not interrupted["public"]
    assert "active publication pointer is stale for its listing version" in interrupted["blockers"]
    hosted_plugins.record_directory_state(
        root,
        "claude-connector",
        "withdrawn",
        expected_state="published",
        expected_record_sha256=published,
        expected_active_submission_sha256=published,
        target_submission_sha256=published,
        deployment_sha256="b" * 64,
    )
    recovered = hosted_plugins.directory_status(
        root, trusted_key_id=key_id, trusted_secret=secret, deployment_sha256="b" * 64
    )["channels"]["claude-connector"]
    assert recovered["active_submission_sha256"] is None
    assert recovered["listing_versions"]["v2"]["state"] == "draft"


def test_marketplace_public_input_guard_rejects_private_review_case_identifier(
    tmp_path: Path,
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-review-cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    cases["positive"][0]["tenant_id"] = "private-value"
    path.write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(ValueError, match="raw private identifier"):
        hosted_plugins.validate_hosted_public_inputs(root, include_generated=False)


def test_marketplace_public_input_guard_rejects_nested_credential_values(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-review-cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    cases["positive"][0]["api_token"] = {"nested": "value"}
    path.write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(ValueError, match="credential"):
        hosted_plugins.validate_hosted_public_inputs(root, include_generated=False)


@pytest.mark.parametrize("private_value", [{"nested": "value"}, 7])
def test_marketplace_public_input_guard_rejects_private_field_names_for_all_value_types(
    tmp_path: Path, private_value: object
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/marketplace-review-cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    cases["positive"][0]["tenant_id"] = private_value
    path.write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(ValueError, match="raw private identifier"):
        hosted_plugins.validate_hosted_public_inputs(root, include_generated=False)


def test_directory_submission_rejects_arbitrary_extra_fields(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = next((root / "plugins/hosted/directory/submissions/claude-connector").glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["unexpected"] = 1
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported fields"):
        hosted_plugins.directory_record_sha256(root, "claude-connector")


def test_openai_packet_rejects_sale_language_in_review_material(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    cases_path = root / "plugins/hosted/marketplace-review-cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases["negative"][0]["expected_outcome"] = "Invite the user to buy a Pro plan."
    cases_path.write_text(json.dumps(cases), encoding="utf-8")

    with pytest.raises(ValueError, match="sell or upsell"):
        hosted_plugins.directory_packets(
            root, channel="openai-plugin", openai_app_id="asdk_app_releaseinput123"
        )


def test_openai_packet_rejects_sale_language_in_tool_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    original_manifest = hosted_plugins.compatibility_manifest

    def sale_manifest(repo_root: Path | None = None) -> dict[str, object]:
        manifest = json.loads(json.dumps(original_manifest(repo_root)))
        manifest["agent_contract"]["commands"][0]["mcp_tool"]["description"] = "Buy Pro access now."
        return manifest

    monkeypatch.setattr(hosted_plugins, "compatibility_manifest", sale_manifest)
    with pytest.raises(ValueError, match="sell or upsell"):
        hosted_plugins.directory_packets(
            root, channel="openai-plugin", openai_app_id="asdk_app_releaseinput123"
        )


@pytest.mark.parametrize("metadata", ["default", "example", "examples", "enum", "const", "value"])
def test_directory_packet_rejects_live_credential_schema_literals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    original_manifest = hosted_plugins.compatibility_manifest

    def credential_manifest(repo_root: Path | None = None) -> dict[str, object]:
        manifest = json.loads(json.dumps(original_manifest(repo_root)))
        schema = manifest["agent_contract"]["commands"][0]["mcp_tool"]["inputSchema"]
        schema["properties"]["api_token"] = {
            "type": "string",
            metadata: ["actual-secret"] if metadata in {"examples", "enum"} else "actual-secret",
        }
        return manifest

    monkeypatch.setattr(hosted_plugins, "compatibility_manifest", credential_manifest)
    with pytest.raises(ValueError, match="credential value"):
        hosted_plugins.directory_packets(
            root, channel="openai-plugin", openai_app_id="asdk_app_releaseinput123"
        )


def openai_published_receipt(root: Path) -> tuple[str, str, dict[str, object]]:
    app_id = "asdk_app_releaseinput123"
    hosted_plugins.render(root, platform="openai", openai_app_id=app_id)
    key_id, secret = ready_directory_evidence(root)
    bindings = hosted_plugins._directory_bindings(root, "openai-plugin", openai_app_id=app_id)
    lock = json.loads(
        (root / "plugins/hosted/generated/openai.lock.json").read_text(encoding="utf-8")
    )
    (root / "plugins/hosted/promotion/openai.json").write_text(
        json.dumps(
            {
                "state": "live",
                "compatibility_sha256": bindings["compatibility_sha256"],
                "package_lock": lock,
            }
        ),
        encoding="utf-8",
    )
    packet = json.loads(
        hosted_plugins.directory_packets(root, channel="openai-plugin", openai_app_id=app_id)[
            "openai-plugin"
        ]
    )
    directory_plugin_id = "plugin_asdk_app_directory123"
    return (
        key_id,
        secret,
        signed_evidence(
            {
                "schema_version": 1,
                "channel": "openai-plugin",
                "state": "published",
                "listing_version": "v1",
                "listing_sha256": packet["listing_sha256"],
                "compatibility_sha256": bindings["compatibility_sha256"],
                "package_lock_sha256": bindings["package_lock_sha256"],
                "archive_lock_sha256": bindings["archive_lock_sha256"],
                "promotion_record_sha256": hosted_plugins.promotion_record_sha256(root, "openai"),
                "provider_directory_id_sha256": hosted_plugins._directory_plugin_id_sha256(
                    directory_plugin_id
                ),
                "registered_app_id_sha256": bindings["registered_app_id_sha256"],
                "directory_plugin_id": directory_plugin_id,
                "recorded_at": datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "deployment_sha256": "b" * 64,
                "public_url": "https://example.com/exomem",
            },
            secret,
        ),
    )


def _write_openai_post_install_evidence(
    root: Path, active_submission_sha256: str, receipt: dict[str, object], secret: str
) -> None:
    checked_at = datetime.now(UTC).replace(microsecond=0)
    evidence = signed_evidence(
        {
            "schema_version": 1,
            "evidence_type": "directory-post-install",
            "deployment_sha256": "b" * 64,
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "expires_at": (checked_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "channel": "openai-plugin",
            "submission_sha256": active_submission_sha256,
            "listing_sha256": receipt["listing_sha256"],
            "package_lock_sha256": receipt["package_lock_sha256"],
            "public_url": receipt["public_url"],
            "sampled_output_sale_free": True,
            "checks": {
                "fresh_non_reviewer_oauth": True,
                "tool_and_skill_discovery": True,
                "governed_recall_with_citation": True,
                "durable_capture": True,
                "fresh_chat_recall": True,
                "do_not_capture": True,
                "revocation": True,
            },
        },
        secret,
    )
    path = (
        root
        / f"plugins/hosted/directory/post-install-evidence/openai-plugin/{active_submission_sha256}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence), encoding="utf-8")


def test_persisted_receipt_timestamp_is_valid_without_submission_ttl(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    key_id, secret, receipt = openai_published_receipt(root)
    receipt["recorded_at"] = (
        (datetime.now(UTC) - timedelta(days=30))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    receipt = signed_evidence(receipt, secret)
    active_submission_sha256 = "a" * 64
    _write_openai_post_install_evidence(root, active_submission_sha256, receipt, secret)

    assert not hosted_plugins._post_install_blockers(
        root,
        "openai-plugin",
        active_submission_sha256,
        {"listing_version": "v1", "receipt": receipt},
        openai_app_id="asdk_app_releaseinput123",
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    assert (
        hosted_plugins._validate_directory_receipt(
            root,
            "openai-plugin",
            "published",
            receipt,
            listing_version="v1",
            openai_app_id="asdk_app_releaseinput123",
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
            require_fresh_timestamp=True,
            require_current_listing=True,
        )
        == "directory receipt recorded_at is stale"
    )
    for timestamp, message in (
        (
            (datetime.now(UTC) + timedelta(days=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "in the future",
        ),
        ("not-a-timestamp", "must be canonical UTC"),
    ):
        invalid = signed_evidence({**receipt, "recorded_at": timestamp}, secret)
        blockers = hosted_plugins._post_install_blockers(
            root,
            "openai-plugin",
            active_submission_sha256,
            {"listing_version": "v1", "receipt": invalid},
            openai_app_id="asdk_app_releaseinput123",
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )
        assert message in blockers[0]


def test_openai_published_receipt_requires_registered_app_input_to_activate(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    app_id = "asdk_app_releaseinput123"
    key_id, secret, receipt = openai_published_receipt(root)
    record_sha256 = hosted_plugins.directory_record_sha256(root, "openai-plugin")
    previous_state = "draft"
    for state in ("submitted", "in_review", "approved", "published"):
        state_receipt = signed_evidence({**receipt, "state": state}, secret)
        hosted_plugins.record_directory_state(
            root,
            "openai-plugin",
            state,
            expected_state=previous_state,
            expected_record_sha256=record_sha256,
            receipt=state_receipt,
            openai_app_id=app_id,
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )
        record_sha256 = hosted_plugins.directory_record_sha256(root, "openai-plugin")
        previous_state = state
    _write_openai_post_install_evidence(root, record_sha256, state_receipt, secret)

    with pytest.raises(ValueError, match="registered OpenAI app"):
        hosted_plugins.activate_directory_submission(
            root,
            "openai-plugin",
            target_submission_sha256=record_sha256,
            expected_active_submission_sha256=None,
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )
    with pytest.raises(ValueError, match="current artifact"):
        hosted_plugins.activate_directory_submission(
            root,
            "openai-plugin",
            target_submission_sha256=record_sha256,
            expected_active_submission_sha256=None,
            openai_app_id="asdk_app_otherrelease456",
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )

    hosted_plugins.activate_directory_submission(
        root,
        "openai-plugin",
        target_submission_sha256=record_sha256,
        expected_active_submission_sha256=None,
        openai_app_id=app_id,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )
    assert hosted_plugins.directory_status(
        root,
        openai_app_id=app_id,
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )["channels"]["openai-plugin"]["public"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda receipt: receipt.pop("directory_plugin_id"), "unsupported fields"),
        (
            lambda receipt: receipt.update(provider_directory_id_sha256="f" * 64),
            "invalid provider directory identity",
        ),
        (lambda receipt: receipt.update(schema_version=2), "unsupported fields"),
        (lambda receipt: receipt.update(listing_sha256="malformed"), "invalid digest"),
        (
            lambda receipt: receipt.update(registered_app_id_sha256="f" * 64),
            "does not bind the registered application",
        ),
    ],
)
def test_persisted_openai_receipt_rejects_each_required_binding(
    tmp_path: Path, mutate: object, match: str
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    key_id, secret, receipt = openai_published_receipt(root)
    active_submission_sha256 = "a" * 64
    _write_openai_post_install_evidence(root, active_submission_sha256, receipt, secret)
    altered = json.loads(json.dumps(receipt))
    mutate(altered)
    if "operator_signature" in altered:
        altered = signed_evidence(altered, secret)

    blockers = hosted_plugins._post_install_blockers(
        root,
        "openai-plugin",
        active_submission_sha256,
        {"listing_version": "v1", "receipt": altered},
        openai_app_id="asdk_app_releaseinput123",
        trusted_key_id=key_id,
        trusted_secret=secret,
        deployment_sha256="b" * 64,
    )

    assert len(blockers) == 1
    assert match in blockers[0]


def test_openai_receipt_rejects_mismatched_directory_identity_hash(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    app_id = "asdk_app_releaseinput123"
    hosted_plugins.render(root, platform="openai", openai_app_id=app_id)
    key_id, secret = ready_directory_evidence(root)
    bindings = hosted_plugins._directory_bindings(root, "openai-plugin", openai_app_id=app_id)
    lock = json.loads(
        (root / "plugins/hosted/generated/openai.lock.json").read_text(encoding="utf-8")
    )
    (root / "plugins/hosted/promotion/openai.json").write_text(
        json.dumps(
            {
                "state": "live",
                "compatibility_sha256": bindings["compatibility_sha256"],
                "package_lock": lock,
            }
        ),
        encoding="utf-8",
    )
    packet = json.loads(
        hosted_plugins.directory_packets(root, channel="openai-plugin", openai_app_id=app_id)[
            "openai-plugin"
        ]
    )
    receipt = signed_evidence(
        {
            "schema_version": 1,
            "channel": "openai-plugin",
            "state": "submitted",
            "listing_version": "v1",
            "listing_sha256": packet["listing_sha256"],
            "compatibility_sha256": packet["bindings"]["compatibility_sha256"],
            "package_lock_sha256": packet["bindings"]["package_lock_sha256"],
            "archive_lock_sha256": packet["bindings"]["archive_lock_sha256"],
            "promotion_record_sha256": hosted_plugins.promotion_record_sha256(root, "openai"),
            "provider_directory_id_sha256": "f" * 64,
            "registered_app_id_sha256": packet["bindings"]["registered_app_id_sha256"],
            "directory_plugin_id": "plugin_asdk_app_directory123",
            "recorded_at": datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "deployment_sha256": "b" * 64,
            "public_url": "https://example.com/exomem",
        },
        secret,
    )

    with pytest.raises(ValueError, match="provider directory identity"):
        hosted_plugins.record_directory_state(
            root,
            "openai-plugin",
            "submitted",
            expected_state="draft",
            expected_record_sha256=hosted_plugins.directory_record_sha256(root, "openai-plugin"),
            receipt=receipt,
            openai_app_id=app_id,
            trusted_key_id=key_id,
            trusted_secret=secret,
            deployment_sha256="b" * 64,
        )


def test_directory_record_cli_normalizes_none_active_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = load_hosted_plugin_cli()
    captured: dict[str, object] = {}

    def record(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli.hosted_plugins, "record_directory_state", record)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hosted-plugin.py",
            "directory-record",
            "--channel",
            "claude-connector",
            "--directory-state",
            "rejected",
            "--expected-state",
            "draft",
            "--expected-record-sha256",
            "a" * 64,
            "--expected-active-submission-sha256",
            "none",
        ],
    )

    assert cli.main() == 0
    assert captured["expected_active_submission_sha256"] is None


def test_directory_activate_cli_forwards_openai_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = load_hosted_plugin_cli()
    captured: dict[str, object] = {}

    def activate(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli.hosted_plugins, "activate_directory_submission", activate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hosted-plugin.py",
            "directory-activate",
            "--channel",
            "openai-plugin",
            "--target-submission-sha256",
            "a" * 64,
            "--expected-active-submission-sha256",
            "none",
            "--openai-app-id",
            "asdk_app_releaseinput123",
        ],
    )

    assert cli.main() == 0
    assert captured["openai_app_id"] == "asdk_app_releaseinput123"


def test_directory_cli_checks_a_claude_channel_without_openai_registration() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/hosted-plugin.py",
            "directory-check",
            "--channel",
            "claude-connector",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["channel"] == "claude-connector"
