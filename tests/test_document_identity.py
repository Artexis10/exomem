"""Identity evidence of a captured PDF: what the document shows against the DOI its url claims."""

from __future__ import annotations

from pathlib import Path

import pytest
from _identity_pdf import XMP_HEAD, XMP_TAIL, make_pdf, xmp_with_doi

from exomem import document_identity as identity

CLAIM = "https://doi.org/10.5555/aaa"


# --- the DOI syntax -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see https://doi.org/10.5555/aaa.", ("10.5555/aaa",)),
        ("DOI: 10.5555/AAA-1, and more", ("10.5555/aaa-1",)),
        ("10.5555/(sici)1097-4571(1998)49:8<693", ("10.5555/(sici)1097-4571(1998)49:8",)),
        ("(doi 10.5555/aaa)", ("10.5555/aaa",)),
        ("https://x.example/doi/10.5555%2Faaa", ()),
        ("first 10.5555/aaa then 10.5555/bbb then 10.5555/aaa", ("10.5555/aaa", "10.5555/bbb")),
        ("version 10.12 of the tool, ratio 3/4", ()),
        (f"10.5555/{'a' * 400}", ()),
        ("cited as \u201c10.5555/aaa\u201d in the text", ("10.5555/aaa",)),
        ("DOI 10.5555/aaa\u20101 set with a Unicode hyphen", ("10.5555/aaa-1",)),
        ("DOI 10.5555/j.\u200baaa with a zero-width break", ("10.5555/j.aaa",)),
        ("10.5555/abc\udced\udca0\udc80 with a lone surrogate in the text", ("10.5555/abc",)),
    ],
)
def test_the_doi_syntax_is_read_from_running_text(text: str, expected: tuple[str, ...]) -> None:
    assert identity.dois_in(text) == expected


def test_a_doi_split_after_the_prefix_is_joined() -> None:
    assert identity.dois_in("doi:10.\n5555/aaa") == ("10.5555/aaa",)


def test_a_doi_split_after_the_slash_is_joined() -> None:
    assert identity.dois_in("https://doi.org/10.5555/\naaa") == ("10.5555/aaa",)


def test_a_doi_wrapped_after_a_dot_before_a_digit_is_joined() -> None:
    assert identity.dois_in("https://doi.org/10.5555/glx.2018.\n0048") == ("10.5555/glx.2018.0048",)


def test_a_sentence_ending_dot_does_not_join_the_next_line() -> None:
    assert identity.dois_in("doi:10.5555/abc.\nThe next paragraph") == ("10.5555/abc",)


def test_a_claimed_doi_one_numeric_step_past_the_printed_one_is_a_conflict() -> None:
    # A remembered DOI from the same journal and year must not read as the printed one's rendition.
    result = identity.check(["Harbour lanterns. https://doi.org/10.5555/glx.2018"], [], "https://doi.org/10.5555/glx.2018.0066")

    assert result.state == identity.CONFLICT


def test_a_page_that_wraps_the_doi_still_catches_a_remembered_neighbour() -> None:
    result = identity.check(["Harbour lanterns. https://doi.org/10.5555/glx.2018.\n0048"], [], "https://doi.org/10.5555/glx.2018.0066")

    assert result.state == identity.CONFLICT
    assert result.printed_dois == ("10.5555/glx.2018.0048",)


def test_a_url_with_an_encoded_slash_is_claimed_after_decoding() -> None:
    claimed = identity.check(["Harbour lanterns. 10.5555/aaa"], [], "https://doi.org/10.5555%2Faaa")

    assert claimed.claimed_dois == ("10.5555/aaa",)


# --- the states ------------------------------------------------------------------------------


def test_a_url_without_a_doi_makes_no_claim() -> None:
    result = identity.check(["Harbour lanterns, 10.5555/aaa"], [], "https://example.org/articles/harbour")

    assert result.state == identity.NO_CLAIM
    assert result.opening == "Harbour lanterns, 10.5555/aaa"


def test_no_claim_still_lists_the_dois_the_document_shows() -> None:
    result = identity.check(["Harbour lanterns, 10.5555/aaa"], ["doi:10.5555/zzz"], None)

    assert result.state == identity.NO_CLAIM
    assert result.declared_dois == ("10.5555/zzz",)
    assert result.printed_dois == ("10.5555/aaa",)


def test_no_url_makes_no_claim() -> None:
    assert identity.check(["Harbour lanterns"], [], None).state == identity.NO_CLAIM


