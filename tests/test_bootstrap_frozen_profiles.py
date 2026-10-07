"""Released hosted profiles keep their compact bootstrap payload byte for byte.

`hosted-alpha-agent-v1` to `-v4` are published identities with committed
candidates. The core/section split (openspec change `shrink-bootstrap`) applies
to every other surface only, so a client on a released profile is served exactly
what it was served before the split.

The digests below were recorded on the base (integration/wave-bcd) before the split
was applied, and re-recorded from the untouched base when it advanced (efe52086):
the pin says this change alters no released profile, not that no one ever does. Only
values that legitimately move between releases are normalised away: the
server's own version and the tool-surface fingerprints, which the tool-schema
lane edits independently.

The capture behavior migrates through bootstrap contract 2026-10-07.1. Historical
command descriptors stay frozen; bootstrap and runtime refusals explicitly
supersede their optional-kind and `other` guidance. Re-record the payload pins
only after that migration and its shared rule have been verified.
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
        "off": "59a2b5b988633d0013c28d0d0842632f5209577448e86d550fb2c0bc5a303c39",
        "light": "c9a852ad009e5e6bbfe7a94fc8e9b795a28a91f2b88a888cb4880808e045a0b7",
        "balanced": "435e642217fea601f56485b55480fdd2b1209016d55bc1d117b333ad97d1b9f6",
        "maximal": "bd86dfe433a3fe8cea7fd38791b9a0ed8474de468172502db407e61d99385481",
    },
    "hosted-alpha-agent-v2": {
        "off": "d9602bbbcdfc3581e3f861734f9121c045480a2cbbf1553163f1b6a8fc04eb18",
        "light": "7093ae98574f5d67b9a1636e7001098d84730b11dab57779101b27f39d074b7a",
        "balanced": "24996de3edafbfcfa9c1b792d68bc4f5cd4c28eb25d434be02618965348c37cf",
        "maximal": "1be4f750b419b858f72895ae7e77896c30b85cfdb8ffe742b9408550e16e80d1",
    },
    "hosted-alpha-agent-v3": {
        "off": "4558caa9b0267f72f79d00e3f3cdde81b9d7a6234aa11cde803956a9e5d4cd38",
        "light": "59afe3ae5424a8c4dc1851e0393a21ca4c0057b9a497506d1454b7355cb172ca",
        "balanced": "9c850dd7117f1e2b3cbdfd39155149263e76b1f0700bc8358475184019d3c63a",
        "maximal": "4f6199288b58c7ee6d35ea33fccce129b1e4ee91b6e0e22ff1412a754d2fda3f",
    },
    "hosted-alpha-agent-v4": {
        "off": "b48ddc8c0cc452131d7520f0fd91b8f67043eb0956b1ce4e7dc260cd23a61a55",
        "light": "ff69e381fb81c64ba07354cb6cda87e663457ab51daed1562a0992e61381c75c",
        "balanced": "274c2c1c35b915426e9bd6126a268df294dafcc64f9e4d3b0589537d97a632bd",
        "maximal": "c28c64f21a65be60bd9a2395ab5c764a62d10a022050b64c116bb1cef6abcc2f",
    },
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
