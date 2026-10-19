"""`browse_memory` list returns bounded pages that resume from a cursor.

An unbounded listing read every file of a folder and decided every entry
twice, so one large folder took minutes and slowed every concurrent call. A
page now costs the entries it returns, and a cursor continues the listing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import commands, list_directory
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

_BULK = "Knowledge Base/Notes/Bulk"


def _write_notes(vault: Path, names: list[str]) -> None:
    folder = vault / _BULK
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / f"{name}.md").write_text(
            f"---\ntype: note\ntitle: {name}\n---\n\nBody of {name}.\n", encoding="utf-8"
        )


def _paths(page: dict) -> list[str]:
    return [entry["path"] for entry in page["entries"]]


def test_a_large_folder_returns_one_bounded_page_with_a_cursor(vault: Path) -> None:
    _write_notes(vault, [f"note-{index:04d}" for index in range(201)])

    page = commands.op_browse_memory(vault, path=_BULK, mode="list")

    assert len(page["entries"]) == 200
    assert page["truncated"] is True
    assert page["next_cursor"]


def test_following_cursors_yields_the_whole_listing_once_in_order(vault: Path) -> None:
    _write_notes(vault, [f"note-{index:02d}" for index in range(20)])
    (vault / _BULK / "zeta-folder").mkdir()
    (vault / _BULK / "alpha-folder").mkdir()
    whole = commands.op_list_directory(vault, path=_BULK)

    pages = [commands.op_browse_memory(vault, path=_BULK, mode="list", limit=7)]
    while pages[-1]["truncated"]:
        pages.append(commands.op_browse_memory(
            vault, path=_BULK, mode="list", limit=7, cursor=pages[-1]["next_cursor"]
        ))

    assert [path for page in pages for path in _paths(page)] == _paths(whole)
    assert [len(page["entries"]) for page in pages] == [7, 7, 7, 1]


def test_a_page_that_ends_at_the_last_entry_reports_no_further_page(vault: Path) -> None:
    _write_notes(vault, ["one", "two", "three", "four", "five"])

    page = commands.op_browse_memory(vault, path=_BULK, mode="list", limit=5)

    assert len(page["entries"]) == 5
    assert page["truncated"] is False
    assert page["next_cursor"] is None


def test_a_cursor_from_another_listing_is_refused(vault: Path) -> None:
    _write_notes(vault, ["one", "two", "three"])
    cursor = commands.op_browse_memory(vault, path=_BULK, mode="list", limit=1)["next_cursor"]

    with pytest.raises(ValueError, match="INVALID_CURSOR"):
        commands.op_browse_memory(vault, path="Knowledge Base/Notes", mode="list", cursor=cursor)


def test_withheld_entries_neither_shorten_a_page_nor_appear_in_a_cursor(vault: Path) -> None:
    _write_notes(vault, ["a-open", "b-secret", "c-open", "d-secret", "e-open", "f-open", "g-secret", "h-open"])
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "secret.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Secret\npaths: ["Notes/Bulk/*-secret.md"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "secret-external.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\nscope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        first = commands.op_browse_memory(vault, path=_BULK, mode="list", limit=4)
        second = commands.op_browse_memory(
            vault, path=_BULK, mode="list", limit=4, cursor=first["next_cursor"]
        )

    names = [Path(path).stem for path in _paths(first) + _paths(second)]
    assert names == ["a-open", "c-open", "e-open", "f-open", "h-open"]
    assert len(first["entries"]) == 4
    resumed_after = list_directory.decode_cursor(first["next_cursor"], _BULK, False)
    assert "secret" not in resumed_after[2]


def test_the_owners_page_and_cursor_skip_a_page_whose_deletion_is_pending(vault: Path) -> None:
    """The owner's leaf filters nothing, but egress drops a page being deleted.

    A cursor built from that page would name it to a client that never received it.
    """
    from exomem.governance import lifecycle

    _write_notes(vault, ["a-open", "b-secret", "c-open", "d-open"])
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "secret.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Secret\npaths: ["Notes/Bulk/*-secret.md"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "secret-external.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\nscope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )
    # Governed, so the deletion writes the tombstone that egress honours.
    lifecycle.begin_deletion(
        vault, source_rel=f"{_BULK}/b-secret.md", trash_rel="Knowledge Base/_trash/b-secret.md"
    )

    # The local owner holds whole-corpus authority, so the leaf withholds nothing.
    with request_scope(owner_principal(surface="cli")):
        first = commands.op_browse_memory(vault, path=_BULK, mode="list", limit=2)

    assert [Path(path).stem for path in _paths(first)] == ["a-open", "c-open"]
    assert list_directory.decode_cursor(first["next_cursor"], _BULK, False)[2] == f"{_BULK}/c-open.md"


@pytest.mark.skipif(os.name != "posix", reason="counts reads on the POSIX held filesystem")
def test_a_page_reads_only_the_files_it_returns(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import _held_fs_posix

    _write_notes(vault, [f"note-{index:03d}" for index in range(60)])
    reads: list[object] = []
    real_read = _held_fs_posix.PosixHeldFilesystem.read

    def counted_read(self, file, **kwargs):
        reads.append(file)
        return real_read(self, file, **kwargs)

    monkeypatch.setattr(_held_fs_posix.PosixHeldFilesystem, "read", counted_read)

    page = commands.op_browse_memory(vault, path=_BULK, mode="list", limit=10)

    assert len(page["entries"]) == 10
    assert len(reads) <= 10
