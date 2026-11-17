"""A PDF Source reports what the document says about itself, and a wrong Source is corrected without a re-upload."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

import pytest
import yaml
from _identity_pdf import make_pdf, nested_forms_pdf, text_bomb_pdf

from exomem import (
    client_artifacts,
    commands,
    document_forms,
    document_identity,
    mutation_terminal,
    source_corrections,
)
from exomem import schema as schema_module

CLAIM = "https://doi.org/10.5555/bbb"
PRINTED_PAGE = "A study of harbour lanterns. Published at https://doi.org/10.5555/aaa by the invented Lantern Press."


@pytest.fixture(autouse=True)
def _needs_pymupdf() -> None:
    pytest.importorskip("fitz")


def _stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pdf: Path, filename: str = "paper.pdf") -> None:
    """Hand `capture_source` this PDF as the staged download of its one file handle."""
    payload = pdf.read_bytes()

    def staged(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        path = tmp_path / f"staged-{uuid.uuid4().hex}.bin"
        path.write_bytes(payload)
        return client_artifacts.StagedArtifact(
            file_id=file["file_id"], path=path, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            content_type="application/pdf", filename=filename,
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", staged)


def _save_pdf(vault: Path, schema: schema_module.SourceSchema, *, upload: bool = True, **fields: object) -> dict:
    """The compact reply an agent receives for one saved PDF; `upload=False` sends no file."""
    leaf = commands.op_capture_source(
        vault, schema, title=fields.pop("title", "Harbour lanterns"),
        source_kind=fields.pop("source_kind", "paper"),
        files=[{"file_id": "pdf-1", "download_url": "https://files.example/pdf-1"}] if upload else None, **fields,
    )
    terminal = mutation_terminal.committed_terminal(leaf, request_id="r", receipt_id=None, idempotency_key=None)
    return mutation_terminal.project_terminal(terminal, "compact", outcome_verb="saved")


def _frontmatter(vault: Path, rel_path: str) -> dict:
    text = (vault / rel_path).read_text(encoding="utf-8")
    return yaml.safe_load(text.split("\n---\n", 1)[0].removeprefix("---\n"))


@pytest.mark.usefixtures("owner_scope")
def test_a_conflicting_doi_is_stated_in_the_reply_and_corrected_without_a_reupload(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE], info={"title": "Harbour Lanterns"})
    _stage(monkeypatch, tmp_path, pdf)

    reply = _save_pdf(vault, source_schema, url=CLAIM)

    row = reply["files"][0]
    assert row["identity"]["state"] == "conflict"
    assert row["identity"]["claimed_dois"] == ["10.5555/bbb"]
    assert row["identity"]["printed_dois"] == ["10.5555/aaa"]
    assert "10.5555/aaa" in reply["user_summary"]
    assert "10.5555/bbb" in reply["user_summary"]
    assert "correction" in reply["next_action"]
    assert row["ref"] in reply["next_action"]
    assert row["page_hash"] in reply["next_action"]
    predecessor_page, predecessor_original = row["page"], row["stored_path"]
    page_before = (vault / predecessor_page).read_bytes()
    original_before = (vault / predecessor_original).read_bytes()
    assert _frontmatter(vault, predecessor_page)["identity_check"]["state"] == "conflict"

    successor_reply = _save_pdf(
        vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa",
        correction={"supersedes": row["ref"], "expected_hash": row["page_hash"], "reason": "The URL named a different article."},
    )

    successor = successor_reply["files"][0]
    assert successor["identity"]["state"] == "printed"
    assert successor["hash"] == hashlib.sha256(original_before).hexdigest()
    assert successor["page"] != predecessor_page
    assert (vault / predecessor_page).read_bytes() == page_before
    assert (vault / predecessor_original).read_bytes() == original_before
    assert (vault / successor["stored_path"]).read_bytes() == original_before
    assert len(list(source_corrections.receipt_root(vault).glob("*.json"))) == 1

    found = commands.op_find(vault, query="harbour lanterns", limit=10)
    hits = found["hits"] if isinstance(found, dict) else found
    hit = next(hit for hit in hits if hit.get("path") == predecessor_page)
    assert hit["corrected_by"]["path"] == successor["page"]


# --- the correction route: guards and what it leaves behind ----------------------------------------


def _correction(row: dict, **overrides: object) -> dict:
    return {"supersedes": row["ref"], "expected_hash": row["page_hash"], "reason": "The url named a different article.", **overrides}


def _sources(vault: Path) -> set[Path]:
    return set((vault / "Knowledge Base/Sources").rglob("*.md"))


@pytest.fixture
def saved(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> dict:  # noqa: ANN001, ANN201
    """One saved PDF whose url conflicts with the DOI it prints."""
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    return _save_pdf(vault, source_schema, url=CLAIM)["files"][0]


def test_a_changed_original_refuses_the_correction_and_writes_nothing(vault, source_schema, saved) -> None:
    (vault / saved["stored_path"]).write_bytes(b"replaced bytes")
    before = _sources(vault)

    with pytest.raises(ValueError, match="SOURCE_ORIGINAL_CHANGED"):
        _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(saved))

    assert _sources(vault) == before
    assert not list(source_corrections.receipt_root(vault).glob("*.json"))


def test_a_stale_page_hash_refuses_the_metadata_only_correction(vault, source_schema, saved) -> None:
    before = _sources(vault)

    with pytest.raises(ValueError, match="SOURCE_CORRECTION_STALE"):
        _save_pdf(vault, source_schema, upload=False, correction=_correction(saved, expected_hash="0" * 64))

    assert _sources(vault) == before
    assert not list(source_corrections.receipt_root(vault).glob("*.json"))


def test_a_second_correction_of_the_same_predecessor_is_refused(vault, source_schema, saved) -> None:
    _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(saved))
    before = _sources(vault)

    with pytest.raises(ValueError, match="SOURCE_ALREADY_CORRECTED"):
        _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/ccc", correction=_correction(saved))

    assert _sources(vault) == before
    assert len(list(source_corrections.receipt_root(vault).glob("*.json"))) == 1


def test_an_undo_is_a_further_correction_and_history_only_grows(vault, source_schema, saved) -> None:
    first = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(saved))["files"][0]
    kept = [vault / name for name in (saved["page"], saved["stored_path"], first["page"], first["stored_path"])]
    before = [path.read_bytes() for path in kept]
    count = len(_sources(vault))

    undone = _save_pdf(vault, source_schema, upload=False, url=CLAIM, correction=_correction(first))["files"][0]

    assert undone["hash"] == saved["hash"]
    assert [path.read_bytes() for path in kept] == before
    assert len(_sources(vault)) == count + 1
    assert len(list(source_corrections.receipt_root(vault).glob("*.json"))) == 2


def test_a_correction_carries_the_predecessors_ingested_into(vault, source_schema, saved) -> None:
    page = vault / saved["page"]
    page.write_text(page.read_text(encoding="utf-8").replace("ingested_into: []", 'ingested_into: ["[[Notes/Insights/compiled-claim]]"]'), encoding="utf-8")
    row = dict(saved, page_hash=mutation_hash(page))

    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(row))["files"][0]

    assert _frontmatter(vault, successor["page"])["ingested_into"] == ["[[Notes/Insights/compiled-claim]]"]


def mutation_hash(page: Path) -> str:
    from exomem import vault as vault_module

    return vault_module.content_hash(page.read_text(encoding="utf-8"))


@pytest.mark.usefixtures("duplicate_index")
def test_a_plain_second_capture_of_the_same_pdf_is_still_already_stored(vault, source_schema, saved) -> None:
    again = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]

    assert again["state"] == "already_stored"
    assert again["stored_path"] == saved["stored_path"]


def test_a_text_source_cannot_be_corrected_without_content(vault, source_schema, owner_scope) -> None:
    from exomem import add

    old = add.add(vault, source_schema, source_type="session", title="Notes", content="Original body")
    correction = {"supersedes": old.path, "expected_hash": mutation_hash(vault / old.path), "reason": "Fix the kind."}

    with pytest.raises(ValueError, match="SOURCE_CORRECTION_CONTENT_REQUIRED"):
        commands.op_capture_source(vault, source_schema, title="Notes", source_kind="session", correction=correction)


# --- identity states a capture reports ----------------------------------------------------------------


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


@pytest.mark.parametrize(
    ("builder", "state", "reason"),
    [
        (lambda path: make_pdf(path, [""]), "unavailable", "no_text_layer"),
        (lambda path: make_pdf(path, ["A study of harbour lanterns."], password="secret"), "unavailable", "encrypted"),
        (lambda path: _write(path, b"%PDF-1.7 not a pdf at all"), "unavailable", "unreadable"),
        # Two million positioned glyph runs in a 400 KB file: about 540 MB to extract, past the read's cap.
        (lambda path: text_bomb_pdf(path, runs=2_000_000), "unavailable", "too_large"),
    ],
    ids=["no-text-layer", "encrypted", "malformed", "text-past-the-memory-cap"],
)
def test_an_unreadable_pdf_is_still_stored_with_the_reason(vault, source_schema, tmp_path, monkeypatch, owner_scope, builder, state, reason) -> None:
    _stage(monkeypatch, tmp_path, builder(tmp_path / "paper.pdf"))

    row = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]

    assert row["outcome"] == "stored"
    assert (row["identity"]["state"], row["identity"]["reason"]) == (state, reason)
    assert (vault / row["stored_path"]).is_file()
    assert _frontmatter(vault, row["page"])["identity_check"]["reason"] == reason


def test_a_pdf_that_outruns_the_read_deadline_is_stored_with_the_reason_within_the_deadline(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    # Seven levels of form XObjects, each drawing the next ten times: ten million draws from a 4 KB file.
    _stage(monkeypatch, tmp_path, nested_forms_pdf(tmp_path / "paper.pdf", depth=7, fanout=10))

    started = time.monotonic()
    row = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]
    elapsed = time.monotonic() - started

    assert row["outcome"] == "stored"
    assert (row["identity"]["state"], row["identity"]["reason"]) == ("unavailable", "timeout")
    assert (vault / row["stored_path"]).is_file()
    # The rest of the capture takes well under two seconds; reading the page to its end takes about ten.
    assert elapsed < document_identity.READ_DEADLINE_SECONDS + 2


def test_a_url_without_a_doi_makes_no_claim_and_asks_nothing_of_the_agent(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))

    reply = _save_pdf(vault, source_schema, url="https://example.org/lanterns.pdf")

    assert reply["files"][0]["identity"]["state"] == "no_claim"
    assert "correction" not in reply.get("next_action", "")


# --- document form is registry data --------------------------------------------------------------------


def test_a_shipped_document_form_key_and_an_alias_resolve_to_the_registered_key(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))

    first = _save_pdf(vault, source_schema, url=CLAIM, document_form="full-text")["files"][0]
    second = _save_pdf(vault, source_schema, title="Harbour lanterns, second", url=CLAIM, document_form="abstract-only")["files"][0]

    assert _frontmatter(vault, first["page"])["document_form"] == "full-text"
    assert _frontmatter(vault, second["page"])["document_form"] == "abstract"
    assert not document_forms.registry_path(vault).exists()


def test_a_new_document_form_key_registers_in_the_capture_and_a_malformed_one_is_refused(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))

    row = _save_pdf(vault, source_schema, url=CLAIM, document_form="conference-poster")["files"][0]

    assert _frontmatter(vault, row["page"])["document_form"] == "conference-poster"
    assert "conference-poster" in document_forms.registry_path(vault).read_text(encoding="utf-8")
    before = _sources(vault)
    refused = _save_pdf(vault, source_schema, title="Other", url=CLAIM, document_form="!!!")["files"][0]
    assert refused["outcome"] == "failed"
    assert refused["reason"].startswith("INVALID_DOCUMENT_FORM")
    assert _sources(vault) == before


# --- extraction and the audit after capture ------------------------------------------------------------


def _extracted(vault: Path, page: str, text: str) -> str:
    """The Source page as media completion renders it, using the function the extraction worker commits."""
    from exomem import preserve

    path = vault / page
    before, guard = preserve.read_guarded_text(vault, path)
    _plan, write = preserve.sidecar_extraction_writes(vault, path, before, guard, text=text, engine="pymupdf")
    return write.content


def _front(markdown: str) -> dict:
    return yaml.safe_load(markdown.split("\n---\n", 1)[0].removeprefix("---\n"))


def test_extraction_keeps_the_identity_check_and_the_document_form(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    row = _save_pdf(vault, source_schema, url=CLAIM, document_form="full-text")["files"][0]
    recorded = _frontmatter(vault, row["page"])

    extracted = _front(_extracted(vault, row["page"], f"[page 1]\n{PRINTED_PAGE}"))

    assert extracted["extracted_by"] == "pymupdf"
    assert extracted["identity_check"] == recorded["identity_check"]
    assert extracted["document_form"] == "full-text"


def test_ocr_text_replaces_the_unavailable_check_of_a_scanned_pdf(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "scan.pdf", [""]))
    row = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]
    assert _frontmatter(vault, row["page"])["identity_check"]["reason"] == "no_text_layer"

    extracted = _front(_extracted(vault, row["page"], f"[page 1]\n{PRINTED_PAGE}\n\n[page 3]\nCites https://doi.org/10.5555/ccc"))

    assert extracted["identity_check"]["state"] == "conflict"
    assert extracted["identity_check"]["printed_dois"] == ["10.5555/aaa"]


def test_ocr_text_does_not_touch_a_check_that_already_read_the_document(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    row = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]
    recorded = _frontmatter(vault, row["page"])["identity_check"]

    extracted = _front(_extracted(vault, row["page"], "[page 1]\nOnly https://doi.org/10.5555/bbb now"))

    assert extracted["identity_check"] == recorded


def _findings(vault: Path) -> list:
    from exomem import audit

    return audit.audit(vault, categories=["source_identity_conflict"]).findings


def test_the_audit_names_an_uncorrected_conflict_and_stops_after_the_correction(vault, source_schema, saved) -> None:
    [finding] = _findings(vault)

    assert finding.path == saved["page"]
    assert "10.5555/aaa" in finding.detail and "10.5555/bbb" in finding.detail
    assert saved["ref"] in finding.proposed_fix

    _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(saved))

    assert _findings(vault) == []


@pytest.mark.parametrize(
    ("page_text", "claim"),
    [(PRINTED_PAGE, "https://doi.org/10.5555/aaa"), ("A study with no identifier anywhere.", CLAIM)],
    ids=["printed", "not-shown"],
)
def test_the_audit_reports_neither_printed_nor_not_shown(vault, source_schema, tmp_path, monkeypatch, owner_scope, page_text, claim) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [page_text]))
    _save_pdf(vault, source_schema, url=claim)

    assert _findings(vault) == []


def test_a_withheld_successor_adds_no_corrected_by_to_the_predecessor_hit(vault, source_schema, saved, monkeypatch) -> None:
    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(saved))["files"][0]
    monkeypatch.setattr(source_corrections, "_authorizer", lambda _root: lambda path: path != successor["page"])

    assert source_corrections.corrected_by(vault, saved["ref"], saved["page"]) is None
    monkeypatch.undo()
    assert source_corrections.corrected_by(vault, saved["ref"], saved["page"])["path"] == successor["page"]


def test_a_corrected_source_never_counts_as_unprocessed_while_a_live_note_cites_the_predecessor(vault, source_schema, saved) -> None:
    from exomem import audit, note

    note.note(vault, content="## Claim\n\nLanterns exist.\n", note_type="insight", title="Lantern claim", status="active",
              sources=[saved["page"].removesuffix(".md")], relation_disposition="reviewed_none", relation_review_reason="A standalone claim for the test.")
    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(
        saved, expected_hash=mutation_hash(vault / saved["page"])))["files"][0]

    flagged = {item.path for item in audit.audit(vault, categories=["unprocessed_source"]).findings}

    assert saved["page"] not in flagged
    assert successor["page"] not in flagged