def test_metadata_that_declares_the_claimed_doi_is_declared() -> None:
    result = identity.check(["Harbour lanterns"], ["PDF 1.7", "doi:10.5555/AAA"], CLAIM)

    assert result.state == identity.DECLARED
    assert result.declared_dois == ("10.5555/aaa",)


def test_metadata_that_declares_only_another_doi_is_a_conflict_even_when_the_page_prints_the_claim() -> None:
    result = identity.check(["Cites https://doi.org/10.5555/aaa"], ["10.5555/zzz"], CLAIM)

    assert result.state == identity.CONFLICT
    assert result.declared_dois == ("10.5555/zzz",)
    assert result.printed_dois == ("10.5555/aaa",)


def test_pages_that_print_the_claimed_doi_are_printed_never_verified() -> None:
    result = identity.check(["Thesis summary of a cited article https://doi.org/10.5555/aaa"], [], CLAIM)

    assert result.state == identity.PRINTED
    assert "Thesis summary" in result.opening


def test_pages_that_print_only_another_doi_are_a_conflict() -> None:
    result = identity.check(["First page prints https://doi.org/10.5555/aaa"], [], "https://doi.org/10.5555/bbb")

    assert result.state == identity.CONFLICT
    assert result.claimed_dois == ("10.5555/bbb",)
    assert result.printed_dois == ("10.5555/aaa",)


@pytest.mark.parametrize(
    ("url", "printed"),
    [
        pytest.param("https://publisher.example/content/pdf/10.5555/s0001-020-0001-1.pdf", "10.5555/s0001-020-0001-1", id="pdf-extension"),
        pytest.param("https://preprints.example/content/10.5555/2020.01.01.900001v2", "10.5555/2020.01.01.900001", id="version"),
        pytest.param("https://preprints.example/content/10.5555/2020.01.01.900001v1.full.pdf", "10.5555/2020.01.01.900001", id="version-full-pdf"),
        pytest.param("https://journal.example/articles/10.5555/fpsyg.2020.00001/full", "10.5555/fpsyg.2020.00001", id="full-text-path"),
        pytest.param("https://journal.example/article/10.5555/1742-6596/1/1/012001/pdf", "10.5555/1742-6596/1/1/012001", id="pdf-path"),
        pytest.param("https://doi.org/10.5555/aaa/", "10.5555/aaa", id="trailing-slash"),
    ],
)
def test_a_publisher_url_that_continues_past_the_doi_names_that_doi(url: str, printed: str) -> None:
    result = identity.check([f"Harbour lanterns. https://doi.org/{printed}"], [], url)

    assert result.state == identity.PRINTED


def test_a_claimed_doi_that_only_starts_with_the_printed_one_is_a_conflict() -> None:
    result = identity.check(["Harbour lanterns. https://doi.org/10.5555/aaa"], [], "https://doi.org/10.5555/aaab")

    assert result.state == identity.CONFLICT


def test_a_doi_on_the_second_page_feeds_the_opening_but_is_not_printed() -> None:
    result = identity.check(["A short note on harbour lanterns.", "References. 10.5555/zzz"], [], CLAIM)

    assert result.state == identity.NOT_SHOWN
    assert result.printed_dois == ()
    assert "References. 10.5555/zzz" in result.opening


def test_page_text_that_cannot_be_stored_as_utf8_is_unavailable_not_an_error() -> None:
    result = identity.check(["Harbour \udced\udca0\udc80 lanterns. 10.5555/aaa"], [], CLAIM)

    assert result.state == identity.UNAVAILABLE
    assert result.claimed_dois == ("10.5555/aaa",)


def test_a_document_that_shows_no_doi_is_not_shown() -> None:
    result = identity.check(["A study of harbour lanterns."], ["Harbour study"], CLAIM)

    assert result.state == identity.NOT_SHOWN


def test_the_opening_is_the_first_600_characters_with_whitespace_collapsed() -> None:
    result = identity.check(["word  " * 300, "second page"], [], CLAIM)

    assert len(result.opening) == 600
    assert "  " not in result.opening


def test_the_page_record_keeps_no_opening_text_and_the_reply_keeps_it() -> None:
    result = identity.check(["Opening words 10.5555/zzz"], [], CLAIM)

    assert "opening" not in result.frontmatter()
    assert result.frontmatter()["checked_at"].endswith("Z")
    assert result.reply()["opening"] == "Opening words 10.5555/zzz"


