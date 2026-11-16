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
from test_records_recall_policy import _manifest

from exomem import call_ledger, commands, move_file, pointer_write, writer_lease
from exomem import server as server_module
from exomem import vault as vault_module

NOTE = "Knowledge Base/Notes/Insights/autovacuum-thresholds-prevent-table-bloat.md"
MOVED_NOTE = "Knowledge Base/Notes/Insights/Postgres/autovacuum-thresholds-prevent-table-bloat.md"
SOURCE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
EVIDENCE = "Knowledge Base/Evidence/Tuning/vacuum-measurements.md"
WRITABLE = "Knowledge Base/Notes/Insights/tuning-log.md"
ROW = "Knowledge Base/Records/Measurements/items/first-reading.md"
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
    """A preview that labels a repointed Source as rewritten, rewrites a collection's row instead
    of leaving it to the lineage, or writes any page."""
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
        (ROW, "unchanged: resolves via prior path"),
        (SOURCE, "pointer-repointed (append-only page)"),
    ]
    assert preview["preview_digest"]
    assert [page.read_bytes() for page in pages] == before
    assert not (vault / MOVED_NOTE).exists()


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


def test_a_records_row_keeps_its_bytes_and_its_link_follows_the_moved_note(vault: Path) -> None:
    """A move that rewrites a row its collection's writer owns, or that leaves the row's link
    dangling."""
    _with_identity(vault, NOTE)
    row = _records_row_linking(vault, NOTE)
    before = row.read_bytes()

    _move(vault)

    assert row.read_bytes() == before
    assert vault_module.normalize_wikilink(NOTE.removesuffix(".md"), vault) == (
        MOVED_NOTE.removesuffix(".md"),
        None,
    )


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
    assert repointed == original.replace(_link(SOURCE), _link(moved["path"]))
    assert evidence.read_text(encoding="utf-8") == original
