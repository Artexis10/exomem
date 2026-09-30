from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "infra/contracts/exomem-hosted-activation-ack-v1.schema.json"
FIXTURES = ROOT / "tests/fixtures/hosted-activation-ack-v1"
MAX_SQLITE_INTEGER = 2**63 - 1


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def _validator(definition: str) -> Draft202012Validator:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]})


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _uds_ack_request() -> dict[str, object]:
    proof = _fixture("intermediate-child-proof.json")
    return {
        "protocol": "exomem.hosted-activation-ack/v1",
        "request_id": "9" * 64,
        "operation": "ack",
        "budget_ms": 5000,
        "publication": proof["publication"],
    }


def test_schema_is_draft_2020_12_and_exposes_the_complete_closed_contract() -> None:
    schema = _schema()

    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(schema["$defs"]) >= {
        "activationTuple",
        "publicationSelector",
        "proofRequest",
        "proofResponse",
        "udsCheckRequest",
        "udsAckRequest",
        "udsRequest",
        "udsResponse",
        "httpAckRequest",
        "httpBundleResponse",
        "httpCurrentOrAckBundleResponse",
        "errorResponse",
    }
    for definition in schema["$defs"].values():
        if isinstance(definition, dict) and definition.get("type") == "object":
            assert definition["additionalProperties"] is False


@pytest.mark.parametrize(
    ("fixture_name", "component_kind"),
    [
        ("intermediate-child-proof.json", "hosted-mutation-child/v1"),
        ("final-commit-proof.json", "hosted-mutation-commit/v1"),
    ],
)
def test_proof_fixtures_validate_against_schema_root_and_proof_definition(
    fixture_name: str, component_kind: str
) -> None:
    raw = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    proof_validator = _validator("proofResponse")
    root_validator = Draft202012Validator(_schema())

    proof_payload = json.loads(raw)
    root_payload = json.loads(raw)
    proof_validator.validate(proof_payload)
    root_validator.validate(root_payload)

    assert proof_payload == root_payload
    assert proof_payload["publication_evidence"]["component_kind"] == component_kind


def test_proof_request_is_the_closed_unsigned_echo_contract() -> None:
    request = _fixture("final-commit-proof.json")
    for response_only in ("publication_evidence", "signing_key_id", "mac"):
        request.pop(response_only)

    _validator("proofRequest").validate(request)
    assert not _validator("proofRequest").is_valid({**request, "protocol": "wrong/v1"})


def test_every_object_rejects_unknown_fields() -> None:
    proof = _fixture("final-commit-proof.json")
    cases = [
        ("proofResponse", proof),
        ("publicationSelector", proof["publication"]),
        ("activationTuple", proof["publication"]["successor"]),
        ("publicationEvidence", proof["publication_evidence"]),
        ("udsAckRequest", _uds_ack_request()),
    ]

    for definition, payload in cases:
        forged = copy.deepcopy(payload)
        forged["unexpected"] = "drift"
        assert not _validator(definition).is_valid(forged), definition


@pytest.mark.parametrize("field", ["challenge_id", "cell_id", "signing_key_id"])
def test_proof_identifiers_use_the_provisioner_identity_grammar(field: str) -> None:
    proof = _fixture("final-commit-proof.json")
    proof[field] = "contains a space"

    assert not _validator("proofResponse").is_valid(proof)


def test_patterns_reject_a_trailing_newline_at_the_actual_string_boundary() -> None:
    activation = copy.deepcopy(_fixture("final-commit-proof.json")["publication"]["successor"])

    identifier = {**activation, "activation_store_id": activation["activation_store_id"] + "\n"}
    digest = {
        **activation,
        "activation_state_digest": activation["activation_state_digest"] + "\n",
    }

    assert not _validator("activationTuple").is_valid(identifier)
    assert not _validator("activationTuple").is_valid(digest)
    assert not _validator("custodyRecordBase64Url").is_valid("e30\n")


@pytest.mark.parametrize("field", ["issued_at", "expires_at", "attachment_epoch"])
def test_boolean_values_are_not_integers_on_the_wire(field: str) -> None:
    proof = _fixture("final-commit-proof.json")
    proof[field] = True

    assert not _validator("proofResponse").is_valid(proof)


