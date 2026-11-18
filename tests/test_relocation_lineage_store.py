"""Lineage and prior-path resolution in a store-mode vault match file mode.

OpenSpec add-curation-relocation-protocol task 1.9. The vault is the migration suite's generated
vault in store mode with the writer bound, as the lifecycle-writer suite runs it: a page that a
commit stores has no file, so every answer comes from the store.
"""

from __future__ import annotations

import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_bypass_writers import GAMMA, store_delta, store_gamma
from test_vault_store_lifecycle_writers import differences_from_a_cold_build
from test_vault_store_migration import vault  # noqa: F401

from exomem import move_file, page_lineage
from exomem import vault as vault_module
from exomem.vault_store import read

stored, writer, served = read_write.stored, read_write.writer, write_path.served

MIDDLE = "Knowledge Base/Notes/Gamma Archive.md"
FINAL = "Knowledge Base/Notes/Tides/Gamma Ledger.md"


def test_a_page_moved_twice_resolves_its_prior_paths_as_file_mode_does(served, tmp_path):
    """A store commit that drops the lineage entry, derives no prior rows, or derives rows that a
    cold build from the files would not."""
    store_gamma(served)
    store_delta(served)

    move_file.move_file(served, old_path=GAMMA, new_path=MIDDLE, update_wikilinks=False)
    move_file.move_file(served, old_path=MIDDLE, new_path=FINAL, update_wikilinks=False)

    front, _body, _block = vault_module.parse_frontmatter(
        read.owned_bytes(served, FINAL).decode("utf-8")
    )
    assert [entry["path"] for entry in front[page_lineage.LINEAGE_FIELD]] == [GAMMA, MIDDLE]
    for prior in (GAMMA, MIDDLE):
        assert vault_module.normalize_wikilink(prior, served) == (FINAL.removesuffix(".md"), None)
    assert differences_from_a_cold_build(served, tmp_path) == []
