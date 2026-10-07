"""Historical descriptors stay frozen while bootstrap versions runtime migrations.

Hosted v1-v4 retain their pre-split compact bootstrap shape. Contract
2026-10-07.1 explicitly supersedes optional-kind and `other` capture guidance.
The payload pins were recorded after proving that undoing only the migration
fields reproduces every origin/main digest. Only the server version and tool
surface fingerprints are normalized away.

"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import tempfile

import pytest

from exomem import capabilities, commands, hosted_legacy_schemas, source_taxonomy

LEGACY_PROFILES = tuple(f"hosted-alpha-agent-v{n}" for n in range(1, 5))
LEVELS = ("off", "light", "balanced", "maximal")

#: profile -> level -> sha256 of the normalised compact payload.
GOLDEN: dict[str, dict[str, str]] = {
    "hosted-alpha-agent-v1": {
        "off": "47432b52c7ffba2b4fc86f99b40eddc463b7c1b6cd785a1081829852a82ef86b",
        "light": "ce36a62fbe8048325487e6857353d40b1cdca63f08c5b89c941b835904f596df",
        "balanced": "383c646bd92d4e35aa8ac8aadcf94750208c55b6fc03a1f8e67ede2df54ac938",
        "maximal": "fdba8d1d1866fcc9a319df08427bee7535ccaeb8391f583b4fd6dcd84d014cad"
    },
    "hosted-alpha-agent-v2": {
        "off": "3aaed417196a84e59629d56367a8df74e5b45ced0228cf0e17c9448a7aab7939",
        "light": "b94b2f3bb5871a6cf7708e4042f63cb189cef7fa208d4f3c1104cddf2ec8e3c2",
        "balanced": "e243a26d3d49c08c9bc55040a93b59401f857310c2cabeb75c3871afb06ddd25",
        "maximal": "276aa7f45a22b2561d1382df46d0b9c0d14da208f9adbf23b0adc6052d8d2659"
    },
    "hosted-alpha-agent-v3": {
        "off": "cf0e7b499f71e23d426803cb7a720abfca223ef6526757745165dabc842b8f1f",
        "light": "cd0641fe2c65ca9d8359eaf683bda786065785fe1807827fd765957389d39213",
        "balanced": "a7634b1720111b027dc5197d457b6fccdab80def7eefa3f44a442354a0dc7a64",
        "maximal": "3af955e27372c3c605f855f408a47350a7d54daf6f463e265eab64e1c6918846"
    },
    "hosted-alpha-agent-v4": {
        "off": "225e7ba49ad5ea42307a12f1b5edc027774214b1a7a9dcc20e6a099e2f102405",
        "light": "895751a5378e798ac6562a3e1e7dc7916b8bd49b5cfbc3ad7d46655b90f1374e",
        "balanced": "175483688259f20a02a22e5187f8f99711910d201e1a6ca3471184ff811c9c39",
        "maximal": "8b36571706ad2d7cf3fb6f91d1c781e1367e4153d8caeadc1d3e7a272232775a"
    }
}

_VOLATILE_SERVER_KEYS = (
    "version",
    "published_mcp_tool_surface_sha256",
    "canonical_mcp_tool_surface",
)


def normalised_digest(payload: dict) -> str:
    payload = json.loads(json.dumps(payload))
    for key in _VOLATILE_SERVER_KEYS:
        payload["server"].pop(key, None)
    payload["active_capabilities"].pop("active_capability_sha256", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("profile", LEGACY_PROFILES)
def test_a_released_profile_serves_its_versioned_payload(monkeypatch, profile, level):
    payload = legacy_compact(monkeypatch, profile, level)

    assert payload["profile"] == "compact"
    assert "sections" not in payload
    assert payload["contract_version"] == "2026-10-07.1"
    assert payload["source_taxonomy"]["kind_rule"] == source_taxonomy.CAPTURE_KIND_RULE
    assert payload["source_taxonomy"]["migration"] == source_taxonomy.CAPTURE_KIND_MIGRATION
    assert normalised_digest(payload) == GOLDEN[profile][level]


@pytest.mark.parametrize("profile", LEGACY_PROFILES)
def test_a_released_profile_teaches_one_authoring_contract(monkeypatch, profile):
    """Bootstrap and the pinned tool descriptions name the same contract identity.

    GOLDEN pins the bootstrap bytes and the legacy schema pin pins the tool
    descriptions; neither notices a client being told two contract versions.
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
