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


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_key",
        "unknown_version",
        "duplicate_blocks",
        "duplicate_blocks_with_ticks",
        "unterminated",
        "dangling",
        "duplicate_membership",
        "boolean_offset",
        "nonlocal_unit",
        "unknown_scope",
        "nonfinite",
    ],
)
def test_invalid_reserved_payloads_keep_their_spans_and_refuse_new_authoring(case: str) -> None:
    """Malformed metadata must remain removable and must never become assessed support."""
    payload = _payload()
    if case == "dangling":
        payload["bindings"][0]["inputs"] = ["missing"]
    elif case == "duplicate_membership":
        payload["assessments"][0]["inputs"] = ["A", "A"]
    elif case == "boolean_offset":
        payload["inputs"]["A"]["span"] = {"start_offset": True, "end_offset": 5}
    elif case == "nonlocal_unit":
        payload["bindings"][0]["scope"] = {
            "kind": "unit",
            "unit_ref": _REF + "#claim",
            "fingerprint": _HASH,
        }
    elif case == "unknown_scope":
        payload["bindings"][0]["scope"]["kind"] = "page"
    elif case == "nonfinite":
        payload["assessments"][0]["reason"] = float("nan")
    elif case == "duplicate_blocks_with_ticks":
        payload["assessments"][0]["by"] = "Agent ` attribution"
    block = _comment(payload)
    if case == "duplicate_key":
        block = block.replace('"inputs": {', '"inputs": {}, "inputs": {', 1)
    elif case == "unknown_version":
        block = block.replace("exomem-origin:v1", "exomem-origin:v2")
    elif case == "duplicate_blocks":
        block += "\n" + block
    elif case == "duplicate_blocks_with_ticks":
        block += " " + block
    elif case == "unterminated":
        block = block.removesuffix(" -->")
    body = "Before.\n" + block + "\nAfter."
    result = provenance.parse_origin(body, managed=True)
    assert result.status == "unassessed" and result.payload is None
    assert result.spans
    assert result.spans[0][0] == len("Before.\n")
    assert len(result.spans) == (2 if case.startswith("duplicate_blocks") else 1)
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


def test_copies_under_different_labels_and_selected_scopes_count_once() -> None:
    """Input labels and destination scopes cannot mint independent original identities."""
    payload = _payload()
    payload["bindings"].append(
        {"inputs": ["A"], "scope": {"kind": "unit", "unit_ref": "#copy", "fingerprint": _HASH}}
    )
    selected = [label for binding in payload["bindings"] for label in binding["inputs"]]
    summary = provenance.summarize_origins(
        payload["assessments"],
        input_roots={"A": "original", "B": "original"},
        contributing_inputs=selected,
    )
    assert summary == {"status": "assessed", "assessed_lower_bound": 1, "basis": "agent_assessment"}


def test_pair_assessments_cannot_be_composed_into_an_assessed_triple() -> None:
    """Even all three pairs support at most a witnessed pair, scoped to contributing roots."""
    assessments = [_assessment("A", "B"), _assessment("B", "C"), _assessment("A", "C")]
    roots = {label: "original-" + label for label in ("A", "B", "C")}
    summary = provenance.summarize_origins(
        assessments, input_roots=roots, contributing_inputs=["A", "B", "C"]
    )
    assert summary["assessed_lower_bound"] == 2
    assert (
        provenance.summarize_origins(assessments, input_roots=roots, contributing_inputs=["A"])[
            "assessed_lower_bound"
        ]
        == 1
    )


def test_unassessed_or_incomplete_inputs_are_not_proven_absence() -> None:
    """Missing bindings and unsupported assessments abstain; only no selected inputs gives zero."""
    roots = {"A": "original-A"}
    for assessments, selected in (
        ([], ["A"]),
        ([_assessment("A", "B")], ["A"]),
        ([_assessment("A")], ["missing"]),
    ):
        summary = provenance.summarize_origins(
            assessments, input_roots=roots, contributing_inputs=selected
        )
        assert summary == {"status": "unassessed", "assessed_lower_bound": None, "basis": None}
    assert provenance.summarize_origins([], input_roots=roots, contributing_inputs=[]) == {
        "status": "absent",
        "assessed_lower_bound": 0,
        "basis": None,
    }


