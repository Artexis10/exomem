from __future__ import annotations

import pytest


def test_nested_image_labels_map_code_to_original_unicode_crlf_offsets() -> None:
    """Nested image-label parsing cannot reset offsets onto an earlier equal code span."""
    from exomem.markdown_regions import mask_code, scan_markdown

    text = "# Ω ![`same` ![`same`](inner)](outer)\r\n> text `same` <!-- actual -->\r\n"
    regions = scan_markdown(text)
    positions = [index for index in range(len(text)) if text.startswith("`same`", index)]
    assert regions.code_spans == tuple((index, index + len("`same`")) for index in positions)
    start = text.index("<!-- actual -->")
    assert regions.comment_spans == ((start, start + len("<!-- actual -->")),)
    masked = mask_code(text, regions)
    assert len(masked) == len(text)
    assert masked == text.replace("`same`", " " * len("`same`"))


def test_multiline_code_mask_preserves_both_original_line_ending_characters() -> None:
    """Masking normalized inline code must retain CR and LF for exact consumer offsets."""
    from exomem.markdown_regions import mask_code, scan_markdown

    text = "> ``Ω\r\n> same`` [[Actual]]\r\n"
    regions = scan_markdown(text)
    start, end = text.index("``"), text.index("`` [[") + 2
    assert regions.code_spans == ((start, end),)
    masked = mask_code(text, regions)
    assert (
        masked
        == text[:start]
        + "".join(char if char in "\r\n" else " " for char in text[start:end])
        + text[end:]
    )


@pytest.mark.parametrize("owner", ["", "<script>", "<title>"])
def test_malformed_inline_checkpoint_is_admitted_only_in_html_data(owner: str) -> None:
    """An upstream-escaped unfinished opener stays removable only outside raw/RCDATA text."""
    from exomem.markdown_regions import scan_markdown

    before = "prose " + owner
    text = before + "<!-- unfinished"
    assert scan_markdown(text).comment_spans == (() if owner else ((len(before), len(text)),))


def test_unfinished_inline_tag_cannot_invent_attribute_ownership() -> None:
    """An unrecognized tag is escaped upstream; its independently consumed comment is real."""
    from exomem.markdown_regions import scan_markdown

    before = 'prose <div title="'
    comment = "<!-- actual -->"
    assert scan_markdown(before + comment).comment_spans == (
        (len(before), len(before) + len(comment)),
    )


def test_rendered_entity_cr_preserves_original_container_comment_position() -> None:
    """An entity-decoded CR before HTML must not shift the original Unicode/CRLF carrier."""
    from exomem.markdown_regions import scan_markdown

    before = '> Ω &#13;\r\n> <span title="same">'
    comment = "<!-- actual -->"
    text = before + comment + "</span>\r\n"
    assert scan_markdown(text).comment_spans == ((len(before), len(before) + len(comment)),)
