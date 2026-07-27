"""The hosted descriptor identifies the contract surface, not the build.

Before this, `compatibility_sha256` hashed the Exomem release three ways over:
`source_release`, `agent_contract.exomem_release`, and `definition_sha256` (a
digest of a definition that pinned the release). Every version bump therefore
invalidated the committed artifacts -- which broke release CI on 0.34.0 -- and
invalidated any live promotion record for a plugin whose contract had not
changed, forcing a re-promotion per patch release.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import hosted_gateway, hosted_plugins

REPO_ROOT = Path(__file__).parents[1]


def test_a_version_bump_does_not_change_the_descriptor(monkeypatch) -> None:
    """The regression that cost a release: a bump must be a no-op here."""

    before = hosted_plugins.compatibility_manifest(REPO_ROOT)
    monkeypatch.setattr(hosted_gateway, "__version__", "99.99.99")
    after = hosted_plugins.compatibility_manifest(REPO_ROOT)

    assert after == before
    assert after["compatibility_sha256"] == before["compatibility_sha256"]


def test_the_descriptor_never_carries_the_release(monkeypatch) -> None:
    monkeypatch.setattr(hosted_gateway, "__version__", "99.99.99")
    serialized = json.dumps(hosted_plugins.compatibility_manifest(REPO_ROOT), sort_keys=True)

    assert "99.99.99" not in serialized
    assert "exomem_release" not in serialized
    assert "source_release" not in serialized


def test_the_runtime_contract_still_reports_the_release(monkeypatch) -> None:
    """Decoupling the artifact must not blind the running server."""

    monkeypatch.setattr(hosted_gateway, "__version__", "99.99.99")
    contract = hosted_gateway.build_agent_gateway_contract(profile="hosted-alpha-agent-v1")

    assert contract["exomem_release"] == "99.99.99"


def test_a_contract_surface_change_still_moves_the_hash(monkeypatch) -> None:
    """Decoupling must not make the descriptor insensitive to real changes.

    Mutates real surface content rather than the digest field: the descriptor
    now recomputes that digest from the published contract, so injecting one
    would simply be overwritten and prove nothing.
    """

    before = hosted_plugins.compatibility_manifest(REPO_ROOT)
    real = hosted_gateway.build_agent_gateway_contract

    def altered(*args, **kwargs):
        contract = real(*args, **kwargs)
        commands = [dict(item) for item in contract["commands"]]
        commands[0]["name"] = f"{commands[0]['name']}_renamed"
        return {**contract, "commands": commands}

    monkeypatch.setattr(hosted_gateway, "build_agent_gateway_contract", altered)
    after = hosted_plugins.compatibility_manifest(REPO_ROOT)

    assert after["commands"] != before["commands"]
    assert after["schema_contract_sha256"] != before["schema_contract_sha256"]
    assert after["compatibility_sha256"] != before["compatibility_sha256"]


def test_the_definition_no_longer_pins_a_release() -> None:
    raw = json.loads(
        (REPO_ROOT / "plugins/hosted/definition.json").read_text(encoding="utf-8")
    )
    assert "source_release" not in raw

    definition = hosted_plugins.load_definition(REPO_ROOT)
    assert not hasattr(definition, "source_release")


def test_an_unknown_definition_field_is_still_rejected(tmp_path: Path) -> None:
    """Removing a field must not loosen the schema."""

    raw = json.loads(
        (REPO_ROOT / "plugins/hosted/definition.json").read_text(encoding="utf-8")
    )
    raw["source_release"] = "0.34.0"
    root = tmp_path / "repo"
    (root / "plugins" / "hosted").mkdir(parents=True)
    (root / "plugins/hosted/definition.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported Hosted definition fields"):
        hosted_plugins.load_definition(root)
