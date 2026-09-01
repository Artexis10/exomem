"""Unit tests for the shared envelope + arg coercion (cli_ops)."""

from __future__ import annotations

import datetime
import json

import pytest

from exomem import cli_ops
from exomem import vault as vault_module
from exomem.commands import Param


# Real stdlib exceptions used as leaf_contract_code boundary cases (see
# test_leaf_contract_code_is_none_for_unexpected_exceptions): built once at
# module scope so pytest.param can carry live exception instances.
def _fromisoformat_error(value: str) -> ValueError:
    try:
        datetime.date.fromisoformat(value)
    except ValueError as e:
        return e
    raise AssertionError("expected fromisoformat to raise")  # pragma: no cover


def _json_decode_error() -> json.JSONDecodeError:
    try:
        json.loads("{}x")
    except json.JSONDecodeError as e:
        return e
    raise AssertionError("expected json.loads to raise")  # pragma: no cover


def _int_base10_error() -> ValueError:
    try:
        int("x", 10)
    except ValueError as e:
        return e
    raise AssertionError("expected int() to raise")  # pragma: no cover


def _unicode_decode_error() -> UnicodeDecodeError:
    try:
        b"\xff".decode("utf-8")
    except UnicodeDecodeError as e:
        return e
    raise AssertionError("expected bytes.decode to raise")  # pragma: no cover


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
    assert cli_ops.http_status_for("RECORD_RECOVERY_REQUIRED") == 503
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


# ---------------- leaf_contract_code (issue #553: journal classification) ----------------


def test_leaf_contract_code_from_op_error() -> None:
    assert cli_ops.leaf_contract_code(cli_ops.OpError("BAD_BOOL", "nope")) == "BAD_BOOL"


def test_leaf_contract_code_parses_leaf_contract_valueerror() -> None:
    err = ValueError("NOT_FOUND: no such file")
    assert cli_ops.leaf_contract_code(err) == "NOT_FOUND"


def test_leaf_contract_code_from_as_public_dict_exception() -> None:
    # BatchWriteError (vault.py) carries `.code`/`as_public_dict()` like OpError
    # but isn't an OpError subclass — the duck-typed public-dict path must
    # still surface its real code.
    from exomem import vault as vault_module

    error = vault_module.BatchWriteError(
        "BATCH_ROLLBACK_INCOMPLETE",
        vault_module.BatchTargetSummary(1, ("a.md",), 0),
        committed=False,
    )
    assert cli_ops.leaf_contract_code(error) == "BATCH_ROLLBACK_INCOMPLETE"


def test_leaf_contract_code_prefers_semantic_chain_over_leaf_prefix() -> None:
    # ~25 sites in commands.py re-wrap a SemanticWriteError as
    # `raise ValueError(f"{e.code}: {e.reason}") from e`. The re-wrap's own
    # str() carries the *raw* SemanticWriteError.code
    # ("SEMANTIC_CONTRACT_VIOLATION"), but `as_semantic_validation_error()`
    # on the chained `__cause__` projects the *canonical* semantic code
    # ("missing_semantic_unit") that `error_dict` — and therefore the
    # client — actually surfaces. The journal must agree with what the
    # client saw, not with the discarded raw prefix.
    from exomem import semantic_contract, semantic_writes

    finding = semantic_contract.ContractFinding(
        code="missing_semantic_unit",
        severity="error",
        path="Knowledge Base/Notes/x.md",
        span=None,
        detail="page has no semantic unit",
        remediation="add one",
        governed_element_identity=(),
        resolved_rule=("r", "r", "r"),
    )
    cause = semantic_writes.SemanticWriteError(
        "SEMANTIC_CONTRACT_VIOLATION",
        "page has no semantic unit",
        validation_findings=(finding,),
    )
    try:
        raise ValueError(f"{cause.code}: {cause.reason}") from cause
    except ValueError as rewrapped:
        assert str(rewrapped) == "SEMANTIC_CONTRACT_VIOLATION: page has no semantic unit"
        assert cli_ops.leaf_contract_code(rewrapped) == "missing_semantic_unit"
        # Parity with the client-facing envelope: the journal must not
        # diverge from what error_dict actually answers for the same error.
        assert cli_ops.error_dict(rewrapped)["code"] == "missing_semantic_unit"


def test_leaf_contract_code_semantic_chain_outranks_public_dict_and_own_code() -> None:
    # Precedence check in isolation from the real semantic-authoring
    # machinery: a chained cause exposing `as_semantic_validation_error()`
    # must win even over an exception that ALSO carries its own
    # `as_public_dict()`/`.code` (which would otherwise win via the
    # duck-typed or OpError branches).
    class _Cause(Exception):
        def as_semantic_validation_error(self):
            return {"code": "missing_semantic_unit"}

    rewrap = cli_ops.OpError("SEMANTIC_CONTRACT_VIOLATION", "page has no semantic unit")
    rewrap.__cause__ = _Cause("synthetic cause")
    assert cli_ops.leaf_contract_code(rewrap) == "missing_semantic_unit"


def test_leaf_contract_code_empty_string_code_is_treated_as_absent() -> None:
    # Nothing in the codebase produces an empty "code" today, but the
    # contract is pinned explicitly: an empty string is not a real refusal
    # code, so `leaf_contract_code` must not return it in place of `None`.
    # The mutation journal's `leaf_contract_code(error) or
    # type(error).__name__` then still falls back to the class name.
    class _EmptyCode(Exception):
        def as_public_dict(self):
            return {"code": ""}

    error = _EmptyCode("does not matter")
    assert cli_ops.leaf_contract_code(error) is None
    assert (cli_ops.leaf_contract_code(error) or type(error).__name__) == "_EmptyCode"


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("just a message, no code prefix"),
        TypeError("bad type somewhere"),
        RuntimeError("something broke"),
        KeyError("missing"),
        # Boundary cases: real stdlib exceptions whose messages sit close to
        # the "CODE: message" shape (a leading capital, an embedded colon)
        # without actually matching `_CODE_PREFIX` — must still yield None,
        # not a code sniffed out of an unrelated message.
        pytest.param(
            _fromisoformat_error("2026"),
            id="date-fromisoformat",
        ),
        pytest.param(_json_decode_error(), id="json-decode-error"),
        pytest.param(_int_base10_error(), id="int-base10"),
        pytest.param(_unicode_decode_error(), id="unicode-decode-error"),
    ],
)
def test_leaf_contract_code_is_none_for_unexpected_exceptions(exc: BaseException) -> None:
    # Case 3's safety property: an exception that is NOT the leaf-contract
    # "CODE: message" shape must yield None, not a laundered/plausible code —
    # callers fall back to the exception's own class name.
    assert cli_ops.leaf_contract_code(exc) is None


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
