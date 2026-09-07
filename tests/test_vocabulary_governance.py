"""Agent routes expose pending status but cannot construct an approval."""

import inspect

import pytest

from exomem import commands, reserved_paths, vocabulary_authority
from exomem.governance import authorization_request, operations
from exomem.governance.principal import library_scope


def test_pending_variants_are_finite_read_only_and_require_session():
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "govern_memory")
    for operation in ("vocabulary-request", "vocabulary-status"):
        assert operation in operations.OPERATION_SPECS
        assert commands.invocation_is_read_only(command, {"operation": operation})
        assert authorization_request.credential_rule(
            "govern_memory", {"operation": operation}
        ) == authorization_request.CredentialRule.REQUIRED
    assert "vocabulary_request_id" in inspect.signature(commands.op_govern_memory).parameters


def test_request_route_uses_only_server_owned_request_and_returns_no_content(tmp_path, monkeypatch):
    observed = []

    class Authority:
        def request_status(self, request_id, *, principal):
            observed.append(request_id)
            return vocabulary_authority.AuthorityRequestStatus(
                request_id, "pending", 1234,
                ({"action": "edge.add", "path": "private.md", "key": "private"},),
            )

    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: Authority())
    with library_scope(), reserved_paths._owner_authority_scope("govern_memory"):
        result = commands.op_govern_memory(
            tmp_path, operation="vocabulary-request", vocabulary_request_id="server-request"
        )
        with pytest.raises(Exception, match="VOCABULARY_AUTHORITY_INVALID"):
            commands.op_govern_memory(
                tmp_path, operation="vocabulary-request",
                vocabulary_request_id="server-request", documents={"approval": "yes"},
            )
    assert observed == ["server-request"]
    assert result["request_id"] == "server-request"
    assert result["state"] == "pending"
    assert "private" not in str(result)


def test_authority_error_has_stable_public_request_route():
    error = vocabulary_authority.VocabularyAuthorityDenied("approval required")
    error.details["vocabulary_request_id"] = "request-1"
    assert error.as_public_dict()["code"] == "VOCABULARY_AUTHORITY_DENIED"
    assert error.as_public_dict()["vocabulary_request_id"] == "request-1"


@pytest.mark.parametrize("mode", ["v2", "unavailable"])
def test_bootstrap_reports_current_authority_without_downgrading(vault, monkeypatch, mode):
    class Authority:
        def runtime_status(self):
            return vocabulary_authority.AuthorityStatus(mode, 7 if mode == "v2" else None, 0)

    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: Authority())
    authority = commands.op_bootstrap(vault)["vocabulary_workflow"]["authority"]
    assert authority["contract_version"] == mode
    assert authority["scoped_delegation"] is (mode == "v2")
    assert authority["status"]["route"] == {
        "tool": "govern_memory", "args": {"operation": "vocabulary-status"}
    }
    if mode == "v2":
        assert "exact approval" in authority["rule"]
    else:
        assert "refuse" in authority["rule"]
