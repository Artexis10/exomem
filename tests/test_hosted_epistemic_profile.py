"""The Hosted epistemic profile: revision, intent, and in-place correction.

`hosted-alpha-agent-v1` and `hosted-alpha-agent-v2` are accumulation-only. This
module pins `hosted-alpha-agent-v3` -- v2 plus `replace_memory`, `plan_memory`,
and `edit_memory` -- and pins the two things the widening must *not* do: mutate
an already-published profile, or move the full local tool surface.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from exomem import commands, hosted_plugins
from exomem import hosted_gateway as gateway

REPO_ROOT = Path(__file__).resolve().parents[1]

V1_PROFILE = "hosted-alpha-agent-v1"
V2_PROFILE = "hosted-alpha-agent-v2"
V3_PROFILE = "hosted-alpha-agent-v3"

V1_COMMANDS = (
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
V2_COMMANDS = (*V1_COMMANDS, "record_memory")
EPISTEMIC_ADDITIONS = ("replace_memory", "plan_memory", "edit_memory")
V3_COMMANDS = (*V2_COMMANDS, *EPISTEMIC_ADDITIONS)

STILL_FORBIDDEN = {
    "coordination_status",
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

MCP_SCHEMA_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "mcp_tool_schemas.json"
TOOL_SURFACE_CONTRACT = REPO_ROOT / "src" / "exomem" / "tool_surface_contract.json"
CHATGPT_PLUGIN_CONTRACT = REPO_ROOT / "deploy" / "chatgpt" / "personal-plugin-contract.json"
V1_RELEASE_IDENTITIES = REPO_ROOT / "tests" / "fixtures" / "hosted" / "v1-release-identities.json"


def test_epistemic_profile_is_registered_with_exact_ordered_membership() -> None:
    assert V3_PROFILE in commands.PRODUCT_SURFACE_PROFILES
    assert commands.HOSTED_ALPHA_AGENT_V3_PROFILE == V3_PROFILE

    selected = commands.product_commands_for_profile(V3_PROFILE, "rest")

    assert tuple(command.name for command in selected) == V3_COMMANDS
    assert all(command.tier == 1 for command in selected)
    assert all("rest" in command.surfaces for command in selected)
    canonical = {command.name: command for command in commands.PRODUCT_COMMANDS}
    assert all(command is canonical[command.name] for command in selected)


def test_epistemic_profile_closes_the_loop_that_v1_and_v2_leave_open() -> None:
    v1 = tuple(c.name for c in commands.product_commands_for_profile(V1_PROFILE, "rest"))
    v2 = tuple(c.name for c in commands.product_commands_for_profile(V2_PROFILE, "rest"))
    v3 = tuple(c.name for c in commands.product_commands_for_profile(V3_PROFILE, "rest"))

    # The accumulation-only gap this change exists to close.
    assert set(EPISTEMIC_ADDITIONS).isdisjoint(v1)
    assert set(EPISTEMIC_ADDITIONS).isdisjoint(v2)
    assert set(EPISTEMIC_ADDITIONS).issubset(v3)

    # v3 extends v2 as a prefix, so `command_surface_sha256` ordering is checkable.
    assert v3[: len(v2)] == v2
    assert v3[len(v2) :] == EPISTEMIC_ADDITIONS


def test_widening_does_not_reopen_tier2_or_broad_administration() -> None:
    selected = {c.name for c in commands.product_commands_for_profile(V3_PROFILE, "rest")}

    assert STILL_FORBIDDEN.isdisjoint(selected)


@pytest.mark.parametrize("surface", ["mcp", "rest", "cli"])
def test_epistemic_membership_cannot_expand_on_another_surface(surface: str) -> None:
    selected = commands.product_commands_for_profile(V3_PROFILE, surface)

    assert tuple(command.name for command in selected) == V3_COMMANDS


def test_published_profiles_are_untouched_by_the_widening() -> None:
    v1 = commands.product_commands_for_profile(V1_PROFILE, "rest")
    v2 = commands.product_commands_for_profile(V2_PROFILE, "rest")

    assert tuple(command.name for command in v1) == V1_COMMANDS
    assert tuple(command.name for command in v2) == V2_COMMANDS


def test_epistemic_profile_yields_a_deterministic_agent_contract() -> None:
    descriptor = gateway.hosted_agent_surface_descriptor(V3_PROFILE)
    contract = gateway.build_agent_gateway_contract(profile=V3_PROFILE)
    repeated = gateway.build_agent_gateway_contract(profile=V3_PROFILE)

    assert descriptor.profile == V3_PROFILE
    assert descriptor.product_commands == V3_COMMANDS
    assert gateway.canonical_contract_json(contract) == gateway.canonical_contract_json(repeated)
    assert tuple(entry["name"] for entry in contract["commands"]) == V3_COMMANDS

    v2_contract = gateway.build_agent_gateway_contract(profile=V2_PROFILE)
    shared = {entry["name"]: entry for entry in v2_contract["commands"]}
    for entry in contract["commands"]:
        if entry["name"] in shared:
            assert entry == shared[entry["name"]]


def test_epistemic_commands_are_registry_identical_between_profiles_and_local_surface() -> None:
    """No per-surface schema fork: the gateway forwards registry bytes."""

    fixture = json.loads(MCP_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
    contract = gateway.build_agent_gateway_contract(profile=V3_PROFILE)
    entries = {entry["name"]: entry for entry in contract["commands"]}

    for name in EPISTEMIC_ADDITIONS:
        assert entries[name]["mcp_tool"]["inputSchema"] == fixture[name]["inputSchema"]
        assert entries[name]["mcp_tool"]["description"] == fixture[name]["description"]


def test_hosted_v3_candidate_is_declared_and_self_contained() -> None:
    definition = hosted_plugins.load_definition(
        REPO_ROOT, candidate=hosted_plugins.EPISTEMIC_CANDIDATE
    )

    assert hosted_plugins.EPISTEMIC_CANDIDATE == V3_PROFILE
    assert definition.profile == V3_PROFILE
    assert definition.plugin_id != hosted_plugins.load_definition(REPO_ROOT).plugin_id

    candidate_root = REPO_ROOT / "plugins/hosted/candidates" / V3_PROFILE
    assert (candidate_root / "definition.json").is_file()
    assert (candidate_root / "selection-cases.json").is_file()
    assert (candidate_root / "skills/exomem-records/SKILL.md").is_file()
    assert (candidate_root / "skills/exomem-supersede/SKILL.md").is_file()


def test_v3_skills_teach_supersession_planning_and_correction() -> None:
    dependencies = hosted_plugins.skill_dependencies(
        REPO_ROOT, candidate=hosted_plugins.EPISTEMIC_CANDIDATE
    )

    assert "exomem-supersede" in dependencies
    assert set(dependencies["exomem-supersede"]) >= set(EPISTEMIC_ADDITIONS)
    assert set(dependencies) == set(hosted_plugins.SKILL_NAMES) | {
        "exomem-records",
        "exomem-supersede",
    }


def test_v3_compatibility_binds_the_records_reader_floor_and_its_own_selection_cases() -> None:
    manifest = hosted_plugins.compatibility_manifest(
        REPO_ROOT, candidate=hosted_plugins.EPISTEMIC_CANDIDATE
    )
    v2_manifest = hosted_plugins.compatibility_manifest(
        REPO_ROOT, candidate=hosted_plugins.LIFECYCLE_CANDIDATE
    )

    assert manifest["profile"] == V3_PROFILE
    assert manifest["commands"] == list(V3_COMMANDS)
    assert manifest["minimum_records_reader_version"] == 2
    assert manifest["commands"][: len(v2_manifest["commands"])] == v2_manifest["commands"]

    files = hosted_plugins.candidate_files(
        REPO_ROOT, platform="claude", candidate=hosted_plugins.EPISTEMIC_CANDIDATE
    )
    lock = json.loads(files["claude.lock.json"])
    assert lock["profile"] == V3_PROFILE
    assert lock["minimum_records_reader_version"] == 2
    assert lock["selection_cases_sha256"]


def test_committed_v3_claude_artifacts_are_fresh() -> None:
    hosted_plugins.check(
        REPO_ROOT, platform="claude", candidate=hosted_plugins.EPISTEMIC_CANDIDATE
    )


def test_v3_ships_no_openai_package_until_a_chatgpt_refresh_is_accepted() -> None:
    generated = REPO_ROOT / "plugins/hosted/generated/candidates" / V3_PROFILE

    assert (generated / "claude.lock.json").is_file()
    assert not (generated / "openai").exists()
    assert not (generated / "openai.lock.json").exists()

    for platform in hosted_plugins.PLATFORMS:
        record = json.loads(
            hosted_plugins.promotion_record(
                REPO_ROOT, platform, candidate=hosted_plugins.EPISTEMIC_CANDIDATE
            ).read_text(encoding="utf-8")
        )
        assert record["state"] == "pending"
        assert record["candidate"] == V3_PROFILE


def test_public_input_gate_exempts_only_the_documented_continuation_field(
    tmp_path: Path,
) -> None:
    """`edit_memory`'s canonical description documents `transition_token=`.

    That is a governance continuation value the caller echoes back, not a
    credential, and the description is registry-canonical -- editing it would
    move the registered external connector fingerprint. Exempting it must not
    blunt the gate for anything else.
    """

    hosted_plugins.validate_hosted_public_inputs(REPO_ROOT)

    root = tmp_path / "repo"
    for relative in (
        "plugins/hosted/definition.json",
        "plugins/hosted/skills/exomem/SKILL.md",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / relative).read_bytes())
    for name in hosted_plugins.SKILL_NAMES:
        target = root / "plugins/hosted/skills" / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / "plugins/hosted/skills" / name / "SKILL.md").read_bytes())

    real_transition_token = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"after_hash": "a" * 64, "path": "Knowledge Base/Notes/private-project.md"}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )

    leaky = root / "plugins/hosted/skills/exomem/SKILL.md"
    original = leaky.read_bytes()
    for credential in (
        "API_TOKEN=abc123",
        "access_token: abc123",
        "operator_secret=abc123",
        "my_password = abc123",
        "x_transition_token=abc123",
        "transition_token_secret=abc123",
        # A *real* transition token is unsigned base64url JSON carrying a
        # vault-relative page path in cleartext. Base64 slips past the
        # `vault_path` alternations, so this rule is the only thing stopping it.
        f"transition_token={real_transition_token}",
        "transition_token=returned",
    ):
        leaky.write_bytes(original + f"\n{credential}\n".encode())
        with pytest.raises(ValueError, match="unsafe"):
            hosted_plugins.validate_hosted_public_inputs(root, include_generated=False)
    leaky.write_bytes(
        original + b"\nEcho back `transition_token=<returned transition_token>`.\n"
    )
    hosted_plugins.validate_hosted_public_inputs(root, include_generated=False)


def test_v1_and_v2_generated_artifacts_still_match_a_fresh_render() -> None:
    for candidate in (hosted_plugins.DEFAULT_CANDIDATE, hosted_plugins.LIFECYCLE_CANDIDATE):
        hosted_plugins.check(REPO_ROOT, platform="claude", candidate=candidate)

    fixture = json.loads(V1_RELEASE_IDENTITIES.read_text(encoding="utf-8"))
    for relative, expected in fixture.items():
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == expected


def test_profile_membership_does_not_move_the_local_tool_surface() -> None:
    """A changed local surface would invalidate the ChatGPT plugin fingerprint."""

    surface = json.loads(TOOL_SURFACE_CONTRACT.read_text(encoding="utf-8"))
    plugin = json.loads(CHATGPT_PLUGIN_CONTRACT.read_text(encoding="utf-8"))
    fixture = json.loads(MCP_SCHEMA_FIXTURE.read_text(encoding="utf-8"))

    # The full local surface exposes every product command; a profile is a
    # strict subset view over it and cannot add or remove local tools.
    assert set(V3_COMMANDS).issubset(fixture)
    assert plugin["pending_tool_surface_sha256"] == surface["sha256"]
    assert V3_PROFILE not in json.dumps(surface)
    assert V3_PROFILE not in json.dumps(plugin)
