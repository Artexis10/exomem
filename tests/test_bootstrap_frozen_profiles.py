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
        "off": "dfdc462e01280cdc5cc7d625773626633cc265bc0ed9e940e265eea2b3fef4e7",
        "light": "59eb03dc0eb3fdf3021e2029293ec48a2cb8bcd9553a07f8e4b98bea7b1c35a0",
        "balanced": "4abcae2c87d5e4d4c249126ee45dc63a92812eae5f595054d1fe53f6a0b4cdd7",
        "maximal": "0b199fa64e0379f51cbe627a8f95576f2f17c038cb9800a90ea883b92085ffd7"
    },
    "hosted-alpha-agent-v2": {
        "off": "a5844c6eae11f18f3f92dfbe20d24791964372c6a1dbaf28a7cbd0756d300fdd",
        "light": "e782c24b571aa9864ab3f479a121627328a75ffc1a3581c1a0a05ed299ee65dd",
        "balanced": "4e4c8e3df1aff14cd9c9f04f9c9422ab88005c78b0ccf36ab258923f45649ab8",
        "maximal": "9ab97289f0f5090e545a5ab3c0a7a731f0b458092de6fff454f11012b73a3bd5"
    },
    "hosted-alpha-agent-v3": {
        "off": "784ae82a0968a7dc72a2c4d726a21dc3b3503e25ca69bcf992adb22bfbe5633c",
        "light": "177836497958d495ed74976408d7339e283044f21095e3cea9d99669a42d5e9d",
        "balanced": "ba691b78eb6bf68806ed769058fa190f6a2349b997483930dd4a8f028466b623",
        "maximal": "06075c15bd77591aede3f4e17e002bf47290743e9476752e2e942ae7beabaa6e"
    },
    "hosted-alpha-agent-v4": {
        "off": "b9f8048ea2b21192a5c0785a027f0f5982bef709db43bd20095cbe841f48b7c2",
        "light": "ff127de724952bd322ef894c49b8e36ab4a74a0633dc33c83ab6e32ac56fc54a",
        "balanced": "768b992244e854f103af79d2f1c11d031377529abbccccd66e9fa74472f5ad88",
        "maximal": "9c684d8eef1bb79ac62ac65382e48d1d66ff379be006ad9c258a27e860bb7f07"
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
