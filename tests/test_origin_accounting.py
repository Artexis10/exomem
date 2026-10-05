from __future__ import annotations

import copy
import json

import pytest

from exomem import provenance

_REF = "exomem://memory/12345678-1234-5678-1234-567812345678"
_HASH = "a" * 64


def _assessment(*labels: str) -> dict:
    return {
        "inputs": list(labels),
        "basis": "agent_assessment",
        "by": "Reviewing agent",
        "reason": "Independent retained originals.",
    }


def _payload(scope: dict | None = None) -> dict:
    return {
        "inputs": {label: {"reference": _REF, "version": _HASH} for label in ("A", "B")},
        "assessments": [_assessment("B", "A")],
        "bindings": [
            {
                "inputs": ["B", "A"],
                "scope": scope or {"kind": "field", "field": "summary", "fingerprint": _HASH},
            }
        ],
    }


def _comment(payload: dict) -> str:
    return "<!-- exomem-origin:v1 " + json.dumps(payload) + " -->"


def test_origin_comments_preserve_authored_text_and_cannot_inject_html() -> None:
    """A reason cannot close its carrier; literal examples never become managed metadata."""
    payload = _payload()
    payload["assessments"][0]["reason"] = "Keep `literal` text --> <!-- exomem-origin:v2 secret -->"
    block = provenance.encode_origin(payload)
    assert block.count("<!--") == block.count("-->") == 1
    reordered = copy.deepcopy(payload)
    reordered["inputs"] = dict(reversed(list(reordered["inputs"].items())))
    reordered["assessments"][0]["inputs"].reverse()
    reordered["bindings"][0]["inputs"].reverse()
    assert provenance.encode_origin(reordered) == block
    prefix = (
        "Authored π text.\n```json\n"
        + block
        + "\n```\n`"
        + block
        + "`\n``"
        + block
        + "\n``\n`<!-- literal example`\n"
    )
    body = prefix + block + "\nUnchanged conclusion.\n"
    parsed = provenance.parse_origin(body, managed=True)
    assert parsed.status == "valid"
    assert parsed.payload["assessments"][0]["reason"] == payload["assessments"][0]["reason"]
    assert parsed.spans == ((len(prefix), len(prefix) + len(block)),)
    assert (
        body[: parsed.spans[0][0]] + body[parsed.spans[0][1] :]
        == prefix + "\nUnchanged conclusion.\n"
    )
    raw = provenance.parse_origin(body, managed=False)
    assert raw.status == "absent" and raw.spans == ()
    assert provenance.parse_origin(prefix, managed=True).status == "absent"


@pytest.mark.parametrize(
    "scope",
    [
        {
            "kind": "unit",
            "unit_ref": "#claim",
            "fingerprint": _HASH,
            "span": {"start_offset": 0, "end_offset": 5},
        },
        {
            "kind": "relation",
            "relation": "supports",
            "direction": "outbound",
            "peer": _REF,
            "occurrence_fingerprint": _HASH,
        },
        {"kind": "field", "field": "summary", "fingerprint": _HASH},
        {
            "kind": "record_field",
            "collection_id": "12345678-1234-5678-1234-567812345678",
            "item_key": "retained-item",
            "field": "body",
            "fingerprint": _HASH,
        },
    ],
)
def test_closed_scope_variants_require_persisted_target_fingerprints(scope: dict) -> None:
    """Preparation may fill a new target; persisted scopes cannot silently rebind it."""
    payload = _payload(scope)
    payload["inputs"]["A"].update(
        reference=_REF + "#claim", unit_fingerprint=_HASH, span={"start_offset": 0, "end_offset": 5}
    )
    assert (
        provenance.parse_origin(provenance.encode_origin(payload), managed=True).status == "valid"
    )
    fingerprint = "occurrence_fingerprint" if scope["kind"] == "relation" else "fingerprint"
    del payload["bindings"][0]["scope"][fingerprint]
    with pytest.raises(provenance.OriginError, match="ORIGIN_METADATA_INVALID"):
        provenance.encode_origin(payload)
    authored = provenance.encode_origin(payload, authoring=True)
    assert provenance.parse_origin(authored, managed=True, authoring=True).status == "valid"
    assert provenance.parse_origin(authored, managed=True).status == "unassessed"
    payload["bindings"][0]["scope"]["unexpected"] = "not a scope field"
    with pytest.raises(provenance.OriginError):
        provenance.encode_origin(payload, authoring=True)


@pytest.mark.parametrize("case", ["bad_json", "grammar", "multiple_blocks"])
def test_invalid_reserved_payloads_keep_their_spans_and_refuse_new_authoring(case: str) -> None:
    """Malformed metadata must remain removable and must never become assessed support."""
    payload = _payload()
    if case == "grammar":
        payload["bindings"][0]["inputs"] = ["missing"]
    block = _comment(payload)
    if case == "bad_json":
        block = block.replace('"inputs": {', '"inputs": {,', 1)
    elif case == "multiple_blocks":
        block += "\n" + block
    body = "Before.\n" + block + "\nAfter."
    result = provenance.parse_origin(body, managed=True)
    assert result.status == "unassessed" and result.payload is None
    assert result.spans[0][0] == len("Before.\n")
    assert len(result.spans) == (2 if case == "multiple_blocks" else 1)
    with pytest.raises(provenance.OriginError, match="ORIGIN_METADATA_INVALID"):
        provenance.parse_origin(body, managed=True, strict=True)


@pytest.mark.parametrize("case", ["utf8", "escaping", "assessment_inputs", "bindings"])
def test_origin_bounds_refuse_the_complete_payload(case: str) -> None:
    """Unicode/HTML escaping and collection bounds cannot bypass the metadata ceiling."""
    payload = _payload()
    if case in {"utf8", "escaping"}:
        payload["assessments"][0]["reason"] = ("é" if case == "utf8" else "<") * 9000
    elif case == "assessment_inputs":
        payload["inputs"] = {str(i): {"reference": _REF, "version": _HASH} for i in range(9)}
        payload["assessments"] = [_assessment(*payload["inputs"])]
        payload["bindings"] = []
    else:
        payload["bindings"] *= 33
    with pytest.raises(provenance.OriginError):
        provenance.encode_origin(payload)
    assert provenance.parse_origin(_comment(payload), managed=True).status == "unassessed"


@pytest.mark.parametrize("slashes, status", [(1, "absent"), (2, "valid")])
def test_origin_comment_escape_parity(slashes: int, status: str) -> None:
    """An escaped opener is literal, but an escaped backslash leaves a real comment."""
    block = provenance.encode_origin(_payload())
    parsed = provenance.parse_origin("\\" * slashes + block, managed=True)
    assert parsed.status == status
    assert parsed.spans == (() if slashes == 1 else ((slashes, slashes + len(block)),))


def test_reserved_carrier_offsets_survive_unicode_crlf_and_repeated_container_text() -> None:
    """Withholding removes the original carrier, not a normalized or first equal copy."""
    block = provenance.encode_origin(_payload())
    before = "Ω\r\n> `" + block + "`\r\n> "
    trailing = "\r\n\r\n<!-- exomem-origin:v2 unfinished Ω\r\n"
    body = before + block + trailing
    parsed = provenance.parse_origin(body, managed=True)
    assert parsed.status == "unassessed"
    assert parsed.spans == (
        (len(before), len(before) + len(block)),
        (body.index("<!-- exomem-origin:v2"), len(body)),
    )
