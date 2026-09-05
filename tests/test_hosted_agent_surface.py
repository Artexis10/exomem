from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from exomem import command_surface, commands, workflow_skills
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
    "preserve_artifacts",
    "process_media",
    "adopt_vault",
    "adoption_studio",
    "maintain_memory",
    "schema_memory",
    "govern_memory",
    "manage_memory_file",
    "query_dataset",
    "read_media",
}
MCP_SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "mcp_tool_schemas.json"


def _without_mcp_transport_credential(schema: dict[str, object]) -> dict[str, object]:
    normalized = json.loads(json.dumps(schema))
    properties = normalized.get("properties")
    assert isinstance(properties, dict)
    properties.pop("authorization_session_credential", None)
    return normalized


#: Reasons that record a decision not yet taken rather than a technical
#: obstacle. An exclusion carrying one of these is the drift the registry
#: exists to prevent, wearing the registry's clothes.
PLACEHOLDER_REASONS = (
    "tbd",
    "todo",
    "not yet reviewed",
    "not reviewed",
    "alpha scope",
    "out of scope",
    "for now",
    "later",
)


def test_hosted_v4_membership_equals_the_product_surface_minus_exclusions() -> None:
    """Hosted parity is a rule, not a list.

    v1 was an allowlist, so every command added after it was absent from
    hosted until somebody remembered -- and for `adopt`, `maintain` and
    `record` nobody did. The literal below stays pinned because a published
    profile's `command_surface_sha256` must not move under an unchanged
    identifier; this asserts the literal still equals what the rule derives,
    so adding a product command without deciding its hosted status fails here.
    """
    profile = commands.PRODUCT_SURFACE_PROFILES[commands.HOSTED_ALPHA_AGENT_V4_PROFILE]

    assert profile.command_names == commands.hosted_complete_surface_names()

    excluded = set(commands.HOSTED_SURFACE_EXCLUSIONS)
    every = {command.name for command in commands.PRODUCT_COMMANDS}
    assert set(profile.command_names) == every - excluded
    assert excluded <= every, "an exclusion names a command that does not exist"


def test_hosted_exclusions_state_a_reason_and_a_lifting_condition() -> None:
    assert set(commands.HOSTED_SURFACE_EXCLUSIONS) == {
        "transfer_artifact",
        "adopt_vault",
        "process_media",
        "read_media",
    }

    for name, exclusion in commands.HOSTED_SURFACE_EXCLUSIONS.items():
        assert exclusion.command == name
        assert len(exclusion.reason) > 60, f"{name}: reason is too thin to be a reason"
        assert exclusion.lifted_when, f"{name}: no condition would ever lift this"
        blob = f"{exclusion.reason} {exclusion.lifted_when}".lower()
        for placeholder in PLACEHOLDER_REASONS:
            assert placeholder not in blob, f"{name}: placeholder reason {placeholder!r}"


def test_hosted_v4_carries_tier_two_and_resolves_on_every_surface() -> None:
    """Tier 2 was withheld by a blanket rule in the resolver, not by a decision.

    Every tier-2 command operates inside the calling tenant's own vault, which
    is the blast radius a local operator already has, so the exposure is the
    default and the resolver's refusal is now per-profile rather than absolute.
    """
    tier2 = {command.name for command in commands.PRODUCT_COMMANDS if command.tier == 2}
    v4 = set(
        commands.PRODUCT_SURFACE_PROFILES[
            commands.HOSTED_ALPHA_AGENT_V4_PROFILE
        ].command_names
    )
    assert tier2 - set(commands.HOSTED_SURFACE_EXCLUSIONS) <= v4

    for surface in ("mcp", "rest", "cli"):
        selected = commands.product_commands_for_profile(
            commands.HOSTED_ALPHA_AGENT_V4_PROFILE, surface
        )
        assert len(selected) == len(v4)

    # Fail-closed is preserved: a profile that did not opt in still refuses.
    assert not commands.PRODUCT_SURFACE_PROFILES[ALPHA_PROFILE].expose_tier2


def test_hosted_v4_leaves_only_adopt_degraded_and_says_why() -> None:
    """Under v1 three actions were unavailable and the catalog said nothing useful."""
    v4 = commands.PRODUCT_SURFACE_PROFILES[
        commands.HOSTED_ALPHA_AGENT_V4_PROFILE
    ].command_names
    catalog = commands.simple_action_catalog(available_tools=frozenset(v4))

    degraded = {
        action: entry
        for action, entry in catalog.items()
        if entry.get("available") is False
    }
    assert set(degraded) == {"adopt"}

    entry = degraded["adopt"]
    assert entry["unavailable_command"] == "adopt_vault"
    reason = entry["unavailable_reason"]
    assert "adopt_vault" in reason
    assert "HOSTED_IMPORT_INTERCEPT_REQUIRED" in reason
    assert "Lifted when" in reason