def test_a_reply_block_that_does_not_read_as_one_is_refused_not_shown_as_a_state() -> None:
    block = identity.check(["Opening 10.5555/zzz"], [], CLAIM).reply()

    assert identity.reply_block(block) == block
    assert identity.reply_block({**block, "printed_dois": "10.5555/zzz"}) is None
    assert identity.reply_block({**block, "opening": "x" * 601}) is None
    assert identity.reply_block({**block, "state": ""}) is None
    assert identity.reply_block("conflict") is None


# --- reading a real PDF -----------------------------------------------------------------------


@pytest.fixture
def pdfs(tmp_path: Path) -> Path:
    pytest.importorskip("pypdfium2")
    return tmp_path


def test_a_doi_declared_in_xmp_makes_the_pdf_declared(pdfs: Path) -> None:
    path = make_pdf(pdfs / "a.pdf", ["A study of harbour lanterns."], xmp=xmp_with_doi("10.5555/aaa"))

    result = identity.inspect(path, CLAIM)

    assert result.state == identity.DECLARED
    assert result.declared_dois == ("10.5555/aaa",)


def test_a_doi_in_an_xmp_text_element_is_found_without_naming_its_namespace(pdfs: Path) -> None:
    xmp = f'{XMP_HEAD}<rdf:Description><custom:ref xmlns:custom="urn:x">doi:10.5555/aaa</custom:ref></rdf:Description>{XMP_TAIL}'
    path = make_pdf(pdfs / "a.pdf", ["A study of harbour lanterns."], xmp=xmp)

    assert identity.inspect(path, CLAIM).state == identity.DECLARED


def test_a_doi_in_the_document_information_makes_the_pdf_declared(pdfs: Path) -> None:
    path = make_pdf(pdfs / "a.pdf", ["A study of harbour lanterns."], info={"subject": "doi:10.5555/aaa"})

    assert identity.inspect(path, CLAIM).state == identity.DECLARED


def test_the_pdf_title_and_opening_come_from_the_document(pdfs: Path) -> None:
    path = make_pdf(pdfs / "a.pdf", ["A study of harbour lanterns. 10.5555/aaa"], info={"title": "Harbour Lanterns"})

    result = identity.inspect(path, CLAIM)

    assert result.pdf_title == "Harbour Lanterns"
    assert result.opening.startswith("A study of harbour lanterns.")


def test_only_the_first_two_pages_are_read(pdfs: Path) -> None:
    path = make_pdf(
        pdfs / "a.pdf",
        ["A study of harbour lanterns.", "Second page of the study.", "Third page prints 10.5555/aaa"],
    )

    assert identity.inspect(path, CLAIM).state == identity.NOT_SHOWN


def test_an_encrypted_pdf_is_unavailable_and_names_the_reason(pdfs: Path) -> None:
    path = make_pdf(pdfs / "locked.pdf", ["A study of harbour lanterns."], password="secret")

    result = identity.inspect(path, CLAIM)

    assert (result.state, result.reason) == (identity.UNAVAILABLE, identity.ENCRYPTED)
    assert result.claimed_dois == ("10.5555/aaa",)


def test_a_malformed_pdf_is_unavailable_and_raises_nothing(pdfs: Path) -> None:
    path = pdfs / "broken.pdf"
    path.write_bytes(b"%PDF-1.7 this is not a pdf")

    result = identity.inspect(path, CLAIM)

    assert (result.state, result.reason) == (identity.UNAVAILABLE, identity.UNREADABLE)


def test_a_pdf_with_no_text_layer_is_unavailable_for_that_reason(pdfs: Path) -> None:
    path = make_pdf(pdfs / "scan.pdf", ["", ""])

    result = identity.inspect(path, CLAIM)

    assert (result.state, result.reason) == (identity.UNAVAILABLE, identity.NO_TEXT_LAYER)


def test_an_xmp_packet_with_entity_expansion_is_ignored_not_parsed(pdfs: Path) -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "10.5555/zzz"><!ENTITY b "&a;&a;&a;&a;">]>'
        "<x:xmpmeta xmlns:x='adobe:ns:meta/'>&b;</x:xmpmeta>"
    )
    path = make_pdf(pdfs / "a.pdf", ["A study of harbour lanterns. 10.5555/aaa"], xmp=bomb)

    result = identity.inspect(path, CLAIM)

    assert result.state == identity.PRINTED
    assert result.declared_dois == ()
