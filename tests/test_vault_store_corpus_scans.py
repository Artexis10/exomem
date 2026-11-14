"""Whole-corpus readers retain stored pages without a query per page."""

from __future__ import annotations

import json
import os
import sys

import pytest
from test_artifact_role_projection import write as write_role
from test_artifact_role_review import method
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_graph_readers import record_store_reads, write_pages
from test_vault_store_migration import (  # noqa: F401
    build_indexes,
    migrate_apply,
    rollback_apply,
    vault,
)

from exomem import audit, commands, due_state, find_corpus, get_page, lexstore
from exomem.vault import kb_root
from exomem.vault_store import directory, mode, ownership, read

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only"),
    pytest.mark.timeout(180),
]


def test_audit_reads_stored_corpus_in_one_query(vault, monkeypatch):  # noqa: F811
    """A deleted projection must keep its findings without N keyed body or metadata reads."""
    generated = {
        f"Knowledge Base/Notes/Signal-{number}.md": (
            f"---\ntitle: Signal {number}\ntype: note\n---\n# Signal {number}\n\n"
            f"The lantern records signal {number}.\n"
        )
        for number in range(24)
    }
    write_pages(vault, generated)
    build_indexes(vault)
    expected = {
        page.rel_path: (page.body, page.mtime)
        for page in audit._parse_all(kb_root(vault), vault, keep=None)
    }
    owned = {
        item.rel for item in ownership.iter_owned(vault)
        if item.ownership is ownership.Ownership.PAGE
    }
    assert migrate_apply(vault) == 0
    for rel in owned:
        (vault / rel).unlink()
    statements = []
    with record_store_reads(vault, monkeypatch, page_paths=owned) as events:
        with read.request_snapshot(vault), read.snapshot(vault) as view:
            view.connection.set_trace_callback(statements.append)
            try:
                actual = {
                    page.rel_path: (page.body, page.mtime)
                    for page in audit._parse_all(kb_root(vault), vault, keep=None)
                }
                # Audit composes several corpus consumers inside the same request.
                repeated = {
                    page.rel_path: (page.body, page.mtime)
                    for page in audit._parse_all(kb_root(vault), vault, keep=None)
                }
            finally:
                view.connection.set_trace_callback(None)
    assert actual == expected
    assert repeated == actual
    selects = [sql for sql in statements if sql.startswith("SELECT") and "store_pages" in sql]
    assert len(selects) == 1, selects
    assert not events["page_io"]
    assert len(events["snapshots"]) == 1
    assert not events["sidecars"]
    assert not events["machinery"]


def test_audit_uses_current_unowned_link_targets(vault):  # noqa: F811
    """A changed unowned title must change the audit's link findings beside damaged projections."""
    external = vault / "Reference" / "Signal.md"
    external.parent.mkdir()
    external.write_text("---\ntitle: Earlier Reference\n---\n# Reference signal\n", encoding="utf-8")
    alpha = "Knowledge Base/Notes/Alpha.md"
    with (vault / alpha).open("a", encoding="utf-8") as page:
        page.write("\n[[Earlier Reference]] and [[Current Reference]].\n")
    build_indexes(vault)
    lexstore.search_bm25(vault, "signal", 3, scope="vault", repair=True)
    before = commands.op_audit(vault, categories=["forward_reference"], detail="full")
    findings = [row for row in before["findings"] if row["path"] == alpha]
    assert len(findings) == 1
    assert "Current Reference" in findings[0]["detail"]
    assert migrate_apply(vault) == 0
    external.write_text("---\ntitle: Current Reference\n---\n# Reference signal\n", encoding="utf-8")
    for item in ownership.iter_owned(vault):
        if item.ownership is ownership.Ownership.PAGE:
            (vault / item.rel).write_text("Damaged projection", encoding="utf-8")
    with read.request_snapshot(vault):
        after = commands.op_audit(vault, categories=["forward_reference"], detail="full")
    findings = [row for row in after["findings"] if row["path"] == alpha]
    assert len(findings) == 1
    assert "Earlier Reference" in findings[0]["detail"]


def test_log_reads_keep_unprojected_history(vault):  # noqa: F811
    """Review and census history must include a rotated log after its projection disappears."""
    from exomem import vault as vault_module

    archive = vault / "Knowledge Base/_archive/logs/log-2025-01.md"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("## [2025-01-02] edit | Notes/Alpha\n\nClarified the signal.\n", encoding="utf-8")
    live = vault / "Knowledge Base/log.md"
    live.write_bytes(
        b"## [2025-01-03] edit | Notes/Alpha\r\n\r\nUpdated the signal.\r\nSecond line.\r"
    )
    build_indexes(vault)
    expected_census = vault_module.read_operation_log(vault)
    expected_history = vault_module.read_log_entries(vault, "Knowledge Base/Notes/Alpha.md")
    assert expected_census["files_read"] == 2
    assert migrate_apply(vault) == 0
    archive.unlink()
    live.unlink()
    with read.request_snapshot(vault):
        assert vault_module.read_operation_log(vault) == expected_census
        assert vault_module.read_log_entries(vault, "Knowledge Base/Notes/Alpha.md") == expected_history


