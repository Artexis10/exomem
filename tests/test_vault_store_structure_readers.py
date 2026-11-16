"""Structure scans retain their content and work bounds when projections vanish."""

from __future__ import annotations

import shutil

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_graph_readers import record_store_reads
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import put, stored, writer  # noqa: F401

from exomem import commands, find, overview, semantic_census
from exomem.vault_store import read, write
from exomem.writer_lease import invoke_command

pytestmark = pytest.mark.timeout(900)


def test_structure_reads_unprojected_subtrees_without_opening_oversized_pages(
    stored, writer, monkeypatch,  # noqa: F811
):
    """An unprojected folder disappears, or overview loads a page above its content cap."""
    folder = "Knowledge Base/Notes/Unprojected"
    small = b"---\ntype: insight\n---\n- [finding] A stored observation.\n"
    large = b"x" * (overview.CONTENT_READ_CAP + 1)
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test",
        puts=(write.PagePut(f"{folder}/small.md", small), write.PagePut(f"{folder}/large.md", large)),
    ))
    shutil.rmtree(stored / folder, ignore_errors=True)
    statements = []
    with record_store_reads(stored, monkeypatch) as events, read.request_snapshot(stored):
        with read.snapshot(stored) as snapshot:
            snapshot.connection.set_trace_callback(statements.append)
            try:
                report = overview.overview(stored, path=folder)
            finally:
                snapshot.connection.set_trace_callback(None)
    assert report["totals"]["files"] == 2
    assert report["totals"]["bytes"] == len(small) + len(large)
    assert report["skipped"]["oversized_files"] == 1
    body_reads = [sql for sql in statements if "body FROM store_pages" in sql]
    assert body_reads
    assert all(f"{folder}/large.md" not in sql for sql in body_reads)
    assert all(f"{folder}/small.md" in sql or "_Schema/" in sql for sql in body_reads)
    assert events["page_io"] == []
    assert len(events["snapshots"]) == 1


def test_semantic_census_batches_pages_and_keeps_file_and_byte_limits(
    stored, writer, monkeypatch, record_property,  # noqa: F811
):
    """A bounded census reads the whole corpus, issues one query per page, or ignores its byte cap."""
    folder = "Knowledge Base/Notes/Census"
    body = b"- [finding] The lantern burns oil.\n"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test",
        puts=tuple(write.PagePut(f"{folder}/{index:03}.md", body) for index in range(40)),
    ))
    shutil.rmtree(stored / folder, ignore_errors=True)
    statements = []
    with record_store_reads(stored, monkeypatch) as events, read.request_snapshot(stored):
        with read.snapshot(stored) as snapshot:
            snapshot.connection.set_trace_callback(statements.append)
            try:
                report = semantic_census.scan(stored, path=folder, max_files=16, max_bytes=len(body) * 16)
            finally:
                snapshot.connection.set_trace_callback(None)
    assert report["coverage"]["markdown_files_scanned"] == 16
    assert report["coverage"]["bytes_scanned"] == len(body) * 16
    assert report["coverage"]["truncated"] is True
    page_reads = [sql for sql in statements if "FROM store_pages" in sql]
    record_property("page_reads", len(page_reads))
    assert len(page_reads) <= 12, page_reads
    assert events["page_io"] == []
    assert len(events["snapshots"]) == 1
    with read.request_snapshot(stored):
        too_small = semantic_census.scan(stored, path=folder, max_files=16, max_bytes=len(body) - 1)
    assert too_small["coverage"]["markdown_files_scanned"] == 0
    assert too_small["coverage"]["bytes_scanned"] == 0
    with read.request_snapshot(stored):
        entry_limited = semantic_census.scan(stored, path=folder, max_files=1)
    assert entry_limited["coverage"]["directory_entry_budget_exhausted"] is True
    assert entry_limited["coverage"]["directory_entries_enumerated"] <= entry_limited["limits"]["max_directory_entries"]


def test_virtual_directory_does_not_follow_a_projection_symlink(stored, writer, tmp_path):  # noqa: F811
    """A damaged directory projection imports unrelated files from a symlink's target."""
    folder = "Knowledge Base/Notes/Virtual"
    body = b"- [finding] A canonical observation.\n"
    write.commit(writer, put(f"{folder}/page.md", body))
    shutil.rmtree(stored / folder, ignore_errors=True)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "unrelated.bin").write_bytes(b"unrelated content")
    (stored / folder).symlink_to(outside, target_is_directory=True)
    with read.request_snapshot(stored):
        report = overview.overview(stored, path=folder)
    assert report["totals"]["files"] == 1
    assert report["totals"]["bytes"] == len(body)
    shutil.rmtree(stored / "Knowledge Base")
    with read.request_snapshot(stored):
        assert overview.overview(stored, path=folder)["kb"]["present"] is True
        assert semantic_census.scan(stored, path=folder)["governance"]["kb_present"] is True


def test_bounded_viewer_keeps_stored_edges_and_reports_oversized_sources(
    stored, writer, monkeypatch,  # noqa: F811
):
    """Missing projection signatures erase every edge, or stored sizes bypass the source cap."""
    folder = "Knowledge Base/Notes/Viewer"
    source, target, large = (f"{folder}/{name}.md" for name in ("source", "target", "large"))
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test",
        puts=(
            write.PagePut(source, f"---\ntype: insight\n---\n## Relations\n\n- supports [[{target[:-3]}]]\n".encode()),
            write.PagePut(target, b"---\ntype: insight\n---\nA target.\n"),
            write.PagePut(large, b"x" * (65 * 1024)),
        ),
    ))
    shutil.rmtree(stored / folder, ignore_errors=True)
    with read.request_snapshot(stored):
        find.recall_resolver_snapshot(stored)
    command = next(command for command in commands.COMMANDS if command.name == "viewer_map")
    with record_store_reads(stored, monkeypatch) as events:
        result = invoke_command(command, stored)
    assert any(edge["src_key"] == f"file:{source}" and edge["dst_key"] == f"file:{target}"
               for edge in result["edges"]), result
    assert any(item["reason"] == "source_work_limit" for item in result["truncation"])
    assert all(item["reason"] != "live_signature_unavailable" for item in result["truncation"])
    with read.snapshot(stored) as snapshot:
        assert result["snapshot"]["generation"] == str(snapshot.revision)
    assert events["page_io"] == []
    assert events["sidecars"] == []
    assert events["machinery"] == []
    assert len(events["snapshots"]) == 1


def test_undecodable_census_pages_do_not_trigger_a_query_per_later_page(
    stored, writer, record_property,  # noqa: F811
):
    """Invalid UTF-8 consumes no file slot, leaving later pages outside the first bounded batch."""
    folder = "Knowledge Base/Notes/Unreadable"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test",
        puts=tuple(write.PagePut(f"{folder}/{index:03}.md", b"\xff") for index in range(16)) + (
            write.PagePut(f"{folder}/valid.md", b"- [finding] A readable observation.\n"),
        ),
    ))
    statements = []
    with read.request_snapshot(stored), read.snapshot(stored) as snapshot:
        snapshot.connection.set_trace_callback(statements.append)
        try:
            report = semantic_census.scan(stored, path=folder, max_files=1)
        finally:
            snapshot.connection.set_trace_callback(None)
    assert report["coverage"]["unreadable_files"] == 16
    assert report["coverage"]["markdown_files_scanned"] == 1
    page_reads = [sql for sql in statements if "body FROM store_pages" in sql]
    record_property("body_reads", len(page_reads))
    assert len(page_reads) <= 10, page_reads
