"""URI decoding must preserve handoffs without carrying encoded credentials."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

import pytest
from fastmcp.tools import ToolResult
from mcp.types import TextContent
from test_governance_postfilter import HIGH_ENTROPY

from exomem import adoption_run, capabilities, context_refs, operation_entries
from exomem.governance import egress, scrubber
from exomem.governance.principal import owner_principal, request_scope


@pytest.mark.parametrize("tool_shape", operation_entries.TOOL_SHAPES)
def test_generated_handoff_survives_terminal_and_mcp_text_scans(
    vault: Path, tool_shape: str
) -> None:
    run_id = adoption_run.new_run_id(
        "collection", "2026-10-01T00:00:00Z", "inventory", today=dt.date(2026, 10, 1)
    )
    run = {"run_id": run_id, "run_ref": context_refs.adoption_run_ref(run_id)}
    surface = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="full", tier2_enabled=False,
        product_commands=("adoption_studio",), tool_shape=tool_shape,
    )
    with capabilities.active_surface(surface):
        handoff = adoption_run._handoff(run)
    original = handoff["links"]["claude"]
    with egress.disclosure_boundary(vault, "adoption_studio") as collector:
        cleaned = egress.postfilter("adoption_studio", {"handoff": handoff}, vault)
        original_text = json.dumps(cleaned, indent=2)
        wire = ToolResult(
            content=[TextContent(type="text", text=original_text)],
            structured_content=cleaned,
        )
        returned = egress.postfilter("adoption_studio", wire, vault)
        egress.emit_boundary_receipt(collector)
    assert returned.structured_content == {"handoff": handoff}
    assert json.loads(returned.content[0].text) == {"handoff": handoff}
    assert returned.content[0].text == original_text
    assert returned.structured_content["handoff"]["links"]["claude"] == original
    assert dict(parse_qsl(urlsplit(original).query))["q"] == handoff["prompt_text"]
    assert not list((vault / "Knowledge Base" / "_Governance" / "events").glob("**/*.jsonl"))


@pytest.mark.parametrize("encode", [quote, quote_plus], ids=["percent", "form"])
@pytest.mark.parametrize(
    "credential",
    [sample for spec in scrubber.CREDENTIAL_PATTERNS for sample in spec.samples],
)
def test_declared_credentials_cannot_leave_in_encoded_query_values(credential, encode) -> None:
    uri = "https://example.test/start?q=" + encode(f"before {credential} after")
    assert scrubber.scrub_text(uri) == (scrubber.NOTICE, True)
    assert scrubber.scrub_value({"ref": uri}) == ({"ref": scrubber.NOTICE}, True)


@pytest.mark.parametrize("encode", [quote, quote_plus], ids=["percent", "form"])
@pytest.mark.parametrize(
    "credential",
    [sample for spec in scrubber.CREDENTIAL_PATTERNS for sample in spec.samples],
)
def test_encoded_credentials_cannot_leave_in_mcp_json_text(vault, credential, encode) -> None:
    uri = "https://example.test/start?q=" + encode(f"before {credential} after")
    wire = ToolResult(content=[TextContent(type="text", text=json.dumps({"link": uri}))])
    returned = egress.postfilter("test", wire, vault)
    assert json.loads(returned.content[0].text) == {"link": scrubber.NOTICE}


def test_mcp_json_text_keeps_entropy_checks_for_structural_field_names(vault: Path) -> None:
    wire = ToolResult(
        content=[TextContent(type="text", text=json.dumps({"ref": HIGH_ENTROPY}))]
    )
    returned = egress.postfilter("test", wire, vault)
    assert json.loads(returned.content[0].text) == {"ref": scrubber.NOTICE}


@pytest.mark.parametrize("source_version", [False, True], ids=["field", "source-version"])
def test_long_mcp_json_strings_keep_the_text_channel_entropy_checks(
    vault: Path, source_version: bool
) -> None:
    text = "ordinary context " * 80 + HIGH_ENTROPY
    value = [{"path": text, "hash": "digest"}] if source_version else {"ref": text}
    wire = ToolResult(content=[TextContent(type="text", text=json.dumps(value))])
    returned = egress.postfilter("test", wire, vault)
    assert HIGH_ENTROPY not in returned.content[0].text
    assert scrubber.NOTICE in returned.content[0].text


def test_duplicate_json_keys_cannot_hide_an_earlier_credential(vault: Path) -> None:
    text = '{"value": "' + HIGH_ENTROPY + '", "value": "safe"}'
    wire = ToolResult(content=[TextContent(type="text", text=text)])
    returned = egress.postfilter("test", wire, vault)
    assert HIGH_ENTROPY not in returned.content[0].text
    assert scrubber.NOTICE in returned.content[0].text


def test_unrelated_duplicate_keys_cannot_bypass_uri_decoding(vault: Path) -> None:
    credential = next(
        spec.samples[0] for spec in scrubber.CREDENTIAL_PATTERNS
        if spec.name == "aws_access_key_id"
    )
    uri = "https://example.test/?q=" + quote(f"before {credential} after")
    text = '{"x": 0, "x": 1, "link": ' + json.dumps(uri) + "}"
    wire = ToolResult(content=[TextContent(type="text", text=text)])
    returned = egress.postfilter("test", wire, vault)
    assert returned.content[0].text == scrubber.NOTICE


def test_clean_duplicate_json_keeps_the_original_handoff_bytes(vault: Path) -> None:
    run_id = adoption_run.new_run_id(
        "collection", "2026-10-01T00:00:00Z", "inventory", today=dt.date(2026, 10, 1)
    )
    handoff = adoption_run._handoff(
        {"run_id": run_id, "run_ref": context_refs.adoption_run_ref(run_id)}
    )
    text = '{"x": 0, "x": 1, "handoff": ' + json.dumps(handoff) + "}"
    wire = ToolResult(content=[TextContent(type="text", text=text)])
    returned = egress.postfilter("test", wire, vault)
    assert returned.content[0].text == text


@pytest.mark.parametrize("prefix", ["", "\x00"], ids=["plain", "leading-control"])
def test_uri_scheme_keeps_its_original_entropy_coverage(prefix: str) -> None:
    assert scrubber.scrub_text(prefix + HIGH_ENTROPY[:32] + ":value") == (scrubber.NOTICE, True)


@pytest.mark.parametrize("component", ["authority", "path", "query_key", "fragment"])
def test_decoded_uri_components_retain_explicit_credential_checks(component: str) -> None:
    credential = next(
        spec.samples[0] for spec in scrubber.CREDENTIAL_PATTERNS
        if spec.name == "aws_access_key_id"
    )
    encoded = quote(credential, safe="")
    uri = {
        "authority": f"https://before%20{encoded}%20after@example.test/",
        "path": f"https://example.test/before%20{encoded}%20after",
        "query_key": f"https://example.test/?before%20{encoded}%20after=value",
        "fragment": f"https://example.test/#before%20{encoded}%20after",
    }[component]
    assert scrubber.scrub_text(uri) == (scrubber.NOTICE, True)


@pytest.mark.parametrize("component", ["path", "query"])
@pytest.mark.parametrize("encode_plus", [False, True], ids=["literal-plus", "encoded-plus"])
def test_bare_credentials_keep_literal_and_encoded_plus_coverage(
    component: str, encode_plus: bool
) -> None:
    credential = HIGH_ENTROPY[:24] + "+" + HIGH_ENTROPY[24:]
    encoded = quote(credential, safe="") if encode_plus else credential
    uri = "https://example.test/" + ("?q=" if component == "query" else "") + encoded
    assert scrubber.scrub_text(uri) == (scrubber.NOTICE, True)
    assert scrubber.scrub_value({"ref": uri}) == ({"ref": uri}, False)


def test_persisted_run_metadata_cannot_disclose_an_encoded_credential(vault: Path) -> None:
    credential = scrubber.CREDENTIAL_PATTERNS[0].samples[0]
    run_id = "adr-20261001-01234567"
    store = adoption_run.AdoptionRunStore(vault)
    store.save({"run_id": run_id, "run_ref": f"reference {credential}"})
    handoff = adoption_run._handoff(store.load(run_id))
    with request_scope(owner_principal()):
        with egress.disclosure_boundary(vault, "adoption_studio") as collector:
            cleaned = egress.postfilter("adoption_studio", {"handoff": handoff}, vault)
            egress.emit_boundary_receipt(collector)
    assert credential not in json.dumps(cleaned)
    assert scrubber.NOTICE in cleaned["handoff"]["prompt_text"]
    assert cleaned["handoff"]["links"]["claude"] == scrubber.NOTICE
    records = [
        json.loads(line)
        for path in (vault / "Knowledge Base" / "_Governance" / "events").glob("**/*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event_type"] for record in records] == ["credential_block"]
    assert credential not in json.dumps(records)
