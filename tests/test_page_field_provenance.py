"""The shipped page-field pack declares the origin fields that import filing writes.

OpenSpec file-imports-on-the-cell, group 1. The vault is the canonical fixture. Each test names
the failure it catches.
"""

from __future__ import annotations

from pathlib import Path

from exomem import page_fields
from exomem.vocabulary import registry


def test_the_shipped_pack_declares_the_four_origin_fields_with_their_source(vault: Path) -> None:
    """A dropped or mis-keyed entry, so filing writes no origin or reads the wrong envelope key."""
    declared = {
        field.field: (field.source, field.envelope) for field in page_fields.provenance_fields(vault)
    }

    assert declared == {
        "origin_label": ("envelope", "label"),
        "upload_device": ("envelope", "device_family"),
        "upload_browser": ("envelope", "browser_family"),
        "file_modified": ("member-time", None),
    }
    # An origin field is not a pointer, so the governed pointer write never offers to change it.
    assert not declared.keys() & page_fields.pointer_fields(vault)


def test_an_overlay_that_adds_a_provenance_field_is_reported_and_ignored(vault: Path) -> None:
    """An overlay written straight to disk that makes a new key a provenance field, so a vault could
    widen what filing stamps onto every imported Source."""
    page_fields.registry_path(vault).write_text(
        "schema_version: 1\nentries:\n  uploader-ip:\n    label: Uploader address\n"
        "    attributes: {field: uploader_ip, role: provenance, source: envelope, envelope: address}\n",
        encoding="utf-8",
    )
    registry.invalidate(vault)

    snapshot = registry.load(page_fields.SPEC, vault)

    assert [finding["code"] for finding in snapshot.findings] == ["invalid_registry_overlay"]
    assert page_fields.WIDENING_CODE in snapshot.findings[0]["detail"]
    assert "uploader_ip" not in {field.field for field in page_fields.provenance_fields(vault)}
    assert len(page_fields.provenance_fields(vault)) == 4


def test_an_overlay_may_retire_one_origin_field(vault: Path) -> None:
    """A retirement that also refuses, so an owner could not stop the device from being recorded."""
    page_fields.registry_path(vault).write_text(
        "schema_version: 1\nentries:\n  upload-device:\n    status: deprecated\n", encoding="utf-8"
    )
    registry.invalidate(vault)

    fields = {field.field for field in page_fields.provenance_fields(vault)}

    assert list(registry.load(page_fields.SPEC, vault).findings) == []
    assert fields == {"origin_label", "upload_browser", "file_modified"}
