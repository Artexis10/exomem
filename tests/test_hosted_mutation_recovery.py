from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

import exomem.hosted_mutation_recovery as recovery
from exomem.hosted_mutation_recovery import (
    RecoveryDescriptorError,
    bind_child_prepared_result,
    bind_descriptor,
    descriptor_digest,
    encode_descriptor,
    prepare_descriptor,
    validate_descriptor,
    validate_prepared_payloads,
    verify_child_prepared_result_binding,
    verify_descriptor_binding,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "infra/contracts/exomem-prepared-canonical-mutation-v1.schema.json"
VERSION = "exomem.prepared-canonical-mutation/v1"
SECRET = b"s" * 32


def _children() -> list[dict[str, object]]:
    return [
        {"id": "catalog-1", "kind": "catalog", "plan_sha256": "1" * 64, "requires": []},
        {
            "id": "policy-1",
            "kind": "policy",
            "plan_sha256": "2" * 64,
            "requires": ["catalog-1"],
        },
        {
            "id": "sidecar-1",
            "kind": "sidecar",
            "plan_sha256": "3" * 64,
            "requires": ["policy-1"],
        },
    ]


def _recipe() -> dict[str, object]:
    return {
        "kind": "object",
        "members": {
            "path": {"kind": "child-field", "child_id": "catalog-1", "field": "path"},
            "status": {"kind": "literal", "value": "committed"},
            "facts": {
                "kind": "list",
                "items": [
                    {"kind": "child-field", "child_id": "policy-1", "field": "receipt"},
                    {"kind": "literal", "value": {"nested": [None, True, 7]}},
                ],
            },
        },
    }


def _descriptor(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "scoped_idempotency_digest": "a" * 64,
        "command_digest": "b" * 64,
        "attempt_id": "c" * 24,
        "commit_token": "d" * 24,
        "command": "remember",
        "selector_digest": "e" * 64,
        "cell_id": "cell é",
        "logical_vault_id": "vault-1",
        "registry_attachment_id": "attachment/1",
        "attachment_epoch": 7,
        "activation_store_id": "store:1",
        "required_children": _children(),
        "result_recipe": _recipe(),
    }
    values.update(overrides)
    return prepare_descriptor(**values)


def test_prepare_descriptor_returns_the_exact_closed_data_only_contract() -> None:
    descriptor = _descriptor()

    assert list(descriptor) == [
        "version",
        "scoped_idempotency_digest",
        "command_digest",
        "attempt_id",
        "commit_token",
        "command",
        "selector_digest",
        "cell_id",
        "logical_vault_id",
        "registry_attachment_id",
        "attachment_epoch",
        "activation_store_id",
        "required_children",
        "result_recipe",
    ]
    assert descriptor["version"] == VERSION
    assert json.loads(json.dumps(descriptor, allow_nan=False)) == descriptor
    assert validate_descriptor(descriptor) == descriptor


def test_schema_accepts_the_prepared_descriptor() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(_descriptor())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scoped_idempotency_digest", "a" * 64 + "\n"),
        ("attempt_id", "c" * 24 + "\n"),
    ],
)
def test_schema_digest_and_attempt_patterns_reject_a_final_newline(
    field: str, value: str
) -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    descriptor = _descriptor()
    descriptor[field] = value

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(descriptor)