def test_stored_page_keeps_the_file_timestamp(vault):  # noqa: F811
    """A direct page read must retain the exact advisory timestamp after migration."""
    rel = "Knowledge Base/Notes/Alpha.md"
    path = vault / rel
    # This timestamp exposes the rounding difference between a float division and a file stat.
    timestamp_ns = 1_794_532_852_076_726_900
    os.utime(path, ns=(timestamp_ns, timestamp_ns))
    build_indexes(vault)
    expected = get_page.get_page(vault, path=rel).as_dict()
    assert migrate_apply(vault) == 0
    path.unlink()
    with read.request_snapshot(vault):
        assert get_page.get_page(vault, path=rel).as_dict() == expected


def test_mode_changes_keep_existing_role_advice(vault, monkeypatch, tmp_path):  # noqa: F811
    """Migration and rollback must preserve review obligations when their fingerprint owner changes."""
    write_role(vault, method())
    build_indexes(vault)
    due_state.reconcile(vault)
    before = commands.op_bootstrap(vault, profile="full")
    assert before["due_state"]["categories"]["artifact_role_promotion"] == 1
    assert migrate_apply(vault) == 0
    owned = {
        item.rel: (vault / item.rel).read_bytes() for item in ownership.iter_owned(vault)
        if item.ownership is ownership.Ownership.PAGE
    }
    for rel in owned:
        (vault / rel).unlink()
    with record_store_reads(vault, monkeypatch, page_paths=owned) as events:
        with read.request_snapshot(vault):
            after = commands.op_bootstrap(vault, profile="full")
    assert after["due_state"]["categories"]["artifact_role_promotion"] == 1
    assert not events["page_io"]
    assert len(events["snapshots"]) == 1
    assert not events["sidecars"]
    assert not events["machinery"]
    for rel, body in owned.items():
        (vault / rel).write_bytes(body)
    assert rollback_apply(vault, tmp_path / "export") == 0
    restored = commands.op_bootstrap(vault, profile="full")
    assert restored["due_state"]["categories"]["artifact_role_promotion"] == 1


def test_review_refresh_failure_preserves_committed_migration(vault, monkeypatch, capsys):  # noqa: F811
    """An advisory-cache error must not claim that the canonical cutover rolled back."""
    due_state.reconcile(vault)

    def unavailable(_root):
        raise OSError("review state unavailable")

    monkeypatch.setattr(due_state, "reconcile", unavailable)
    assert migrate_apply(vault) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "migrated"
    assert mode.store_mode_for_reads(vault)
    assert result["review_advice"]["state"] == "refresh_failed"
    assert result["warnings"]
    assert result["diagnostics"]["review_advice"]["error"] == "OSError"


def test_preparation_keeps_stat_precision(vault):  # noqa: F811
    """Prepared graph inputs must retain stat precision in either canonical backend."""
    from exomem import runtime_resources

    rel = "Knowledge Base/Notes/Alpha.md"
    path = vault / rel
    stamp = 1_794_532_852_076_726_900
    os.utime(path, ns=(stamp, stamp))
    expected = path.stat().st_mtime
    assert stamp / 1e9 != expected
    with runtime_resources.semantic_preparation(vault, path) as prepared:
        assert prepared.mtime == expected
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    path.unlink()
    with read.request_snapshot(vault):
        with runtime_resources.semantic_preparation(vault, path) as prepared:
            assert prepared.mtime == expected


def test_missing_parent_keeps_the_file_read_refusal(vault, monkeypatch):  # noqa: F811
    """A stale derived row whose parent disappears must report missing_parent in either backend."""
    from exomem import semantic_index
    from exomem import vault as vault_module

    missing = "Knowledge Base/Notes/Absent.md"
    path = vault / missing
    path.write_text("# A parent that will disappear\n", encoding="utf-8")
    arguments = dict(parent_path=missing, parent_generation_value="old",
                     parent_source_hash="old", parser_version=semantic_index.PARSER_VERSION)
    guarded_read = vault_module.read_guarded_text

    def disappeared(root, target, **kwargs):
        if target == path:
            path.unlink()
        return guarded_read(root, target, **kwargs)

    with monkeypatch.context() as race:
        race.setattr(vault_module, "read_guarded_text", disappeared)
        expected = semantic_index.validate_parent_record(vault, **arguments).code
    assert expected == "missing_parent"
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    with read.request_snapshot(vault):
        assert semantic_index.validate_parent_record(vault, **arguments).code == expected


