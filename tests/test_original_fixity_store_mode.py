"""In store mode, a damaged text original names the store's own page when that page still holds its text.

The vault is the store suite's migrated vault; the data is invented.
"""

# Fixtures imported from the sibling test modules are redefined by each test's arguments.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
import sys

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import put, stored, writer  # noqa: F401

from exomem import adopt, original_fixity
from exomem.vault_store import write

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only")

SOURCE = "Knowledge Base/Sources/Gauge survey.md"


def test_a_line_added_to_a_store_owned_text_source_routes_to_the_store_s_page(stored, writer, monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_ORIGINAL_FIXITY", raising=False)
    text = "Meeting notes from the gauge survey.\nThe river rose overnight.\n"
    planned = {"title": "Gauge survey", "original_path": "Notes/gauge-survey.md",
               "original_sha256": hashlib.sha256(text.encode()).hexdigest(), "original_bytes": len(text)}
    page = adopt.render_text_source(planned, text, date_iso="2026-11-01", collection=None).encode()
    write.commit(writer, put(SOURCE, page))
    (stored / SOURCE).write_bytes(page)  # as the projector writes the stored page
    original_fixity.walk(stored)
    original_fixity.run_pass(stored)

    (stored / SOURCE).write_bytes(page + b"An afterthought typed below the fence.\n")
    assert original_fixity.run_pass(stored, budget=0).outcomes == {"corrupt_no_copy": 1}

    [finding], _coverage = original_fixity.attention_findings(stored)
    assert finding.meta["route"] == [{"source": "vault_store", "path": SOURCE},
                                     {"source": "import_again", "path": "Notes/gauge-survey.md"}]