def test_activation_epochs_are_positive_bounded_sqlite_integers() -> None:
    activation = copy.deepcopy(_fixture("final-commit-proof.json")["publication"]["successor"])
    validator = _validator("activationTuple")

    activation["activation_epoch"] = -1
    assert not validator.is_valid(activation)
    activation["activation_epoch"] = True
    assert not validator.is_valid(activation)
    activation["activation_epoch"] = MAX_SQLITE_INTEGER
    validator.validate(activation)
    activation["activation_epoch"] = MAX_SQLITE_INTEGER + 1
    assert not validator.is_valid(activation)


def test_epoch_adjacency_is_a_runtime_rule_not_a_shape_rule() -> None:
    publication = copy.deepcopy(_fixture("final-commit-proof.json")["publication"])
    publication["predecessor"]["activation_epoch"] = 1
    publication["successor"]["activation_epoch"] = 3

    _validator("publicationSelector").validate(publication)


def test_socket_request_variants_enforce_operation_specific_publication() -> None:
    ack = _uds_ack_request()
    check = {**ack, "operation": "check", "publication": None}

    _validator("udsAckRequest").validate(ack)
    _validator("udsCheckRequest").validate(check)
    _validator("udsRequest").validate(ack)
    _validator("udsRequest").validate(check)
    assert not _validator("udsAckRequest").is_valid({**ack, "publication": None})
    assert not _validator("udsCheckRequest").is_valid({**check, "publication": ack["publication"]})


@pytest.mark.parametrize("budget", [0, 5001, True, 1.5])
def test_request_budget_is_an_integer_from_one_to_five_seconds(budget: object) -> None:
    request = _uds_ack_request()
    request["budget_ms"] = budget

    assert not _validator("udsAckRequest").is_valid(request)


def test_request_ids_are_exactly_32_random_bytes_in_lowercase_hex() -> None:
    request = _uds_ack_request()
    validator = _validator("udsAckRequest")

    for invalid in ("a" * 63, "a" * 65, "A" * 64, "g" * 64):
        assert not validator.is_valid({**request, "request_id": invalid})


def test_error_request_id_is_null_only_for_malformed_request_identifiers() -> None:
    malformed = {
        "protocol": "exomem.hosted-activation-ack/v1",
        "request_id": None,
        "code": "MALFORMED_REQUEST",
        "retry_after_ms": 0,
    }
    validator = _validator("errorResponse")

    validator.validate(malformed)
    assert not validator.is_valid({**malformed, "code": "AUTHENTICATION_FAILED"})
    validator.validate({**malformed, "request_id": "a" * 64, "code": "AUTHENTICATION_FAILED"})


def test_retry_after_is_always_a_bounded_integer() -> None:
    error = {
        "protocol": "exomem.hosted-activation-ack/v1",
        "request_id": "a" * 64,
        "code": "ACK_UNAVAILABLE",
        "retry_after_ms": 0,
    }
    validator = _validator("errorResponse")

    validator.validate(error)
    for invalid in (None, True, -1, 5001):
        assert not validator.is_valid({**error, "retry_after_ms": invalid})


def test_error_codes_are_stable_and_exhaustive() -> None:
    codes = _schema()["$defs"]["errorCode"]["enum"]

    assert codes == [
        "MALFORMED_REQUEST",
        "AUTHENTICATION_FAILED",
        "ACTIVATION_CONFLICT",
        "ACK_CAPACITY_EXCEEDED",
        "ACK_UNAVAILABLE",
        "ACK_DEADLINE_EXCEEDED",
        "ACK_PENDING",
        "KEYRING_NOT_AVAILABLE",
        "ACKNOWLEDGED",
        "ACK_READY",
    ]


def test_schema_documents_checks_that_json_schema_cannot_enforce() -> None:
    comment = _schema()["$comment"]

    for boundary in ("duplicate", "UTF-8", "byte", "authentication", "runtime"):
        assert boundary in comment