def test_published_hosted_profiles_are_not_mutated_by_v4() -> None:
    pinned = {
        "hosted-alpha-agent-v1": 13,
        "hosted-alpha-agent-v2": 14,
        "hosted-alpha-agent-v3": 17,
    }
    for name, count in pinned.items():
        profile = commands.PRODUCT_SURFACE_PROFILES[name]
        assert len(profile.command_names) == count
        assert profile.command_names[:13] == ALPHA_COMMANDS
        assert not profile.expose_tier2


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
        "authorization_session",
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
        assert mcp_tool["inputSchema"] == _without_mcp_transport_credential(
            fixture[name]["inputSchema"]
        )
        expected_annotations = command_surface.mcp_tool_annotations(
            name,
            read_only=canonical_commands[name].read_only,
            open_world=True,
            idempotent=canonical_commands[name].read_only,
        ).model_dump(mode="json", by_alias=True)
        assert mcp_tool["annotations"] == expected_annotations
        assert mcp_tool["annotations"]["idempotentHint"] is canonical_commands[name].read_only


def test_hosted_alpha_mcp_tools_omit_absent_optional_fields_without_losing_schema_nulls() -> None:
    contract = gateway.build_agent_gateway_contract(profile=ALPHA_PROFILE)
    repeated = gateway.build_agent_gateway_contract(profile=ALPHA_PROFILE)
    fixture = json.loads(MCP_SCHEMA_FIXTURE.read_text(encoding="utf-8"))

    assert gateway.canonical_contract_json(contract) == gateway.canonical_contract_json(
        repeated
    )
    assert tuple(entry["name"] for entry in contract["commands"]) == ALPHA_COMMANDS

    for entry in contract["commands"]:
        mcp_tool = entry["mcp_tool"]
        assert "icons" not in mcp_tool
        assert "execution" not in mcp_tool
        assert all(value is not None for value in mcp_tool.values())
        assert mcp_tool["description"] == fixture[entry["name"]]["description"]
        assert mcp_tool["inputSchema"] == _without_mcp_transport_credential(
            fixture[entry["name"]]["inputSchema"]
        )
        assert mcp_tool["annotations"]
        assert mcp_tool["outputSchema"]

    workflow = contract["commands"][0]["mcp_tool"]["inputSchema"]["properties"][
        "workflow"
    ]
    assert workflow["default"] is None
    assert {branch["type"] for branch in workflow["anyOf"]} == {"string", "null"}
    assert workflow["description"]


def test_agent_contract_rejects_unknown_profile_with_stable_error() -> None:
    contract_builder = getattr(gateway, "build_agent_gateway_contract", None)
    assert contract_builder is not None, "missing Hosted agent gateway contract"

    with pytest.raises(gateway.HostedGatewayError) as error:
        contract_builder(profile="hosted-alpha-agent-v999")

    assert error.value.code == "HOSTED_SURFACE_PROFILE_UNSUPPORTED"


@pytest.mark.parametrize("bootstrap_profile", ["compact", "full", "diagnostics", "session"])
def test_agent_bootstrap_advertises_only_the_active_profile(
    tmp_path: Path,
    bootstrap_profile: str,
) -> None:
    descriptor = gateway.hosted_agent_surface_descriptor(ALPHA_PROFILE)

    with active_surface(descriptor):
        payload = commands.op_bootstrap(
            tmp_path,
            profile=bootstrap_profile,
            **(
                {"skill_contract": workflow_skills.skill_contract()}
                if bootstrap_profile == "session"
                else {}
            ),
        )

    assert payload["active_capabilities"] == descriptor.as_metadata()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert all(name not in serialized for name in FORBIDDEN_COMMANDS)
    if bootstrap_profile == "session":
        assert "simple_actions" not in payload
    else:
        for action in ("ask", "remember", "capture", "review", "connect"):
            route = payload["simple_actions"][action]["route"]
            assert route["tool"] in descriptor.callable_commands
        for action in ("adopt", "maintain"):
            assert payload["simple_actions"][action]["available"] is False
            assert "route" not in payload["simple_actions"][action]
