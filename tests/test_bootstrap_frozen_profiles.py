"""Released descriptors retain their authoring identity under current runtime guidance."""

from __future__ import annotations

import pathlib
import re
import tempfile

import pytest

from exomem import capabilities, commands, hosted_legacy_schemas, source_taxonomy

LEGACY_PROFILES = tuple(f"hosted-alpha-agent-v{n}" for n in range(1, 5))
def legacy_compact(monkeypatch: pytest.MonkeyPatch, profile: str, level: str) -> dict:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    registry = commands.product_commands_for_profile(profile, "rest")
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="hosted-agent",
        profile=profile,
        tier2_enabled=commands.PRODUCT_SURFACE_PROFILES[profile].expose_tier2,
        product_commands=tuple(command.name for command in registry),
    )
    with capabilities.active_surface(descriptor):
        return commands.op_bootstrap(root, profile="compact")


@pytest.mark.parametrize("profile", LEGACY_PROFILES)
def test_a_released_profile_serves_its_versioned_payload(monkeypatch, profile):
    payload = legacy_compact(monkeypatch, profile, "balanced")

    assert payload["profile"] == "compact"
    assert "sections" not in payload
    assert payload["contract_version"] == "2026-10-08.1"
    assert payload["source_taxonomy"]["kind_rule"] == source_taxonomy.CAPTURE_KIND_RULE
    assert payload["source_taxonomy"]["migration"] == source_taxonomy.CAPTURE_KIND_MIGRATION


@pytest.mark.parametrize("profile", LEGACY_PROFILES)
def test_a_released_profile_teaches_one_authoring_contract(monkeypatch, profile):
    """Bootstrap and the pinned tool descriptions name the same contract identity.

    A frozen client must receive the identity its published tools advertise.
    """
    contract = legacy_compact(monkeypatch, profile, "balanced")["semantic_authoring"]
    published = {
        match
        for command in hosted_legacy_schemas.LEGACY_PROFILE_CONTRACTS[profile].values()
        for match in re.findall(
            r"exomem\.semantic-authoring:v(\d+) (sha256:[0-9a-f]{64})", command.description
        )
    }

    assert published == {(str(contract["version"]), contract["content_digest"])}


@pytest.mark.parametrize("profile", LEGACY_PROFILES)
def test_a_released_profile_rejects_a_section_argument(monkeypatch, profile):
    """The pinned wire has no `section`; the leaf refuses it rather than ignoring it."""
    monkeypatch.setenv("EXOMEM_PROMINENCE", "balanced")
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    registry = commands.product_commands_for_profile(profile, "rest")
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="hosted-agent",
        profile=profile,
        tier2_enabled=commands.PRODUCT_SURFACE_PROFILES[profile].expose_tier2,
        product_commands=tuple(command.name for command in registry),
    )
    with capabilities.active_surface(descriptor):
        with pytest.raises(ValueError, match="section"):
            commands.op_bootstrap(root, profile="compact", section="authoring")


def test_frozen_v3_authoring_follows_a_governed_status_replacement(vault):
    from jsonschema import Draft202012Validator

    from exomem import activation_manifest
    from exomem.governance.principal import library_scope
    from exomem.vault import content_hash
    from exomem.writer_lease import invoke_command

    path = "Knowledge Base/Notes/Insights/harbour-timetable.md"
    page = vault / path
    page.parent.mkdir(parents=True, exist_ok=True)
    source = (
        "---\ntype: insight\ntitle: Harbour timetable\nstatus: under-review\n"
        "exomem_id: 00000000-0000-4000-8000-000000000091\n---\n\nOrdinary draft prose.\n"
    )
    page.write_text(source)
    with library_scope():
        before = commands.op_schema_memory(vault, subject="statuses", operation="inspect")
        saved = commands.op_schema_memory(
            vault,
            subject="statuses",
            operation="save",
            expected_hash=before["content_hash"],
            proposal={"upsert": {"under-review": {"attributes": {"class": "pending"}}}},
            why="the timetable remains a draft",
        )
        activation_manifest.ensure_manifest(vault)
        profile = "hosted-alpha-agent-v3"
        exposed = {
            command.name: command
            for command in commands.product_commands_for_profile(profile, "rest")
        }
        descriptor = capabilities.ActiveSurfaceDescriptor(
            surface="hosted-agent",
            profile=profile,
            tier2_enabled=commands.PRODUCT_SURFACE_PROFILES[profile].expose_tier2,
            product_commands=tuple(exposed),
        )
        arguments = {
            "path": path,
            "why": "validate the timetable",
            "validate_only": True,
            "operation": {
                "kind": "replace_body",
                "new_body": "Ordinary draft prose.",
                "expected_hash": content_hash(source),
            },
        }
        schema = hosted_legacy_schemas.json_value(
            hosted_legacy_schemas.LEGACY_PROFILE_CONTRACTS[profile]["edit_memory"].input_schema
        )
        Draft202012Validator(schema).validate(arguments)
        with capabilities.active_surface(descriptor):
            teaching = invoke_command(exposed["bootstrap"], vault, profile="full")
            assert "page_status" in teaching["workflow"]
            assert "schema_memory" not in exposed
            pending = invoke_command(exposed["edit_memory"], vault, **arguments)
            assert not pending["semantic"]["contract_result"]["should_block"]

        # A class is immutable: the owner introduces a distinct live meaning.
        commands.op_schema_memory(
            vault,
            subject="statuses",
            operation="save",
            expected_hash=saved["saved"]["content_hash"],
            proposal={
                "upsert": {"adopted": {"attributes": {"class": "live"}}},
                "deprecate": {"under-review": "adopted"},
            },
            why="the owner adopts the timetable",
        )
        with capabilities.active_surface(descriptor):
            empty = invoke_command(exposed["edit_memory"], vault, **arguments)
            assert any(
                finding["code"] == "missing_semantic_unit"
                for finding in empty["semantic"]["contract_result"]["errors"]
            )
            arguments["operation"]["new_body"] = (
                "## Observations\n\n- [decision] The ferry runs hourly. ^schedule\n"
            )
            complete = invoke_command(exposed["edit_memory"], vault, **arguments)
            assert not any(
                finding["code"] == "missing_semantic_unit"
                for finding in complete["semantic"]["contract_result"]["errors"]
            )
    assert page.read_text() == source
