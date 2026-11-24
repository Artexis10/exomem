"""Low-confidence OCR words: split at extraction, kept in the sidecar, left out of search."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml
from _identity_pdf import make_pdf

from exomem import (
    commands,
    extract,
    find_types,
    image_tags,
    media_jobs,
    media_processing,
    media_worker,
    ocr_models,
    preserve,
    prior_extraction,
    semantic_segments,
)
from exomem.find import find
from exomem.governance import catalog_publication, egress
from exomem.ocr_facts import MAX_EXTRACTION_ENGINE_BYTES, OcrFacts, engine_with_ocr_text, ocr_text_available
from exomem.ocr_models import OcrReading, OcrWord
from exomem.vault import parse_frontmatter

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


@pytest.mark.skipif(sys.platform == "win32", reason="the fake Tesseract is a POSIX script")
@pytest.mark.parametrize(
    ("word_floor", "available", "extracted"),
    [("70", True, "harbour lantern  was repaired\n\nTags: whiteboard"), ("100", False, "Tags: whiteboard")],
)
def test_page_read_reports_image_ocr_text_availability(
    vault, tmp_path, monkeypatch: pytest.MonkeyPatch, word_floor: str, available: bool, extracted: str
) -> None:
    pytest.importorskip("PIL.Image")
    monkeypatch.setenv("EXOMEM_TESSERACT_CMD", _fake_tesseract(tmp_path))
    monkeypatch.setenv(ocr_models.WORD_FLOOR_ENV, word_floor)
    monkeypatch.delenv("EXOMEM_VISION_CAPTION", raising=False)
    monkeypatch.setenv("EXOMEM_IMAGE_TAGS", "1")
    monkeypatch.setattr(image_tags, "compute_tags", lambda _path: ["whiteboard"])
    picture = Path(__file__).parent / "fixtures" / "media-samples" / "ocr-eng.png"
    stored = preserve.preserve_bytes(
        vault, scope="Rental", category="image", filename="label.png", data=picture.read_bytes()
    )
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(media_jobs.MediaJob(
        binary_path=vault / stored.path, sidecar_path=vault / stored.sidecar_path, media_type="image"
    ))
    job = store.claim_next()
    assert job is not None
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker._process(job)
    [pending] = store.pending_results()
    worker._publish_parent_results([pending])

    options = {"path": stored.sidecar_path, "include_raw": True}
    page = egress.postfilter("read_memory", commands.op_read_memory(vault, **options), vault, request_kwargs=options)
    assert page["frontmatter"]["processing_state"] == "completed"
    assert preserve.extracted_text(page["body"]) == extracted
    assert [hit.path for hit in find(vault, query="whiteboard", mode="keyword")] == [stored.sidecar_path]
    assert find(vault, query="blorft", mode="keyword") == []
    assert page.get("ocr_text_available") is available
    assert "text_available" not in pending.payload["ocr"]
    raw = (vault / stored.sidecar_path).read_bytes()
    assert page["content"].encode() == raw
    assert page["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert page["frontmatter"] == parse_frontmatter(page["content"])[0]
    assert "ocr_text_available" not in page["frontmatter"]


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

    legacy = preserve.render_sidecar_extraction(plain, text="steady", engine="tesseract", ocr=OcrFacts(confidence=71.3))
    assert _frontmatter_of(legacy)["ocr_confidence"] == 71.3
    assert "ocr_text_available" not in _frontmatter_of(legacy)

    doubtful = preserve.render_sidecar_extraction(
        legacy, text="steady", engine="tesseract",
        ocr=OcrFacts(confidence=71.3, low_confidence_text="qz", text_available=True),
    )
    assert "ocr_confidence: 71.3" in doubtful
    assert _frontmatter_of(doubtful)["extracted_by"] == "ocr-v1:1:tesseract"
    assert preserve.extracted_text(doubtful) == "steady"
    assert preserve.low_confidence_text(doubtful) == "qz"

    again = preserve.render_sidecar_extraction(
        doubtful, text="steady calm", engine="tesseract", ocr=OcrFacts(confidence=90.0)
    )
    assert preserve.LOW_CONFIDENCE_OCR_HEADING not in again
    assert preserve.low_confidence_text(again) == ""
    assert _frontmatter_of(again)["extracted_by"] == "tesseract"
    assert "ocr_text_available" not in _frontmatter_of(again)


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
        _SIDECAR, text="Tags: whiteboard", engine="tesseract+tags",
        ocr=OcrFacts(confidence=31.0, low_confidence_text="qz", text_available=False),
    )

    prior = prior_extraction.render(
        sidecar, artifact_name="label.png", sidecar_link="Evidence/label.png", ordinal=1, date_iso="2026-11-01",
        text=preserve.extracted_text(sidecar),
    )

    assert preserve.extracted_text(prior) == "Tags: whiteboard"
    assert preserve.low_confidence_text(prior) == "qz"
    page = find_types.ParsedPage(Path("p.md"), "p.md", _frontmatter_of(prior), prior, "p", 0.0)
    assert page.frontmatter["extracted_by"] == "ocr-v1:0:tesseract+tags"
    assert "ocr_text_available" not in page.frontmatter
    assert "qz" not in page.search_body and "whiteboard" in page.search_body


def test_the_ocr_facts_survive_the_job_store_and_a_malformed_one_voids_the_result() -> None:
    facts = OcrFacts(confidence=64.25, low_confidence_text="qz")
    payload = {"text": "steady", "engine": "tesseract", "ocr": facts.as_payload()}

    kept = media_jobs._validated_result_payload("extraction", payload)
    voided = media_jobs._validated_result_payload("extraction", {**payload, "ocr": {"confidence": "high"}})
    unexpected = media_jobs._validated_result_payload("extraction", {**payload, "ocr": {"text_available": True}})

    assert kept is not None and OcrFacts.from_payload(kept["ocr"]) == facts
    assert voided is None
    assert unexpected is None


def test_pending_legacy_ocr_results_keep_their_canonical_payload() -> None:
    # Changing this payload would make the stored hash fail validation after an upgrade.
    payload = {
        "text": "steady", "engine": "tesseract", "ocr": {"confidence": 64.25, "low_confidence_text": "qz"},
    }

    assert media_jobs._validated_result_payload("extraction", payload) == payload


def test_a_configured_caption_basename_cannot_imitate_the_measurement() -> None:
    engine = "tesseract+ocr-v1:1:tesseract"

    marked = engine_with_ocr_text(engine, OcrFacts(text_available=False))

    assert ocr_text_available(engine) is None
    assert ocr_text_available(marked) is False
    assert marked.endswith(engine)
    assert ocr_text_available("ocr-v1:1:pdfium+tesseract") is None


def test_an_engine_at_the_byte_limit_keeps_its_valid_payload_without_a_measurement() -> None:
    engine = "tesseract+" + "é" * ((MAX_EXTRACTION_ENGINE_BYTES - len("tesseract+")) // 2)
    assert len(engine.encode()) == MAX_EXTRACTION_ENGINE_BYTES

    packed = engine_with_ocr_text(engine, OcrFacts(text_available=True))
    payload = {"text": "steady", "engine": packed}

    assert packed == engine
    assert ocr_text_available(packed) is None
    assert media_jobs._validated_result_payload("extraction", payload) == payload


def test_a_scanned_pdf_page_keeps_all_of_its_text(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only an image's text is split; a PDF page has no sidecar section to hold the rest.
    pytest.importorskip("pypdfium2")
    make_pdf(tmp_path / "scan.pdf", [""])  # no text layer, as a scanned page has none
    reading = _reading(("steady", 95.0), ("qz", 10.0), text="steady qz")
    monkeypatch.setattr(extract, "resolve_tesseract_cmd", lambda: "tesseract")
    monkeypatch.setattr(ocr_models, "read_image", lambda _cmd, _image: reading)

    assert extract.extract_text(tmp_path / "scan.pdf").text == "[page 1]\nsteady qz"


def test_text_that_holds_the_section_heading_stays_in_the_extraction() -> None:
    # A screenshot of this very heading is OCR text, and a heading proves no ownership.
    page = preserve.render_sidecar_extraction(
        _SIDECAR,
        text="notes\n## Low-confidence OCR\nmore",
        engine="tesseract",
        ocr=OcrFacts(confidence=80.0, low_confidence_text="qz"),
    )

    assert preserve.extracted_text(page) == "notes\n## Low-confidence OCR\nmore"
    assert preserve.low_confidence_text(page) == "qz"
    assert "more" in preserve.without_low_confidence_ocr(page)
    assert "qz" not in preserve.without_low_confidence_ocr(page)


def test_a_photo_whose_every_word_is_doubted_has_empty_text_and_still_counts_as_extracted() -> None:
    facts = OcrFacts(confidence=31.0, low_confidence_text="qz")
    page = preserve.render_sidecar_extraction(
        _SIDECAR,
        text=extract.ExtractResult("", "image", "tesseract", ocr=facts).sidecar_text,
        engine="tesseract",
        ocr=facts,
    )

    assert preserve.extracted_text(page) == ""
    assert extract.NO_TEXT_DETECTED not in page
    assert media_processing.has_completed_transcript(page, media_type="image")


def test_a_photo_with_no_words_at_all_says_no_text_was_detected() -> None:
    page = preserve.render_sidecar_extraction(
        _SIDECAR,
        text=extract.ExtractResult("", "image", "tesseract", ocr=OcrFacts(confidence=None)).sidecar_text,
        engine="tesseract",
    )

    assert preserve.extracted_text(page) == extract.NO_TEXT_DETECTED
    assert media_processing.has_completed_transcript(page, media_type="image")


def test_a_frame_sidecar_gives_the_scene_matcher_its_kept_words_only(tmp_path: Path) -> None:
    page = preserve.render_sidecar_extraction(
        _SIDECAR, text="steady", engine="tesseract", ocr=OcrFacts(confidence=80.0, low_confidence_text="qz")
    )
    sidecar = tmp_path / "frame.jpg.md"
    sidecar.write_text(page, encoding="utf-8")

    assert semantic_segments._frame_ocr_tokens(sidecar) == {"steady"}


def test_a_re_rendered_sidecar_does_not_turn_the_withheld_words_into_searchable_notes(vault) -> None:
    data = b"\x89PNG label"
    stub = preserve.preserve_bytes(vault, scope="Rental", category="image", filename="label.png", data=data)
    sidecar = vault / stub.sidecar_path
    preserve.update_sidecar_extraction(
        vault, sidecar, text="harbour lantern", engine="tesseract",
        ocr=OcrFacts(confidence=80.0, low_confidence_text="zyxqv blorft"),
    )
    binary = vault / stub.path
    seen = binary.stat()
    provenance = media_processing._BinaryProvenance(
        relative_path=stub.path, original_filename=binary.name, sha256=hashlib.sha256(data).hexdigest(),
        size=seen.st_size, mtime_ns=seen.st_mtime_ns, ctime_ns=seen.st_ctime_ns,
        device=seen.st_dev, inode=seen.st_ino,
    )

    rendered = media_processing._render_pending_sidecar(
        binary=binary, media_type="image", provenance=provenance, original=sidecar.read_text(encoding="utf-8")
    )

    frontmatter, body, _marker = parse_frontmatter(rendered)
    searchable = find_types.ParsedPage(Path("p.md"), "p.md", frontmatter, body, "p", 0.0).search_body
    assert "harbour lantern" in searchable
    assert "zyxqv" not in searchable and "blorft" not in searchable


def test_the_governed_projection_body_leaves_out_the_withheld_words() -> None:
    page = preserve.render_sidecar_extraction(
        _SIDECAR, text="harbour lantern", engine="tesseract", ocr=OcrFacts(confidence=80.0, low_confidence_text="zyxqv")
    )
    frontmatter, body, _marker = parse_frontmatter(page)
    parsed = find_types.ParsedPage(Path("p.md"), "p.md", frontmatter, body, "p", 0.0)

    projected = catalog_publication.search_fields(parsed)["body"]

    assert "harbour lantern" in projected
    assert "zyxqv" not in projected
