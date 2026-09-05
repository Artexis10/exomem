"""A committed response should support the next edit without an extra read."""

from pathlib import Path

import pytest

from exomem import commands, mutation_terminal, vault

PAGE = "Knowledge Base/Notes/Insights/closure.md"
PAGE_ID = "00000000-0000-4000-8000-000000000081"


def _compact(leaf):
    return mutation_terminal.project_terminal(mutation_terminal.committed_terminal(
        leaf, request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None, idempotency_key=None,
    ))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_edit_receipt_hash_chains_without_reading_again(tmp_path: Path, newline: str):
    page = tmp_path / PAGE
    page.parent.mkdir(parents=True)
    source = (
        "---\ntitle: Closure\ntype: insight\nstatus: active\n"
        f"exomem_id: {PAGE_ID}\nupdated: 2026-09-01\n---\n\n"
        "# Closure\n\nOriginal prose.\n"
    ).replace("\n", newline)
    page.write_bytes(source.encode())
    initial_hash = vault.content_hash(source)
    first = _compact(commands.op_edit_memory(
        tmp_path, path=PAGE, why="Complete the tracker",
        operation={"kind": "replace_string", "old_string": "Original prose.",
                   "new_string": "Completed prose.", "expected_hash": initial_hash},
    ))
    assert first["after_hash"] == vault.content_hash(page.read_bytes().decode())
    second = _compact(commands.op_edit_memory(
        tmp_path, path=PAGE, why="Add the remaining condition",
        operation={"kind": "replace_string", "old_string": "Completed prose.",
                   "new_string": "Completed prose. One follow-up remains.",
                   "expected_hash": first["after_hash"]},
    ))
    assert second["after_hash"] != first["after_hash"]
    assert second["after_hash"] == vault.content_hash(page.read_bytes().decode())


def test_observation_compact_preserves_producer_hashes_and_exact_unit_reference(tmp_path: Path):
    page = tmp_path / PAGE
    page.parent.mkdir(parents=True)
    page.write_text(
        f"---\ntitle: Closure\ntype: insight\nstatus: active\nexomem_id: {PAGE_ID}\n"
        "updated: 2026-09-01\n---\n\n# Closure\n\nExisting prose.\n"
    )
    leaf = commands.op_observe_memory(
        tmp_path, path=PAGE, category="fact", content="Certificate received",
    )
    compact = _compact(leaf)
    for key in ("before_hash", "after_hash", "unit_ref"):
        assert compact[key] == leaf[key]
    assert "unit" not in compact
    assert "semantic" not in compact
    assert commands.op_read_memory(tmp_path, path=PAGE, unit_ref=compact["unit_ref"])["status"] == "found"


@pytest.mark.parametrize("leaf", [
    {"after_hash": "private prose", "before_hash": "A" * 64},
    {"unit_ref": "https://example.invalid/secret", "removed_unit_ref": "x" * 10000},
    {"semantic": {"after_hash": "a" * 64, "path": "wrong.md"}},
])
def test_unvalidated_metadata_never_escapes_compact(leaf):
    compact = _compact({"path": PAGE, **leaf})
    assert not {"before_hash", "after_hash", "unit_ref", "removed_unit_ref"} & compact.keys()


def test_portable_receipt_does_not_fabricate_missing_metadata():
    assert "after_hash" not in _compact({})
