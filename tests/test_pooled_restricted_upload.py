"""A restricted writer's file upload tells it nothing about originals it cannot read.

Every archive here is disposable. The writer may write its own inbox and reads the unclassified
pages; an owner-only filing is withheld from it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURE_VAULT, initialize_vault_state_offline
from granted_writer import grant_writer

from exomem import add as add_module
from exomem import (
    artifact_lifecycle,
    commands,
    duplicates,
    held_fs,
    held_uploads,
    lexstore,
    media_store,
    original_pool,
    preserve,
    writer_lease,
)
from exomem import schema as schema_module
from exomem.governance import connector_boundary, principal
from exomem.kbdir import kb_dirname

HIDDEN_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FC1"
INBOX = "Knowledge Base/Sources/Inbox/writer"
# The same length, so both archives need the same capacity for the upload.
UPLOAD = b"\x89PNG\r\n\x1a\n" + b"a" * 700
OTHER = b"\x89PNG\r\n\x1a\n" + b"b" * 700


def _archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, *, pooled: bool,
             hidden: bytes | None = None, earlier: bytes | None = None):
    """One disposable governed archive, its restricted writer and the pages the owner filed.

    `hidden` is filed twice where the writer cannot read it: owner-only Evidence, and a Source that the
    governance withholds. `earlier` is filed as a Source the writer can read.
    """

    def file_source(title: str, data: bytes) -> str:
        stage = tmp_path / f"{name}-stage"
        stage.mkdir(exist_ok=True)
        (stage / "upload.png").write_bytes(data)
        return add_module.add(
            vault, schema_module.load_source_schema(vault), content="", title=title, source_type="session",
            today=dt.date(2026, 11, 15),
            artifact=add_module.SourceArtifact(staged_path=stage / "upload.png", filename="upload.png")).path

    vault = tmp_path / name
    monkeypatch.delenv(connector_boundary.CONFIG_ENV, raising=False)  # An earlier archive's host configuration.
    # The schema and indexes that a capture reads, without the fixture's pages, which would slow every index build.
    shutil.copytree(FIXTURE_VAULT / kb_dirname() / "_Schema", vault / kb_dirname() / "_Schema")
    (vault / kb_dirname() / "Sources").mkdir()
    for relative in ("index.md", "log.md", "Sources/index.md"):
        shutil.copy(FIXTURE_VAULT / kb_dirname() / relative, vault / kb_dirname() / relative)
    initialize_vault_state_offline(vault, source="restricted writer archive")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    if pooled:
        original_pool.enable(vault)
    filed = []
    with principal.library_scope():
        if hidden is not None:
            filed.append(preserve.preserve_bytes(vault, scope="private", category="raw", filename="hidden.png",
                                                 data=hidden, raw_protection=True).sidecar_path)
            filed.append(file_source("Withheld capture", hidden))
        if earlier is not None:
            filed.append(file_source("Earlier capture", earlier))
    assert lexstore.get_store(vault).rebuild_atomic()
    protected = []
    if hidden is not None:
        scopes = vault / "Knowledge Base/_Governance/scopes"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "hidden.yaml").write_text(
            f'governance_version: 1\nid: {HIDDEN_SCOPE}\npaths: ["Evidence/private/**", "Sources/Sessions/**"]\n',
            encoding="utf-8")
        protected = [HIDDEN_SCOPE]
    config_dir = tmp_path / f"{name}-host"
    config_dir.mkdir()
    writer = grant_writer(vault, tmp_path, monkeypatch, writable_paths=["Sources/Inbox/writer/**"],
                          capture_paths=[INBOX], protected=protected, config_dir=config_dir)("external")
    # A verified session of its own: a hold is bound to the client session that sent it.
    return vault, dataclasses.replace(writer, issuer_family="mcp-oauth:synthetic"), filed


def _capture(vault: Path, writer, held: dict):
    with principal.request_scope(writer):
        return writer_lease.invoke_command(
            next(command for command in commands.PRODUCT_COMMANDS if command.name == "capture_source"),
            vault, schema_module.load_source_schema(vault), title="Upload", source_kind="session",
            files=[held["file"]], idempotency_key="upload")


def _hold(vault: Path, writer, data: bytes) -> dict:
    with principal.request_scope(writer):
        return held_uploads.hold(vault, io.BytesIO(data), lane="source", filename="upload.png",
                                 content_type="image/png", max_bytes=len(data))


@pytest.mark.timeout(180)  # Two governed archives, each with several durable commits, on a shared disk.
def test_a_restricted_upload_cannot_tell_a_hidden_pool_match_from_a_miss(tmp_path, monkeypatch) -> None:
    """A writer who cannot read an owner-only filing must not learn that its bytes were already pooled.

    The two archives differ only in what the owner hid, so any difference in the writer's admission, session charge,
    staging or receipt would answer whether the writer's bytes were already in the archive.
    """
    staged: list[int] = []
    prepare = original_pool.prepare

    def counting_prepare(vault, digest, size, data, **kwargs):
        def chunks():
            for chunk in data.open():
                staged.append(len(chunk))
                yield chunk

        return prepare(vault, digest, size, held_fs.Chunks(chunks), **kwargs)

    def admission(vault: Path, allowance: int) -> str | None:
        config = tmp_path / f"media-{vault.name}.json"
        config.write_text(json.dumps({"vaults": {str(vault.resolve()): {
            "store": "synthetic", "enabled": True, "allowance_bytes": allowance}}}))
        monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
        try:
            media_store.take_census(vault)
            return media_store.upload_refusal(vault, len(UPLOAD), "upload.png", "source")
        finally:
            monkeypatch.delenv("EXOMEM_MEDIA_STORE_CONFIG")

    seen = {}
    for name, hidden in (("hit", UPLOAD), ("miss", OTHER)):
        vault, writer, filed = _archive(tmp_path, monkeypatch, name, pooled=True, hidden=hidden)
        with principal.request_scope(writer):
            assert len(filed) == 2 and all(duplicates.withheld(vault, page) for page in filed)
        monkeypatch.setattr(original_pool, "prepare", counting_prepare)
        staged.clear()
        refused = admission(vault, len(UPLOAD) - 1)
        admitted = admission(vault, 10 * len(UPLOAD))
        held = _hold(vault, writer, UPLOAD)
        with pytest.raises(held_uploads.HeldUploadError) as full:
            _hold(vault, writer, UPLOAD)
        receipt = _capture(vault, writer, held)
        monkeypatch.setattr(original_pool, "prepare", prepare)
        row = receipt["files"][0]
        row.pop("file_id"), row.pop("ref")  # Generated per hold and per page.
        seen[name] = {
            "refused": refused, "admitted": admitted, "second hold": full.value.code,
            "staged bytes": sum(staged), "receipt": receipt,
            "pool objects": len(list(original_pool.pool_root(vault).rglob("*.blob"))),
        }

    assert seen["hit"]["pool objects"] == 1 and seen["miss"]["pool objects"] == 2  # The hit really shared an object.
    assert seen["hit"]["refused"] is not None and seen["hit"]["admitted"] is None
    assert seen["hit"]["staged bytes"] == len(UPLOAD)
    assert seen["hit"]["receipt"]["files"][0]["outcome"] == "stored"
    for key in ("refused", "admitted", "second hold", "staged bytes", "receipt"):
        assert seen["hit"][key] == seen["miss"][key], key
    revealed = json.dumps(seen["hit"]["receipt"]).lower()
    for hidden_fact in ("private", "hidden", "_originals", ".blob", "already", "duplicate"):
        assert hidden_fact not in revealed


def test_a_restricted_upload_of_bytes_it_can_read_is_answered_with_the_existing_filing_in_either_layout(
        tmp_path, monkeypatch) -> None:
    """Pooling must not change logical reuse: the writer is told about the filing it can read, as before pooling."""
    answers = {}
    for name, pooled in (("ordinary", False), ("pooled", True)):
        vault, writer, filed = _archive(tmp_path, monkeypatch, name, pooled=pooled, earlier=UPLOAD)
        receipt = _capture(vault, writer, _hold(vault, writer, UPLOAD))
        row = receipt["files"][0]
        assert row.get("state") == "already_stored"
        assert row["duplicate_of"]["path"] == filed[0].removesuffix(".md")
        row.pop("file_id"), row.pop("ref"), row["duplicate_of"].pop("ref")  # Generated per hold and per page.
        answers[name] = {"receipt": receipt, "inbox": sorted(path.name for path in (vault / INBOX).rglob("*.md"))}

    assert answers["pooled"] == answers["ordinary"]
    assert answers["pooled"]["inbox"] == []  # The upload filed nothing new.
