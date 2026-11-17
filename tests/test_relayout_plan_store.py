"""The relayout plan over a store-mode vault: it reads the store's pages, never the deleted files.

The vault is the migration suite's generated vault with the nested relayout fixture written into
it, with its scopes, access file and collection pages, and moved into store mode by
`store-mode migrate`. The plan of the same vault, read as files before their projections go, is
the reference.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from relayout_fixture import (
    KB,
    RECORD_PAGE,
    add_attachment_links,
    add_governed_content,
    add_links_from_collection_and_part_pages,
    build_nested_vault,
)
from test_collection_store_migration import vault_bytes
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_relayout_plan import PART_PAGE
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401

from exomem import __main__ as cli
from exomem.vault_store import mode, ownership, read


@pytest.fixture(autouse=True)
def linux_only():
    """The offline-owner lease that the migration takes is Linux-only; autouse fixtures run before `vault`."""
    if sys.platform != "linux":
        pytest.skip("The offline-owner lease is Linux-only")


def planned(capsys, root: Path) -> dict:
    code = cli.main(["store-mode", "relayout", "--plan", "--full", "--vault", str(root)])
    captured = capsys.readouterr()
    assert (code, captured.err) == (0, "")
    return json.loads(captured.out)


def files_the_plan_lists(root: Path) -> int:
    """Files below the managed folder from os.walk, leaving out hidden folders except part folders."""
    found = 0
    for _folder, directories, names in os.walk(root / KB):
        directories[:] = [
            name for name in directories if not name.startswith(".") or name.endswith(".parts")
        ]
        found += len(names)
    return found


def test_a_store_mode_plan_equals_the_file_mode_plan_and_changes_nothing(
    vault,  # noqa: F811
    capsys,
    monkeypatch,
):
    build_nested_vault(vault)
    add_governed_content(vault)
    add_links_from_collection_and_part_pages(vault)
    add_attachment_links(vault)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    capsys.readouterr()
    owned = [
        item.rel
        for item in ownership.iter_owned(vault)
        if item.ownership is ownership.Ownership.PAGE
    ]
    expected_files = files_the_plan_lists(vault)
    with monkeypatch.context() as as_files:
        as_files.setattr(mode, "store_mode_for_reads", lambda _root: False)
        from_files = planned(capsys, vault)
    for rel in owned:
        (vault / rel).unlink()
    bytes_before = vault_bytes(vault)
    revision_before = read.current_revision(vault)

    body_reads: list[str] = []

    def counted_reader(method):
        def reading(self, *args, **options):
            body_reads.append(method.__name__)
            return method(self, *args, **options)

        return reading

    with monkeypatch.context() as counted:
        # Each call is a store query: `page` reads a single page, `bodies` reads a batch of up to 500.
        for name in ("page", "bodies"):
            counted.setattr(
                read.StoreSnapshot, name, counted_reader(getattr(read.StoreSnapshot, name))
            )
        from_store = planned(capsys, vault)

    # Two passes read every page; a query per page and pass would cost twice the page count.
    assert 0 < len(body_reads) <= len(owned) // 4

    assert (
        from_files["diagnostics"]["read"]["mode"],
        from_store["diagnostics"]["read"]["mode"],
    ) == (
        "file",
        "store",
    )
    assert from_store["diagnostics"]["read"]["store_revision"] == revision_before
    # The equality below holds a disclosure that read policy, pages and collection pages.
    assert from_store["disclosure"]["scopes"]["state"] == "evaluated"
    assert from_store["disclosure"]["figures"]["differences"] > 0
    foreign = from_store["links_in_collection_and_part_pages"]["edits"]["items"]
    assert {RECORD_PAGE, PART_PAGE} <= {edit["page"] for edit in foreign}
    assert from_store["counts"]["files"] == from_files["counts"]["files"] == expected_files
    assert from_store["counts"]["link_edits_to_attachments"] == 1
    # The read section names the mode, the revision and the stage timings, so it differs by design.
    del from_files["diagnostics"]["read"], from_store["diagnostics"]["read"]
    assert from_store == from_files
    assert read.current_revision(vault) == revision_before
    assert vault_bytes(vault) == bytes_before
