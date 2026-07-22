"""Unit tests for the shared envelope + arg coercion (cli_ops)."""

from __future__ import annotations

import pytest

from exomem import cli_ops
from exomem import vault as vault_module
from exomem.commands import Param


def test_envelope_success_shape() -> None:
    env = cli_ops.envelope(True, data=[1, 2, 3])
    assert env == {"success": True, "data": [1, 2, 3]}
    assert "error" not in env


def test_envelope_failure_shape() -> None:
    env = cli_ops.envelope(False, error={"code": "X", "message": "y", "remediation": None})
    assert env == {"success": False, "error": {"code": "X", "message": "y", "remediation": None}}


def test_error_dict_from_op_error() -> None:
    err = cli_ops.error_dict(cli_ops.OpError("BAD_BOOL", "nope"))
    assert err["code"] == "BAD_BOOL"
    assert err["message"] == "nope"
    assert err["remediation"]  # BAD_BOOL has a canned remediation


def test_error_dict_parses_leaf_contract_valueerror() -> None:
    err = cli_ops.error_dict(ValueError("NOT_FOUND: no such file"))
    assert err["code"] == "NOT_FOUND"
    assert err["message"] == "no such file"


def test_error_dict_unprefixed_valueerror_is_op_error() -> None:
    err = cli_ops.error_dict(ValueError("just a message"))
    assert err["code"] == "OP_ERROR"
    assert err["message"] == "just a message"


def test_http_status_mapping() -> None:
    assert cli_ops.http_status_for("NOT_FOUND") == 404
    assert cli_ops.http_status_for("OLD_NOT_FOUND") == 404
    assert cli_ops.http_status_for("ENTITY_EXISTS") == 409
    assert cli_ops.http_status_for("ENTITY_AMBIGUOUS") == 409
    assert cli_ops.http_status_for("WRITER_FENCED") == 409
    assert cli_ops.http_status_for("MUTATION_BUSY") == 409
    assert cli_ops.http_status_for("MUTATION_WARMING") == 409
    assert cli_ops.http_status_for("MUTATION_LOCK_UNAVAILABLE") == 503
    assert cli_ops.http_status_for("INGRESS_BYPASSED") == 403
    assert cli_ops.http_status_for("INVALID_NOTE") == 400


def test_ingress_bypassed_is_a_terminal_refusal_with_actionable_remediation() -> None:
    # Registered alongside WRITER_LEASE_REQUIRED (design.md Decision 1): a
    # deliberate refusal — not a retryable/transient status — surfaced as 403
    # with its own canned remediation.
    op_error = cli_ops.OpError(
        "INGRESS_BYPASSED", "request reached the origin without transiting the HA edge"
    )
    error = cli_ops.error_dict(op_error)
    assert error["code"] == "INGRESS_BYPASSED"
    assert cli_ops.http_status_for(error["code"]) == 403
    assert "EXOMEM_EDGE_STAMP_ENFORCE=0" in error["remediation"]


@pytest.mark.parametrize(
    ("code", "committed"),
    [
        ("BATCH_ROLLBACK_INCOMPLETE", False),
        ("BATCH_CLEANUP_INCOMPLETE", True),
    ],
)
def test_batch_write_error_uses_shared_public_payload_and_conflict_status(
    code: str,
    committed: bool,
) -> None:
    error = vault_module.BatchWriteError(
        code,
        vault_module.BatchTargetSummary(2, ("first.md", "second.md"), 0),
        committed=committed,
    )

    assert cli_ops.error_dict(error) == error.as_public_dict()
    assert cli_ops.http_status_for(code) == 409


@pytest.mark.parametrize("code", ["MUTATION_BUSY", "MUTATION_WARMING", "MUTATION_LOCK_UNAVAILABLE"])
def test_mutation_lock_errors_have_actionable_remediation(code: str) -> None:
    error = cli_ops.error_dict(cli_ops.OpError(code, "hosted mutation unavailable"))
    assert error["code"] == code
    assert error["remediation"]


# ---------------- coercion ----------------

_PARAMS = (
    Param("query", "str"),
    Param("limit", "int"),
    Param("graph", "bool"),
    Param("tags", "list[str]"),
    Param("frontmatter", "dict"),
    Param("value", "json"),
)


def test_coerce_passthrough_json_native() -> None:
    out = cli_ops.coerce(
        _PARAMS,
        {"query": "x", "limit": 5, "graph": True, "tags": ["a", "b"]},
        tool="find",
    )
    assert out == {"query": "x", "limit": 5, "graph": True, "tags": ["a", "b"]}


