"""Serving readiness must distinguish implemented erasure from enabled host policy."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811

import json

from test_passage_evidence import passage_tools  # noqa: F401

from exomem import capabilities, cell_identity, commands, source_erasure


def test_public_policy_tracks_configuration_without_claiming_the_operation_is_enabled(vault, passage_tools, monkeypatch):
    call, _ = passage_tools
    monkeypatch.delenv("EXOMEM_SOURCE_ERASURE_POLICY", raising=False)
    request = {"operation": "sources.erasure_policy", "arguments": {"system": "archive"}}
    disabled = call("inspect_memory", request)
    assert disabled["state"] == "observed" and disabled["enabled"] is False, disabled
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "inspect_capture_erasure_policy")
    surface = capabilities.ActiveSurfaceDescriptor(surface="mcp", profile="product", tier2_enabled=False,
                                                  product_commands=(command.name,), contributing_commands=(command,))
    assert "captured_source_erasure_policy" in cell_identity.service_contract(surface)["operations"]
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", json.dumps({"enabled_systems": ["archive", "another-system"]}))
    enabled = call("inspect_memory", request)
    assert enabled["state"] == "observed" and enabled["enabled"] is True, enabled
    assert enabled["policy_revision"] != disabled["policy_revision"]
    assert "another-system" not in json.dumps(enabled)


def test_public_policy_keeps_malformed_configuration_unknown(vault, passage_tools, monkeypatch):
    call, _ = passage_tools
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", "unreadable-policy-sentinel")
    result = call("inspect_memory", {"operation": "sources.erasure_policy", "arguments": {"system": "archive"}})
    assert result == {"state": "unknown", "enabled": None, "policy_revision": None,
                      "failure": {"code": "SOURCE_ERASURE_POLICY_UNAVAILABLE"}}, result


def test_public_policy_rechecks_namespace_authority_at_terminal_release(vault, passage_tools, monkeypatch):
    from test_governance_egress import write_rule, write_scope

    from exomem.governance.principal import OWNER_AUDIENCE

    call, _ = passage_tools
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", json.dumps({"enabled_systems": ["archive"]}))
    original = source_erasure.inspect_policy

    def revoked(root, **kwargs):
        result = original(root, **kwargs)
        write_scope(root, paths="Sources/**")
        write_rule(root, ceiling=0, audience=OWNER_AUDIENCE)
        return result

    monkeypatch.setattr(source_erasure, "inspect_policy", revoked)
    result = call("inspect_memory", {"operation": "sources.erasure_policy", "arguments": {"system": "archive"}})
    assert "SOURCE_IDENTITY_FORBIDDEN" in str(result)
    assert "enabled" not in result
