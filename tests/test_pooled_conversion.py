"""The owner's conversion of an existing archive's ordinary originals into shared original storage."""

from __future__ import annotations

import datetime as dt
import hashlib
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_media_store

from exomem import (
    artifact_lifecycle,
    maintenance_jobs,
    media_jobs,
    media_processing,
    media_store,
    original_conversion,
    original_pool,
    originals,
    preserve,
    reserved_paths,
    state_migration,
)
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope

# A configured media store, for the cases that offload an original.
selected_store = test_media_store.selected_store

RECEIPT = b"\x89PNG\r\n\x1a\n" + b"one receipt, filed twice" * 64
INVOICE = b"%PDF-1.7\n" + b"an invoice the owner filed once" * 64


@pytest.fixture
def supported(monkeypatch):
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))


def _file(vault: Path, category: str, name: str, data: bytes, *, protected: bool = False) -> tuple[str, str]:
    """One ordinary Evidence filing, as an archive created before shared storage holds it."""
    with library_scope():
        filed = preserve.preserve_bytes(vault, scope="work", category=category, filename=name, data=data,
                                        raw_protection=protected, today=dt.date(2026, 11, 16))
    return filed.path, filed.sidecar_path


def _convert(vault: Path, **arguments) -> dict:
    with library_scope():
        return original_conversion.convert(vault, **arguments)


def _objects(vault: Path) -> list[Path]:
    root = original_pool.pool_root(vault)
    return sorted(root.rglob("*.blob")) if root.exists() else []


def _read(vault: Path, original: str) -> bytes:
    """What a supported reader gets for the filing, from its ordinary file or its shared object."""
    shared = originals.pooled(vault, original)
    return (vault / original).read_bytes() if shared is None else shared.local_path.read_bytes()


def test_a_conversion_keeps_identical_filings_as_one_shared_object_and_their_pages_unchanged(vault, supported):
    """Catches a conversion that leaves a second copy, rewrites a page, or retires a file without a shared copy."""
    first, first_page = _file(vault, "receipts", "receipt.png", RECEIPT)
    second, second_page = _file(vault, "expenses", "receipt-copy.png", RECEIPT)
    pages = {page: (vault / page).read_bytes() for page in (first_page, second_page)}

    preview = _convert(vault)
    report = _convert(vault, dry_run=False, plan_id=preview["plan_id"])

    assert (preview["would_convert"], report["converted"]) == (2, 2)
    assert not (vault / first).exists() and not (vault / second).exists()
    assert _objects(vault) == [original_pool.object_path(vault, hashlib.sha256(RECEIPT).hexdigest())]
    assert _read(vault, first) == _read(vault, second) == RECEIPT
    assert {page: (vault / page).read_bytes() for page in pages} == pages


def test_a_suffixless_original_stays_an_ordinary_file_through_conversion(vault, supported):
    """A pooled filing exists solely as a companion the census tells from a note by its suffix, so a
    suffix-less original converts like a dataset — it keeps its file — while a suffixed one pools."""
    plain, _page = _file(vault, "receipts", "LICENSE", RECEIPT)
    pdf, _pdf_page = _file(vault, "receipts", "scan.pdf", INVOICE)

    preview = _convert(vault)
    report = _convert(vault, dry_run=False, plan_id=preview["plan_id"])

    assert (preview["would_convert"], report["converted"]) == (1, 1)
    assert (vault / plain).read_bytes() == RECEIPT
    assert originals.pooled(vault, plain) is None
    assert _read(vault, pdf) == INVOICE
    assert originals.pooled(vault, pdf) is not None


def test_a_protected_filing_keeps_its_grant_bound_page_byte_identical(vault, supported):
    """Catches a conversion that rewrites a protected page, whose grants bind its exact bytes."""
    original, page = _file(vault, "contracts", "letter.pdf", INVOICE, protected=True)
    before = (vault / page).read_bytes()

    _convert(vault, dry_run=False)

    assert (vault / page).read_bytes() == before
    assert _read(vault, original) == INVOICE


