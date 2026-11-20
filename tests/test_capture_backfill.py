"""The owner's backfill of capture metadata into the sidecars of photos filed before it was recorded.

Every case runs on a disposable vault with photos built in the test.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
import test_media_store

from exomem import (
    artifact_lifecycle,
    capture_backfill,
    capture_metadata,
    media_store,
    original_conversion,
    preserve,
    prior_extraction,
)
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope

pytestmark = pytest.mark.frame_codecs

# A configured media store, for the case that offloads an original.
selected_store = test_media_store.selected_store

CAPTURE_LINE = re.compile(r"(?m)^(?:" + "|".join(capture_metadata.FIELDS) + r"):.*\n")
HELSINKI = {1: "N", 2: (60.0, 10.0, 30.0), 3: "E", 4: (24.0, 56.0, 15.0)}


def _jpeg(*, taken: str | None = None, gps: dict[int, object] | None = None, size=(64, 48)) -> bytes:
    from PIL import Image

    exif = Image.Exif()
    if taken is not None:
        exif.get_ifd(0x8769)[0x9003] = taken
    for tag, value in (gps or {}).items():
        exif.get_ifd(0x8825)[tag] = value
    out = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(out, "JPEG", exif=exif)
    return out.getvalue()


def _file(vault: Path, name: str, data: bytes, *, text: str = "A harbour at dusk.") -> tuple[str, str]:
    """A photo filed and extracted before capture metadata existed: its page has text and no capture fields."""
    with library_scope():
        filed = preserve.preserve_bytes(
            vault, scope="Photos", category="image", filename=f"{name}.jpg", data=data, content_type="image/jpeg"
        )
        preserve.update_sidecar_extraction(vault, vault / filed.sidecar_path, text=text, engine="ocr")
    return filed.path, filed.sidecar_path


def _backfill(vault: Path, **arguments) -> dict:
    with library_scope():
        return capture_backfill.backfill(vault, **arguments)


def _run(vault: Path) -> dict:
    return _backfill(vault, dry_run=False, plan_id=_backfill(vault)["plan_id"])


def test_a_backfill_adds_the_capture_block_to_a_filed_photo_and_changes_nothing_else(vault):
    """Catches a backfill that rewrites the extracted text, a preserved note or another field of the page."""
    _phone, phone_page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    _broken, broken_page = _file(vault, "broken", b"\xff\xd8 not a jpeg")
    before = {page: (vault / page).read_text(encoding="utf-8") for page in (phone_page, broken_page)}

    preview = _backfill(vault)

    assert (preview["would_update"], preview["no_capture_metadata"]) == (1, 1)
    assert {page: (vault / page).read_text(encoding="utf-8") for page in before} == before  # a preview writes nothing

    report = _run(vault)

    after = (vault / phone_page).read_text(encoding="utf-8")
    assert (report["updated"], report["skipped_changed"]) == (1, 0)
    assert 'capture_time: "2024-06-01T14:03:22"' in after and "capture_lat: 60.175" in after
    assert CAPTURE_LINE.sub("", after) == before[phone_page]
    assert (vault / broken_page).read_text(encoding="utf-8") == before[broken_page]


def test_a_prior_extraction_page_is_left_as_it_is(vault):
    """Catches a backfill that treats the earlier extraction a reprocess kept as a second sidecar of the photo."""
    _original, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    sidecar = vault / page
    prior, ordinal = prior_extraction.next_page(sidecar)
    prior.write_text(
        prior_extraction.render(
            sidecar.read_text(encoding="utf-8"),
            artifact_name="phone.jpg",
            sidecar_link=prior_extraction.link_to(vault, sidecar),
            ordinal=ordinal,
            date_iso="2024-06-02",
            text="An earlier reading.",
        ),
        encoding="utf-8",
    )
    kept = prior.read_bytes()

    report = _run(vault)

    assert report["updated"] == 1
    assert prior.read_bytes() == kept


def test_a_second_backfill_has_nothing_to_do(vault):
    """Catches a backfill that rewrites pages that already hold what their original states."""
    _original, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    _run(vault)
    settled = (vault / page).read_bytes()

    again = _backfill(vault)
    _run(vault)

    assert (again["would_update"], again["already_current"]) == (0, 1)
    assert (vault / page).read_bytes() == settled


def test_a_page_with_unmarked_preserved_notes_is_skipped_and_counted(vault):
    """Catches a backfill that guesses where machine-owned text ends and overwrites a person's notes."""
    _original, page = _file(vault, "notes", _jpeg(taken="2024:06:01 14:03:22"))
    legacy = (vault / page).read_text(encoding="utf-8") + "\n## Preserved notes\n\nMy own note.\n"
    (vault / page).write_text(legacy, encoding="utf-8")
    _file(vault, "clean", _jpeg(taken="2024:06:02 09:00:00"))

    report = _run(vault)

    assert report["skipped"] == {f"{capture_backfill.AMBIGUOUS_BOUNDARY}: unmarked nonempty preserved notes have no machine-owned boundary": 1}
    assert report["updated"] == 1
    assert (vault / page).read_text(encoding="utf-8") == legacy


def test_an_original_that_is_not_on_the_volume_is_skipped_and_counted(vault):
    """Catches a backfill that fails, or records an empty block, for an original it cannot read."""
    original, page = _file(vault, "gone", _jpeg(taken="2024:06:01 14:03:22"))
    _file(vault, "here", _jpeg(taken="2024:06:02 09:00:00"))
    (vault / original).unlink()
    before = (vault / page).read_text(encoding="utf-8")

    report = _run(vault)

    assert report["skipped"] == {capture_backfill.BYTES_NOT_ON_VOLUME: 1}
    assert report["updated"] == 1
    assert (vault / page).read_text(encoding="utf-8") == before


def test_an_offloaded_original_is_skipped_and_never_fetched_back(vault, selected_store):
    """Catches a backfill that hydrates every offloaded original, which turns a metadata pass into a download."""
    from test_vault_store_media import offloaded

    original = offloaded(vault, "Knowledge Base/Notes/Media/offloaded.jpg")
    assert media_store.evict(vault, original)
    before = (vault / f"{original.relative_to(vault)}.md").read_text(encoding="utf-8")

    report = _run(vault)

    assert report["skipped"] == {capture_backfill.BYTES_NOT_ON_VOLUME: 1}
    assert not original.exists()
    assert media_store.original_state(vault, original).state == "evicted"
    assert (vault / f"{original.relative_to(vault)}.md").read_text(encoding="utf-8") == before


def test_a_photo_held_in_shared_storage_is_read_from_its_shared_object(vault, monkeypatch):
    """Catches a backfill that reads only ordinary files, so the photos of a converted archive keep no capture block."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original, page = _file(vault, "shared", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    with library_scope():
        original_conversion.convert(vault, dry_run=False)
    assert not (vault / original).exists()

    report = _run(vault)

    assert report["updated"] == 1
    assert "capture_lat: 60.175" in (vault / page).read_text(encoding="utf-8")


def test_only_the_owner_may_run_the_backfill(vault):
    """Catches a restricted caller who learns where hidden photos were taken, or has their pages rewritten."""
    _original, page = _file(vault, "phone", _jpeg(taken="2024:06:01 14:03:22", gps=HELSINKI))
    before = (vault / page).read_bytes()

    with pytest.raises(OpError) as refused:
        capture_backfill.backfill(vault, dry_run=False)

    assert refused.value.code == "MAINTENANCE_OWNER_REQUIRED"
    assert (vault / page).read_bytes() == before
