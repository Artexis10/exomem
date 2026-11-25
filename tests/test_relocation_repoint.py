"""A move repoints its append-only referrers through the governed pointer write.

OpenSpec add-curation-relocation-protocol, group 3. The vault is the canonical fixture in file
mode. Each test names the failure it catches.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from fastmcp import Client
from lifecycle_fixtures import PLANNING_PATH, planning_manifest
from test_collection_store_conversion import convert, serve, settled
from test_collection_store_migration import WORK, env, legacy_vault, migrate  # noqa: F401 - env fixture
from test_collection_store_s1_gate import _until
from test_collection_store_service import OWNER, call
from test_collection_store_writer import CID
from test_records_recall_policy import _manifest

from exomem import call_ledger, commands, move_file, pointer_write, repoint_links, writer_lease
from exomem import server as server_module
from exomem.governance.principal import request_scope

NOTE = "Knowledge Base/Notes/Insights/autovacuum-thresholds-prevent-table-bloat.md"
MOVED_NOTE = "Knowledge Base/Notes/Insights/Postgres/autovacuum-thresholds-prevent-table-bloat.md"
SOURCE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
EVIDENCE = "Knowledge Base/Evidence/Tuning/vacuum-measurements.md"
WRITABLE = "Knowledge Base/Notes/Insights/tuning-log.md"
ROW = "Knowledge Base/Records/Measurements/items/first-reading.md"
PLAN_ITEM = "Knowledge Base/Planning/Delivery/Items/tune-autovacuum.md"
MANAGE = next(c for c in commands.PRODUCT_COMMANDS if c.name == "manage_memory_file")


def _with_identity(vault: Path, rel: str) -> None:
    path = vault / rel
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("---\n", f"---\nexomem_id: {uuid.uuid4()}\n", 1), encoding="utf-8")


def _write(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _link(rel: str) -> str:
    return f"[[{rel.removesuffix('.md')}]]"


def _source_linking(vault: Path, target: str) -> Path:
    path = vault / SOURCE
    path.write_text(
        path.read_text(encoding="utf-8") + f"\nSee {_link(target)} for tuning.\n", encoding="utf-8"
    )
    return path


def _records_row_linking(vault: Path, target: str) -> Path:
    _write(vault, "Knowledge Base/Records/Measurements/_collection.md", _manifest())
    return _write(vault, ROW, f"---\nobserved: '{_link(target)}'\n---\nFirst reading.\n")


def _move(vault: Path, **kwargs):
    return writer_lease.invoke_command(
        MANAGE, vault, operation="move", old_path=NOTE, new_path=MOVED_NOTE, **kwargs
    )


def test_a_preview_gives_each_referrer_one_outcome_and_writes_nothing(vault: Path) -> None:
    """A preview that labels a repointed Source as rewritten, or writes any page."""
    _with_identity(vault, NOTE)
    pages = [
        _write(vault, WRITABLE, f"---\ntype: note\n---\nSee {_link(NOTE)}.\n"),
        _source_linking(vault, NOTE),
        _records_row_linking(vault, NOTE),
        vault / NOTE,
    ]
    before = [page.read_bytes() for page in pages]

    preview = _move(vault, validate_only=True)

    assert sorted((item["path"], item["outcome"]) for item in preview["referrers"]) == [
        (WRITABLE, "rewritten"),
        (ROW, "rewritten"),
        (SOURCE, "pointer-repointed (append-only page)"),
    ]
    assert preview["preview_digest"]
    assert [page.read_bytes() for page in pages] == before
    assert not (vault / MOVED_NOTE).exists()


def test_a_sealed_link_rewrite_leaves_out_a_referrer_a_restricted_caller_cannot_see(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A curation link rewrite that filters only a connector-limited caller, so the plan of a
    restricted caller outside any connector limit lists and binds a page withheld from it."""
    from exomem import repoint_links
    from exomem.governance import egress

    hidden = "Knowledge Base/Notes/Withheld/tuning-draft.md"
    _with_identity(vault, NOTE)
    _write(vault, WRITABLE, f"---\ntype: note\n---\nSee {_link(NOTE)}.\n")
    _write(vault, hidden, f"---\ntype: note\n---\nSee {_link(NOTE)}.\n")
    monkeypatch.setattr(egress, "caller_restricted", lambda _root: True)
    monkeypatch.setattr(egress, "restricted_release_filter", lambda _root: lambda path: path != hidden)

    sealed = repoint_links.prepare(vault, [(NOTE, MOVED_NOTE)], {})

    assert [item["path"] for item in sealed["referrers"]] == [WRITABLE]
    assert [page["path"] for page in sealed["pages"]] == [WRITABLE]