def test_overview_batches_only_kept_directory_bodies(vault, monkeypatch):  # noqa: F811
    """Structure reports must not retain every body or fetch bodies from pruned subtrees."""
    generated = {
        f"Knowledge Base/Notes/{folder}/Page-{number}.md":
            f"---\ntitle: Signal {number}\n---\n# Signal\nA lantern observation.\n"
        for folder in ("Kept", "Later", "_trash", ".hidden") for number in range(40)
    }
    write_pages(vault, generated)
    build_indexes(vault)
    expected = commands.op_overview(vault, path="Knowledge Base/Notes")
    assert migrate_apply(vault) == 0
    for rel in generated:
        (vault / rel).unlink()
    prefetch = read.StoreSnapshot.prefetch_pages
    batches = []
    peak_bodies = []

    def observed(view, paths):
        paths = tuple(paths)
        batches.append(paths)
        result = prefetch(view, paths)
        peak_bodies.append(sum(page.meta.size for page in view.memo.get("pages", {}).values()
                               if page is not None))
        return result

    monkeypatch.setattr(read.StoreSnapshot, "prefetch_pages", observed)
    with read.request_snapshot(vault):
        assert commands.op_overview(vault, path="Knowledge Base/Notes") == expected
    fetched = {path for batch in batches for path in batch}
    assert {path for path in generated if "/Kept/" in path or "/Later/" in path} <= fetched
    assert not {path for path in generated if "/_trash/" in path or "/.hidden/" in path} & fetched
    assert max(map(len, batches)) <= 32
    assert max(peak_bodies) <= 32 * 512 * 1024


@pytest.mark.parametrize("corpus_first", [False, True])
def test_directory_scans_preserve_metadata_held_by_the_corpus(vault, corpus_first):  # noqa: F811
    """A later directory scan must not make an already loaded corpus page disappear."""
    assert migrate_apply(vault) == 0
    rel = "Knowledge Base/Notes/Alpha.md"
    (vault / rel).unlink()
    with read.request_snapshot(vault), read.snapshot(vault) as view:
        reader = directory.Reader(vault)
        if corpus_first:
            list(view.page_sources())
        reader.entries(vault / "Knowledge Base/Notes")
        list(view.page_sources())
        expected = read.owned_meta(vault, rel)
        assert expected is not None
        reader.entries(vault / "Knowledge Base")
        assert read.owned_meta(vault, rel) == expected


def test_file_page_identity_refuses_paths_outside_the_vault(vault, tmp_path):  # noqa: F811
    """An escaped file identity must never enter a parser or a release decision as an absolute path."""
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the vault"):
        find_corpus.page_relative_path(outside, vault)
    linked = vault / "Knowledge Base/Notes/Linked.md"
    linked.symlink_to(outside)
    with pytest.raises(ValueError, match="outside the vault"):
        find_corpus.page_relative_path(linked, vault)
    assert find_corpus.page_relative_path(vault / "Knowledge Base/Notes/Alpha.md", vault) == (
        "Knowledge Base/Notes/Alpha.md"
    )


def test_tag_preview_batches_canonical_metadata_and_bodies(vault, record_property):  # noqa: F811
    """Tag maintenance must not issue one metadata or body query per candidate page."""
    from test_vault_store_readers import EXTERNAL, restrict_external

    from exomem.governance.principal import request_scope
    from exomem.writer_lease import invoke_command

    generated = {
        f"Knowledge Base/Notes/Batch Tag-{number}.md":
            f"---\ntitle: Batch Tag {number}\ntags: [{tag}]\n---\n# Tag evidence\n"
        for number, tag in enumerate(["harbors"] * 40 + ["harbor"] * 80)
    }
    generated["Knowledge Base/Notes/Batch Tag-secret.md"] = (
        "---\ntitle: Hidden tag evidence\ntags: [harbors, secret]\n---\n# Private tag evidence\n"
    )
    restrict_external(vault, 'tags: ["secret"]')
    write_pages(vault, generated)
    build_indexes(vault)
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "maintain_memory")
    with request_scope(EXTERNAL):
        expected = invoke_command(command, vault, mode="tag-variants")
    assert migrate_apply(vault) == 0
    statements = []
    with request_scope(EXTERNAL), read.request_snapshot(vault), read.snapshot(vault) as view:
        view.connection.set_trace_callback(statements.append)
        try:
            actual = invoke_command(command, vault, mode="tag-variants")
        finally:
            view.connection.set_trace_callback(None)
    assert actual == expected
    page_queries = [
        sql for sql in statements
        if sql.lstrip().upper().startswith("SELECT")
        and "store_pages" in sql.lower() and "batch tag-" in sql.lower()
    ]
    record_property("generated_tagged_pages", len(generated))
    record_property("tag_page_queries", len(page_queries))
    assert len(page_queries) <= 8, page_queries
