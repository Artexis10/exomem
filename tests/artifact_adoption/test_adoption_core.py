from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from exomem.vault import parse_frontmatter

_ARTIFACTS = (
    ("image.png", "image/png", b"\x89PNG\r\n\x1a\nsynthetic-image\x00"),
    ("document.pdf", "application/pdf", b"%PDF-1.7\nsynthetic-pdf\n%%EOF\n"),
    ("audio.mp3", "audio/mpeg", b"ID3\x04\x00\x00synthetic-audio\xff\xfb"),
    ("video.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42synthetic-video"),
    (
        "slides.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        b"PK\x03\x04synthetic-slide-package\x00",
    ),
    (
        "sheet.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        b"PK\x03\x04synthetic-spreadsheet-package\xff",
    ),
    ("notes.txt", "text/plain", "Exact text — not reconstructed.\n".encode()),
)


def _handle(file_id: str, filename: str, content_type: str) -> dict[str, str]:
    return {
        "download_url": f"https://files.example/{file_id}",
        "file_id": file_id,
        "file_name": filename,
        "mime_type": content_type,
    }


def _stage_factory(
    tmp_path: Path,
    payloads: dict[str, tuple[str, str, bytes]],
    calls: list[str],
):
    from exomem import client_artifacts

    attempt = 0

    def stage(file, _budget, **_kwargs):
        nonlocal attempt
        file_id = str(file["file_id"])
        calls.append(file_id)
        filename, content_type, data = payloads[file_id]
        staged = tmp_path / f"stage-{attempt}-{filename}"
        attempt += 1
        staged.write_bytes(data)
        return client_artifacts.StagedArtifact(
            file_id=file_id,
            path=staged,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
            filename=filename,
        )

    return stage


def _adoption(key: str, selected: str = "file-b", trigger: str = "selected") -> dict[str, str]:
    return {"key": key, "trigger": trigger, "selected_file_id": selected}


def _receipt_from_result(result: dict, selected: str = "file-b") -> dict:
    row = next(item for item in result["files"] if item["file_id"] == selected)
    return row["adoption"]


def _replace_receipt_scalar(page: Path, field: str, before: str, after: str) -> None:
    source = page.read_text(encoding="utf-8")
    frontmatter, _body, _marker = parse_frontmatter(source, strict=True)
    assert frontmatter["artifact_adoption"][field] == before
    prefix = f"  {field}: "
    lines = source.splitlines(keepends=True)
    indexes = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    assert len(indexes) == 1
    ending = "\n" if lines[indexes[0]].endswith("\n") else ""
    lines[indexes[0]] = prefix + json.dumps(after, ensure_ascii=True) + ending
    page.write_text("".join(lines), encoding="utf-8")


@pytest.mark.parametrize(("filename", "content_type", "data"), _ARTIFACTS)
def test_evidence_adoption_round_trips_exact_cross_media_bytes_and_receipt(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    from exomem import client_artifacts, media_processing

    calls: list[str] = []
    payloads = {
        "file-a": ("draft-a.bin", "application/octet-stream", b"draft-a"),
        "file-b": (filename, content_type, data),
        "file-c": ("draft-c.bin", "application/octet-stream", b"draft-c"),
    }
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    # Exact-byte adoption does not depend on extraction support. Reconciliation
    # persistence is covered separately below.
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="generated",
        files=[
            _handle("file-a", *payloads["file-a"][:2]),
            _handle("file-b", filename, content_type),
            _handle("file-c", *payloads["file-c"][:2]),
        ],
        adoption=_adoption(f"cross-media:{filename}"),
    )

    assert calls == ["file-b"]
    assert [row["outcome"] for row in result["files"]] == [
        "unselected",
        "stored",
        "unselected",
    ]
    receipt = _receipt_from_result(result)
    digest = hashlib.sha256(data).hexdigest()
    assert receipt == {
        "version": 1,
        "committed": True,
        "key_digest": hashlib.sha256(f"cross-media:{filename}".encode()).hexdigest(),
        "trigger": "selected",
        "selected_file_id": "file-b",
        "lane": "evidence",
        "destination": "Knowledge Base/Evidence/case/generated",
        "stored_path": receipt["stored_path"],
        "page_path": receipt["page_path"],
        "hash_algorithm": "sha256",
        "hash": digest,
        "size": len(data),
        "content_type": content_type,
        "media_id": f"sha256:{digest}",
    }
    assert (vault / receipt["stored_path"]).read_bytes() == data
    assert not (vault / receipt["stored_path"]).with_name("draft-a.bin").exists()
    assert not (vault / receipt["stored_path"]).with_name("draft-c.bin").exists()
    page = (vault / receipt["page_path"]).read_text(encoding="utf-8")
    frontmatter, _body, marker = parse_frontmatter(page, strict=True)
    assert marker is not None
    assert frontmatter["artifact_adoption"] == receipt


