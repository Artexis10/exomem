"""Released hosted profiles keep their compact bootstrap payload byte for byte.

`hosted-alpha-agent-v1` to `-v4` are published identities with committed
candidates. The core/section split (openspec change `shrink-bootstrap`) applies
to every other surface only, so a client on a released profile is served exactly
what it was served before the split.

The digests below were recorded on the base before the split was written. Only
values that legitimately move between releases are normalised away: the
server's own version and the tool-surface fingerprints, which the tool-schema
lane edits independently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import tempfile

import pytest

from exomem import capabilities, commands

LEGACY_PROFILES = tuple(f"hosted-alpha-agent-v{n}" for n in range(1, 5))
LEVELS = ("off", "light", "balanced", "maximal")

#: profile -> level -> sha256 of the normalised compact payload.
GOLDEN: dict[str, dict[str, str]] = {
    "hosted-alpha-agent-v1": {
        "off": "aa0e7818973c0483fa7bd587bf4a0875997c99dea131098d24190f0a3b8f1d32",
        "light": "e16171701b4ca1460545026d0b4c213f7fdc8d19de5c1a72fa057cdbf2d7ce8b",
        "balanced": "b0c50696b0adc1b2527a6f95883ac471ac2e438cd4c1a061cb40782a6d39ec26",
        "maximal": "a29193ca7a34d4b0d6ac815129b9163fb235fac11824fd9e5ca139c904a23c00",
    },
    "hosted-alpha-agent-v2": {
        "off": "68d2a13d0e33fa75db2a71159300913b7b939e4e8606fb8c8249b3e0b2a1df02",
        "light": "756e12ff2c1027a41ace64c45c0ff8ff9a1b7cace8815c37a25fe491aa81ecb3",
        "balanced": "03e8bcb49a7bc8866fd66794dddcd0f849e041024868dd2a3f29c6dac4e168b2",
        "maximal": "06f06ac45cc07245d2d113ceed683ae9a57a7616be0740a1b51d0cc12ec91c63",
    },
    "hosted-alpha-agent-v3": {
        "off": "0e59ddfc97a422016522f893ede192eefaf69a7cab949db83e769f1554222890",
        "light": "ea2313c46cef67a2b361562958f9a64b62fceb9c4fdcca08b19fbe25c99ab817",
        "balanced": "80c4f7f7812bfcfd454d1ee5f7b9ef35b5857bf0729d1aa74dbb2a784aa60721",
        "maximal": "248d9dadddad994a027e3abeffd7cf0bb68b739a5fbec908cff632d1b660c705",
    },
    "hosted-alpha-agent-v4": {
        "off": "b6595039c5bc010d04ca29923d2c591d14c2e0474169a9760214337c79e8e8d6",
        "light": "4a69fd6fdc1a8dc33ef2987381a7766a657bad77363b5834d0d62aa5633e55ae",
        "balanced": "56044cffb080311e5131680a42556c2ce566dbd618597c990d1e246a1dc7707c",
        "maximal": "3c73fac0d2b31d91280b95ca3e7a72b635ebcf5f8bb6f245f8069a89c7b6536e",
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
    logging.disable(logging.CRITICAL)
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
def test_a_released_profile_serves_the_pre_split_payload(monkeypatch, profile, level):
    payload = legacy_compact(monkeypatch, profile, level)

    assert payload["profile"] == "compact"
    assert "sections" not in payload
    assert normalised_digest(payload) == GOLDEN[profile][level]


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
