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
        "off": "2be2e4fc7b0f2df1ae7ce5e4b00ef00e539e867c0a60ac6789d58ba5ffeb72bb",
        "light": "a8b86e762cfc4c91dc6fc1264594056b3af0c6cdb004a5fcc8163178fc53f072",
        "balanced": "628982b4aa3922cd9d7490d1554cb813475c2c80b8c5c7e9f88e49ef45510707",
        "maximal": "cce587e5241fb1bda5a3399d61fc36b133593ba4ea70969b5b3b839a9d1f666d",
    },
    "hosted-alpha-agent-v2": {
        "off": "d62f2fa37e85120bf3a5ff624ff38006e62a22481a14b95a5b09b05a5bc1692b",
        "light": "181f1b5ce82f7de8caf6198c826d795ac58904d89039d7a589847818467d4d7c",
        "balanced": "37c1c3bddabfe2be453ef430ac5c5798006a839826aed5737915fb799065cf22",
        "maximal": "9cb8a290eb1dad908ae525df7b447a4432871a4e62a3ec63b4f5b6ac63337843",
    },
    "hosted-alpha-agent-v3": {
        "off": "b80f29ff8d18815f582fa99c83152ee1cccca8ed49f8ad5afc23d542f0084c0b",
        "light": "e2c1dae68fc930f7a27d0b4569ea9319662f3b21426aba64c664e7a781f3d948",
        "balanced": "78e03c039a417120eb5da46bac3bdd15d0ec483fee3543ce50cf6337f95374e3",
        "maximal": "f902ce970997395e43cbea7ae0514762904a5ba12ce2e355733f669872c1e5e8",
    },
    "hosted-alpha-agent-v4": {
        "off": "d5a967117267a44c35bd30344bde89e638c85f4745278a60fa497ebd3661c6dd",
        "light": "6953349500e403a5a3687eab84d11ca59646f9855b467fcc4bcaf9537f25f979",
        "balanced": "b1ffd3f46470a692bf61de2b7d79246258d72bd419290efc6652b9cbac102de4",
        "maximal": "cc9584123d6d3426a13c0f19b7fb36602170e023dff2d9745700dbd38154937e",
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