def test_a_preview_changes_nothing(vault, supported):
    """Catches a preview that enrols the shared format or moves bytes before the owner applies the plan."""
    original, _page = _file(vault, "receipts", "receipt.png", RECEIPT)

    preview = _convert(vault)

    assert preview["layout"] == "ordinary" and preview["would_convert"] == 1
    assert not original_pool.pool_root(vault).exists()
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)
    assert (vault / original).read_bytes() == RECEIPT


def test_only_the_owner_may_convert(vault, supported):
    """Catches a restricted caller who learns the paths of hidden filings, or relocates bytes it cannot see."""
    original, _page = _file(vault, "receipts", "receipt.png", RECEIPT)

    with pytest.raises(OpError) as refused:
        original_conversion.convert(vault, dry_run=False)

    assert refused.value.code == "MAINTENANCE_OWNER_REQUIRED"
    assert (vault / original).read_bytes() == RECEIPT


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_conversion_without_launcher_support_refuses_before_moving_anything(vault, monkeypatch, dry_run):
    """Catches a conversion that relocates bytes an older managed launcher would then read as deleted files, and a
    preview that offers a plan whose start the same launcher refuses."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    original, _page = _file(vault, "receipts", "receipt.png", RECEIPT)

    with pytest.raises(OpError) as refused:
        _convert(vault, dry_run=dry_run)

    assert refused.value.code == "ORIGINAL_POOL_LIFECYCLE_REQUIRED"
    assert not original_pool.pool_root(vault).exists()
    assert (vault / original).read_bytes() == RECEIPT


def test_a_runtime_without_shared_originals_refuses_a_converted_archive(vault, supported, monkeypatch):
    """Catches a downgrade that serves a converted archive and reads its pooled originals as deleted files."""
    original, _page = _file(vault, "receipts", "receipt.png", RECEIPT)
    _convert(vault, dry_run=False)
    capable = state_migration.supported_state_compatibility_ids()
    older = tuple(item for item in capable if item != state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID)
    monkeypatch.setattr(state_migration, "supported_state_compatibility_ids", lambda: older)

    with pytest.raises(state_migration.StateMigrationOfflineRequired, match="compatible runtime"):
        state_migration.require_vault_state_ready(vault)
    assert state_migration.migration_status(vault) == "unsupported"
    assert original_pool.object_path(vault, hashlib.sha256(RECEIPT).hexdigest()).read_bytes() == RECEIPT


def _fail_publication(monkeypatch):
    def fail(_prepared):
        raise OSError("disk full while publishing")

    monkeypatch.setattr(original_pool.PreparedOriginal, "publish", fail)


def _fail_before_unlink(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("the process stopped after publication")

    monkeypatch.setattr(reserved_paths, "unlink_generic_file", fail)


def _stop_after_first_batch(monkeypatch):
    checkpoint = maintenance_jobs.checkpoint
    calls = []

    def stop(phase, done=None, total=None):
        calls.append(phase)
        if calls.count("converting") == 2:
            raise maintenance_jobs.JobStop(maintenance_jobs.SHUTDOWN_CODE, maintenance_jobs.INTERRUPTED)
        checkpoint(phase, done, total)

    monkeypatch.setattr(maintenance_jobs, "checkpoint", stop)


@pytest.mark.parametrize("interrupt", [_fail_publication, _fail_before_unlink, _stop_after_first_batch])
def test_an_interrupted_conversion_stays_readable_and_a_rerun_completes_it(vault, supported, interrupt):
    """Catches an interruption that loses or misreads bytes in mixed storage, or a rerun that duplicates objects."""
    filed = [_file(vault, "receipts", f"receipt-{index}.png", RECEIPT if index % 2 else INVOICE)
             for index in range(6)]
    with pytest.MonkeyPatch.context() as patched:
        interrupt(patched)
        with pytest.raises((OSError, maintenance_jobs.JobStop)):
            _convert(vault, dry_run=False)

        assert [_read(vault, original) for original, _page in filed] == [
            RECEIPT if index % 2 else INVOICE for index in range(6)]

    rerun = _convert(vault, dry_run=False)

    assert rerun["converted"] + rerun["already_shared"] == 6
    assert not any((vault / original).exists() for original, _page in filed)
    assert [_read(vault, original) for original, _page in filed] == [
        RECEIPT if index % 2 else INVOICE for index in range(6)]
    assert _objects(vault) == sorted(original_pool.object_path(vault, hashlib.sha256(data).hexdigest())
                                     for data in (RECEIPT, INVOICE))


@pytest.mark.parametrize("edited", ["original", "page"])
def test_a_filing_edited_after_its_inventory_is_not_retired(vault, supported, monkeypatch, edited):
    """Catches a retirement that removes an ordinary file whose bytes or binding changed after they were checked."""
    original, page = _file(vault, "receipts", "receipt.png", RECEIPT)
    hold = maintenance_jobs.chunk_hold

    def edit_then_hold(*args, **kwargs):
        if edited == "original":
            (vault / original).write_bytes(INVOICE[:len(RECEIPT)].ljust(len(RECEIPT), b"!"))
        else:
            (vault / page).write_bytes((vault / page).read_bytes() + b"\nA note added while the job ran.\n")
        return hold(*args, **kwargs)

    monkeypatch.setattr(maintenance_jobs, "chunk_hold", edit_then_hold)

    report = _convert(vault, dry_run=False)

    assert report["converted"] == 0 and report["skipped_changed"] == [original]
    assert (vault / original).exists()


@contextmanager
def _reading(vault: Path, original: str):
    with media_store._pin(vault, vault / original, read=True):
        yield


@contextmanager
def _extracting(vault: Path, original: str):
    """A media job claimed for the original, which keeps its bytes until the job ends."""
    queued = media_processing.reconcile_media(vault, original)
    jobs = media_jobs.MediaJobStore(vault)
    claimed = jobs.claim_next()
    assert claimed is not None and claimed.id == queued.job_id
    yield
    jobs.mark(claimed.id, media_jobs.FAILED, "the test ended the job")


@pytest.mark.parametrize("holder", [_reading, _extracting])
def test_a_pinned_original_is_never_retired(vault, supported, holder):
    """Catches a retirement that removes an ordinary file while a reader or a claimed media job holds it."""
    original, _page = _file(vault, "receipts", "receipt.png", RECEIPT)

    with holder(vault, original):
        report = _convert(vault, dry_run=False)
        assert report["kept_pinned"] == [original]
        assert (vault / original).read_bytes() == RECEIPT

    assert _convert(vault, dry_run=False)["converted"] == 1


def test_a_filing_restored_by_hand_while_its_page_names_an_emptied_trash_entry_keeps_its_ordinary_file(
        vault, selected_store, supported):
    """Catches a conversion that pools a filing whose page still names an emptied Trash entry: once its file is
    gone, reads take it for the deleted original, so the conversion reports success and the original reads missing."""
    from test_vault_store_media import ORIGINAL_BYTES, offloaded

    from exomem import delete_file, set_frontmatter_field

    original = "Knowledge Base/Notes/Media/loose.txt"
    offloaded(vault, original)
    trashed = delete_file.delete_file(vault, path=original, confirm=True)
    (vault / trashed.trash_path).rename(vault / original)
    (vault / f"{trashed.trash_path}.md").unlink()
    (vault / f"{trashed.trash_path}.meta.json").unlink()
    assert media_store.read_original_bytes(vault, original).data == ORIGINAL_BYTES

    report = _convert(vault, dry_run=False)

    assert report["converted"] == 0
    assert [entry["path"] for entry in report["unreadable"]] == [original]
    assert "original_retirement_meta" in report["unreadable"][0]["reason"]
    assert (vault / original).read_bytes() == ORIGINAL_BYTES
    assert media_store.read_original_bytes(vault, original).data == ORIGINAL_BYTES

    # The remedy that the reason names lets the next conversion pool the filing.
    with library_scope():
        set_frontmatter_field.set_frontmatter_field(
            vault, path=f"{original}.md", field="original_retirement_meta", value=None, why="restored by hand")
    assert _convert(vault, dry_run=False)["converted"] == 1
    assert media_store.read_original_bytes(vault, original).data == ORIGINAL_BYTES
