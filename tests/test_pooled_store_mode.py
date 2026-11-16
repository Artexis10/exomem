"""A pooled filing is no outside delete for the store-mode importer.

A store-mode vault keeps a binary record for each original that an outside change could remove. A filing whose bytes
moved into the shared pool keeps its record but has no file at its logical path. This suite lives apart from
`test_pooled_export_fixity` because the store-mode suite's `vault` fixture replaces the plain one.
"""

from __future__ import annotations

import sys

import pytest
from test_pooled_export_fixity import _pool
from test_vault_store_media import (  # noqa: F401 - fixtures
    coordinator_url,
    media_state,
    original_thread,
    selected_store,
    stored,
    stored_original,
    vault,
)

from exomem import media_store, originals
from exomem.vault_store import importer
from exomem.vault_store import media as store_media

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only")


def test_a_pooled_filing_is_no_outside_delete_and_keeps_its_binary_record(
    vault, stored_original, original_thread, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """An importer that reads the missing logical file as an outside delete drops the binary record,
    after which the store holds the companion for an original that nothing names."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    before = media_state(vault, rel)

    _pool(vault, monkeypatch, vault / rel)
    swept = settling.sweep()
    named = settling.import_paths([rel])

    assert not (vault / rel).exists()
    assert swept.removed == [] and named.removed == []
    assert media_state(vault, rel) == before


def test_a_pooled_filing_is_no_outside_delete_when_no_media_store_is_configured(
    vault, stored_original, original_thread, monkeypatch  # noqa: F811 - the store-mode suite's fixtures
):
    """Without a media store the importer has no eviction to explain a missing file, so only the pool can."""
    rel = stored_original
    settling = importer.Importer(vault, run=original_thread.run)
    settling.sweep()
    before = media_state(vault, rel)

    _pool(vault, monkeypatch, vault / rel)
    monkeypatch.delenv(originals.CONFIG_ENV)
    assert originals.binding_for(vault) is None
    swept = settling.sweep()
    named = settling.import_paths([rel])

    assert store_media.managed(vault, rel) == media_store.SUPPRESS
    assert swept.removed == [] and named.removed == []
    assert media_state(vault, rel)["binary"] == before["binary"]
