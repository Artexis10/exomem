"""html_text: the text that a mail or note body shows, read with the stdlib parser and no regex.

Each case is one behaviour that a harmless refactor must keep: what a person reads in a mail body.
"""

from __future__ import annotations

import time

from exomem import html_text


def text(markup: str, **kwargs) -> str:
    return html_text.to_text(markup, **kwargs).text


def test_a_block_element_ends_a_line_and_a_paragraph_stands_apart() -> None:
    assert text("<div>one</div><div>two</div>") == "one\ntwo"
    assert text("<p>one</p><p>two</p>") == "one\n\ntwo"
    assert text("one<br>two") == "one\ntwo"


def test_a_list_marks_each_item_and_a_nested_list_is_indented() -> None:
    assert text("<ul><li>one</li><li>two<ul><li>inner</li></ul></li></ul>") == "- one\n- two\n  - inner"
    assert text("<ol><li>first</li><li>second</li></ol>") == "1. first\n2. second"


def test_a_link_keeps_its_text_and_gains_its_address_unless_the_text_is_the_address() -> None:
    assert text('<a href="https://example.invalid/tides">tide tables</a>') == "tide tables (https://example.invalid/tides)"
    assert text('<a href="https://example.invalid">https://example.invalid</a>') == "https://example.invalid"
    assert text('<a href="#top">back</a>') == "back"


def test_script_and_style_are_dropped_with_their_content() -> None:
    assert text("<style>p { color: red }</style><p>shown</p><script>alert('x')</script>") == "shown"


def test_entities_are_decoded_and_whitespace_collapses() -> None:
    assert text("<p>fish &amp; chips&nbsp;&#8211;   cod\n\n and\tplaice</p>") == "fish & chips \u2013 cod and plaice"  # a no-break space is whitespace too


def test_a_table_row_is_a_line_with_its_cells_tab_separated() -> None:
    assert text("<table><tr><td>low</td><td>06:10</td></tr><tr><td>high</td><td>12:30</td></tr></table>") == "low\t06:10\nhigh\t12:30"


def test_preformatted_text_keeps_its_line_breaks() -> None:
    assert text("<pre>row one\n  row two</pre>") == "row one\n  row two"


def test_malformed_markup_gives_the_text_that_can_be_read() -> None:
    assert text("<div><b>open <i>never closed</div><p>next") == "open never closed\nnext"
    assert text("a < b and c > d &unknown; <p") == "a < b and c > d &unknown;"


def test_an_inline_tag_of_another_dialect_is_replaced_by_the_text_the_caller_gives_it() -> None:
    assert text('<div><en-todo checked="true"/>pack</div>', inline={"en-todo": lambda attrs: "[x] " if attrs.get("checked") == "true" else "[ ] "}) == "[x] pack"


def test_markup_over_four_mib_is_read_from_its_start_and_reported_cut() -> None:
    row = "<p>tide row</p>\n"
    markup = row * (html_text.MAX_INPUT_CHARS // len(row) + 50)

    converted = html_text.to_text(markup)

    assert converted.cut and converted.text.startswith("tide row\n\ntide row")
    assert not html_text.to_text(row * 10).cut


def test_a_tag_cut_in_two_by_the_limit_does_not_show_as_text() -> None:
    markup = "<p>" + "x" * (html_text.MAX_INPUT_CHARS - 6) + "</p><a href='https://example.invalid/never'>after"

    converted = html_text.to_text(markup)

    assert converted.cut and "href" not in converted.text and "<" not in converted.text


def test_deeply_nested_links_convert_in_linear_time() -> None:
    """Catches a link close that re-reads the output since its link opened: 80k nested links took seconds."""
    count = 80_000
    started = time.perf_counter()

    converted = html_text.to_text("<a>x" * count + "</a>" * count)

    assert time.perf_counter() - started < 2
    assert converted.text.startswith("xxxx")