@pytest.mark.parametrize("container", ["list", "quote", "indent"])
def test_container_code_does_not_activate_origin_metadata(container: str) -> None:
    """Examples owned by a list, quote or indentation cannot supply assessed support."""
    block = provenance.encode_origin(_payload())
    if container == "list":
        body = "- ```\n  " + block + "\n  ```"
    elif container == "quote":
        body = "> ```\n> " + block + "\n> ```"
    else:
        body = "    " + block
    assert provenance.parse_origin(body, managed=True).status == "absent"


@pytest.mark.parametrize("namespace", ["ordinary", "exomem-origin:v2"])
def test_comment_data_cannot_open_a_fence_over_later_metadata(namespace: str) -> None:
    """A fence inside an earlier comment cannot hide the actual later carrier."""
    before = "<!-- " + namespace + "\n```\n-->\n"
    block = provenance.encode_origin(_payload())
    parsed = provenance.parse_origin(before + block, managed=True)
    assert parsed.spans[-1] == (len(before), len(before) + len(block))
    if namespace == "ordinary":
        assert parsed.status == "valid" and len(parsed.spans) == 1
    else:
        assert parsed.status == "unassessed" and parsed.spans[0] == (0, len(before) - 1)


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


@pytest.mark.parametrize(
    "template",
    [
        '<div title="{}">\n</div>',
        '<div title="{}',
        "[label](destination '{}')",
        "![label](destination '{}')",
        "<script>{}</script>",
        "<style>{}</style>",
        "<textarea>{}</textarea>",
        "<title>\n{}\n</title>",
        "<svg>\n<![CDATA[{}]]>\n</svg>",
        "<![CDATA[{}]]>",
    ],
)
def test_literal_html_and_link_values_cannot_supply_origin_metadata(template: str) -> None:
    """Literal attribute, link, raw-text and CDATA values cannot mint a binding."""
    block = provenance.encode_origin(_payload())
    result = provenance.parse_origin(template.format(block), managed=True)
    assert result.status == "absent" and result.spans == ()


@pytest.mark.parametrize("prefix", ["[label]", "![label]"])
def test_reserved_delimiters_in_link_destinations_are_not_carriers(prefix: str) -> None:
    """A valid no-whitespace destination cannot create even a malformed reserved carrier."""
    body = prefix + "(target<!--exomem-origin:v2{}-->)"
    parsed = provenance.parse_origin(body, managed=True)
    assert parsed.status == "absent" and parsed.spans == ()


@pytest.mark.parametrize("tag", ["div", "pre"])
def test_real_html_element_comments_remain_origin_carriers(tag: str) -> None:
    """HTML block ownership must not withhold real comments, including within pre."""
    block = provenance.encode_origin(_payload())
    before = "<" + tag + ">"
    parsed = provenance.parse_origin(before + block + "</" + tag + ">", managed=True)
    assert parsed.status == "valid"
    assert parsed.spans == ((len(before), len(before) + len(block)),)


def test_standalone_comment_tail_distinguishes_attributes_from_actual_comments() -> None:
    """A same-line HTML tail must neither admit its attribute nor lose its real comment."""
    block = provenance.encode_origin(_payload())
    before = "<!-- ordinary --> <div title='" + block + "'> "
    parsed = provenance.parse_origin(before + block + "</div>", managed=True)
    assert parsed.status == "valid"
    assert parsed.spans == ((len(before), len(before) + len(block)),)


def test_standalone_cdata_protects_data_but_not_a_real_same_line_tail_comment() -> None:
    """CDATA data remains literal past its first >; a real comment after ]]> survives."""
    block = provenance.encode_origin(_payload())
    before = "<![CDATA[> " + block + "]]> "
    parsed = provenance.parse_origin(before + block, managed=True)
    assert parsed.status == "valid"
    assert parsed.spans == ((len(before), len(before) + len(block)),)


