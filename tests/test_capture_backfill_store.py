"""The capture-metadata backfill in a store-mode vault, where a photo's page lives in the page store."""

from __future__ import annotations

import io
import re

import pytest
import test_vault_store_importer as importer_suite
from test_vault_store_media import PICTURE, store_thread, stored_sidecar  # noqa: F401 - store_thread is a fixture

from exomem import capture_backfill, capture_metadata
from exomem.governance.principal import library_scope
from exomem.vault_store import importer

pytestmark = pytest.mark.frame_codecs

# The importer suite's generated vault and its store-mode form.
coordinator_url = importer_suite.coordinator_url
vault = importer_suite.vault
stored = importer_suite.stored

CAPTURE_LINE = re.compile(r"(?m)^(?:" + "|".join(capture_metadata.FIELDS) + r"):.*\n")


def _jpeg() -> bytes:
    from PIL import Image

    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2024:06:01 14:03:22"
    exif.get_ifd(0x8825).update({1: "N", 2: (60.0, 10.0, 30.0), 3: "E", 4: (24.0, 56.0, 15.0)})
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (30, 90, 160)).save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_a_backfill_commits_the_capture_block_of_a_stored_page_through_the_store(stored, store_thread):  # noqa: F811
    """Catches a backfill that finds no page, or no way to commit one, when the page exists only in the store."""
    photo = PICTURE.replace(".png", ".jpg")
    (stored / photo).write_bytes(_jpeg())
    importer.Importer(stored, run=store_thread.run).import_paths([photo])
    before = stored_sidecar(stored, photo).decode()
    assert "capture_lat" not in before

    with library_scope():
        preview = capture_backfill.backfill(stored)
        report = capture_backfill.backfill(stored, dry_run=False, plan_id=preview["plan_id"])

    after = stored_sidecar(stored, photo).decode()
    assert (preview["would_update"], report["updated"]) == (1, 1)
    assert "capture_lat: 60.175" in after
    assert CAPTURE_LINE.sub("", after) == before
