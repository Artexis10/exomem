"""Low-confidence OCR words: split at extraction, kept in the sidecar, left out of search."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from exomem import (
    extract,
    find_types,
    media_jobs,
    media_worker,
    ocr_models,
    preserve,
    prior_extraction,
)
from exomem.find import find
from exomem.ocr_facts import OcrFacts
from exomem.ocr_models import OcrReading, OcrWord

# A Tesseract that writes the .txt and the 12-column .tsv real Tesseract writes (the header
# row, then one row per word), so the whole path from the subprocess to `find` is ours.
_TESSERACT = '''
import pathlib, sys
args = sys.argv[1:]
if args == ["--list-langs"]:
    print("List of available languages (2):\\neng\\nosd")
    sys.exit(0)
if "--psm" in args and args[args.index("--psm") + 1] == "0":
    sys.exit(1)
base = pathlib.Path(args[1])
tab, newline = chr(9), chr(10)
base.with_suffix(".txt").write_text("harbour lantern  zyxqv was repaired" + newline + "blorft" + newline)
header = "level page_num block_num par_num line_num word_num left top width height conf text".split()
words = (("harbour", "96.1"), ("lantern", "92.4"), ("zyxqv", "23.0"), ("was", "88.0"),
         ("repaired", "90.2"), ("blorft", "11.5"))
rows = [header] + [
    ["5", "1", "1", "1", "1", str(i + 1), "0", "0", "10", "10", conf, text]
    for i, (text, conf) in enumerate(words)
]
base.with_suffix(".tsv").write_text(newline.join(tab.join(row) for row in rows) + newline)
'''


def _fake_tesseract(tmp_path: Path) -> str:
    script = tmp_path / "tesseract"
    script.write_text(f"#!{sys.executable}\n" + _TESSERACT)
    script.chmod(0o755)
    return str(script)


def _reading(*words: tuple[str, float], text: str) -> OcrReading:
    return OcrReading(text, tuple(OcrWord(confidence, word) for word, confidence in words))


def _frontmatter_of(text: str) -> dict[str, object]:
    return yaml.safe_load(text.removeprefix("---\n").split("\n---\n", 1)[0])


@pytest.mark.skipif(sys.platform == "win32", reason="the fake Tesseract is a POSIX script")
def test_a_word_tesseract_doubts_is_kept_in_the_sidecar_but_never_found(
    vault, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Image = pytest.importorskip("PIL.Image")
    monkeypatch.setenv("EXOMEM_TESSERACT_CMD", _fake_tesseract(tmp_path))
    monkeypatch.delenv(ocr_models.WORD_FLOOR_ENV, raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    picture = tmp_path / "label.png"
    Image.new("RGB", (60, 20), "white").save(picture)
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="image", filename="label.png", data=picture.read_bytes()
    )
    sidecar = vault / stub.sidecar_path

    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(binary_path=vault / stub.path, sidecar_path=sidecar, media_type="image")
    )

    page = sidecar.read_text(encoding="utf-8")
    body = page.partition(preserve.LOW_CONFIDENCE_OCR_HEADING)
    assert "harbour lantern  was repaired" in body[0]
    assert "zyxqv" in body[2] and "blorft" in body[2]
    assert 0 < _frontmatter_of(page)["ocr_confidence"] < 100
    for word in ("lantern", "repaired"):
        assert [hit.path for hit in find(vault, query=word, mode="hybrid")] == [stub.sidecar_path]
    for word in ("zyxqv", "blorft"):
        assert find(vault, query=word, mode="keyword") == []
        assert find(vault, query=word, mode="hybrid") == []


def test_dropping_a_word_keeps_the_spacing_tesseract_measured_for_the_words_that_stay() -> None:
    # `preserve_interword_spaces` leaves CJK words spaced only where the page had a gap.
    reading = _reading(("東京駅", 95.0), ("zz", 20.0), ("の近く", 91.0), text="東京駅 zz  の近く")

    kept, dropped = reading.split(70)

    assert kept == "東京駅 の近く"
    assert dropped == "zz"


def test_a_line_whose_words_all_fall_under_the_floor_leaves_no_blank_line() -> None:
    reading = _reading(("steady", 95.0), ("qz", 10.0), ("wv", 12.0), ("calm", 90.0), text="steady\nqz wv\ncalm")

    kept, dropped = reading.split(70)

    assert kept == "steady\ncalm"
    assert dropped == "qz wv"


def test_a_floor_of_zero_keeps_every_word() -> None:
    reading = _reading(("steady", 95.0), ("qz", 10.0), text="steady qz")

    assert reading.split(0) == ("steady qz", "")


def test_words_that_do_not_match_tesseracts_text_keep_the_whole_text() -> None:
    # Nothing is withheld unless each word is proven to sit in the text it came from.
    reading = _reading(("steady", 95.0), ("different", 10.0), text="steady qz")

    assert reading.split(70) == ("steady qz", "")


def test_a_reading_without_words_has_no_confidence_and_keeps_its_text() -> None:
    reading = OcrReading("steady", ())

    assert reading.confidence is None
    assert reading.split(70) == ("steady", "")


def test_the_floor_comes_from_the_environment_and_falls_back_on_a_bad_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ocr_models.WORD_FLOOR_ENV, "55.5")
    assert ocr_models.word_floor() == 55.5
    monkeypatch.setenv(ocr_models.WORD_FLOOR_ENV, "high")
    assert ocr_models.word_floor() == ocr_models.DEFAULT_WORD_FLOOR


_SIDECAR = (
    "---\ntype: source\nmedia_type: image\nextracted_by: pending\n---\n\n# Evidence: label.png\n\n"
    "## Extracted text\n\n(pending)\n"
)


def test_a_sidecar_without_ocr_facts_is_unchanged_and_an_empty_one_drops_the_old_section() -> None:
    plain = preserve.render_sidecar_extraction(_SIDECAR, text="steady calm", engine="tesseract")
    assert "ocr_confidence" not in plain and preserve.LOW_CONFIDENCE_OCR_HEADING not in plain

    doubtful = preserve.render_sidecar_extraction(
        plain, text="steady", engine="tesseract", ocr=OcrFacts(confidence=71.3, low_confidence_text="qz")
    )
    assert "ocr_confidence: 71.3" in doubtful
    assert preserve.extracted_text(doubtful) == "steady"
    assert preserve.low_confidence_text(doubtful) == "qz"

    again = preserve.render_sidecar_extraction(
        doubtful, text="steady calm", engine="tesseract", ocr=OcrFacts(confidence=90.0)
    )
    assert preserve.LOW_CONFIDENCE_OCR_HEADING not in again
    assert preserve.low_confidence_text(again) == ""


def test_the_section_sits_inside_the_extraction_so_the_notes_below_it_survive() -> None:
    page = preserve.render_sidecar_extraction(
        f"{_SIDECAR}\n{preserve.SIDECAR_PRESERVED_NOTES_SENTINEL}\nmy own note\n",
        text="steady",
        engine="tesseract",
        ocr=OcrFacts(confidence=80.0, low_confidence_text="qz"),
    )

    withheld = preserve.without_low_confidence_ocr(page)

    assert "qz" not in withheld
    assert "my own note" in withheld and "steady" in withheld


def test_only_a_media_page_loses_a_section_with_that_heading_from_its_search_text() -> None:
    sidecar = preserve.render_sidecar_extraction(
        _SIDECAR, text="steady", engine="tesseract", ocr=OcrFacts(confidence=80.0, low_confidence_text="qz")
    )
    frontmatter, _, body = sidecar.removeprefix("---\n").partition("\n---\n")

    def search_body(facts: dict[str, object]) -> str:
        return find_types.ParsedPage(Path("p.md"), "p.md", facts, body, "p", 0.0).search_body

    assert "qz" not in search_body(yaml.safe_load(frontmatter))
    assert "qz" in search_body({"type": "concept"})


def test_a_prior_extraction_page_keeps_the_withheld_words_and_stays_out_of_search() -> None:
    sidecar = preserve.render_sidecar_extraction(
        _SIDECAR, text="steady", engine="tesseract", ocr=OcrFacts(confidence=80.0, low_confidence_text="qz")
    )

    prior = prior_extraction.render(
        sidecar, artifact_name="label.png", sidecar_link="Evidence/label.png", ordinal=1, date_iso="2026-11-01"
    )

    assert preserve.extracted_text(prior) == "steady"
    assert preserve.low_confidence_text(prior) == "qz"
    page = find_types.ParsedPage(Path("p.md"), "p.md", _frontmatter_of(prior), prior, "p", 0.0)
    assert "qz" not in page.search_body and "steady" in page.search_body


def test_the_ocr_facts_survive_the_job_store_and_a_malformed_one_voids_the_result() -> None:
    facts = OcrFacts(confidence=64.25, low_confidence_text="qz")
    payload = {"text": "steady", "engine": "tesseract", "ocr": facts.as_payload()}

    kept = media_jobs._validated_result_payload("extraction", payload)
    voided = media_jobs._validated_result_payload("extraction", {**payload, "ocr": {"confidence": "high"}})

    assert kept is not None and OcrFacts.from_payload(kept["ocr"]) == facts
    assert voided is None


def test_a_scanned_pdf_page_keeps_all_of_its_text(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only an image's text is split; a PDF page has no sidecar section to hold the rest.
    fitz = pytest.importorskip("fitz")
    with fitz.open() as doc:
        doc.new_page()  # no text layer, as a scanned page has none
        doc.save(tmp_path / "scan.pdf")
    reading = _reading(("steady", 95.0), ("qz", 10.0), text="steady qz")
    monkeypatch.setattr(extract, "resolve_tesseract_cmd", lambda: "tesseract")
    monkeypatch.setattr(ocr_models, "read_image", lambda _cmd, _image: reading)

    assert extract.extract_text(tmp_path / "scan.pdf").text == "[page 1]\nsteady qz"