@pytest.mark.parametrize("closing", ["</script >", "</script bogus>", "</script!>"])
def test_html5_end_tag_handling_controls_later_origin_comments(closing: str) -> None:
    """Only HTML5-recognized end tags leave raw text; malformed tags cannot fake exit."""
    block = provenance.encode_origin(_payload())
    before = "<script>raw" + closing
    parsed = provenance.parse_origin(before + block + "</script>", managed=True)
    if closing == "</script!>":
        assert parsed.status == "absent" and parsed.spans == ()
    else:
        assert parsed.status == "valid"
        assert parsed.spans == ((len(before), len(before) + len(block)),)


def test_html_namespace_bogus_comment_does_not_own_a_later_real_carrier() -> None:
    """CDATA-looking HTML declarations do not admit nested starts or swallow later comments."""
    block = provenance.encode_origin(_payload())
    before = "<div><![CDATA[" + block + "]]></div>\n"
    parsed = provenance.parse_origin(before + block, managed=True)
    assert parsed.status == "valid"
    assert parsed.spans == ((len(before), len(before) + len(block)),)


def test_html_comment_mapping_preserves_nul_unicode_crlf_and_container_offsets() -> None:
    """HTML tokenizer normalization cannot move the removable original carrier."""
    block = provenance.encode_origin(_payload())
    before = '> <div>\r\n> <span title="Ω\0">'
    body = before + block + "</span>\r\n> </div>\r\n"
    parsed = provenance.parse_origin(body, managed=True)
    assert parsed.status == "valid"
    assert parsed.spans == ((len(before), len(before) + len(block)),)


@pytest.mark.parametrize("ending", [" -->\n</div>", ""])
def test_malformed_reserved_html_comments_retain_first_terminator_or_eof(ending: str) -> None:
    """Invalid reserved comments inside HTML stay locatable for whole-carrier withholding."""
    before = "<div>"
    carrier = "<!-- exomem-origin:v2 malformed <!-- nested" + ending
    body = before + carrier
    parsed = provenance.parse_origin(body, managed=True)
    assert parsed.status == "unassessed"
    end = body.index("-->") + 3 if ending else len(body)
    assert parsed.spans == ((len(before), end),)


@pytest.mark.parametrize(
    "before, after",
    [
        ("prose <script>", "</script>"),
        ("prose <title>", "</title>"),
        ("<div><textarea>raw\n\n", "\n</textarea></div>"),
        ("<!-- ordinary --><script>raw\n", "\n</script>"),
    ],
)
def test_rendered_html_owner_survives_inline_and_block_boundaries(before: str, after: str) -> None:
    """One HTML owner must hide metadata until its actual closing tag across Markdown tokens."""
    block = provenance.encode_origin(_payload())
    body = before + block + after
    parsed = provenance.parse_origin(body, managed=True)
    assert parsed.status == "absent" and parsed.spans == ()
    tail = body + "\n\n"
    parsed = provenance.parse_origin(tail + block, managed=True)
    assert parsed.status == "valid"
    assert parsed.spans == ((len(tail), len(tail) + len(block)),)


def test_image_label_html_cannot_supply_origin_metadata() -> None:
    """Image children render as an escaped alt value, never as authoritative HTML."""
    block = provenance.encode_origin(_payload())
    body = "![label " + block + "](image)"
    parsed = provenance.parse_origin(body, managed=True)
    assert parsed.status == "absent" and parsed.spans == ()


def test_standalone_cdata_script_text_cannot_own_a_real_tail_comment() -> None:
    """Escaped standalone CDATA cannot activate a script owner over a later real carrier."""
    block = provenance.encode_origin(_payload())
    before = "<![CDATA[<script> " + block + "]]> "
    parsed = provenance.parse_origin(before + block, managed=True)
    assert parsed.status == "valid"
    assert parsed.spans == ((len(before), len(before) + len(block)),)
