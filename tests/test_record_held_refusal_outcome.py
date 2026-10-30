"""A refused Records write that held its candidate is a refusal, not a commit.

Holding writes a file into the vault, but the caller's write did not happen: no
item was appended. The shared dispatcher must report the refusal with its held
reference, and a retry must run again instead of finding a stuck idempotency row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH, ledger_item, setup_ledger_collection

from exomem import commands, record_formats, writer_lease
from exomem import structured_collections as collections
from exomem.cli_ops import OpError

HELD_DIRECTORY = "Knowledge Base/Records/Publications/Held"


def _refuse_through_the_dispatcher(root: Path, *, mutation_request_id: str) -> Exception:
    """The undeclared-field append every MCP, REST and CLI caller goes through."""
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "record_memory")
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the type is asserted by the caller
        writer_lease.invoke_command(
            command,
            root,
            action="append",
            collection=LEDGER_COLLECTION_PATH,
            item=ledger_item(unlisted_channel="digest"),
            why="record a published entry",
            mutation_request_id=mutation_request_id,
        )
    return caught.value


def _held_files(root: Path) -> list[Path]:
    directory = root / HELD_DIRECTORY
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def _stored_items(root: Path) -> int:
    manifest = collections.load_manifest(root, root / LEDGER_COLLECTION_PATH)
    return len(record_formats.load_adapter(root, manifest).read().records)


def _assert_refused_with_a_held_reference(error: Exception, root: Path) -> dict:
    # The bug surfaced as MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN, then MUTATION_OUTCOME_UNKNOWN.
    assert isinstance(error, OpError), repr(error)
    assert error.code == "SCHEMA_UNKNOWN_FIELD", repr(error)
    assert error.details.get("committed") is not True
    held = error.details["held"]
    assert held["path"] == f"{HELD_DIRECTORY}/{held['held_id']}.md"
    assert [path.name for path in _held_files(root)] == [f"{held['held_id']}.md"]
    assert _stored_items(root) == 0
    return held


def test_a_refused_append_is_reported_as_a_refusal_with_its_held_reference(
    tmp_path: Path,
) -> None:
    setup_ledger_collection(tmp_path)

    error = _refuse_through_the_dispatcher(tmp_path, mutation_request_id="refused-append")

    _assert_refused_with_a_held_reference(error, tmp_path)


def test_an_identical_retry_of_a_refused_append_refuses_again_with_one_held_file(
    tmp_path: Path,
) -> None:
    setup_ledger_collection(tmp_path)

    first = _refuse_through_the_dispatcher(tmp_path, mutation_request_id="refused-append")
    held = _assert_refused_with_a_held_reference(first, tmp_path)

    retried = _refuse_through_the_dispatcher(tmp_path, mutation_request_id="refused-append")

    # The same single Held file, not a second one: the retry ran and rewrote it.
    assert _assert_refused_with_a_held_reference(retried, tmp_path) == held
