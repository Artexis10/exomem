"""A PDF Source reports what the document says about itself, and a wrong Source is corrected without a re-upload."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest
import yaml
from _identity_pdf import make_pdf, nested_forms_pdf, raw_info_pdf, text_bomb_pdf, xmp_with_doi

from exomem import (
    client_artifacts,
    commands,
    document_forms,
    document_identity,
    mutation_terminal,
    source_capture,
    source_corrections,
)
from exomem import schema as schema_module

CLAIM = "https://doi.org/10.5555/bbb"
PRINTED_PAGE = "A study of harbour lanterns. Published at https://doi.org/10.5555/aaa by the invented Lantern Press."


@pytest.fixture(autouse=True)
def _needs_pdf_reader() -> None:
    pytest.importorskip("pypdfium2")


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


def test_only_a_caller_who_can_write_the_receipt_is_offered_a_correction(vault, source_schema, saved, tmp_path, monkeypatch) -> None:
    # A restricted writer's commit was refused at the receipt after the work, with an error naming nothing to change.
    from conftest import initialize_vault_state_offline
    from granted_writer import grant_writer

    from exomem.governance import principal

    assert source_corrections.can_record(vault, saved["ref"])
    initialize_vault_state_offline(vault, source="restricted correction")
    writer = grant_writer(vault, tmp_path, monkeypatch, writable_paths=["Sources/Inbox/writer/**"],
                          capture_paths=["Knowledge Base/Sources/Inbox/writer"])("external")
    with principal.request_scope(writer):
        assert not source_corrections.can_record(vault, saved["ref"])


def test_a_caller_who_cannot_record_the_receipt_is_not_offered_the_correction_call(
    vault, source_schema, tmp_path, monkeypatch, owner_scope
) -> None:
    # A restricted writer who finds a conflict must tell the person which value is wrong; offering
    # it the correction call sends it into a commit the receipt write refuses. `can_record` answers
    # under the writer's own request scope, so every check of it — the row's `correctable`, the
    # correction guard — is the writer's real permission, not a stub.
    from conftest import initialize_vault_state_offline
    from granted_writer import grant_writer

    from exomem.governance import principal

    initialize_vault_state_offline(vault, source="restricted correction")
    writer = grant_writer(vault, tmp_path, monkeypatch, writable_paths=["Sources/Inbox/writer/**"],
                          capture_paths=["Knowledge Base/Sources/Inbox/writer"])("external")
    can_record = source_corrections.can_record

    def as_writer(root, ref):
        with principal.request_scope(writer):
            return can_record(root, ref)

    monkeypatch.setattr(source_corrections, "can_record", as_writer)
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))

    reply = _save_pdf(vault, source_schema, url=CLAIM)

    row = reply["files"][0]
    assert row["identity"]["state"] == "conflict"
    assert row["correctable"] is False
    assert reply["next_action"] == "Compare the opening text with the url, then tell the person which one is wrong."
    assert "correct the Source" not in reply["next_action"]

    with pytest.raises(ValueError, match="SOURCE_CORRECTION_NOT_PERMITTED"):
        _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa",
                  correction=_correction(vault, row))

    assert not list(source_corrections.receipt_root(vault).glob("*.json"))


def test_a_content_capture_says_it_kept_text_not_the_original_file(
    vault, source_schema, tmp_path, monkeypatch, owner_scope
) -> None:
    leaf = commands.op_capture_source(
        vault, source_schema, title="Call notes", source_kind="session",
        content="A transcript's pasted text.")
    reply = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(leaf, request_id="r", receipt_id=None, idempotency_key=None),
        "compact", outcome_verb="saved")
    assert "text only; no original file stored" in reply["user_summary"]

    # An exact-bytes capture gets the same sentence without the clause.
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    stored = _save_pdf(vault, source_schema, url=CLAIM)
    assert "text only" not in stored["user_summary"]


def test_a_source_in_shared_original_storage_is_corrected_without_re_upload(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    # A new archive keeps originals in the content-addressed pool, where no file holds the logical path.
    from conftest import initialize_vault_state_offline

    from exomem import artifact_lifecycle, original_pool

    initialize_vault_state_offline(vault, source="shared storage correction")
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    first = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]

    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, first))["files"][0]

    assert successor["outcome"] == "stored"
    assert successor["hash"] == first["hash"]
    assert successor["identity"]["state"] == "printed"


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
    assert "read_page" in reply["next_action"]
    # The agent gets the DOI the document prints, so it can correct without asking the person to check.
    assert '"https://doi.org/10.5555/aaa"' in reply["next_action"]
    predecessor_page, predecessor_original = row["page"], row["stored_path"]
    page_before = (vault / predecessor_page).read_bytes()
    original_before = (vault / predecessor_original).read_bytes()
    assert _frontmatter(vault, predecessor_page)["identity_check"]["state"] == "conflict"

    successor_reply = _save_pdf(
        vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa",
        correction={"supersedes": row["ref"], "expected_hash": mutation_hash(vault / row["page"]), "reason": "The URL named a different article."},
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
    # The OpenAI-compatible search keeps string metadata only, so it carries the successor as JSON.
    result = next(result for result in commands.op_search(vault, query="harbour lanterns")["results"] if result["id"] == predecessor_page)
    assert json.loads(result["metadata"]["corrected_by"])["path"] == successor["page"]


@pytest.mark.usefixtures("owner_scope")
def test_a_template_doi_in_the_metadata_is_never_offered_as_the_corrected_url(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Publisher tools can leave a template DOI in XMP. Metadata outranks print, so the state is `conflict`,
    # yet the url names the DOI page 1 prints: a directive to the template DOI would overwrite a correct url.
    page = "Meltwater pulses in alpine basins. Published at https://doi.org/10.5555/paper.2021.0042"
    pdf = make_pdf(tmp_path / "paper.pdf", [page], xmp=xmp_with_doi("10.5555/template.0001"))
    _stage(monkeypatch, tmp_path, pdf)

    reply = _save_pdf(vault, source_schema, url="https://doi.org/10.5555/paper.2021.0042")

    assert reply["files"][0]["identity"]["state"] == "conflict"
    assert "10.5555/template.0001" not in reply["next_action"]
    # The maintenance sweep offers the same call, so it must not correct the Source to the template DOI either.
    [finding] = _findings(vault)
    assert "10.5555/template.0001" not in finding.proposed_fix


# --- the correction route: guards and what it leaves behind ----------------------------------------


def _correction(vault: Path, row: dict, **overrides: object) -> dict:
    # The receipt names no page hash, so the guard comes from the page, as an agent reads it with read_page.
    return {"supersedes": row["ref"], "expected_hash": mutation_hash(vault / row["page"]), "reason": "The url named a different article.", **overrides}


def _sources(vault: Path) -> set[Path]:
    return set((vault / "Knowledge Base/Sources").rglob("*.md"))


@pytest.fixture
def saved(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> dict:  # noqa: ANN001, ANN201
    """One saved PDF whose url conflicts with the DOI it prints."""
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    return _save_pdf(vault, source_schema, url=CLAIM)["files"][0]


@pytest.mark.usefixtures("owner_scope")
def test_a_correction_that_names_only_the_url_keeps_every_other_field_of_the_source(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    row = _save_pdf(
        vault, source_schema, url=CLAIM, source_kind="session", domain="urban-planning", projects=["riverside"],
        tags=["lanterns", "harbours"], document_form="full-text", why_captured="Cited in the harbour survey.",
    )["files"][0]

    # What `next_action` asks for: no files, a title, the right url and the correction; tags set anew.
    successor = _save_pdf(
        vault, source_schema, upload=False, source_kind=None, url="https://doi.org/10.5555/aaa", tags=["lighthouses"],
        correction=_correction(vault, row),
    )["files"][0]

    kept = _frontmatter(vault, successor["page"])
    assert kept["url"] == "https://doi.org/10.5555/aaa"
    assert kept["tags"] == ["lighthouses"]
    assert (kept["source_type"], kept["domain"], kept["projects"], kept["document_form"]) == (
        "session", "urban-planning", ["riverside"], "full-text",
    )
    assert "> Cited in the harbour survey." in (vault / successor["page"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["id", "title"])
def test_a_correction_names_the_source_by_any_name_read_page_takes(vault, source_schema, saved, name) -> None:
    # Agents passed the short id or the title that search and read_page showed, and were refused.
    from exomem import page_names

    identity = page_names.read_identity(vault, saved["page"])
    successor = _save_pdf(
        vault, source_schema, upload=False, title="A study of harbour lanterns", url="https://doi.org/10.5555/aaa",
        correction=_correction(vault, saved, supersedes=identity.id if name == "id" else identity.title),
    )["files"][0]

    assert _frontmatter(vault, successor["page"])["supersedes"]


def _edit_in_place(vault: Path, page: str, door: str) -> str:
    """The refusal an in-place edit of a Source gets, through the frontmatter door or the body door."""
    from exomem import edit as edit_module
    from exomem import set_frontmatter_field

    if door == "frontmatter":
        with pytest.raises(set_frontmatter_field.SetFrontmatterError) as caught:
            set_frontmatter_field.set_frontmatter_field(
                vault, path=page, field="title", value="A study of harbour lanterns", why="Fix the title.",
            )
        assert caught.value.code == "APPEND_ONLY"
    else:
        with pytest.raises(edit_module.EditError) as caught:
            edit_module.edit(vault, path=page, why="Fix the title.", old_string="Harbour lanterns", new_string="A study")
    return caught.value.reason


@pytest.mark.parametrize("door", ["frontmatter", "body"])
def test_editing_a_source_in_place_is_refused_with_the_correction_that_does_it(vault, source_schema, saved, door) -> None:
    reason = _edit_in_place(vault, saved["page"], door)

    assert "options.correction" in reason and saved["ref"] in reason


def test_a_writer_who_cannot_record_a_correction_is_not_sent_to_one(vault, source_schema, saved, tmp_path, monkeypatch) -> None:
    # Told to correct the Source, a restricted writer would only meet the receipt refusal.
    from conftest import initialize_vault_state_offline
    from granted_writer import grant_writer

    from exomem.governance import principal

    initialize_vault_state_offline(vault, source="restricted correction route")
    writer = grant_writer(vault, tmp_path, monkeypatch, writable_paths=["Sources/Inbox/writer/**"],
                          capture_paths=["Knowledge Base/Sources/Inbox/writer"])("external")
    can_record = source_corrections.can_record

    def as_writer(root, ref):
        with principal.request_scope(writer):
            return can_record(root, ref)

    monkeypatch.setattr(source_corrections, "can_record", as_writer)

    reason = _edit_in_place(vault, saved["page"], "frontmatter")

    assert "options.correction" not in reason and "cannot record a Source correction" in reason


def test_a_source_in_a_readonly_tree_is_refused_as_not_correctable_here(vault, source_schema, saved) -> None:
    # The caller named the right Source; telling it to pass another name sent agents in circles.
    folder = saved["page"].removeprefix("Knowledge Base/").rsplit("/", 1)[0]
    (vault / "Knowledge Base/_access.yaml").write_text(f"readonly:\n  - {folder}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SOURCE_CORRECTION_NOT_PERMITTED"):
        _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, saved))


@pytest.mark.usefixtures("owner_scope")
def test_a_correction_with_an_empty_url_drops_a_doi_the_document_never_shows(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "summary.pdf", ["A thesis summary that cites a harbour lantern study."]))
    row = _save_pdf(vault, source_schema, url=CLAIM, tags=["lanterns"])["files"][0]

    # What `next_action` asks for when the document has no url of its own: the right kind and url "".
    successor = _save_pdf(
        vault, source_schema, upload=False, title="Thesis summary on harbour lanterns", source_kind="research-report",
        url="", correction=_correction(vault, row, reason="The file is a thesis summary that cites the study."),
    )["files"][0]

    kept = _frontmatter(vault, successor["page"])
    assert not kept.get("url")
    assert not kept["identity_check"].get("claimed_dois")
    assert kept["tags"] == ["lanterns"]


def test_a_metadata_only_correction_scans_the_canonical_sources_once(vault, source_schema, saved, monkeypatch) -> None:
    # Each scan reads every canonical Source page: seconds at the load target of 20,000 pages.
    scans = []
    scan = source_capture.canonical_pages
    monkeypatch.setattr(source_capture, "canonical_pages", lambda *args, **kwargs: scans.append(args) or scan(*args, **kwargs))

    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, saved))

    assert successor["files"][0]["outcome"] == "stored"
    assert len(scans) == 1


def test_a_corrected_source_is_queued_for_processing_when_it_is_saved(vault, source_schema, saved) -> None:
    # Live, two corrected Sources were never processed: their pages kept no text, and reading the old ref
    # redirected to them. Processing must not wait on a file-watcher event that can be missed.
    from exomem import media_jobs

    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa",
                          correction=_correction(vault, saved))["files"][0]

    assert _frontmatter(vault, successor["page"])["processing_state"] == "pending"
    job = media_jobs.MediaJobStore(vault).get_by_binary(vault / successor["stored_path"])
    assert job is not None and job.state == "pending"


def test_a_changed_original_refuses_the_correction_and_writes_nothing(vault, source_schema, saved) -> None:
    (vault / saved["stored_path"]).write_bytes(b"replaced bytes")
    before = _sources(vault)

    with pytest.raises(ValueError, match="SOURCE_ORIGINAL_CHANGED"):
        _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, saved))

    assert _sources(vault) == before
    assert not list(source_corrections.receipt_root(vault).glob("*.json"))


def test_a_stale_page_hash_refuses_the_metadata_only_correction(vault, source_schema, saved) -> None:
    before = _sources(vault)

    with pytest.raises(ValueError, match="SOURCE_CORRECTION_STALE"):
        _save_pdf(vault, source_schema, upload=False, correction=_correction(vault, saved, expected_hash="0" * 64))

    assert _sources(vault) == before
    assert not list(source_corrections.receipt_root(vault).glob("*.json"))


def test_a_second_correction_of_the_same_predecessor_is_refused(vault, source_schema, saved) -> None:
    _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, saved))
    before = _sources(vault)

    with pytest.raises(ValueError, match="SOURCE_ALREADY_CORRECTED"):
        _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/ccc", correction=_correction(vault, saved))

    assert _sources(vault) == before
    assert len(list(source_corrections.receipt_root(vault).glob("*.json"))) == 1


def test_an_undo_is_a_further_correction_and_history_only_grows(vault, source_schema, saved) -> None:
    first = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, saved))["files"][0]
    kept = [vault / name for name in (saved["page"], saved["stored_path"], first["page"], first["stored_path"])]
    before = [path.read_bytes() for path in kept]
    count = len(_sources(vault))

    undone = _save_pdf(vault, source_schema, upload=False, url=CLAIM, correction=_correction(vault, first))["files"][0]

    assert undone["hash"] == saved["hash"]
    assert [path.read_bytes() for path in kept] == before
    assert len(_sources(vault)) == count + 1
    assert len(list(source_corrections.receipt_root(vault).glob("*.json"))) == 2


def test_a_correction_carries_the_predecessors_ingested_into(vault, source_schema, saved) -> None:
    page = vault / saved["page"]
    page.write_text(page.read_text(encoding="utf-8").replace("ingested_into: []", 'ingested_into: ["[[Notes/Insights/compiled-claim]]"]'), encoding="utf-8")
    row = saved

    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, row))["files"][0]

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


def test_a_text_correction_that_names_only_its_title_and_text_keeps_every_other_field(
    vault, source_schema, owner_scope
) -> None:
    old = commands.op_capture_source(
        vault, source_schema, title="Notes", source_kind="session", content="Lantern rota for the harbour.",
        url="https://notes.example/lanterns", domain="urban-planning", projects=["riverside"], tags=["lanterns"],
        custody_only=True,
    )["source"]
    correction = {"supersedes": old["path"], "expected_hash": mutation_hash(vault / old["path"]), "reason": "Fix the title."}

    corrected = commands.op_capture_source(
        vault, source_schema, title="Harbour lantern rota", content="Lantern rota for the harbour.", correction=correction,
    )["source"]

    kept = _frontmatter(vault, corrected["path"])
    assert (kept["title"], kept["source_type"], kept["url"], kept["domain"], kept["projects"], kept["tags"]) == (
        "Harbour lantern rota", "session", "https://notes.example/lanterns", "urban-planning", ["riverside"], ["lanterns"],
    )
    assert kept["custody_only"] is True


def test_a_text_correction_of_an_owner_only_source_stays_owner_only(vault, source_schema, owner_scope) -> None:
    from exomem.governance import raw_protection as raw_guard

    old = commands.op_capture_source(
        vault, source_schema, title="Ledger", source_kind="session", content="Private ledger lines.", raw_protection=True,
    )["source"]
    correction = {"supersedes": old["path"], "expected_hash": mutation_hash(vault / old["path"]), "reason": "Fix the title."}

    corrected = commands.op_capture_source(
        vault, source_schema, title="Household ledger", content="Private ledger lines.", correction=correction,
    )["source"]

    assert raw_guard.marked(old["path"])
    assert raw_guard.marked(corrected["path"])


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
    # Only the memory cap may end the text-bomb read: on a loaded machine the deadline came first.
    monkeypatch.setattr(document_identity, "READ_DEADLINE_SECONDS", 60.0)
    _stage(monkeypatch, tmp_path, builder(tmp_path / "paper.pdf"))

    row = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]

    assert row["outcome"] == "stored"
    assert (row["identity"]["state"], row["identity"]["reason"]) == (state, reason)
    assert (vault / row["stored_path"]).is_file()
    assert _frontmatter(vault, row["page"])["identity_check"]["reason"] == reason


def test_a_pdf_that_outruns_the_read_deadline_is_stored_with_the_reason_within_the_deadline(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    # Seven levels of form XObjects, each drawing the next ten times: ten million draws from a 4 KB file.
    monkeypatch.setattr(document_identity, "READ_DEADLINE_SECONDS", 1.0)
    _stage(monkeypatch, tmp_path, nested_forms_pdf(tmp_path / "paper.pdf", depth=7, fanout=10))

    started = time.monotonic()
    row = _save_pdf(vault, source_schema, url=CLAIM)["files"][0]
    elapsed = time.monotonic() - started

    assert row["outcome"] == "stored"
    assert (row["identity"]["state"], row["identity"]["reason"]) == ("unavailable", "timeout")
    assert (vault / row["stored_path"]).is_file()
    # Reading the page to its end takes about ten seconds; the rest of the capture took up to four on a
    # loaded machine, so this bound fails only when the deadline does not end the read.
    assert elapsed < document_identity.READ_DEADLINE_SECONDS + 5


def test_a_pdf_whose_document_information_holds_a_lone_surrogate_is_stored(vault, source_schema, tmp_path, monkeypatch, owner_scope) -> None:
    # A UTF-16 subject of `10.5555/abc` followed by an unpaired high surrogate, as a broken producer writes it.
    subject = "10.5555/abc".encode("utf-16-be").hex().upper().encode()
    _stage(monkeypatch, tmp_path, raw_info_pdf(tmp_path / "paper.pdf", b"<< /Subject <FEFF" + subject + b"D800> >>"))

    row = _save_pdf(vault, source_schema, url="https://doi.org/10.5555/abc")["files"][0]

    assert row["outcome"] == "stored"
    assert row["identity"]["state"] == "declared"
    assert _frontmatter(vault, row["page"])["identity_check"]["declared_dois"] == ["10.5555/abc"]


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

    _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, saved))

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
    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(vault, saved))["files"][0]
    monkeypatch.setattr(source_corrections, "_authorizer", lambda _root: lambda path: path != successor["page"])

    assert source_corrections.corrected_by(vault, {saved["page"]: saved["ref"]}) == {}
    monkeypatch.undo()
    assert source_corrections.corrected_by(vault, {saved["page"]: saved["ref"]})[saved["page"]]["path"] == successor["page"]


def test_a_corrected_source_never_counts_as_unprocessed_while_a_live_note_cites_the_predecessor(vault, source_schema, saved) -> None:
    from exomem import audit, note

    note.note(vault, content="## Claim\n\nLanterns exist.\n", note_type="insight", title="Lantern claim", status="active",
              sources=[saved["page"].removesuffix(".md")], relation_disposition="reviewed_none", relation_review_reason="A standalone claim for the test.")
    successor = _save_pdf(vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", correction=_correction(
        vault, saved, expected_hash=mutation_hash(vault / saved["page"])))["files"][0]

    flagged = {item.path for item in audit.audit(vault, categories=["unprocessed_source"]).findings}

    assert saved["page"] not in flagged
    assert successor["page"] not in flagged


# --- custody: a Source kept only for safekeeping ------------------------------------------------------


def test_a_custody_only_capture_marks_the_source_and_says_so(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_scope
) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "kept.pdf", ["A handed screenshot."]))
    reply = _save_pdf(vault, source_schema, url=CLAIM, custody_only=True)
    row = reply["files"][0]
    assert _frontmatter(vault, row["page"])["custody_only"] is True
    assert "(kept for custody only)" in reply["user_summary"]


@pytest.mark.usefixtures("owner_scope", "duplicate_index")
def test_a_batch_that_also_holds_an_already_stored_file_still_says_kept_for_custody_only(
    vault, source_schema, tmp_path, monkeypatch
) -> None:
    # A batch with an already-stored file takes the partial sentence, which dropped the custody clause,
    # so the person never saw the mark they might want to overrule.
    payloads = {
        "pdf-old": make_pdf(tmp_path / "old.pdf", ["The first handed page."]).read_bytes(),
        "pdf-new": make_pdf(tmp_path / "new.pdf", ["The second handed page."]).read_bytes(),
    }

    def staged(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        payload = payloads[file["file_id"]]
        path = tmp_path / f"staged-{uuid.uuid4().hex}.bin"
        path.write_bytes(payload)
        return client_artifacts.StagedArtifact(
            file_id=file["file_id"], path=path, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            content_type="application/pdf", filename=f"{file['file_id']}.pdf",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", staged)
    old = {"file_id": "pdf-old", "download_url": "https://files.example/pdf-old"}
    new = {"file_id": "pdf-new", "download_url": "https://files.example/pdf-new"}
    commands.op_capture_source(vault, source_schema, title="First page", source_kind="session", files=[old])

    leaf = commands.op_capture_source(
        vault, source_schema, title="Both pages", source_kind="session", files=[old, new], custody_only=True)
    reply = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(leaf, request_id="r", receipt_id=None, idempotency_key=None),
        "compact", outcome_verb="saved")

    assert "1 already saved" in reply["user_summary"]
    assert "(kept for custody only)" in reply["user_summary"]


def test_a_custody_only_text_capture_adds_the_clause_after_text_only(vault, source_schema, owner_scope) -> None:
    leaf = commands.op_capture_source(
        vault, source_schema, title="Call notes", source_kind="session",
        content="A transcript's pasted text.", custody_only=True)
    reply = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(leaf, request_id="r", receipt_id=None, idempotency_key=None),
        "compact", outcome_verb="saved")
    assert "(text only; no original file stored) (kept for custody only)" in reply["user_summary"]


def test_a_plain_capture_says_nothing_about_custody(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_scope
) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    reply = _save_pdf(vault, source_schema)
    assert "custody" not in reply["user_summary"]


def test_custody_only_accepts_only_a_boolean(vault, source_schema, owner_scope) -> None:
    with pytest.raises(ValueError, match="custody_only"):
        commands.op_capture_source(
            vault, source_schema, title="Call notes", source_kind="session",
            content="A transcript's pasted text.", custody_only="yes")


def test_a_correction_carries_the_custody_mark(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_scope
) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    saved_row = _save_pdf(vault, source_schema, url=CLAIM, custody_only=True)["files"][0]
    successor = _save_pdf(
        vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa",
        correction=_correction(vault, saved_row),
    )["files"][0]
    assert _frontmatter(vault, successor["page"])["custody_only"] is True


def test_a_correction_that_sets_custody_off_leaves_the_successor_unmarked(
    vault: Path, source_schema: schema_module.SourceSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_scope
) -> None:
    _stage(monkeypatch, tmp_path, make_pdf(tmp_path / "paper.pdf", [PRINTED_PAGE]))
    saved_row = _save_pdf(vault, source_schema, url=CLAIM, custody_only=True)["files"][0]
    successor = _save_pdf(
        vault, source_schema, upload=False, url="https://doi.org/10.5555/aaa", custody_only=False,
        correction=_correction(vault, saved_row),
    )["files"][0]
    assert "custody_only" not in _frontmatter(vault, successor["page"])