def test_coerce_cli_strings() -> None:
    out = cli_ops.coerce(
        _PARAMS,
        {"limit": "7", "graph": "false", "tags": "a, b ,c", "frontmatter": '{"k": 1}'},
        tool="find",
    )
    assert out == {"limit": 7, "graph": False, "tags": ["a", "b", "c"], "frontmatter": {"k": 1}}


def test_coerce_bool_variants() -> None:
    for truthy in ("true", "1", "yes", "on", True):
        assert cli_ops.coerce(_PARAMS, {"graph": truthy}, tool="x")["graph"] is True
    for falsy in ("false", "0", "no", "off", False):
        assert cli_ops.coerce(_PARAMS, {"graph": falsy}, tool="x")["graph"] is False


def test_coerce_bad_int_raises() -> None:
    with pytest.raises(cli_ops.OpError) as exc:
        cli_ops.coerce(_PARAMS, {"limit": "abc"}, tool="x")
    assert exc.value.code == "BAD_INT"


def test_coerce_rejects_unknown_keys() -> None:
    with pytest.raises(cli_ops.OpError) as exc:
        cli_ops.coerce(_PARAMS, {"nope": 1}, tool="find")
    assert exc.value.code == "UNKNOWN_PARAM"
    assert "nope" in exc.value.message


def test_coerce_drops_none_values() -> None:
    out = cli_ops.coerce(_PARAMS, {"query": "x", "limit": None}, tool="find")
    assert out == {"query": "x"}  # None → let the leaf default apply


def test_coerce_dict_must_be_object() -> None:
    with pytest.raises(cli_ops.OpError) as exc:
        cli_ops.coerce(_PARAMS, {"frontmatter": "[1,2]"}, tool="x")
    assert exc.value.code == "BAD_JSON"


def test_coerce_blob_guard_rejects_base64() -> None:
    params = (Param("content", "str"),)
    blob = "data:image/png;base64," + "A" * 40000
    with pytest.raises(ValueError) as exc:
        cli_ops.coerce(params, {"content": blob}, guarded_fields=("content",), tool="add")
    assert "BINARY_BLOB_REJECTED" in str(exc.value)


def test_coerce_json_passthrough_for_union() -> None:
    # `value` (edit's union field) accepts arbitrary JSON, passed through untouched.
    out = cli_ops.coerce(_PARAMS, {"value": {"nested": [1, 2]}}, tool="edit")
    assert out == {"value": {"nested": [1, 2]}}
    out2 = cli_ops.coerce(_PARAMS, {"value": "42"}, tool="edit")
    assert out2 == {"value": 42}  # JSON string parsed


def test_coerce_json_rest_rejects_bare_string() -> None:
    # REST (cli=False) stays strict: a bare unquoted string is NOT valid JSON.
    with pytest.raises(cli_ops.OpError) as exc:
        cli_ops.coerce(_PARAMS, {"value": "hello"}, tool="edit")
    assert exc.value.code == "BAD_JSON"


def test_coerce_json_cli_falls_back_to_raw_string() -> None:
    # CLI (cli=True): `kb edit --value hello` — a bare string is itself, not BAD_JSON.
    out = cli_ops.coerce(_PARAMS, {"value": "hello"}, tool="edit", cli=True)
    assert out == {"value": "hello"}
    # Real JSON still parses under cli=True (the fallback only fires on parse failure).
    out2 = cli_ops.coerce(_PARAMS, {"value": "42"}, tool="edit", cli=True)
    assert out2 == {"value": 42}
    out3 = cli_ops.coerce(_PARAMS, {"value": '{"k": 1}'}, tool="edit", cli=True)
    assert out3 == {"value": {"k": 1}}


def test_coerce_guards_nested_edit_new_string() -> None:
    # edit's batch mode hides the write payload in edits[].new_string — it must be
    # blob-guarded too, not just the top-level new_body/new_string.
    params = (Param("path", "str"), Param("why", "str"), Param("edits", "json"))
    blob = "data:image/png;base64," + "A" * 40000
    with pytest.raises(ValueError) as exc:
        cli_ops.coerce(
            params,
            {"path": "x.md", "why": "y", "edits": [{"old_string": "a", "new_string": blob}]},
            guarded_fields=("new_body", "new_string"),
            tool="edit",
        )
    assert "BINARY_BLOB_REJECTED" in str(exc.value)
    assert "edits[].new_string" in str(exc.value)
