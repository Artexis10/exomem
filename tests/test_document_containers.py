"""Signed-container custody uses invented documents and isolated storage."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from pathlib import Path

import pytest

from exomem import preserve


def container_bytes(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", b"application/vnd.etsi.asic-e+zip")
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def test_capture_preserves_container_and_exact_document_payload(vault: Path) -> None:
    """A stored container must also publish its exact document payload for processing."""
    data = container_bytes([("contract.docx", b"document bytes")])
    result = preserve.preserve_stream(
        vault, scope="Examples", category="Documents", filename="bundle.asice", stream=io.BytesIO(data)
    )
    assert (vault / result.path).read_bytes() == data
    assert len(list((vault / result.path).parent.rglob("*.docx"))) == 1


def test_safe_inspection_keeps_exact_member_bytes_and_unverified_signatures() -> None:
    from exomem import document_containers

    data = container_bytes([("folder/contract.docx", b"docx"), ("report.pdf", b"pdf"),
                            ("META-INF/signatures.xml", b"<broken")])
    with document_containers.inspect(io.BytesIO(data)) as inspected:
        assert inspected.state == "inspected"
        assert inspected.metadata()["signature_verification"] == "not_verified"
        assert {member.path: member.stream.read() for member in inspected.members} == {
            "folder/contract.docx": b"docx", "report.pdf": b"pdf"
        }


def _hostile_cases():
    link = zipfile.ZipInfo("link.docx")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    fifo = zipfile.ZipInfo("pipe.docx")
    fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
    encrypted = bytearray(container_bytes([("sealed.pdf", b"sealed")]))
    first = encrypted.index(b"PK\x01\x02")
    encrypted[encrypted.index(b"PK\x01\x02", first + 1) + 8] |= 1
    corrupt = bytearray(container_bytes([("damaged.pdf", b"UNIQUEPAYLOAD")]))
    corrupt[corrupt.index(b"UNIQUEPAYLOAD")] ^= 1
    return [
        (container_bytes([("../escape.docx", b"x")]), "failed"),
        (container_bytes([("/absolute.pdf", b"x")]), "failed"),
        (container_bytes([("C:" + "/drive.pdf", b"x")]), "failed"),
        (container_bytes([("a.pdf", b"x"), ("A.pdf", b"y")]), "failed"),
        (container_bytes([("a/b.pdf", b"x"), ("a//b.pdf", b"y")]), "failed"),
        (container_bytes([("a/../", b"")]), "failed"),
        (container_bytes([(link, b"target")]), "failed"),
        (container_bytes([(fifo, b"")]), "failed"),
        (bytes(encrypted), "failed"),
        (bytes(corrupt), "failed"),
        (container_bytes([("a.pdf", b"x")])[:-12], "failed"),
        (container_bytes([("a.pdf", b"x"), ("spreadsheet.xlsx", b"x")]), "unsupported"),
    ]


@pytest.mark.parametrize(("data", "state"), _hostile_cases(), ids=[
    "traversal", "absolute", "drive", "case-alias", "path-alias", "unsafe-directory", "symlink",
    "fifo", "encrypted", "crc", "truncated", "unsupported-payload",
])
def test_failed_inspection_never_returns_partial_payloads(data: bytes, state: str) -> None:
    from exomem import document_containers

    with document_containers.inspect(io.BytesIO(data)) as inspected:
        assert inspected.state == state
        assert not inspected.members
        assert inspected.metadata()["next_action"]


@pytest.mark.parametrize("limit", ["entries", "directory", "member", "aggregate"])
def test_declared_limits_bound_inspection(monkeypatch, limit: str) -> None:
    from exomem import archive_members, document_containers

    data = container_bytes([("a.pdf", b"1234"), ("b.docx", b"1234")])
    if limit == "entries":
        monkeypatch.setattr(archive_members, "MAX_ENTRIES", 2)
    elif limit == "directory":
        monkeypatch.setattr(archive_members, "MAX_DIRECTORY_BYTES", 32)
    elif limit == "member":
        monkeypatch.setattr(archive_members, "MAX_MEMBER_BYTES", 3)
    else:
        monkeypatch.setattr(document_containers, "MAX_EXPANDED_BYTES", 32)
    with document_containers.inspect(io.BytesIO(data)) as inspected:
        assert inspected.state == "failed"
        assert inspected.metadata()["code"] == archive_members.TOO_LARGE
        assert not inspected.members


def test_streaming_limit_rejects_bytes_beyond_the_declared_budget(monkeypatch) -> None:
    from exomem import archive_members, document_containers

    real_chunks = archive_members.member_chunks
    def oversized(archive, entry, **kwargs):
        yield from real_chunks(archive, entry, **kwargs)
        yield b"undeclared bytes" * 20
    monkeypatch.setattr(archive_members, "member_chunks", oversized)
    monkeypatch.setattr(document_containers, "MAX_EXPANDED_BYTES", 100)
    with document_containers.inspect(io.BytesIO(container_bytes([("a.pdf", b"x")]))) as inspected:
        assert inspected.state == "failed"
        assert not inspected.members


def _docx(text: str) -> bytes:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for paragraph in text.split("\n\n"):
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _capture(vault: Path, data: bytes, *, protected=False):
    return preserve.preserve_stream(
        vault, scope="Examples", category="Documents", filename="bundle.asice",
        stream=io.BytesIO(data), raw_protection=protected,
    )


def _run_documents(vault: Path) -> None:
    from exomem import media_jobs, media_worker

    store = media_jobs.MediaJobStore(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    while (job := store.claim_next()) is not None:
        result = worker._process(job)
        assert result.state == media_worker._COMPLETE, result
        store.complete(job)


def test_public_upload_processes_complete_docx_and_pdf_and_downloads_exact_bytes(vault, monkeypatch):
    from test_upload_endpoint import _client

    from exomem import commands, extraction_parts
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.vault import parse_frontmatter

    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Quartz appendix confirms the harbour inspection.")
    pdf = document.tobytes()
    document.close()
    words = "\n\n".join(f"Paragraph {n}: the dock inspection records durable neutral findings." for n in range(9000))
    docx = _docx(words + "\n\nZirconiumtail confirms the final appendix survived.")
    data = container_bytes([("contract.docx", docx), ("appendix.pdf", pdf), ("META-INF/signatures.xml", b"malformed")])
    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="transport-secret", EXOMEM_JWT_SIGNING_KEY="authority", EXOMEM_BASE_URL="https://archive.example")
    response = client.post("/upload", files={"file": ("bundle.asice", data, "application/vnd.etsi.asic-e+zip")},
                           data={"scope": "Examples", "category": "Documents"}, headers={"authorization": "Bearer transport-secret"})
    assert response.status_code == 201, response.text
    stored = response.json()
    parent = stored["path"]
    fm = parse_frontmatter((vault / (parent + ".md")).read_text())[0]
    children = fm["document_container"]["payloads"]
    assert len(children) == 2
    originals = {parent: data}
    for child in children:
        child_fm = parse_frontmatter((vault / (child + ".md")).read_text())[0]
        binding = child_fm["container_member"]
        original = docx if child.endswith(".docx") else pdf
        originals[child] = original
        assert binding["parent_sha256"] == hashlib.sha256(data).hexdigest()
        assert binding["artifact_sha256"] == hashlib.sha256(original).hexdigest()
        assert (vault / child).read_bytes() == original
    _run_documents(vault)
    docx_child = next(child for child in children if child.endswith(".docx"))
    child_page = vault / (docx_child + ".md")
    parts = list(extraction_parts.folder_for(child_page).glob("*.md"))
    assert parts, "long document text must use complete extraction parts"
    complete = child_page.read_text() + "".join(part.read_text() for part in parts)
    assert "Zirconiumtail" in complete
    assert "container_member:" in child_page.read_text()
    hits = commands.op_find(vault, query="Zirconiumtail", mode="keyword", graph=False)
    assert any(hit["path"] == docx_child + ".md" for hit in hits), hits
    with request_scope(owner_principal(surface="mcp")):
        for path, original in originals.items():
            handoff = commands.op_transfer_artifact(vault, operation="download", artifact=path)
            downloaded = client.get(handoff["deliveries"]["https"]["url"])
            assert downloaded.status_code == 200, downloaded.text
            assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(original).digest()


def test_source_capture_publishes_bound_document_children(vault, source_schema, tmp_path):
    from exomem import add
    from exomem.vault import parse_frontmatter

    data = container_bytes([("original.docx", b"unchanged DOCX bytes")])
    stage = tmp_path / "source.asice"
    stage.write_bytes(data)
    result = add.add(vault, source_schema, title="Neutral contract", source_type="correspondence",
                     content="Original signed document.", artifact=add.SourceArtifact(stage, "source.asice"))
    fm = parse_frontmatter((vault / result.path).read_text())[0]
    child = fm["document_container"]["payloads"][0]
    child_fm = parse_frontmatter((vault / (child + ".md")).read_text())[0]
    assert (vault / result.artifact_path).read_bytes() == data
    assert (vault / child).read_bytes() == b"unchanged DOCX bytes"
    assert child_fm["container_member"]["parent_page"] == result.path
    assert result.as_dict()["document_container"]["inspection_state"] == "inspected"


def test_failed_inspection_keeps_opaque_original_and_actionable_state(vault):
    from exomem.vault import parse_frontmatter

    data = container_bytes([("good.pdf", b"pdf"), ("../unsafe.docx", b"bad")])
    result = _capture(vault, data)
    fm = parse_frontmatter((vault / result.sidecar_path).read_text())[0]
    assert (vault / result.path).read_bytes() == data
    assert fm["document_container"]["inspection_state"] == "failed"
    assert fm["document_container"]["signature_verification"] == "not_verified"
    assert result.as_dict()["document_container"]["next_action"]
    assert not (vault / (result.path + ".documents")).exists()


def test_explicit_process_recovers_opaque_container_idempotently_and_retry_keeps_originals(vault, monkeypatch):
    from exomem import commands, document_containers, extract, media_jobs, media_worker
    from exomem.vault import parse_frontmatter

    data = container_bytes([("original.docx", _docx("Complete recovered contract."))])
    # A previous installation stored the original without container inspection.
    with monkeypatch.context() as old:
        old.setattr(document_containers, "is_container", lambda path: False)
        stored = _capture(vault, data)
    assert commands.op_process_media(vault, path=stored.path)["state"] == "pending"
    fm = parse_frontmatter((vault / stored.sidecar_path).read_text())[0]
    child = fm["document_container"]["payloads"][0]
    binaries = {path: path.read_bytes() for path in (vault / stored.path).parent.rglob("*") if path.is_file() and path.suffix != ".md"}
    page_before = (vault / (child + ".md")).read_bytes()
    assert commands.op_process_media(vault, path=stored.path)["state"] == "pending"
    assert (vault / (child + ".md")).read_bytes() == page_before
    store = media_jobs.MediaJobStore(vault)
    job = store.claim_next()
    assert job is not None
    with monkeypatch.context() as failure:
        failure.setattr(extract, "extract_text", lambda *args, **kwargs: (_ for _ in ()).throw(extract.ExtractionUnavailable("install document dependency")))
        outcome = media_worker.MediaWorker(vault, execution_mode="inline")._process(job)
    assert outcome.state == media_jobs.BLOCKED
    store.mark(job, media_jobs.BLOCKED, outcome.error, blocked_reason=outcome.blocked_reason)
    retried = commands.op_process_media(vault, path=stored.path, operation="retry")
    assert retried["requeued"] == 1
    _run_documents(vault)
    assert commands.op_process_media(vault, path=stored.path)["state"] == "completed"
    assert all(path.read_bytes() == original for path, original in binaries.items())
    assert "Complete recovered contract" in (vault / (child + ".md")).read_text()


def test_missing_original_has_actionable_explicit_processing_error(vault):
    from exomem import commands
    from exomem.cli_ops import OpError

    stored = _capture(vault, container_bytes([("a.pdf", b"pdf")]))
    (vault / stored.path).unlink()
    with pytest.raises(OpError) as error:
        commands.op_process_media(vault, path=stored.path)
    assert error.value.code == "MEDIA_NOT_FOUND"
    assert "restore" in str(error.value).lower()


def test_payload_count_bounds_open_spools_and_storage_failure_keeps_opaque_custody(monkeypatch):
    from exomem import document_containers

    monkeypatch.setattr(document_containers, "MAX_PAYLOADS", 2)
    data = container_bytes([(f"member-{n}.pdf", b"") for n in range(3)])
    with document_containers.inspect(io.BytesIO(data)) as result:
        assert result.state == "failed"
        assert not result.members
        assert "count" in result.reason
    monkeypatch.setattr(document_containers.tempfile, "TemporaryFile", lambda **kwargs: (_ for _ in ()).throw(OSError("no descriptors")))
    with document_containers.inspect(io.BytesIO(container_bytes([("one.pdf", b"x")]))) as result:
        assert result.state == "failed"
        assert not result.members
        assert "resources" in result.reason


def test_parent_restrictions_revoke_child_search_reads_parts_and_binary_downloads(vault, monkeypatch):
    from exomem import commands, extraction_parts, find_corpus, media_processing
    from exomem.governance import egress, membership, policy
    from exomem.governance.decisions import Decision
    from exomem.governance.principal import RequestPrincipal, request_scope

    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 32 * 1024)
    stored = _capture(vault, container_bytes([("contract.docx", _docx("A neutral clause."))]))
    child = stored.document_container["payloads"][0]
    media_processing.reconcile_media(vault, child)
    preserve.update_sidecar_extraction(vault, vault / (child + ".md"), text="Visibleword clause.\n\n" + "Long clause. " * 7000 + "\n\nRevocationtail appendix.", engine="upload")
    parts = list(extraction_parts.folder_for(vault / (child + ".md")).glob("*.md"))
    assert parts
    rules = vault / "Knowledge Base" / "_Governance"
    (rules / "scopes").mkdir(parents=True, exist_ok=True)
    (rules / "rules").mkdir(exist_ok=True)
    scope = rules / "scopes" / "container.yaml"
    scope.write_text('governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["absent/**"]\n')
    (rules / "rules" / "external.yaml").write_text('governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\nscope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 0\n')
    who = RequestPrincipal(audience_id="external")
    with request_scope(who):
        assert egress.release_allows_download(vault, child, principal=who)
        assert commands.op_find(vault, query="Revocationtail", mode="keyword", graph=False)
        assert commands.op_fetch(vault, id=child + ".md")["text"]
    scope.write_text(f'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["{stored.path}"]\n')
    compiled = policy.load(vault)
    assert membership.evaluate_path_only(vault, child, compiled).scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})
    page = find_corpus.parse_page(vault / (child + ".md"), 0, vault)
    assert membership.evaluate_snapshot(page, compiled, content_hash=hashlib.sha256((vault / (child + ".md")).read_bytes()).hexdigest())
    with request_scope(who):
        assert not egress.release_allows_download(vault, child, principal=who)
        assert not egress.release_allows_download(vault, stored.path, principal=who)
        assert not egress.quick_page_visible(vault, child + ".md", principal=who)
        assert not egress.quick_page_visible(vault, parts[-1].relative_to(vault).as_posix(), principal=who)
        assert not commands.op_find(vault, query="Revocationtail", mode="keyword", graph=False)
        for path in (child + ".md", parts[-1].relative_to(vault).as_posix()):
            with pytest.raises(ValueError):
                commands.op_fetch(vault, id=path)
    # A stale projected admission must not bypass a newly restricted parent.
    monkeypatch.setattr(egress, "projection_decision", lambda root, path, **kwargs: Decision(6) if path == child + ".md" else None)
    decision = egress._decide_path(vault, child + ".md", policy=compiled, audience="external", purpose=None, grants_hash="", principal=who)
    assert decision is not None and decision.level == 0
    # Parent metadata can change without a policy fingerprint change.
    scope.write_text('governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: [private-container]\n')
    parent_page = vault / stored.sidecar_path
    text = parent_page.read_text()
    assert egress.release_allows_download(vault, child, principal=who)
    text = text.replace("tags: [", "tags: [private-container, ", 1)
    parent_page.write_text(text)
    assert egress.release_allows_download(vault, child, principal=who) is False


def test_forged_missing_or_changed_parent_binding_never_grants_access(vault):
    from exomem.governance import egress
    from exomem.governance.principal import owner_principal

    stored = _capture(vault, container_bytes([("one.pdf", b"pdf")]))
    child = stored.document_container["payloads"][0]
    page = vault / (child + ".md")
    before = page.read_text()
    page.write_text(before.replace("member_path: one.pdf", "member_path: different.pdf"))
    assert not egress.content_permits(vault, child, owner_principal())
    page.write_text(preserve._remove_frontmatter_fields(before, lambda key: key == "container_member"))
    assert not egress.content_permits(vault, child, owner_principal())
    page.write_text(before.replace("---\n", "---\npart_of: "+ child + ".md\n", 1))
    assert not egress.content_permits(vault, child, owner_principal())
    page.write_text(before)
    (vault / stored.path).write_bytes(b"different original")
    assert not egress.content_permits(vault, child, owner_principal())


def test_raw_protection_covers_the_entire_document_subtree(vault):
    from exomem.governance import egress, raw_protection
    from exomem.governance.principal import RequestPrincipal

    stored = _capture(vault, container_bytes([("one.pdf", b"pdf")]), protected=True)
    child = stored.document_container["payloads"][0]
    assert raw_protection.marked(child)
    assert not egress.content_permits(vault, child, RequestPrincipal(audience_id="external"))


def test_catalog_preflight_classifies_staged_parent_and_payload_bytes(vault):
    from exomem import document_containers, find_corpus
    from exomem.governance import membership, policy
    from exomem.vault import PlannedWrite, PreparedBinaryContent

    parent = "Knowledge Base/Evidence/Examples/Documents/new.asice"
    data = container_bytes([("one.pdf", b"pdf")])
    digest = hashlib.sha256(data).hexdigest()
    parent_page = preserve._render_sidecar(artifact_name="new.asice", scope="Examples", category="Documents", date_iso="2026-01-01",
                                           binary_sha256=digest, binary_size=len(data), governance_artifact_path=parent,
                                           governance_artifact_sha256=digest, governance_artifact_size=len(data))
    scope = policy.Scope(id="01ARZ3NDEKTSV4RRFFQ69G5FAV", source="container.yaml", name="Container", paths=(parent,))
    compiled = policy.Policy(scopes={scope.id: scope}, rules=(), grants=(), fingerprint="staged")
    with document_containers.plan(vault, parent=parent, parent_page=parent + ".md", digest=digest, size=len(data), stream=io.BytesIO(data), date_iso="2026-01-01") as planned:
        writes = [PlannedWrite(vault / parent, PreparedBinaryContent(io.BytesIO(data), len(data), digest)),
                  PlannedWrite(vault / (parent + ".md"), parent_page), *planned.writes]
        child_page = next(write for write in planned.writes if write.path.suffix == ".md")
        parsed = find_corpus.parse_page(child_page.path, 0, vault, content=child_page.content.encode())
        with document_containers.planned_scope(vault, writes):
            assert membership.evaluate_snapshot(parsed, compiled, content_hash="staged") == frozenset({scope.id})
    assert not (vault / parent).exists()


def test_duplicate_preservation_writes_nothing_and_does_not_repair_children(vault, tmp_path, monkeypatch):
    from test_client_artifacts import _staged

    from exomem import client_artifacts, commands

    data = container_bytes([("one.pdf", b"pdf")])
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda file, budget, **kwargs: _staged(tmp_path, file["file_id"], "bundle.asice", data))
    files = [{"file_id": "container", "download_url": "https://files.example/bundle"}]
    first = commands.op_preserve_artifacts(vault, scope="Examples", category="Documents", files=files)
    row = first["files"][0]
    assert row["state"] == "stored", first
    assert row["document_container"]["signature_verification"] == "not_verified"
    from exomem.mutation_terminal import committed_terminal, project_terminal
    terminal = committed_terminal(first, request_id="11111111-1111-4111-8111-111111111111", receipt_id="example", idempotency_key=None)
    compact = project_terminal(terminal, "compact")
    assert compact["files"][0]["document_container"]["inspection_state"] == "inspected"
    child = next((vault / row["path"]).parent.rglob("*.pdf"))
    child.unlink()
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in child.parent.parent.rglob("*") if path.is_file()}
    second = commands.op_preserve_artifacts(vault, scope="Examples", category="Documents", files=files)
    assert second["files"][0]["state"] == "already_stored"
    assert not child.exists()
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in child.parent.parent.rglob("*") if path.is_file()} == before


def test_failed_atomic_capture_leaves_neither_container_nor_children(vault, monkeypatch):
    from exomem import vault as vault_module

    actual = vault_module._after_batch_destination_published
    def interrupted(path):
        actual(path)
        if ".asice.documents" in str(path):
            raise OSError("simulated interrupted publication")
    monkeypatch.setattr(vault_module, "_after_batch_destination_published", interrupted)
    with pytest.raises(OSError, match="interrupted publication"):
        _capture(vault, container_bytes([("one.pdf", b"pdf")]))
    folder = vault / "Knowledge Base" / "Evidence" / "Examples" / "Documents"
    assert not folder.exists() or not any(path.is_file() for path in folder.rglob("*"))


@pytest.mark.parametrize("damage", ["missing-pair", "missing-provenance", "wrong-bytes", "wrong-binding"])
def test_explicit_recovery_repairs_only_missing_exact_custody(vault, damage):
    from exomem import commands
    from exomem.cli_ops import OpError
    from exomem.vault import parse_frontmatter

    stored = _capture(vault, container_bytes([("one.pdf", b"pdf")]))
    child = stored.document_container["payloads"][0]
    binary = vault / child
    page = vault / (child + ".md")
    before = page.read_text()
    if damage == "missing-pair":
        binary.unlink()
        page.unlink()
    elif damage == "missing-provenance":
        page.write_text(preserve._remove_frontmatter_fields(before, lambda key: key == "container_member"))
    elif damage == "wrong-bytes":
        binary.write_bytes(b"conflicting bytes")
    else:
        page.write_text(before.replace("member_path: one.pdf", "member_path: another.pdf"))
    if damage.startswith("wrong"):
        with pytest.raises(OpError) as error:
            commands.op_process_media(vault, path=stored.path)
        assert error.value.code == "CONTAINER_CONFLICT"
        return
    commands.op_process_media(vault, path=stored.path)
    assert binary.read_bytes() == b"pdf"
    assert parse_frontmatter(page.read_text())[0]["container_member"]["member_path"] == "one.pdf"


def test_reconciliation_fallback_keeps_container_provenance(vault):
    from exomem import media_processing
    from exomem.vault import parse_frontmatter

    stored = _capture(vault, container_bytes([("one.pdf", b"pdf")]))
    child = stored.document_container["payloads"][0]
    page = vault / (child + ".md")
    before = parse_frontmatter(page.read_text())[0]["container_member"]
    page.write_text(preserve._remove_frontmatter_fields(page.read_text(), lambda key: key == "captured"))
    media_processing.reconcile_media(vault, child)
    after = parse_frontmatter(page.read_text())[0]
    assert after["container_member"] == before
    assert after["governance_companion"]["artifact_path"] == child
