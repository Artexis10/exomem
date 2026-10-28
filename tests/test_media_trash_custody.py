"""Trash lists a retained original without inventing another recoverable item."""

import hashlib
import json
from pathlib import Path

import pytest

from exomem import list_trash, originals
from exomem import vault as vault_module


@pytest.mark.parametrize("body", ["", "Independent content that must remain visible."])
def test_trash_distinguishes_retention_metadata_from_an_independent_page(vault: Path, body: str):
    original = vault / "Knowledge Base/_trash/2026-01-02/clip.bin"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"retained original")
    relative = original.relative_to(vault).as_posix()
    placement = original.with_name(original.name + ".meta.json")
    placement.write_text(json.dumps({
        "original_path": "Knowledge Base/Evidence/clip.bin", "trashed_at": "2026-01-02T12:00:00Z",
    }))
    binary = originals.BinaryBinding(relative, hashlib.sha256(original.read_bytes()).hexdigest(), original.stat().st_size)
    reference = originals.StoredObject(1, "test-store", "1", "a" * 64, "version", 50, "b" * 64)
    companion = originals.sidecar_path(original)
    companion.write_text(vault_module.render_frontmatter_document(
        vault_module.serialize_frontmatter(originals.reference_companion_fields(binary, reference)), body,
    ))

    result = list_trash.list_trash(vault)

    assert result.count == 1 and result.entries[0].trash_path == relative
    assert result.orphan_sidecars == []
    assert result.orphan_files == ([companion.relative_to(vault).as_posix()] if body else [])
