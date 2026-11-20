"""One shared original object, held by independently governed filings, stays correct across its custody life."""

from __future__ import annotations

import contextlib
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_attachment_custody import _local_session
from test_dataset_value_search import _as_external
from test_media_store import _retained, selected_store  # noqa: F401
from test_pooled_export_fixity import _pool, _restore
from test_pooled_move_trash import _filing
from test_shared_originals import _pooled_capture
from test_vault_portability import _context

from exomem import (
    artifact_recovery,
    commands,
    delete_file,
    held_uploads,
    media_bindings,
    media_store,
    original_pool,
    originals,
    preserve,
    reserved_paths,
    state_migration,
    vault_portability,
)
from exomem.governance import egress, raw_protection
from exomem.governance.principal import (
    RequestPrincipal,
    library_scope,
    owner_principal,
    request_scope,
)
from exomem.writer_lease import invoke_command

PAYROLL = b"item,value\nreserve,4.2\nreview,thursday\n"
LEDGER = b"entry,amount\nrent,1200\nfuel,85\n"  # a second object, which the census counts once for two filings
GRANTS = ("01ARZ3NDEKTSV4RRFFQ69G5FC0", "01ARZ3NDEKTSV4RRFFQ69G5FC1")


def _stream(vault: Path, path: str, who: RequestPrincipal) -> bytes:
    """What a download streams for `who`, through the service's own recovery path."""
    with artifact_recovery.stage(vault, path, who) as (held, descriptor):
        return reserved_paths.read_held(held, 0, descriptor["size"])


def _refusals(vault: Path, path: str, who: RequestPrincipal) -> list[str]:
    """The refusal of each request `who` makes for a protected filing (stream, metadata, rows), by its name."""
    calls = (lambda: _stream(vault, path, who),
             lambda: commands.op_read_memory(vault, path=path + ".md", frontmatter_only=True),
             lambda: commands.op_query_dataset(vault, path=path))
    texts = []
    with request_scope(who):
        for call in calls:
            with pytest.raises(ValueError) as refused:
                call()
            texts.append(str(refused.value).replace(path, "<path>"))
    assert not [text for text in texts if "reserve" in text]
    return texts


