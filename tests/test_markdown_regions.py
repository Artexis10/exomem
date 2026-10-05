from __future__ import annotations


def test_nested_image_labels_map_code_to_original_unicode_crlf_offsets() -> None:
    """Nested image-label parsing cannot reset offsets onto an earlier equal code span."""
    from exomem.markdown_regions import scan_markdown

    text = "# Ω ![`same` ![`same`](inner)](outer)\r\n> text `same` <!-- actual -->\r\n"
    regions = scan_markdown(text)
    positions = [index for index in range(len(text)) if text.startswith("`same`", index)]
    assert regions.code_spans == tuple((index, index + len("`same`")) for index in positions)
    start = text.index("<!-- actual -->")
    assert regions.comment_spans == ((start, start + len("<!-- actual -->")),)


def test_multiline_inline_code_spans_both_original_line_ending_characters() -> None:
    """Inline code normalized across CRLF must still map to its exact original span."""
    from exomem.markdown_regions import code_spans

    text = "> ``Ω\r\n> same`` [[Actual]]\r\n"
    assert code_spans(text) == ((text.index("``"), text.index("`` [[") + 2),)


def test_an_opener_inside_raw_html_is_still_a_removable_carrier() -> None:
    """HTML context never hides a reserved opener: it must stay removable before disclosure."""
    from exomem import provenance

    before = "prose <script>"
    text = before + "<!-- exomem-origin:v1 unfinished"
    parsed = provenance.parse_origin(text, managed=True)
    assert parsed.status == "unassessed"
    assert parsed.spans == ((len(before), len(text)),)
