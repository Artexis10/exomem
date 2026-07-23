from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from exomem import commands
from exomem import hosted_gateway as gateway
from exomem.capabilities import active_surface

ALPHA_PROFILE = "hosted-alpha-agent-v1"
ALPHA_COMMANDS = (
    "bootstrap",
    "ask_memory",
    "read_memory",
    "browse_memory",
    "remember",
    "observe_memory",
    "capture_source",
    "compile_source",
    "preserve_evidence",
    "review_memory",
    "review_item_context",
    "triage_memory",
    "connect_memory",
)
FORBIDDEN_COMMANDS = {
    "coordination_status",
    "edit_memory",
    "replace_memory",
    "transfer_artifact",
    "process_media",
    "adopt_vault",
    "adoption_studio",
    "maintain_memory",
    "schema_memory",
    "manage_memory_file",
    "query_dataset",
    "read_media",
}
MCP_SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_tool_schemas.json"


def test_hosted_alpha_agent_profile_is_exact_and_fail_closed() -> None:
    resolver = getattr(commands, "product_commands_for_profile", None)
    assert resolver is not None, "missing canonical product surface-profile resolver"

    selected = resolver(ALPHA_PROFILE, "rest")

    assert tuple(command.name for command in selected) == ALPHA_COMMANDS
    assert all(command.tier == 1 for command in selected)
    assert all("rest" in command.surfaces for command in selected)
    assert FORBIDDEN_COMMANDS.isdisjoint(command.name for command in selected)
    canonical = {command.name: command for command in commands.PRODUCT_COMMANDS}
    assert all(command is canonical[command.name] for command in selected)

    with pytest.raises(ValueError, match="unsupported product surface profile"):
        resolver("hosted-alpha-agent-v999", "rest")


@pytest.mark.parametrize("surface", ["mcp", "rest", "cli"])
def test_hosted_alpha_membership_cannot_expand_on_another_surface(surface: str) -> None:
    resolver = getattr(commands, "product_commands_for_profile", None)
    assert resolver is not None, "missing canonical product surface-profile resolver"

    selected = resolver(ALPHA_PROFILE, surface)

    assert tuple(command.name for command in selected) == ALPHA_COMMANDS
    assert FORBIDDEN_COMMANDS.isdisjoint(command.name for command in selected)


def test_agent_contract_is_mcp_ready_deterministic_and_additive() -> None:
    descriptor_builder = getattr(gateway, "hosted_agent_surface_descriptor", None)
    contract_builder = getattr(gateway, "build_agent_gateway_contract", None)
    assert descriptor_builder is not None, "missing Hosted agent surface descriptor"
    assert contract_builder is not None, "missing Hosted agent gateway contract"

    descriptor = descriptor_builder(ALPHA_PROFILE)
    contract = contract_builder(profile=ALPHA_PROFILE)
    repeated = contract_builder(profile=ALPHA_PROFILE)
    legacy = gateway.build_gateway_contract()

    assert gateway.canonical_contract_json(contract) == gateway.canonical_contract_json(
        repeated
    )
    assert tuple(entry["name"] for entry in contract["commands"]) == ALPHA_COMMANDS
    assert contract["agent_profile"] == {
        **descriptor.as_metadata(),
        "immutable": True,
    }
    assert descriptor.product_commands == ALPHA_COMMANDS
    assert descriptor.tier2_enabled is False
    assert descriptor.fingerprint == contract["agent_profile"][
        "active_capability_sha256"
    ]

    unsigned = dict(contract)
    digest = unsigned.pop("digest")
    assert digest == {
        "algorithm": "sha256",
        "value": hashlib.sha256(gateway.canonical_json(unsigned)).hexdigest(),
    }
    assert set(legacy) == {
        "schema_version",
        "protocol_version",
        "exomem_release",
        "compatibility",
        "trusted_headers",
        "envelopes",
        "transfer_grant",
        "commands",
        "digest",
    }
    assert "agent_profile" not in legacy
    assert "transfer_grant" not in contract

    fixture = json.loads(MCP_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
    canonical_commands = {command.name: command for command in commands.PRODUCT_COMMANDS}
    legacy_entries = {entry["name"]: entry for entry in legacy["commands"]}
    for entry in contract["commands"]:
        name = entry["name"]
        mcp_tool = entry["mcp_tool"]
        base_entry = {key: value for key, value in entry.items() if key != "mcp_tool"}
        assert base_entry == legacy_entries[name]
        assert mcp_tool["name"] == name
        assert mcp_tool["description"] == fixture[name]["description"]
        assert mcp_tool["inputSchema"] == fixture[name]["inputSchema"]
        assert mcp_tool["annotations"] == canonical_commands[
            name
        ].mcp_annotations.model_dump(mode="json", by_alias=True)


def test_agent_contract_rejects_unknown_profile_with_stable_error() -> None:
    contract_builder = getattr(gateway, "build_agent_gateway_contract", None)
    assert contract_builder is not None, "missing Hosted agent gateway contract"

    with pytest.raises(gateway.HostedGatewayError) as error:
        contract_builder(profile="hosted-alpha-agent-v999")

    assert error.value.code == "HOSTED_SURFACE_PROFILE_UNSUPPORTED"


@pytest.mark.parametrize("bootstrap_profile", ["compact", "full", "diagnostics"])
def test_agent_bootstrap_advertises_only_the_active_profile(
    tmp_path: Path,
    bootstrap_profile: str,
) -> None:
    descriptor = gateway.hosted_agent_surface_descriptor(ALPHA_PROFILE)

    with active_surface(descriptor):
        payload = commands.op_bootstrap(tmp_path, profile=bootstrap_profile)

    assert payload["active_capabilities"] == descriptor.as_metadata()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert all(name not in serialized for name in FORBIDDEN_COMMANDS)
    for action in ("ask", "remember", "capture", "review", "connect"):
        route = payload["simple_actions"][action]["route"]
        assert route["tool"] in descriptor.callable_commands
    for action in ("adopt", "maintain"):
        assert payload["simple_actions"][action]["available"] is False
        assert "route" not in payload["simple_actions"][action]