def test_a_committed_move_repoints_an_evidence_body_link_and_records_the_old_one(
    vault: Path,
) -> None:
    """A repoint that changes other bytes of the Evidence page, or leaves no record that holds
    the old link for its revert."""
    _with_identity(vault, NOTE)
    evidence = _write(
        vault, EVIDENCE, f"---\ntype: evidence\n---\n\nMeasured in {_link(NOTE)} on site.\n"
    )
    before = evidence.read_text(encoding="utf-8")

    preview = move_file.move_file(vault, old_path=NOTE, new_path=MOVED_NOTE, preview=True)
    moved = move_file.move_file(
        vault, old_path=NOTE, new_path=MOVED_NOTE, preview_digest=preview.preview_digest
    )

    assert evidence.read_text(encoding="utf-8") == before.replace(_link(NOTE), _link(MOVED_NOTE))
    [record_id] = moved.pointer_writes
    record = pointer_write.load_record(vault, record_id)
    assert record["page"] == EVIDENCE
    assert [(span["before"], span["after"]) for span in record["spans"]] == [
        (NOTE.removesuffix(".md"), MOVED_NOTE.removesuffix(".md"))
    ]


def test_a_move_whose_commit_fails_leaves_no_pointer_record(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record created before a commit that then fails: it names a write that never happened."""
    from exomem import private_state, semantic_writes

    _with_identity(vault, NOTE)
    evidence = _write(vault, EVIDENCE, f"---\ntype: evidence\n---\n\nMeasured in {_link(NOTE)}.\n")
    before = evidence.read_bytes()
    preview = move_file.move_file(vault, old_path=NOTE, new_path=MOVED_NOTE, preview=True)

    def fail(*_args, **_kwargs):
        raise semantic_writes.SemanticWriteError("SOURCE_CHANGED", "the source changed")

    monkeypatch.setattr(semantic_writes, "commit_move", fail)
    with pytest.raises(move_file.MoveFileError):
        move_file.move_file(
            vault, old_path=NOTE, new_path=MOVED_NOTE, preview_digest=preview.preview_digest
        )

    assert evidence.read_bytes() == before
    assert list(private_state.store(vault, pointer_write.STORE).iterdir()) == []


def test_a_move_rewrites_the_moved_path_in_a_records_row_and_a_planning_item(vault: Path) -> None:
    """A move that leaves a Records row or a Planning item on the old path, so it reaches the moved
    note only through the lineage, which is the fallback for references Evermatter cannot rewrite."""
    _with_identity(vault, NOTE)
    _write(vault, PLANNING_PATH, planning_manifest())
    pages = [
        _records_row_linking(vault, NOTE),
        _write(vault, PLAN_ITEM, f"---\ntitle: Tune autovacuum\n---\nFollows {_link(NOTE)}.\n"),
    ]
    before = [page.read_text(encoding="utf-8") for page in pages]

    _move(vault)

    assert [page.read_text(encoding="utf-8") for page in pages] == [
        text.replace(_link(NOTE), _link(MOVED_NOTE)) for text in before
    ]


def test_a_preview_names_the_outcome_of_rows_the_collection_store_keeps(env, monkeypatch, corpus_cache_on):  # noqa: F811
    """A preview that lists only pages where the collection store keeps rows without one, so the
    mover reads the listed referrers as all of them; or a stored row whose link to the old path
    no longer reaches the note once the note moves."""
    root = legacy_vault(env, relationships=True)
    assert migrate(root, "apply") == 0
    service = serve(root, monkeypatch)
    try:
        # The first page bootstraps the vault's relation review; the moved note links to it.
        first = call(root, "remember", content="# Bloat\n\n- [operating constraint] Bloat slows scans.\n",
                     title="Bloat", slug="bloat", suggestions=False)["path"]
        old = call(root, "remember", title="Thresholds", slug="thresholds", suggestions=False,
                   content=f"# Thresholds\n\n- [operating constraint] They bound {_link(first)}.\n")["path"]
        folder, name = old.rsplit("/", 1)
        new = f"{folder}/Tuning/{name}"
        work = call(root, "record_memory", action="query", collection=WORK)
        row = next(row for row in work["rows"] if row["title"] == "One")
        # A plain path under the knowledge-base folder, as an agent may write it.
        link = old.removeprefix("Knowledge Base/")
        call(root, "record_memory", action="update", collection=WORK, item_key=row["record_id"],
             changes={"related": link}, why="link the note",
             expected_container_hash=work["snapshot"], expected_item_version=row["item_version"])
        _until(lambda: settled(root, CID), 30)
        # The row now lives only in the store: no page holds it for a preview to find.
        convert(root, WORK)

        preview = call(root, "manage_memory_file", operation="move", old_path=old, new_path=new,
                       validate_only=True)
        with request_scope(OWNER):
            sealed = repoint_links.prepare(root, [(old, new)], {})
        call(root, "manage_memory_file", operation="move", old_path=old, new_path=new)

        assert preview["store_rows"] == sealed["store_rows"] == move_file.KEPT
        # The query resolves the stored ref through the link index that the service publishes.
        rows = call(root, "record_memory", action="query", collection=WORK)["rows"]
        # The write stored the note's ref for the path it was given, so the link follows the note.
        related = next(row for row in rows if row["title"] == "One").get("related")
        moved = call(root, "read_memory", path=new)
        assert related == moved["ref"]
        assert call(root, "read_memory", path=related)["path"] == new
        assert call(root, "read_memory", path=link)["path"] == new
    finally:
        service.stop()


@pytest.fixture
def connector(vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A call to one public tool of a local server, as a connected agent makes it."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    call_ledger.reset_chain_cache()
    mcp = server_module.build_server(require_auth=False)

    def call(tool: str, operation: str, arguments: dict) -> dict:
        async def run():
            async with Client(mcp) as client:
                body = {"operation": operation, "arguments": arguments}
                return (await client.call_tool(tool, body, raise_on_error=False)).structured_content

        return asyncio.run(run())

    yield call
    writer_lease.reset_managers_for_tests()


def test_a_move_that_repoints_a_source_commits_only_with_a_current_preview_digest(
    vault: Path, connector
) -> None:
    """A connected agent that cannot preview a move or pass its digest, a commit that changes a
    Source the call never named without the agent having seen the change, or one that accepts a
    digest after the Source changed."""
    _with_identity(vault, NOTE)
    source = _source_linking(vault, NOTE)
    move = {"old_path": NOTE, "new_path": MOVED_NOTE}
    first = connector("inspect_memory", "files.move", move)
    source.write_text(source.read_text(encoding="utf-8") + "\nA later capture note.\n", "utf-8")
    captured = source.read_text(encoding="utf-8")

    for digest in (None, first["preview_digest"]):
        refused = connector("manage_memory", "files.move", {**move, "preview_digest": digest})
        assert refused["error"]["code"] == "POINTER_PREVIEW_REQUIRED"
        assert "inspect_memory(operation='files.move'" in refused["error"]["message"]
        assert source.read_text(encoding="utf-8") == captured
        assert (vault / NOTE).is_file() and not (vault / MOVED_NOTE).exists()

    second = connector("inspect_memory", "files.move", move)
    connector("manage_memory", "files.move", {**move, "preview_digest": second["preview_digest"]})

    assert source.read_text(encoding="utf-8") == captured.replace(_link(NOTE), _link(MOVED_NOTE))


def test_a_committed_move_names_the_append_only_pages_it_repointed(vault: Path, connector) -> None:
    """A receipt that reports only the moved page, so the agent and the person never learn that
    the move also changed a Source the call did not name."""
    _with_identity(vault, NOTE)
    _source_linking(vault, NOTE)
    move = {"old_path": NOTE, "new_path": MOVED_NOTE}
    preview = connector("inspect_memory", "files.move", move)

    done = connector(
        "manage_memory", "files.move", {**move, "preview_digest": preview["preview_digest"]}
    )

    assert "Repointed the links to it in 1 append-only page" in done["user_summary"]
    assert done["pointer_repointed"] == ["Source: Best EGCG and Vitamin C Supplements"]


def test_a_promotion_that_repoints_no_other_captured_page_commits_without_a_digest(
    vault: Path,
) -> None:
    """A digest demanded of the call that names the page it moves, which would block every
    promotion of a Source."""
    promoted = "Knowledge Base/Evidence/Supplements/2026-05-04-best-egcg-supplements.md"

    writer_lease.invoke_command(
        MANAGE, vault, operation="move", old_path=SOURCE, new_path=promoted,
        promotion_reason="the dose data now backs a case",
    )

    assert (vault / promoted).is_file() and not (vault / SOURCE).exists()


def test_a_reclassification_repoints_an_evidence_link_and_its_revert_restores_the_bytes(
    vault: Path,
) -> None:
    """A reclassification that refuses a Source an Evidence page links by path, rewrites more
    than the link, or a revert that leaves the Evidence page other than it was."""
    _with_identity(vault, SOURCE)
    evidence = _write(
        vault, EVIDENCE, f"---\ntype: evidence\n---\n\nQuoted from {_link(SOURCE)}.\n"
    )
    original = evidence.read_text(encoding="utf-8")
    correction = {"source_kind": "research-report", "domain": "health"}

    proposal = writer_lease.invoke_command(
        MANAGE, vault, operation="propose-reclassification", path=SOURCE, **correction
    )
    moved = writer_lease.invoke_command(
        MANAGE, vault, operation="reclassify", path=SOURCE, reason="a survey, not an article",
        preview_digest=proposal["preview_digest"], **correction,
    )
    repointed = evidence.read_text(encoding="utf-8")
    writer_lease.invoke_command(
        MANAGE, vault, operation="revert-reclassification", path=moved["path"],
        reason="it was an article after all",
    )

    assert proposal["referrers"]["pointer_repointed"] == [EVIDENCE]
    assert moved["pointer_repointed"] == ["vacuum measurements"]
    assert repointed == original.replace(_link(SOURCE), _link(moved["path"]))
    assert evidence.read_text(encoding="utf-8") == original


def test_a_reclassified_source_does_not_say_how_many_pages_it_repointed(vault: Path) -> None:
    """A lineage entry that lists the pointer-write records, so any reader of the Source counts
    the append-only pages the correction repointed, withheld ones included; or a revert that can
    no longer find those records once the page names only the correction."""
    from exomem import private_state
    from exomem.vault import parse_frontmatter

    def newest_entry(rel: str) -> dict:
        return parse_frontmatter((vault / rel).read_text(encoding="utf-8"))[0]["moved_from"][-1]

    _with_identity(vault, SOURCE)
    correction = {"source_kind": "research-report", "domain": "health"}
    alone = writer_lease.invoke_command(
        MANAGE, vault, operation="reclassify", path=SOURCE, reason="a survey", **correction
    )
    entry_alone = newest_entry(alone["path"])
    writer_lease.invoke_command(
        MANAGE, vault, operation="revert-reclassification", path=alone["path"], reason="undo"
    )
    referrers = [
        _write(vault, EVIDENCE, f"---\ntype: evidence\n---\n\nQuoted from {_link(SOURCE)}.\n"),
        _write(
            vault, "Knowledge Base/Evidence/Tuning/dose-table.md",
            f"---\ntype: evidence\n---\n\nDoses as in {_link(SOURCE)}.\n",
        ),
    ]
    originals = [page.read_text(encoding="utf-8") for page in referrers]
    proposal = writer_lease.invoke_command(
        MANAGE, vault, operation="propose-reclassification", path=SOURCE, **correction
    )
    moved = writer_lease.invoke_command(
        MANAGE, vault, operation="reclassify", path=SOURCE, reason="a survey",
        preview_digest=proposal["preview_digest"], **correction,
    )
    moved_text = (vault / moved["path"]).read_text(encoding="utf-8")
    entry_two = newest_entry(moved["path"])
    record_ids = [path.stem for path in private_state.store(vault, pointer_write.STORE).iterdir()]
    operation_records = pointer_write.operation_records(vault, entry_two["operation"])
    writer_lease.invoke_command(
        MANAGE, vault, operation="revert-reclassification", path=moved["path"], reason="undo"
    )

    # One record for each repointed page and one for the Source's own lineage write.
    assert sorted(operation_records) == sorted(
        [*(page.relative_to(vault).as_posix() for page in referrers), moved["path"]]
    )
    assert not any(record_id in moved_text for record_id in record_ids)
    assert entry_two["operation"] != entry_alone["operation"]
    assert {**entry_two, "operation": None} == {**entry_alone, "operation": None}
    assert [page.read_text(encoding="utf-8") for page in referrers] == originals
