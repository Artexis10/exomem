"""A hosted refusal must tell its recipient what to do about it.

`_error_response` is a redaction boundary: the handler that feeds it converts an
arbitrary exception into a code, and passing that exception's text onward would
leak vault paths and internal detail to a hosted client. It used to discharge
that duty by hardcoding `remediation: None`, which is safe and also useless — a
person whose write was refused for a nameable, fixable reason got
`{"message": "hosted command failed", "remediation": null}`.

These tests pin the narrow seam that fixes it: message and remediation resolve
from a static code-keyed table, never from the exception, so the redaction
guarantee holds while an expected refusal becomes actionable.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from exomem import commands, find, schema, semantic_authoring, server_hosted
from exomem.cli_ops import error_dict
from exomem.init import init_vault


def _error_block(code: str) -> dict:
    response = server_hosted._error_response(
        code,
        config=SimpleNamespace(cell_id="cell-refusal-test"),
        operation="remember",
        started=0.0,
    )
    return json.loads(response.body)["error"]


# --- The refusals a hosted user actually hits ------------------------------


def test_missing_semantic_unit_refusal_carries_remediation() -> None:
    error = _error_block("missing_semantic_unit")

    assert error["code"] == "missing_semantic_unit"
    assert error["remediation"], "a refusal with a defined remedy must not report null"
    assert error["message"] != "hosted command failed"


def test_relation_disposition_refusal_carries_remediation() -> None:
    """The second memory a hosted user saves fails here, so it must be legible."""
    for code in ("RELATION_DISPOSITION_MISSING", "SEMANTIC_CONTRACT_BLOCKED"):
        error = _error_block(code)
        assert error["remediation"], f"{code} must carry remediation"
        assert error["message"] != "hosted command failed", code


def test_unknown_code_stays_generic_and_redacted() -> None:
    """A code with no entry degrades to today's behaviour — the safe direction."""
    error = _error_block("SOME_UNCLASSIFIED_FAILURE")

    assert error["message"] == "hosted command failed"
    assert error["remediation"] is None


def test_existing_hosted_codes_are_unchanged() -> None:
    """The table must not shadow the message rules already asserted elsewhere."""
    assert _error_block("HOSTED_UNAUTHORIZED")["message"] == "private authentication failed"
    assert _error_block("MUTATION_BUSY")["remediation"] is None


# --- The table is derived, not copied -------------------------------------


def test_authoring_remediation_is_derived_from_the_contract() -> None:
    """A hand-copied string would drift from the contract the moment it changed."""
    definition = semantic_authoring.AUTHORING_CONTRACT.findings["missing_semantic_unit"]
    expected = f"{definition['compact_remediation']} {definition['rich_remediation']}"

    assert _error_block("missing_semantic_unit")["remediation"] == expected

    empty_rich = semantic_authoring.AUTHORING_CONTRACT.findings["empty_rich_unit"]
    assert _error_block("empty_rich_unit")["remediation"] == empty_rich["remediation"]


def test_no_exception_text_reaches_the_response() -> None:
    """The seam takes a code and a static table — never a raised exception."""
    with tempfile.TemporaryDirectory() as directory:
        vault = Path(directory)
        init_vault(vault)
        try:
            commands.op_remember(
                vault, title="Hello", content="Hello there", note_type="insight"
            )
        except Exception as exc:  # noqa: BLE001 - the refusal under test
            raised = str(exc)
            derived = error_dict(exc)
        else:  # pragma: no cover - the governed lane must refuse this
            raise AssertionError("plain prose must not satisfy the governed contract")

    error = _error_block(derived["code"])

    # The vault path is in the raised text and must not survive the boundary.
    assert str(vault) not in json.dumps(error)
    assert "Knowledge Base/Notes" not in json.dumps(error)
    assert error["message"] not in raised or error["message"] == "hosted command failed"


# --- The two lanes this change is specified against ------------------------


def test_capture_lane_accepts_consecutive_ordinary_sentences() -> None:
    """What the hosted capture box will depend on once it is routed correctly."""
    sentences = [
        ("Dentist on Thursday at 3pm.", "Dentist Thursday"),
        ("The office moved to Pier 9.", "Office moved"),
        ("Renew the passport before March.", "Passport renewal"),
    ]

    with tempfile.TemporaryDirectory() as directory:
        vault = Path(directory)
        init_vault(vault)
        source_schema = schema.load_source_schema(vault)

        for content, title in sentences:
            result = commands.op_capture_source(
                vault, source_schema, content=content, title=title, source_type="other"
            )
            # `capture_source` nests its result under `source`, unlike
            # `remember` — the companion UI change depends on this shape.
            assert result["source"]["path"], f"capture refused {title!r}"

        find.clear_cache()
        hits = json.dumps(
            commands.op_ask_memory(vault, query="dentist", mode="keyword", limit=5),
            default=str,
        )
        assert "dentist" in hits.lower(), "captured material must be recallable"


def test_governed_lane_still_refuses_an_ungrounded_second_conclusion() -> None:
    """Proof the contract was not weakened to make capture work."""
    body = "## Observations\n\n- [fact] The office moved to Pier 9 #office ^office-pier9\n"

    with tempfile.TemporaryDirectory() as directory:
        vault = Path(directory)
        init_vault(vault)

        commands.op_remember(vault, title="Note 1", content=body, note_type="insight")

        try:
            commands.op_remember(vault, title="Note 2", content=body, note_type="insight")
        except Exception as exc:  # noqa: BLE001 - the refusal under test
            derived = error_dict(exc)
        else:  # pragma: no cover
            raise AssertionError("a second ungrounded conclusion must still be refused")

    assert "RELATION_DISPOSITION_MISSING" in json.dumps(derived)
    assert _error_block(derived["code"])["remediation"]
