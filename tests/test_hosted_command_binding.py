from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands, hosted_plugins
from exomem import hosted_gateway as gateway

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "contracts" / "hosted-agent-command-binding-v1.json"
COMMAND_BINDING_CANDIDATE = "hosted-alpha-agent-v4-command-binding-v1"
DIRECT_CANDIDATE = "hosted-alpha-agent-v4-direct-v1"


def _fixture_producer_tuple(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, object], dict[str, object]]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expected = fixture["expectedTuple"]
    monkeypatch.setattr(gateway, "__version__", expected["release"])
    contract = gateway.build_agent_gateway_contract(profile=commands.HOSTED_ALPHA_AGENT_V4_PROFILE)
    return fixture, {
        "surfaceProfile": commands.HOSTED_ALPHA_AGENT_V4_PROFILE,
        "release": contract["exomem_release"],
        "commandFingerprint": contract["agent_profile"]["active_capability_sha256"],
        "contractDigest": gateway.published_agent_contract_digest(contract),
    }


def test_command_binding_fixture_uses_the_published_agent_contract_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, producer_tuple = _fixture_producer_tuple(monkeypatch)

    assert fixture["expectedTuple"] == producer_tuple


def test_command_binding_fixture_does_not_follow_current_package_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "__version__", "99.0.0")
    current_contract = gateway.build_agent_gateway_contract(
        profile=commands.HOSTED_ALPHA_AGENT_V4_PROFILE
    )
    fixture, producer_tuple = _fixture_producer_tuple(monkeypatch)

    assert current_contract["exomem_release"] == "99.0.0"
    assert fixture["expectedTuple"] == producer_tuple


def test_signed_compatibility_advertises_command_binding() -> None:
    for candidate in {COMMAND_BINDING_CANDIDATE, DIRECT_CANDIDATE}:
        manifest = hosted_plugins.compatibility_manifest(ROOT, candidate=candidate)

        assert manifest["features"] == ["agent-command-binding-v1"]
        assert manifest["profile"] == commands.HOSTED_ALPHA_AGENT_V4_PROFILE
    assert hosted_plugins.load_definition(ROOT, candidate=COMMAND_BINDING_CANDIDATE).version == "0.4.1"
    for candidate in hosted_plugins.CANDIDATE_PROFILES:
        if candidate not in {COMMAND_BINDING_CANDIDATE, DIRECT_CANDIDATE}:
            assert "features" not in hosted_plugins.compatibility_manifest(ROOT, candidate=candidate)


def test_command_binding_candidate_is_self_contained_with_v4_selection_cases() -> None:
    candidate_root = ROOT / "plugins" / "hosted" / "candidates" / COMMAND_BINDING_CANDIDATE

    assert COMMAND_BINDING_CANDIDATE in hosted_plugins.SELF_CONTAINED_CANDIDATES
    assert COMMAND_BINDING_CANDIDATE not in hosted_plugins.FIXTURE_BOUND_CANDIDATES
    assert hosted_plugins._skill_paths(ROOT, COMMAND_BINDING_CANDIDATE)
    assert all(path.is_relative_to(candidate_root) for path in hosted_plugins._skill_paths(ROOT, COMMAND_BINDING_CANDIDATE))
    for skill in hosted_plugins.SKILL_NAMES:
        assert (
            (candidate_root / "skills" / skill / "SKILL.md").read_bytes()
            == (ROOT / "plugins/hosted/skills" / skill / "SKILL.md").read_bytes()
        )
    for skill in hosted_plugins.CANDIDATE_SKILL_NAMES[hosted_plugins.PARITY_CANDIDATE]:
        assert (
            (candidate_root / "skills" / skill / "SKILL.md").read_bytes()
            == (
                ROOT
                / "plugins/hosted/candidates/hosted-alpha-agent-v4/skills"
                / skill
                / "SKILL.md"
            ).read_bytes()
        )
    assert (
        (candidate_root / "selection-cases.json").read_bytes()
        == (ROOT / "plugins/hosted/candidates/hosted-alpha-agent-v4/selection-cases.json").read_bytes()
    )


def test_command_binding_candidate_generated_artifacts_are_current() -> None:
    hosted_plugins.check(ROOT, platform="all", candidate=COMMAND_BINDING_CANDIDATE)
