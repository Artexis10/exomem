"""Parent guards use the same store snapshot as the records they validate."""

from __future__ import annotations

import hashlib

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_graph_readers import (
    LAMP,
    UNIT_PAGES,
    record_store_reads,
    write_pages,
)
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401
from test_vault_store_read_write import put, stored, writer  # noqa: F401
from test_vault_store_readers import EXTERNAL, damage_every_owned_projection, restrict_external

from exomem import commands, edit, find, semantic_index
from exomem import vault as vaults
from exomem.governance.principal import request_scope
from exomem.vault_store import read, write

pytestmark = pytest.mark.timeout(900)


@pytest.mark.parametrize("damage", ["deleted", "poisoned"])
def test_unit_neighbours_validate_the_stored_parent(vault, monkeypatch, damage):  # noqa: F811
    """Unit neighbours disappear or report stale when their parent's projection is damaged."""
    restrict_external(vault, 'tags: ["secret"]')
    write_pages(vault, UNIT_PAGES)
    build_indexes(vault)
    expected = commands.op_graph_context(vault, path=LAMP, depth=2)
    assert any(node["kind"] != "file" for node in expected["nodes"])
    with request_scope(EXTERNAL):
        external = commands.op_graph_context(vault, path=LAMP, depth=2)
    assert migrate_apply(vault) == 0
    damage_every_owned_projection(vault, damage)
    find.clear_cache()

    with record_store_reads(vault, monkeypatch) as events:
        actual = commands.op_graph_context(vault, path=LAMP, depth=2)
        with request_scope(EXTERNAL):
            actual_external = commands.op_graph_context(vault, path=LAMP, depth=2)

    assert actual == expected
    assert actual_external == external
    assert events["page_io"] == []
    assert events["sidecars"] == []
    assert events["machinery"] == []
    assert len(events["snapshots"]) == 2


def test_parent_freshness_batches_and_keeps_the_request_revision(
    stored, writer, record_property  # noqa: F811
):
    """Many parents cause one query each, or a commit changes freshness inside a request."""
    sources = {
        f"Knowledge Base/Notes/parent-{index}.md": (
            "---\ntype: insight\n---\n## Claim\n\nThe lantern burns oil.\n"
        )
        for index in range(32)
    }
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test",
        puts=tuple(write.PagePut(path, source.encode()) for path, source in sources.items()),
    ))
    states = {
        path: semantic_index.build_parent_index_state(stored, path, source=source)
        for path, source in sources.items()
    }
    statements = []
    with read.request_snapshot(stored), read.snapshot(stored) as snapshot:
        snapshot.connection.set_trace_callback(statements.append)
        try:
            parents = semantic_index.StoredParentRecords(stored)
            parents.prefetch(sources)
            first = next(iter(sources))
            write.commit(writer, put(first, sources[first].replace("oil", "gas").encode()))
            for path, state in states.items():
                freshness = parents.validate(
                    parent_path=path,
                    parent_generation_value=state.parent_generation,
                    parent_source_hash=state.parent_source_hash,
                    parser_version=state.parser_version,
                )
                assert freshness.current, freshness.code
        finally:
            snapshot.connection.set_trace_callback(None)
    page_reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
                  and "FROM store_pages" in sql]
    record_property("parent_count", len(states))
    record_property("parent_reads", len(page_reads))
    assert len(page_reads) <= 3, page_reads
    state = states[first]
    changed = semantic_index.validate_parent_record(
        stored, parent_path=first, parent_generation_value=state.parent_generation,
        parent_source_hash=state.parent_source_hash, parser_version=state.parser_version,
    )
    assert changed.code == "parent_source_hash_mismatch"


def test_content_guard_checks_stored_bytes_when_the_projection_is_gone(stored, writer):  # noqa: F811
    """A predecessor guard trusts stale bytes or rejects a stored page with no projection."""
    path = "Knowledge Base/Notes/predecessor.md"
    source = b"---\ntype: insight\n---\nA predecessor.\n"
    write.commit(writer, put(path, source))
    (stored / path).unlink(missing_ok=True)
    guard = vaults.PathGuard.capture(
        stored, path, leaf_policy="content", expected_content_hash=hashlib.sha256(source).hexdigest(),
        expected_content_size=len(source),
    )
    guard.recheck(stored)
    write.commit(writer, put(path, source + b"Changed.\n"))
    with pytest.raises(vaults.PathGuardError, match="guarded content changed"):
        guard.recheck(stored)
    with pytest.raises(vaults.PathGuardError, match="guarded content changed"):
        vaults.PathGuard.capture(
            stored, path, leaf_policy="content", expected_content_hash=hashlib.sha256(source).hexdigest(),
        )


def test_page_history_reads_the_stored_log(stored, writer):  # noqa: F811
    """A missing log projection hides a page's stored history, or CRLF changes its summary."""
    path = "Knowledge Base/Notes/predecessor.md"
    log_path = "Knowledge Base/log.md"
    log = (
        "# Activity\r\n\r\n---\r\n\r\n"
        "## [2026-01-01] edit | Notes/predecessor\r\n\r\nClarify the claim.\r\n"
    )
    write.commit(writer, put(log_path, log.encode()))
    (stored / log_path).unlink(missing_ok=True)

    assert vaults.read_log_entries(stored, path) == [
        {"date": "2026-01-01", "op": "edit", "summary": "Clarify the claim."},
    ]


def test_a_real_page_outside_the_governed_root_keeps_its_refusal(vault):  # noqa: F811
    """The stored-parent resolver reports a real outside page as missing under the governed root."""
    outside = vault / "guide.md"
    source = "# Guide\n\nA page outside the governed root.\n"
    outside.write_text(source)
    with pytest.raises(edit.EditError) as before:
        edit.load_editable(vault, "guide")
    assert before.value.code == "OUTSIDE_GOVERNED_ROOT"
    outside.unlink()
    assert migrate_apply(vault) == 0
    outside.write_text(source)
    with pytest.raises(edit.EditError) as after:
        edit.load_editable(vault, "guide")
    assert after.value.as_dict() == before.value.as_dict()