def test_source_and_evidence_use_one_receipt_vocabulary_with_lane_specific_paths(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"same offered bytes\x00in both semantic lanes"
    payloads = {
        "source-file": ("reasoning.bin", "application/octet-stream", data),
        "evidence-file": ("deliverable.bin", "application/octet-stream", data),
    }
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)

    source = client_artifacts.capture_source_artifacts(
        vault,
        source_schema=source_schema,
        title="Reasoning input",
        files=[_handle("source-file", "reasoning.bin", "application/octet-stream")],
        adoption=_adoption("source-key", selected="source-file", trigger="approved"),
    )
    evidence = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[_handle("evidence-file", "deliverable.bin", "application/octet-stream")],
        adoption=_adoption("evidence-key", selected="evidence-file", trigger="approved"),
    )

    source_receipt = _receipt_from_result(source, "source-file")
    evidence_receipt = _receipt_from_result(evidence, "evidence-file")
    assert source_receipt.keys() == evidence_receipt.keys()
    assert source_receipt["lane"] == "source"
    assert evidence_receipt["lane"] == "evidence"
    assert source_receipt["destination"] == "Knowledge Base/Sources/Other"
    assert evidence_receipt["destination"] == "Knowledge Base/Evidence/case/outputs"
    assert source_receipt["stored_path"].startswith("Knowledge Base/Sources/")
    assert source_receipt["page_path"].startswith("Knowledge Base/Sources/")
    assert evidence_receipt["stored_path"].startswith("Knowledge Base/Evidence/")
    assert evidence_receipt["page_path"].startswith("Knowledge Base/Evidence/")
    for field in ("trigger", "hash_algorithm", "hash", "size", "content_type", "media_id"):
        assert source_receipt[field] == evidence_receipt[field]


def test_durable_replay_restages_exact_bytes_and_never_rewrites(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing, preserve

    data = b"the selected original bytes"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    calls: list[str] = []
    writes: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    original = preserve.batch_atomic_write

    def count_writes(planned, **kwargs):
        writes.append("canonical")
        return original(planned, **kwargs)

    monkeypatch.setattr(preserve, "batch_atomic_write", count_writes)
    kwargs = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
        "adoption": _adoption("durable-key"),
    }

    first = client_artifacts.preserve_artifacts(vault, **kwargs)
    replay = client_artifacts.preserve_artifacts(vault, **kwargs)

    assert calls == ["file-b", "file-b"], "durable replay must re-stage after transport replay"
    assert writes == ["canonical"]
    assert first["files"][0]["outcome"] == "stored"
    assert replay["files"][0]["outcome"] == "replayed"
    assert replay["files"][0]["adoption"] == first["files"][0]["adoption"]