def test_canonical_encoding_and_digest_are_stable() -> None:
    descriptor = _descriptor()
    reordered = dict(reversed(list(descriptor.items())))

    assert encode_descriptor(reordered) == json.dumps(
        descriptor,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert descriptor_digest(reordered) == descriptor_digest(descriptor)
    assert len(descriptor_digest(descriptor)) == 64


def test_private_json_preserves_finite_float_facts_in_canonical_hmac_data() -> None:
    descriptor = _descriptor(
        result_recipe={"kind": "literal", "value": {"frame_ts": 0.125}}
    )
    result = {"frame_ts": 0.125}

    assert b'"frame_ts":0.125' in encode_descriptor(descriptor)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(descriptor)
    binding = bind_child_prepared_result(
        descriptor,
        child_id="catalog-1",
        result=result,
        attempt_secret=SECRET,
    )
    assert verify_child_prepared_result_binding(
        descriptor,
        child_id="catalog-1",
        result=result,
        binding=binding,
        attempt_secret=SECRET,
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_private_json_rejects_non_finite_float_facts(value: float) -> None:
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(result_recipe={"kind": "literal", "value": {"frame_ts": value}})

    with pytest.raises(RecoveryDescriptorError):
        bind_child_prepared_result(
            _descriptor(),
            child_id="catalog-1",
            result={"frame_ts": value},
            attempt_secret=SECRET,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scoped_idempotency_digest", "A" * 64),
        ("command_digest", "f" * 63),
        ("selector_digest", "g" * 64),
        ("attempt_id", "0" * 23),
        ("commit_token", "z" * 24),
        ("attachment_epoch", True),
        ("attachment_epoch", 0),
        ("cell_id", " leading"),
        ("logical_vault_id", "control\ncharacter"),
        ("activation_store_id", "x" * 513),
    ],
)
def test_scalar_grammars_fail_closed(field: str, value: object) -> None:
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(**{field: value})


def test_descriptor_rejects_unknown_fields_and_non_data_values() -> None:
    descriptor = _descriptor()

    with pytest.raises(RecoveryDescriptorError):
        validate_descriptor({**descriptor, "authorization_bearer": "secret"})
    with pytest.raises(RecoveryDescriptorError):
        validate_descriptor({**descriptor, "renderer": "module:function"})
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(result_recipe={"kind": "literal", "value": lambda: None})


def test_child_manifest_rejects_duplicate_children_and_duplicate_prerequisites() -> None:
    children = _children()
    children.append(copy.deepcopy(children[0]))
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(required_children=children)

    children = _children()
    children[1]["requires"] = ["catalog-1", "catalog-1"]
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(required_children=children)


@pytest.mark.parametrize(
    "children",
    [
        [
            {"id": "first", "kind": "catalog", "plan_sha256": "1" * 64, "requires": []},
            {
                "id": "second",
                "kind": "policy",
                "plan_sha256": "2" * 64,
                "requires": ["missing"],
            },
        ],
        [
            {
                "id": "first",
                "kind": "catalog",
                "plan_sha256": "1" * 64,
                "requires": ["second"],
            },
            {"id": "second", "kind": "policy", "plan_sha256": "2" * 64, "requires": []},
        ],
        [
            {
                "id": "first",
                "kind": "catalog",
                "plan_sha256": "1" * 64,
                "requires": ["second"],
            },
            {
                "id": "second",
                "kind": "policy",
                "plan_sha256": "2" * 64,
                "requires": ["first"],
            },
        ],
    ],
)
def test_child_manifest_rejects_unknown_future_and_cyclic_prerequisites(
    children: list[dict[str, object]],
) -> None:
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(required_children=children, result_recipe={"kind": "literal", "value": None})


def test_child_manifest_rejects_unknown_kinds_and_more_than_256_children() -> None:
    children = _children()
    children[0]["kind"] = "callable"
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(required_children=children)

    too_many = [
        {"id": f"child-{index}", "kind": "catalog", "plan_sha256": "1" * 64, "requires": []}
        for index in range(257)
    ]
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(required_children=too_many, result_recipe={"kind": "literal", "value": None})


def test_recipe_rejects_unknown_children_empty_fields_and_unknown_node_shapes() -> None:
    invalid = [
        {"kind": "child-field", "child_id": "missing", "field": "path"},
        {"kind": "child-field", "child_id": "catalog-1", "field": ""},
        {"kind": "call", "import_path": "package.module:function"},
        {"kind": "literal", "value": None, "extra": True},
    ]

    for recipe in invalid:
        with pytest.raises(RecoveryDescriptorError):
            _descriptor(result_recipe=recipe)


def test_literal_json_cannot_hide_recipe_depth_or_node_count() -> None:
    nested: object = None
    for _ in range(33):
        nested = [nested]
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(result_recipe={"kind": "literal", "value": nested})

    with pytest.raises(RecoveryDescriptorError):
        _descriptor(result_recipe={"kind": "literal", "value": [None] * 16384})


def test_descriptor_rejects_more_than_four_mibibytes_of_canonical_json() -> None:
    with pytest.raises(RecoveryDescriptorError):
        _descriptor(result_recipe={"kind": "literal", "value": "x" * (4 * 1024 * 1024)})


def test_descriptor_and_prepared_child_metadata_share_the_four_mibibyte_bound() -> None:
    descriptor = _descriptor(
        result_recipe={"kind": "literal", "value": "x" * (2 * 1024 * 1024)}
    )

    with pytest.raises(RecoveryDescriptorError):
        validate_prepared_payloads(
            descriptor,
            {
                "catalog-1": {"value": "c" * 800_000},
                "policy-1": {"value": "p" * 800_000},
                "sidecar-1": {"value": "s" * 800_000},
            },
        )


def test_full_prepared_payload_validation_requires_the_exact_child_manifest() -> None:
    descriptor = _descriptor()

    with pytest.raises(RecoveryDescriptorError):
        validate_prepared_payloads(descriptor, {"catalog-1": {"path": "example.md"}})
    with pytest.raises(RecoveryDescriptorError):
        validate_prepared_payloads(
            descriptor,
            {
                "catalog-1": {"path": "example.md"},
                "policy-1": {"receipt": "receipt-1"},
                "sidecar-1": {"status": "ready"},
                "unexpected": {},
            },
        )

    assert validate_prepared_payloads(
        descriptor,
        {
            "catalog-1": {"path": "example.md"},
            "policy-1": {"receipt": "receipt-1"},
            "sidecar-1": {"status": "ready"},
        },
    ) == {
        "catalog-1": {"path": "example.md"},
        "policy-1": {"receipt": "receipt-1"},
        "sidecar-1": {"status": "ready"},
    }


def test_descriptor_hmac_is_domain_bound_and_constant_time_verified() -> None:
    descriptor = _descriptor()
    binding = bind_descriptor(descriptor, attempt_secret=SECRET)

    assert verify_descriptor_binding(descriptor, binding, attempt_secret=SECRET)
    tampered = copy.deepcopy(descriptor)
    tampered["command"] = "edit_memory"
    assert not verify_descriptor_binding(tampered, binding, attempt_secret=SECRET)
    changed_attempt = copy.deepcopy(descriptor)
    changed_attempt["attempt_id"] = "9" * 24
    assert not verify_descriptor_binding(changed_attempt, binding, attempt_secret=SECRET)
    assert not verify_descriptor_binding(descriptor, "0" * 64, attempt_secret=SECRET)
    assert not verify_descriptor_binding(descriptor, "é" * 64, attempt_secret=SECRET)
    assert not verify_descriptor_binding(descriptor, None, attempt_secret=SECRET)


def test_hmac_helpers_require_exactly_32_secret_bytes() -> None:
    descriptor = _descriptor()

    for secret in (b"s" * 31, b"s" * 33, "s" * 32):
        with pytest.raises(RecoveryDescriptorError):
            bind_descriptor(descriptor, attempt_secret=secret)  # type: ignore[arg-type]


def test_child_prepared_result_binding_guards_attempt_child_plan_and_object_payload() -> None:
    descriptor = _descriptor()
    result = {"path": "Knowledge Base/Notes/example.md", "receipt": {"hash": "f" * 64}}
    binding = bind_child_prepared_result(
        descriptor, child_id="catalog-1", result=result, attempt_secret=SECRET
    )

    assert verify_child_prepared_result_binding(
        descriptor,
        child_id="catalog-1",
        result=result,
        binding=binding,
        attempt_secret=SECRET,
    )
    tampered = copy.deepcopy(result)
    tampered["path"] = "different.md"
    assert not verify_child_prepared_result_binding(
        descriptor,
        child_id="catalog-1",
        result=tampered,
        binding=binding,
        attempt_secret=SECRET,
    )
    changed_attempt = copy.deepcopy(descriptor)
    changed_attempt["attempt_id"] = "9" * 24
    assert not verify_child_prepared_result_binding(
        changed_attempt,
        child_id="catalog-1",
        result=result,
        binding=binding,
        attempt_secret=SECRET,
    )
    assert not verify_child_prepared_result_binding(
        descriptor,
        child_id="policy-1",
        result=result,
        binding=binding,
        attempt_secret=SECRET,
    )
    changed_plan = copy.deepcopy(descriptor)
    changed_plan["required_children"][0]["plan_sha256"] = "9" * 64
    assert not verify_child_prepared_result_binding(
        changed_plan,
        child_id="catalog-1",
        result=result,
        binding=binding,
        attempt_secret=SECRET,
    )
    assert not verify_child_prepared_result_binding(
        descriptor,
        child_id="catalog-1",
        result=result,
        binding="é" * 64,
        attempt_secret=SECRET,
    )
    with pytest.raises(RecoveryDescriptorError):
        bind_child_prepared_result(
            descriptor, child_id="missing", result=result, attempt_secret=SECRET
        )
    with pytest.raises(RecoveryDescriptorError):
        bind_child_prepared_result(
            descriptor,
            child_id="catalog-1",
            result=[result],  # type: ignore[arg-type]
            attempt_secret=SECRET,
        )


def test_authenticated_preparation_cannot_be_rendered_as_a_committed_terminal() -> None:
    descriptor = _descriptor()
    binding = bind_child_prepared_result(
        descriptor,
        child_id="catalog-1",
        result={"path": "example.md"},
        attempt_secret=SECRET,
    )

    assert verify_child_prepared_result_binding(
        descriptor,
        child_id="catalog-1",
        result={"path": "example.md"},
        binding=binding,
        attempt_secret=SECRET,
    )
    assert not hasattr(recovery, "render_result")
    assert not hasattr(recovery, "VerifiedPreparedChildResult")