def _govern(vault: Path, **arguments) -> None:
    govern = next(command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory")
    with request_scope(owner_principal(surface="library")):
        assert invoke_command(govern, vault, scope="standing", **arguments)["diagnostics"]["operational"]["ok"]


def _grant(vault: Path, grant_id: str, path: str, reader: RequestPrincipal) -> None:
    _govern(vault, operation="grant", grant_id=grant_id, path=path, audience=reader.audience_id,
            raw_release={"version": 1, "surface": reader.surface, "issuer_family": reader.issuer_family,
                         "purpose": None, "includes_location": True})


def _shared_filings(vault: Path, tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """Two protected datasets, an ordinary Source and an ordinary Notes attachment that hold the same bytes."""
    with library_scope():
        d1 = preserve.preserve_stream(vault, scope="work", category="datasets", filename="payroll-a.csv",
                                      stream=io.BytesIO(PAYROLL), raw_protection=True)
        d2 = preserve.preserve_stream(vault, scope="work", category="datasets", filename="payroll-b.csv",
                                      stream=io.BytesIO(PAYROLL), raw_protection=True)
    source = _pooled_capture(vault, tmp_path, monkeypatch, "payroll.csv", PAYROLL)
    # Datasets and Notes attachments stay ordinary files in every product path, so the tests of shared storage
    # join them to the Source's object by publishing their bytes and removing the file; this does the same.
    _pool(vault, monkeypatch, vault / d1.path)
    _pool(vault, monkeypatch, vault / d2.path)
    attachment, attachment_page = _filing(vault, "payroll.bin", PAYROLL)
    _pool(vault, monkeypatch, vault / attachment)
    return SimpleNamespace(source=source, d1=d1, d2=d2, attachment=attachment, attachment_page=attachment_page)


def test_independent_filings_of_one_object_survive_revocation_trash_interruption_and_cancellation(
        vault, tmp_path, monkeypatch):
    """Catches what no single shared-storage test covers: a grant revoked on one filing that closes or spares another
    filing of the same object, or a refusal that tells an ungranted reader the filing exists; and a Trash, an
    interrupted publication, a concurrent upload or a cancelled hold that costs a surviving filing its bytes."""
    owner = owner_principal(surface="library")
    reader = RequestPrincipal("external", surface="mcp", issuer_family="test-issuer")
    digest = hashlib.sha256(PAYROLL).hexdigest()
    shared = original_pool.object_path(vault, digest)

    def blobs() -> list[Path]:
        return sorted(path for path in (original_pool.pool_root(vault) / "sha256").rglob("*") if path.is_file())

    # 1. Two protected datasets, an ordinary Source and a Notes attachment hold the same bytes as one object.
    filed = _shared_filings(vault, tmp_path, monkeypatch)
    s1, d1, d2, attachment, attachment_page = filed.source, filed.d1, filed.d2, filed.attachment, filed.attachment_page
    filings = [s1.artifact_path, d1.path, d2.path, attachment]
    assert blobs() == [shared]
    assert not any((vault / path).exists() for path in filings)
    assert len({originals.original_state(vault, path).sidecar_path for path in filings}) == 4
    assert [raw_protection.marked(path) for path in filings] == [False, True, True, False]
    pages = {path: (vault / originals.original_state(vault, path).sidecar_path).read_bytes() for path in filings}

    # 2. The owner reads every filing; an ungranted reader reads the Source and is refused each protected dataset
    # in words that match a filing that was never made.
    _as_external(vault, selector='paths: ["**"]', ceiling=egress.LEVEL_FULL)
    with request_scope(owner):
        assert [_stream(vault, path, owner) for path in filings[:3]] == [PAYROLL] * 3
        assert commands.op_query_dataset(vault, path=d1.path)["rows"][0] == {"item": "reserve", "value": "4.2"}
    with request_scope(reader):
        assert _stream(vault, s1.artifact_path, reader) == PAYROLL
    for protected in (d1.path, d2.path):
        assert _refusals(vault, protected, reader) == _refusals(vault, protected.replace(".csv", "-unfiled.csv"), reader)

    # 3. A grant on each protected dataset opens it for the reader alone; revoking one closes only that dataset.
    _grant(vault, GRANTS[0], d1.path, reader)
    _grant(vault, GRANTS[1], d2.path, reader)
    with request_scope(reader):
        assert [_stream(vault, path, reader) for path in (d1.path, d2.path)] == [PAYROLL] * 2
    _govern(vault, operation="revoke", grant_id=GRANTS[0])
    assert _refusals(vault, d1.path, reader) == _refusals(vault, d1.path.replace(".csv", "-unfiled.csv"), reader)
    with request_scope(reader):
        assert [_stream(vault, path, reader) for path in (d2.path, s1.artifact_path)] == [PAYROLL] * 2
    # Trashing the ordinary attachment retires one binding; the other three still read and the object stays.
    with library_scope():
        delete_file.delete_file(vault, path=attachment_page, confirm=True)
    filings.remove(attachment)
    with request_scope(owner):
        assert [_stream(vault, path, owner) for path in filings] == [PAYROLL] * 3
    assert blobs() == [shared] and shared.read_bytes() == PAYROLL

    # 4. A third upload of the same bytes is staged while another upload of them loses power in publication.
    def sources() -> set[str]:
        return {path.name for path in (vault / "Knowledge Base" / "Sources").rglob("*.md")}

    sources_before = sources()
    real_prepare, real_publish = original_pool.prepare, original_pool.PreparedOriginal.publish
    third = []

    @contextlib.contextmanager
    def prepare_while_an_upload_is_interrupted(*args, **kwargs):
        with real_prepare(*args, **kwargs) as staged:
            if not third:
                third.append(staged)
                with pytest.raises(OSError, match="power lost"):
                    _pooled_capture(vault, tmp_path, monkeypatch, "payroll-interrupted.csv", PAYROLL)
            yield staged

    def publish(self):
        if self is not third[0]:
            raise OSError("power lost while publishing")
        real_publish(self)

    with monkeypatch.context() as injected:
        injected.setattr(original_pool, "prepare", prepare_while_an_upload_is_interrupted)
        injected.setattr(original_pool.PreparedOriginal, "publish", publish)
        with library_scope():
            s3 = _pooled_capture(vault, tmp_path, monkeypatch, "payroll-copy.csv", PAYROLL)
    assert sources() - sources_before == {Path(s3.path).name}  # the interrupted upload left no page or binding
    filings.append(s3.artifact_path)
    assert blobs() == [shared]
    with request_scope(owner):
        assert [_stream(vault, path, owner) for path in filings] == [PAYROLL] * 4

    # 5. A held upload of the same bytes fails in preservation, stays held, and is cancelled by its session.
    with _local_session("custody-session"):
        def hold() -> dict:
            return held_uploads.hold(vault, io.BytesIO(PAYROLL), lane="evidence", filename="payroll.txt",
                                     content_type="text/plain", max_bytes=len(PAYROLL))["file"]

        handle = hold()

        def lose_power(self) -> None:
            raise OSError("power lost while publishing")

        with monkeypatch.context() as injected, library_scope():
            injected.setattr(original_pool.PreparedOriginal, "publish", lose_power)
            with pytest.raises(OSError, match="power lost"):
                commands.op_preserve_artifacts(vault, scope="work", category="uploads", files=[handle])
        with pytest.raises(held_uploads.HeldUploadError) as full:
            hold()
        assert full.value.code == "HELD_UPLOAD_QUOTA"  # the failed upload's hold still counts against the session
        cancelled = commands.op_transfer_artifact(vault, operation="cancel", uploads=[handle["download_url"]])
        assert cancelled["uploads"] == [{"index": 0, "state": "cancelled", "released_bytes": len(PAYROLL)}]
        with pytest.raises(held_uploads.HeldUploadError) as gone:
            held_uploads.redeem(vault, handle["download_url"], lane="evidence")
        assert gone.value.code == "HELD_UPLOAD_UNAVAILABLE"
        assert hold()  # nothing stays charged, so a replacement of the same size fits
    assert blobs() == [shared] and shared.read_bytes() == PAYROLL
    with request_scope(owner):
        assert [_stream(vault, path, owner) for path in filings] == [PAYROLL] * 4
    assert {path: (vault / originals.original_state(vault, path).sidecar_path).read_bytes()
            for path in (s1.artifact_path, d1.path, d2.path)} == {path: pages[path] for path in (
                s1.artifact_path, d1.path, d2.path)}  # no companion changed through any step


def test_the_census_and_every_export_keep_each_filing_of_a_shared_object_exact(
        vault, tmp_path, selected_store, monkeypatch):  # noqa: F811
    """Catches a drain that reports zero while a filing of a shared object has no local bytes, and an export or a
    restore that keeps the bytes but drops a protected filing's protection or a filing's page."""
    owner = owner_principal(surface="library")
    reader = RequestPrincipal("external", surface="mcp", issuer_family="test-issuer")
    shared = original_pool.object_path(vault, hashlib.sha256(PAYROLL).hexdigest())
    filed = _shared_filings(vault, tmp_path, monkeypatch)

    # 6. The census counts physical objects. The datasets keep the payroll object local, so only its loss is
    # not zero; a second object that two Sources share is evicted and counted once.
    ledgers = [_pooled_capture(vault, tmp_path, monkeypatch, name, LEDGER) for name in ("ledger.csv", "ledger-copy.csv")]
    for source in (filed.source, *ledgers):
        assert media_store.offload(vault, vault / source.artifact_path)
    assert media_bindings.rebuild(vault)
    for source in (filed.source, ledgers[0]):
        with media_store.open_original(vault, source.artifact_path):
            pass
    assert media_store.evict(vault, vault / ledgers[0].artifact_path)
    assert not media_store.evict(vault, vault / filed.source.artifact_path)
    _retained(selected_store, vault)
    assert media_store.drain_census(vault) == (1, len(LEDGER))
    assert media_store.drain_record(vault).unit == "physical_objects"
    assert media_store.drain_step(vault, claimable=lambda: None) == 1
    assert media_store.drain_census(vault) == (0, 0)
    assert [media_store.read_original_bytes(vault, source.artifact_path).data for source in ledgers] == [LEDGER] * 2
    shared.unlink()
    assert media_store.drain_census(vault) == (1, len(PAYROLL))  # lost bytes: one object, restorable by the Source
    assert media_store.drain_step(vault, claimable=lambda: None) == 1
    assert media_store.drain_census(vault) == (0, 0)  # zero only now that every filing has its bytes again
    with request_scope(owner):
        assert [_stream(vault, path, owner) for path in (filed.source.artifact_path, filed.d1.path, filed.d2.path)] == [
            PAYROLL] * 3
    assert media_store.read_original_bytes(vault, filed.attachment).data == PAYROLL

    # 7. A pooled export and a compatibility export each restore into a fresh location with exact originals,
    # unchanged pages and protection that no grant at the source has opened.
    _as_external(vault, selector='paths: ["**"]', ceiling=egress.LEVEL_FULL)
    exact = {filed.source.artifact_path: PAYROLL, filed.d1.path: PAYROLL, filed.d2.path: PAYROLL,
             filed.attachment: PAYROLL, ledgers[0].artifact_path: LEDGER, ledgers[1].artifact_path: LEDGER}

    def page(root: Path, path: str) -> bytes:
        return (root / originals.original_state(root, path).sidecar_path.relative_to(root)).read_bytes()

    pages = {path: page(vault, path) for path in exact}
    objects = sorted(path.relative_to(vault) for path in (original_pool.pool_root(vault) / "sha256").rglob("*.blob"))
    pooled_export = vault_portability.export_quiesced_vault(vault, tmp_path / "pooled-exports", context=_context())
    compatible_export = vault_portability.export_compatible_vault(vault, tmp_path / "compat-exports", context=_context())
    pooled = _restore(pooled_export.archive_path, tmp_path / "pooled").live_root
    compatible = _restore(compatible_export.archive_path, tmp_path / "compatible").live_root

    assert original_pool.enabled(pooled)
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(pooled)
    assert sorted(path.relative_to(pooled) for path in (original_pool.pool_root(pooled) / "sha256").rglob("*.blob")) == objects
    assert not any((pooled / path).exists() for path in exact)  # the pool alone holds the bytes
    assert not original_pool.pool_root(compatible).exists()
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID not in (state_migration.recorded_descriptor_ids(compatible) or ())
    assert {path: (compatible / path).read_bytes() for path in exact} == exact  # ordinary files for an older reader
    for root in (pooled, compatible):
        with request_scope(owner):
            assert {path: _stream(root, path, owner) for path in exact} == exact
        assert {path: page(root, path) for path in exact} == pages
        with request_scope(reader):
            assert _stream(root, filed.source.artifact_path, reader) == PAYROLL
        for protected in (filed.d1.path, filed.d2.path):
            assert _refusals(root, protected, reader) == _refusals(root, protected.replace(".csv", "-unfiled.csv"), reader)
        _grant(root, GRANTS[1], filed.d2.path, reader)  # the destination's owner grants one dataset again
        with request_scope(reader):
            assert _stream(root, filed.d2.path, reader) == PAYROLL
        assert _refusals(root, filed.d1.path, reader) == _refusals(root, filed.d1.path.replace(".csv", "-unfiled.csv"), reader)