def test_source_durable_replay_restages_and_returns_its_original_page_receipt(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import add, client_artifacts

    data = b"durable source bytes"
    payloads = {"file-b": ("reasoning.bin", "application/octet-stream", data)}
    calls: list[str] = []
    writes: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    original = add.batch_atomic_write

    def count_writes(planned, **kwargs):
        writes.append("canonical")
        return original(planned, **kwargs)

    monkeypatch.setattr(add, "batch_atomic_write", count_writes)
    kwargs = {
        "source_schema": source_schema,
        "title": "Durable reasoning input",
        "files": [_handle("file-b", "reasoning.bin", "application/octet-stream")],
        "adoption": _adoption("durable-source-key"),
    }

    first = client_artifacts.capture_source_artifacts(vault, **kwargs)
    replay = client_artifacts.capture_source_artifacts(vault, **kwargs)

    assert calls == ["file-b", "file-b"]
    assert writes == ["canonical"]
    assert replay["files"][0]["outcome"] == "replayed"
    assert replay["files"][0]["adoption"] == first["files"][0]["adoption"]
    assert replay["files"][0]["page"] == first["files"][0]["adoption"]["page_path"]


def test_durable_replay_fails_closed_for_changed_bytes_and_expired_handles(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    payloads = {"file-b": ("selected.txt", "text/plain", "Résumé\n".encode())}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    kwargs = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.txt", "text/plain")],
        "adoption": _adoption("durable-key"),
    }
    committed = client_artifacts.preserve_artifacts(vault, **kwargs)
    original_receipt = committed["files"][0]["adoption"]

    payloads["file-b"] = ("selected.txt", "text/plain", b"Resume\n")
    changed = client_artifacts.preserve_artifacts(vault, **kwargs)
    assert changed["files"][0]["code"] == "ADOPTION_KEY_REUSED"
    assert changed["files"][0]["outcome"] == "failed"

    def expired(*_args, **_kwargs):
        raise client_artifacts.SafeFetchError("SAFE_FETCH_FAILED", "download expired")

    monkeypatch.setattr(client_artifacts, "stage_artifact", expired)
    unavailable = client_artifacts.preserve_artifacts(vault, **kwargs)
    assert unavailable["files"][0]["code"] == "ADOPTION_REPLAY_UNVERIFIABLE"
    assert unavailable["files"][0]["outcome"] == "failed"
    page = vault / original_receipt["page_path"]
    frontmatter, _body, _marker = parse_frontmatter(page.read_text(encoding="utf-8"), strict=True)
    assert frontmatter["artifact_adoption"] == original_receipt


def test_durable_replay_compares_content_type_even_when_bytes_and_id_match(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"same bytes and selected identifier"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    kwargs = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
        "adoption": _adoption("content-type-key"),
    }
    client_artifacts.preserve_artifacts(vault, **kwargs)
    payloads["file-b"] = ("selected.bin", "application/x-changed", data)

    result = client_artifacts.preserve_artifacts(vault, **kwargs)

    assert result["files"][0]["code"] == "ADOPTION_KEY_REUSED"


@pytest.mark.parametrize("mutation", ("missing", "changed"))
def test_invalid_stored_media_identity_is_never_accepted_as_durable_replay(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"receipt completeness matters"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    kwargs = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
        "adoption": _adoption("incomplete-receipt-key"),
    }
    committed = client_artifacts.preserve_artifacts(vault, **kwargs)
    page = vault / committed["files"][0]["adoption"]["page_path"]
    media_line = f'  media_id: "sha256:{hashlib.sha256(data).hexdigest()}"\n'
    replacement = "" if mutation == "missing" else f'  media_id: "sha256:{"0" * 64}"\n'
    page.write_text(
        page.read_text(encoding="utf-8").replace(media_line, replacement),
        encoding="utf-8",
    )
    calls.clear()

    result = client_artifacts.preserve_artifacts(vault, **kwargs)

    assert calls == [], "an invalid same-key receipt can fail before retrieval"
    assert result["files"][0]["code"] == "ADOPTION_KEY_REUSED"


@pytest.mark.parametrize(
    ("changed", "files"),
    (
        (_adoption("durable-key", selected="file-c"), [_handle("file-c", "c.bin", "application/octet-stream")]),
        (_adoption("durable-key", trigger="published"), [_handle("file-b", "b.bin", "application/octet-stream")]),
    ),
)
def test_reused_key_with_changed_request_identity_fails_before_retrieval(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: dict[str, str],
    files: list[dict[str, str]],
) -> None:
    from exomem import client_artifacts, media_processing

    payloads = {
        "file-b": ("b.bin", "application/octet-stream", b"selected"),
        "file-c": ("c.bin", "application/octet-stream", b"sibling"),
    }
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[_handle("file-b", "b.bin", "application/octet-stream")],
        adoption=_adoption("durable-key"),
    )
    calls.clear()

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=files,
        adoption=changed,
    )

    assert calls == []
    assert next(row for row in result["files"] if row["file_id"] == changed["selected_file_id"])[
        "code"
    ] == "ADOPTION_KEY_REUSED"


