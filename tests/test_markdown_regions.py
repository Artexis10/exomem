from __future__ import annotations


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


def test_an_opener_inside_raw_html_is_still_a_removable_carrier() -> None:
    """HTML context never hides a reserved opener: it must stay removable before disclosure."""
    from exomem.markdown_regions import scan_markdown

    before = "prose <script>"
    text = before + "<!-- unfinished"
    assert scan_markdown(text).comment_spans == ((len(before), len(text)),)
