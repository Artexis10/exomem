"""Regression coverage for issues #559 and #560.

Both live in `record_formats.render_markdown_item_update`, and both are the
same discriminator #546 already keys on -- whether a compose span swallowed
its own trailing newline -- read off the wrong thing.

#559: for a block-style node `end_mark` lands *after* that node's newline, so
the `delete_fields` branch's forward search for the next newline found the
*following* field's terminator and deleted that field with it. The result is
valid, parseable YAML with a governed key silently gone, which the #546
post-write reparse guard cannot see.

#560: the newline was decided per *document* -- one CRLF anywhere selected
`\r\n` for every line -- so on a mixed-ending page an LF-terminated span was
not recognised as newline-consuming and its line ending was never restored.
After #546 that is a refusal rather than corruption, which leaves the page
permanently unwritable through this path.

The CRLF body-replacement case below is the same class found while fixing
those two: `^---\r?$` matches before the LF, so a CRLF fence keeps its CR
inside the match and prefixing a replaced body with the document newline
wrote `---\r\r\n`. That one committed silently.
"""

from __future__ import annotations

import pytest

from exomem import record_formats, vault
from exomem import structured_collections as collections

LF = chr(10)
CR = chr(13)
CRLF = CR + LF

_HEAD = (
    "type: plan",
    "collection_id: 11111111-1111-4111-8111-111111111111",
    "plan_id: 22222222-2222-4222-8222-222222222222",
    "schema_version: 1",
)


def _document(*, block: str, newline: str = LF) -> str:
    lines = ["---", *_HEAD, *block.split(LF), "status: active", "owner: hugo", "---", "Body.", ""]
    return newline.join(lines)


@pytest.mark.parametrize(
    ("label", "block"),
    [
        ("literal-scalar", "label: |" + LF + "  change 1"),
        ("folded-scalar", "label: >" + LF + "  change 1"),
        ("block-sequence", "label:" + LF + "- a" + LF + "- b"),
        ("block-mapping", "label:" + LF + "  k: v"),
    ],
)
def test_deleting_a_block_valued_mid_document_field_keeps_the_next_field(
    label: str, block: str
) -> None:
    """#559: the deletion range must stop at the span, not at the next newline."""
    source = _document(block=block)

    updated = record_formats.render_markdown_item_update(
        source, {}, semantic_profile="planning", delete_fields=("label",)
    )

    parsed, _body, _fm = vault.parse_frontmatter(updated, strict=True)
    assert "label" not in parsed, f"{label}: the field asked for was not removed"
    assert parsed["status"] == "active", f"{label}: the following field was deleted too"
    assert parsed["owner"] == "hugo"


def test_deleting_a_plain_scalar_mid_document_field_keeps_the_next_field() -> None:
    """The control that pins the discriminator: a plain scalar consumes nothing."""
    source = _document(block="label: change 1")

    updated = record_formats.render_markdown_item_update(
        source, {}, semantic_profile="planning", delete_fields=("label",)
    )

    parsed, _body, _fm = vault.parse_frontmatter(updated, strict=True)
    assert "label" not in parsed
    assert parsed["status"] == "active"
    assert "label:" not in updated, f"the deleted line's remains survived: {updated!r}"


def test_deleting_the_last_block_valued_field_leaves_a_well_formed_fence() -> None:
    source = LF.join(["---", *_HEAD, "label: |", "  change 1", "---", "Body.", ""])

    updated = record_formats.render_markdown_item_update(
        source, {}, semantic_profile="planning", delete_fields=("label",)
    )

    parsed, body, _fm = vault.parse_frontmatter(updated, strict=True)
    assert "label" not in parsed and parsed["type"] == "plan"
    assert body == "Body." + LF


def test_a_mixed_line_ending_page_is_writable_and_keeps_its_own_endings() -> None:
    """#560: one CRLF line must not decide the newline for every other line."""
    source = (
        "---" + LF
        + "type: plan" + CRLF
        + "collection_id: 11111111-1111-4111-8111-111111111111" + LF
        + "label: |" + LF
        + "  change 1" + LF
        + "---" + LF
        + "Body." + LF
    )

    updated = record_formats.render_markdown_item_update(
        source, {"label": "change 2", "tags": ["urgent"]}, semantic_profile="planning"
    )

    parsed, body, _fm = vault.parse_frontmatter(updated, strict=True)
    assert parsed["label"] == "change 2"
    assert parsed["tags"] == ["urgent"]
    assert body == "Body." + LF
    # Untouched lines keep exactly the ending they had.
    assert "type: plan" + CRLF in updated
    assert "collection_id: 11111111-1111-4111-8111-111111111111" + LF in updated


def test_a_crlf_page_keeps_one_carriage_return_when_the_body_is_replaced() -> None:
    source = CRLF.join(["---", *_HEAD, "---", "Old body.", ""])

    updated = record_formats.render_markdown_item_update(
        source, {}, semantic_profile="planning", body="New body." + CRLF
    )

    assert CR + CR not in updated, f"a doubled carriage return was written: {updated!r}"
    parsed, body, _fm = vault.parse_frontmatter(updated, strict=True)
    assert parsed["type"] == "plan"
    assert body == "New body." + CRLF


def test_a_splice_that_drops_a_field_is_refused_rather_than_committed() -> None:
    """#559 ask 3: the guard has to see field loss, not only parse failure.

    Forcing the pre-#559 deletion range reproduces exactly the shape that used
    to commit cleanly -- valid YAML, one governed key gone.
    """
    source = _document(block="label: |" + LF + "  change 1")
    real_span_terminator = record_formats._span_terminator

    def blind_to_block_spans(yaml_text: str, span: tuple[int, int]) -> str:
        return ""

    record_formats._span_terminator = blind_to_block_spans
    try:
        with pytest.raises(collections.CollectionError) as raised:
            record_formats.render_markdown_item_update(
                source, {}, semantic_profile="planning", delete_fields=("label",)
            )
    finally:
        record_formats._span_terminator = real_span_terminator

    assert raised.value.code == "INVALID_RECORD_ITEM"
    assert "field set" in str(raised.value)