def test_reused_key_cannot_change_destination_or_lane_before_retrieval(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    payloads = {"file-b": ("b.bin", "application/octet-stream", b"selected")}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    handle = _handle("file-b", "b.bin", "application/octet-stream")
    client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[handle],
        adoption=_adoption("durable-key"),
    )
    calls.clear()

    destination = client_artifacts.preserve_artifacts(
        vault,
        scope="another-case",
        category="outputs",
        files=[handle],
        adoption=_adoption("durable-key"),
    )
    lane = client_artifacts.capture_source_artifacts(
        vault,
        source_schema=source_schema,
        title="Wrong lane",
        files=[handle],
        adoption=_adoption("durable-key"),
    )

    assert calls == []
    assert destination["files"][0]["code"] == "ADOPTION_KEY_REUSED"
    assert lane["files"][0]["code"] == "ADOPTION_KEY_REUSED"


def test_source_destination_change_is_bound_to_the_resolved_source_folder(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts

    payloads = {"file-b": ("b.bin", "application/octet-stream", b"selected")}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    handle = _handle("file-b", "b.bin", "application/octet-stream")
    client_artifacts.capture_source_artifacts(
        vault,
        source_schema=source_schema,
        title="Session input",
        source_type="session",
        files=[handle],
        adoption=_adoption("source-destination-key"),
    )
    calls.clear()

    result = client_artifacts.capture_source_artifacts(
        vault,
        source_schema=source_schema,
        title="Paper input",
        source_type="paper",
        files=[handle],
        adoption=_adoption("source-destination-key"),
    )

    assert calls == []
    assert result["files"][0]["code"] == "ADOPTION_KEY_REUSED"


def test_first_time_fetch_failure_keeps_safe_fetch_code_and_siblings_stay_unselected(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts

    calls: list[str] = []

    def fail(file, *_args, **_kwargs):
        calls.append(str(file["file_id"]))
        raise client_artifacts.SafeFetchError("SAFE_FETCH_FAILED", "download expired")

    monkeypatch.setattr(client_artifacts, "stage_artifact", fail)
    files = [
        _handle("file-a", "a.bin", "application/octet-stream"),
        _handle("file-b", "b.bin", "application/octet-stream"),
        _handle("file-c", "c.bin", "application/octet-stream"),
    ]

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=files,
        adoption=_adoption("first-attempt"),
    )

    assert calls == ["file-b"]
    assert [row["outcome"] for row in result["files"]] == [
        "unselected",
        "failed",
        "unselected",
    ]
    assert result["files"][1]["code"] == "SAFE_FETCH_FAILED"


@pytest.mark.parametrize("lane", ("source", "evidence"))
def test_artifact_and_adoption_receipt_are_one_atomic_write_set(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    from exomem import add, client_artifacts, media_processing, preserve

    data = b"must not become visible"
    payloads = {"file-b": ("atomic.bin", "application/octet-stream", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)

    def fail_publication(*_args, **_kwargs):
        if lane == "source":
            source_tree = vault / "Knowledge Base" / "Sources"
            assert not [
                path for path in source_tree.rglob("*.bin") if "atomic-source" in path.name
            ], (
                "Source bytes became visible before the held receipt publication"
            )
        raise RuntimeError("fault before held publication")

    if lane == "source":
        monkeypatch.setattr(add, "batch_atomic_write", fail_publication)

        def invoke():
            return client_artifacts.capture_source_artifacts(
                vault,
                source_schema=source_schema,
                title="Atomic Source",
                files=[_handle("file-b", "atomic.bin", "application/octet-stream")],
                adoption=_adoption("atomic-source"),
            )

        tree = vault / "Knowledge Base" / "Sources"
    else:
        monkeypatch.setattr(preserve, "batch_atomic_write", fail_publication)

        def invoke():
            return client_artifacts.preserve_artifacts(
                vault,
                scope="atomic-case",
                category="outputs",
                files=[_handle("file-b", "atomic.bin", "application/octet-stream")],
                adoption=_adoption("atomic-evidence"),
            )

        tree = vault / "Knowledge Base" / "Evidence" / "atomic-case"

    with pytest.raises(RuntimeError, match="fault before held publication"):
        invoke()

    assert not list(tree.rglob("atomic.bin")) if tree.exists() else True
    if tree.exists():
        assert all("artifact_adoption:" not in path.read_text(encoding="utf-8") for path in tree.rglob("*.md"))


def test_media_warning_cannot_erase_committed_adoption_receipt(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"\x89PNG\r\n\x1a\ncommitted"
    payloads = {"file-b": ("warning.png", "image/png", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: "image")
    monkeypatch.setattr(
        media_processing,
        "reconcile_media",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("worker unavailable")),
    )

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[_handle("file-b", "warning.png", "image/png")],
        adoption=_adoption("warning-key"),
    )

    row = result["files"][0]
    assert row["outcome"] == "stored"
    assert "media reconciliation failed" in row["warnings"][0]
    receipt = row["adoption"]
    frontmatter, _body, _marker = parse_frontmatter(
        (vault / receipt["page_path"]).read_text(encoding="utf-8"), strict=True
    )
    assert frontmatter["artifact_adoption"] == receipt


def test_media_pending_rerender_preserves_the_committed_adoption_block(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"\x89PNG\r\n\x1a\nrerender"
    payloads = {"file-b": ("rerender.png", "image/png", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    # Leave the initial canonical stub untouched so this test can drive the
    # explicit rebuild branch directly.
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[_handle("file-b", "rerender.png", "image/png")],
        adoption=_adoption("rerender-key"),
    )
    receipt = result["files"][0]["adoption"]
    binary = vault / receipt["stored_path"]
    original = (vault / receipt["page_path"]).read_text(encoding="utf-8")
    # Invalidating a capture-owned field forces the full pending renderer rather
    # than the in-place field updater; the receipt must survive either path.
    original = original.replace("source_type: other", "source_type: ''")
    stat = binary.stat()
    provenance = media_processing._BinaryProvenance(
        relative_path=receipt["stored_path"],
        original_filename=binary.name,
        sha256=receipt["hash"],
        size=receipt["size"],
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
        device=stat.st_dev,
        inode=stat.st_ino,
    )

    rendered = media_processing._render_pending_sidecar(
        binary=binary,
        media_type="image",
        provenance=provenance,
        original=original,
    )

    frontmatter, _body, _marker = parse_frontmatter(rendered, strict=True)
    assert frontmatter["artifact_adoption"] == receipt


def test_ordinary_calls_render_no_adoption_block_and_keep_existing_response_shape(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"ordinary"
    payloads = {"file-one": ("ordinary.bin", "application/octet-stream", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="ordinary",
        files=[_handle("file-one", "ordinary.bin", "application/octet-stream")],
    )

    assert result["summary"] == {"stored": 1, "failed": 0}
    assert "adoption" not in result["files"][0]
    page = vault / f"{result['files'][0]['stored_path']}.md"
    assert "artifact_adoption:" not in page.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "adoption",
    (
        {},
        {"key": "key", "trigger": "selected"},
        {"key": "key", "trigger": "selected", "selected_file_id": "file-b", "extra": True},
    ),
)
def test_invalid_adoption_envelope_never_fetches(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    adoption: dict,
) -> None:
    from exomem import client_artifacts

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args, **_kwargs: pytest.fail("invalid adoption must not fetch"),
    )
    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[_handle("file-b", "b.bin", "application/octet-stream")],
        adoption=adoption,
    )

    assert result["files"][0]["code"] == "INVALID_ADOPTION"


def test_selected_identifier_must_match_exactly_one_handle_without_fetching(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args, **_kwargs: pytest.fail("ambiguous selection must not fetch"),
    )
    duplicate = _handle("file-b", "b.bin", "application/octet-stream")
    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[duplicate, dict(duplicate)],
        adoption=_adoption("ambiguous"),
    )

    assert all(row["code"] == "INVALID_ADOPTION" for row in result["files"])


def test_staged_result_cannot_swap_the_selected_identifier(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts

    staged = tmp_path / "wrong-variant.bin"
    staged.write_bytes(b"variant-c")

    def swap_selected(*_args, **_kwargs):
        return client_artifacts.StagedArtifact(
            file_id="file-c",
            path=staged,
            size=len(b"variant-c"),
            sha256=hashlib.sha256(b"variant-c").hexdigest(),
            content_type="application/octet-stream",
            filename="variant-c.bin",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", swap_selected)
    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[
            _handle("file-a", "a.bin", "application/octet-stream"),
            _handle("file-b", "b.bin", "application/octet-stream"),
            _handle("file-c", "c.bin", "application/octet-stream"),
        ],
        adoption=_adoption("swap-selected-key"),
    )

    selected = next(row for row in result["files"] if row["file_id"] == "file-b")
    assert selected["code"] == "INVALID_FILE"
    assert not list((vault / "Knowledge Base" / "Evidence" / "case").rglob("variant-c.bin"))


@pytest.mark.parametrize("mutation", ("missing", "same-size", "changed-size", "symlink"))
def test_durable_replay_requires_the_canonical_stored_bytes_no_follow(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"canonical custody bytes"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    kwargs = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
        "adoption": _adoption(f"canonical-custody-{mutation}"),
    }
    committed = client_artifacts.preserve_artifacts(vault, **kwargs)
    receipt = _receipt_from_result(committed)
    stored = vault / receipt["stored_path"]
    if mutation == "missing":
        stored.unlink()
    elif mutation == "same-size":
        stored.write_bytes(b"X" * len(data))
    elif mutation == "changed-size":
        stored.write_bytes(data + b"-changed")
    else:
        external = tmp_path / "external-selected.bin"
        external.write_bytes(data)
        stored.unlink()
        try:
            stored.symlink_to(external)
        except OSError:
            pytest.skip("symlinks are unavailable")

    replay = client_artifacts.preserve_artifacts(vault, **kwargs)

    selected = next(row for row in replay["files"] if row["file_id"] == "file-b")
    assert selected["outcome"] == "failed"
    assert selected["code"] == "ADOPTION_REPLAY_UNVERIFIABLE"
    assert calls == ["file-b"], "invalid canonical custody must fail before remote restaging"


@pytest.mark.parametrize("lane", ("source", "evidence"))
def test_receipt_path_must_be_the_lane_specific_canonical_companion_pair(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"canonical pairing matters"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    common = {
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
        "adoption": _adoption(f"canonical-pair-{lane}"),
    }
    if lane == "source":
        invoke = lambda: client_artifacts.capture_source_artifacts(  # noqa: E731
            vault,
            source_schema=source_schema,
            title="Canonical pair",
            **common,
        )
    else:
        invoke = lambda: client_artifacts.preserve_artifacts(  # noqa: E731
            vault,
            scope="case",
            category="outputs",
            **common,
        )
    committed = invoke()
    receipt = _receipt_from_result(committed)
    page = vault / receipt["page_path"]
    forged = (vault / receipt["stored_path"]).with_name("forged.bin")
    forged.write_bytes(data)
    _replace_receipt_scalar(page, "stored_path", receipt["stored_path"], forged.relative_to(vault).as_posix())

    replay = invoke()

    assert replay["files"][0]["outcome"] == "failed"
    assert replay["files"][0]["code"] == "ADOPTION_KEY_REUSED"


def test_windows_style_receipt_traversal_is_rejected_before_retrieval(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"portable path custody"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    kwargs = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
        "adoption": _adoption("windows-traversal"),
    }
    committed = client_artifacts.preserve_artifacts(vault, **kwargs)
    receipt = _receipt_from_result(committed)
    page = vault / receipt["page_path"]
    unsafe = f'{receipt["destination"]}/..\\..\\secrets.txt'
    _replace_receipt_scalar(page, "stored_path", receipt["stored_path"], unsafe)
    calls.clear()

    replay = client_artifacts.preserve_artifacts(vault, **kwargs)

    assert replay["files"][0]["code"] == "ADOPTION_KEY_REUSED"
    assert calls == []


def test_noncanonical_matching_markdown_cannot_claim_an_adoption_key(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing, preserve

    key = "noncanonical-key"
    data = b"selected canonical bytes"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    fake_artifact = notes / "fake.bin"
    fake_artifact.write_bytes(data)
    fake_page = notes / "fake.bin.md"
    digest = hashlib.sha256(data).hexdigest()
    fake_receipt = {
        "version": 1,
        "committed": True,
        "key_digest": hashlib.sha256(key.encode()).hexdigest(),
        "trigger": "selected",
        "selected_file_id": "file-b",
        "lane": "evidence",
        "destination": "Knowledge Base/Notes",
        "stored_path": "Knowledge Base/Notes/fake.bin",
        "page_path": "Knowledge Base/Notes/fake.bin.md",
        "hash_algorithm": "sha256",
        "hash": digest,
        "size": len(data),
        "content_type": "application/octet-stream",
        "media_id": f"sha256:{digest}",
    }
    fake_page.write_text(
        "---\ntype: insight\n" + "\n".join(preserve._render_adoption_receipt_lines(fake_receipt)) + "\n---\n",
        encoding="utf-8",
    )

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[_handle("file-b", "selected.bin", "application/octet-stream")],
        adoption=_adoption(key),
    )

    assert result["files"][0]["outcome"] == "stored"


def test_legacy_body_prose_cannot_poison_frontmatter_receipt_discovery(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    key = "legacy-body-key"
    data = b"selected bytes"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    legacy = vault / "Knowledge Base" / "Evidence" / "legacy.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\ntitle: [legacy malformed frontmatter\n---\n\n"
        "Body prose only:\nartifact_adoption:\n"
        f"  key_digest: {hashlib.sha256(key.encode()).hexdigest()}\n",
        encoding="utf-8",
    )

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="outputs",
        files=[_handle("file-b", "selected.bin", "application/octet-stream")],
        adoption=_adoption(key),
    )

    assert result["files"][0]["outcome"] == "stored"


@pytest.mark.parametrize(
    "field",
    ("trigger", "selected_file_id", "content_type", "destination"),
)
def test_receipt_string_identities_round_trip_yaml_line_characters_losslessly(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from exomem import client_artifacts, media_processing

    selected = "file-b" if field != "selected_file_id" else "file\u2028b"
    trigger = "selected" if field != "trigger" else "sel\u0085ected"
    content_type = "application/octet-stream" if field != "content_type" else "application/x-\u007f"
    scope = "case" if field != "destination" else "case\u2029name"
    data = f"lossless-{field}".encode()
    payloads = {selected: ("selected.bin", content_type, data)}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    kwargs = {
        "scope": scope,
        "category": "outputs",
        "files": [_handle(selected, "selected.bin", content_type)],
        "adoption": _adoption(f"lossless-{field}", selected=selected, trigger=trigger),
    }

    committed = client_artifacts.preserve_artifacts(vault, **kwargs)
    if field == "destination":
        assert committed["files"][0]["outcome"] == "failed"
        assert committed["files"][0]["code"] == "INVALID_PRESERVE"
        assert calls == []
        return
    replay = client_artifacts.preserve_artifacts(vault, **kwargs)

    assert replay["files"][0]["outcome"] == "replayed"
    assert replay["files"][0]["adoption"] == committed["files"][0]["adoption"]


def test_invalid_evidence_destination_only_fails_the_selected_handle(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args, **_kwargs: pytest.fail("invalid destination must not fetch"),
    )
    result = client_artifacts.preserve_artifacts(
        vault,
        scope="..",
        category="outputs",
        files=[
            _handle("file-a", "a.bin", "application/octet-stream"),
            _handle("file-b", "b.bin", "application/octet-stream"),
            _handle("file-c", "c.bin", "application/octet-stream"),
        ],
        adoption=_adoption("invalid-destination"),
    )

    assert [row["outcome"] for row in result["files"]] == [
        "unselected",
        "failed",
        "unselected",
    ]


def test_adoption_key_surrounding_whitespace_is_not_an_identity_alias(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"opaque key bytes"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    calls: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, calls))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    base = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
    }
    first = client_artifacts.preserve_artifacts(vault, adoption=_adoption("opaque"), **base)
    aliased = client_artifacts.preserve_artifacts(vault, adoption=_adoption(" opaque "), **base)

    assert first["files"][0]["outcome"] == "stored"
    assert aliased["files"][0]["outcome"] == "failed"
    assert aliased["files"][0]["code"] == "INVALID_ADOPTION"
    assert calls == ["file-b"]


def test_durable_receipt_page_is_not_read_through_path_read_text(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, media_processing

    data = b"guarded companion bytes"
    payloads = {"file-b": ("selected.bin", "application/octet-stream", data)}
    monkeypatch.setattr(client_artifacts, "stage_artifact", _stage_factory(tmp_path, payloads, []))
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    kwargs = {
        "scope": "case",
        "category": "outputs",
        "files": [_handle("file-b", "selected.bin", "application/octet-stream")],
        "adoption": _adoption("guarded-companion"),
    }
    committed = client_artifacts.preserve_artifacts(vault, **kwargs)
    receipt_page = vault / _receipt_from_result(committed)["page_path"]
    original_read_text = Path.read_text

    def refuse_plain_receipt_read(self: Path, *args, **kwargs):
        if self == receipt_page:
            raise AssertionError("receipt lookup must use a guarded canonical read")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse_plain_receipt_read)

    replay = client_artifacts.preserve_artifacts(vault, **kwargs)

    assert replay["files"][0]["outcome"] == "replayed"
