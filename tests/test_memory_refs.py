from __future__ import annotations

import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from exomem import add, commands, link, memory_refs, note, preserve
from exomem import vault as vault_module

TODAY = dt.date(2026, 7, 9)


def _page(identity: str | None, body: str = "# Page\n") -> str:
    id_line = f"\nexomem_id: {identity}" if identity is not None else ""
    return f"---\ntype: insight\ncreated: 2026-07-09{id_line}\n---\n\n{body}"


def test_reference_round_trip_and_incremental_move(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    old = vault / "Knowledge Base" / "Notes" / "old.md"
    old.parent.mkdir(parents=True)
    identity = memory_refs.new_id()
    old.write_text(_page(identity), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all() == {"indexed": 1, "duplicates": 0, "malformed": 0}
    ref = memory_refs.memory_ref(identity)
    assert memory_refs.parse_memory_ref(ref) == identity
    assert memory_refs.resolve_identifier(vault, ref) == "Knowledge Base/Notes/old.md"

    index.path.unlink()
    assert memory_refs.resolve_identifier(vault, ref) == "Knowledge Base/Notes/old.md"
    assert index.available(), "canonical resolution should rebuild a missing sidecar"

    new = old.with_name("new.md")
    old.rename(new)
    index.refresh_paths([new])
    index.delete_paths(["Knowledge Base/Notes/old.md"])
    assert index.resolve(identity) == "Knowledge Base/Notes/new.md"


def test_bulk_reference_lookup_uses_index_and_refreshes_only_missing_paths(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    first_id = memory_refs.new_id()
    second_id = memory_refs.new_id()
    first_path = "Knowledge Base/Notes/first.md"
    second_path = "Knowledge Base/Notes/second.md"
    (vault / first_path).write_text(_page(first_id), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    index.rebuild_all()
    # Simulate a page created after the sidecar was last refreshed.
    (vault / second_path).write_text(_page(second_id), encoding="utf-8")

    resolved = index.refs_for_paths(
        [first_path, "\\Knowledge Base\\Notes\\second.md", first_path, "missing.md"]
    )

    assert resolved == {
        first_path: memory_refs.memory_ref(first_id),
        second_path: memory_refs.memory_ref(second_id),
        "missing.md": None,
    }


def test_bulk_reference_lookup_answers_phantom_casing_in_the_callers_form(
    tmp_path: Path, monkeypatch
) -> None:
    """A caller may spell a page's folder differently than the disk does.

    Sidecar rows are keyed by the on-disk spelling (`refresh_paths` resolves),
    so the lookup has to canonicalize — but the answer must stay keyed by what
    the caller passed in, because callers index the result by their own string.
    The casefold is modelled here so the contract is pinned on Linux CI too.
    """
    vault = tmp_path / "vault"
    folder = vault / "Knowledge Base" / "Notes" / "POLLY"
    folder.mkdir(parents=True)
    identity = memory_refs.new_id()
    (folder / "terms.md").write_text(_page(identity), encoding="utf-8")
    on_disk = "Knowledge Base/Notes/POLLY/terms.md"
    phantom = "Knowledge Base/Notes/Polly/terms.md"

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all() == {"indexed": 1, "duplicates": 0, "malformed": 0}
    monkeypatch.setattr(
        memory_refs.vault_module,
        "canonical_vault_rel",
        lambda _root, rel: on_disk if rel.casefold() == on_disk.casefold() else rel,
    )

    ref = memory_refs.memory_ref(identity)
    assert index.refs_for_paths([phantom]) == {phantom: ref}
    assert index.ref_for_path(phantom) == ref


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="phantom path casing only names the same page on a case-insensitive filesystem",
)
def test_delete_paths_removes_the_canonical_row_for_phantom_casing(tmp_path: Path) -> None:
    """A delete addressed with the caller's casing must not orphan the real row.

    On NTFS `.../Polly/terms.md` and `.../POLLY/terms.md` open one file, but the
    sidecar keys rows as case-sensitive text: a stale row survives the delete and
    then reads back as a second owner of the identity.
    """
    vault = tmp_path / "vault"
    folder = vault / "Knowledge Base" / "Notes" / "POLLY"
    folder.mkdir(parents=True)
    identity = memory_refs.new_id()
    page = folder / "terms.md"
    page.write_text(_page(identity), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all() == {"indexed": 1, "duplicates": 0, "malformed": 0}
    assert index.resolve(identity) == "Knowledge Base/Notes/POLLY/terms.md"

    page.unlink()
    index.delete_paths(["Knowledge Base/Notes/Polly/terms.md"])

    with pytest.raises(memory_refs.ReferenceError) as excinfo:
        index.resolve(identity)
    assert excinfo.value.code == "REFERENCE_NOT_FOUND"


def test_legacy_negative_reference_is_indexed_without_repeat_scan(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    legacy_path = "Knowledge Base/Notes/legacy.md"
    legacy = vault / legacy_path
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_page(None), encoding="utf-8")

    calls = 0
    original_scan = memory_refs._scan_pages

    def counted_scan(root: Path):
        nonlocal calls
        calls += 1
        return original_scan(root)

    monkeypatch.setattr(memory_refs, "_scan_pages", counted_scan)
    index = memory_refs.ReferenceIndex(vault)

    assert index.refs_for_paths([legacy_path]) == {legacy_path: None}
    assert index.refs_for_paths([legacy_path]) == {legacy_path: None}
    assert index.ref_for_path(legacy_path) is None
    assert calls == 1


def test_bulk_reference_lookup_does_not_scan_per_result(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    paths = []
    for number in range(30):
        path = notes / f"legacy-{number}.md"
        path.write_text(_page(None), encoding="utf-8")
        paths.append(path.relative_to(vault).as_posix())

    calls = 0
    original_scan = memory_refs._scan_pages

    def counted_scan(root: Path):
        nonlocal calls
        calls += 1
        return original_scan(root)

    monkeypatch.setattr(memory_refs, "_scan_pages", counted_scan)
    index = memory_refs.ReferenceIndex(vault)

    assert all(ref is None for ref in index.refs_for_paths(paths).values())
    assert all(ref is None for ref in index.refs_for_paths(paths).values())
    assert calls == 1


def test_concurrent_reference_lookup_shares_initial_rebuild(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    legacy_path = "Knowledge Base/Notes/legacy.md"
    legacy = vault / legacy_path
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_page(None), encoding="utf-8")

    calls = 0
    original_scan = memory_refs._scan_pages

    def counted_scan(root: Path):
        nonlocal calls
        calls += 1
        return original_scan(root)

    monkeypatch.setattr(memory_refs, "_scan_pages", counted_scan)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: memory_refs.ReferenceIndex(vault).ref_for_path(legacy_path),
                range(16),
            )
        )

    assert results == [None] * 16
    assert calls == 1


def test_legacy_negative_reference_refreshes_when_identity_is_added(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    legacy_path = "Knowledge Base/Notes/legacy.md"
    legacy = vault / legacy_path
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_page(None), encoding="utf-8")
    index = memory_refs.ReferenceIndex(vault)

    assert index.ref_for_path(legacy_path) is None
    identity = memory_refs.new_id()
    legacy.write_text(_page(identity), encoding="utf-8")
    index.refresh_paths([legacy])

    assert index.ref_for_path(legacy_path) == memory_refs.memory_ref(identity)


def test_ask_memory_enriches_references_once(vault: Path, monkeypatch) -> None:
    calls = 0
    original = memory_refs.ReferenceIndex.refs_for_paths

    def counted(self, paths: list[str]):
        nonlocal calls
        calls += 1
        return original(self, paths)

    monkeypatch.setattr(memory_refs.ReferenceIndex, "refs_for_paths", counted)

    commands.op_ask_memory(
        vault,
        query="metabolism",
        mode="keyword",
        limit=5,
        detail="compact",
    )

    assert calls == 1


def test_duplicate_and_malformed_ids_are_diagnostic_and_self_healing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    identity = memory_refs.new_id()
    first = notes / "first.md"
    second = notes / "second.md"
    broken = notes / "broken.md"
    first.write_text(_page(identity), encoding="utf-8")
    second.write_text(_page(identity), encoding="utf-8")
    broken.write_text(_page("not-a-uuid"), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all() == {"indexed": 2, "duplicates": 1, "malformed": 1}
    issues = index.issues()
    assert [item["kind"] for item in issues] == ["duplicate", "duplicate", "malformed"]
    try:
        index.resolve(identity)
    except memory_refs.ReferenceError as exc:
        assert exc.code == "AMBIGUOUS_REFERENCE"
    else:
        raise AssertionError("duplicate identity unexpectedly resolved")

    second.unlink()
    index.delete_paths(["Knowledge Base/Notes/second.md"])
    assert index.resolve(identity) == "Knowledge Base/Notes/first.md"


# --------------------------------------------------------------------------
# Identity collisions are a path oracle unless the error is content-free
#
# Merging or duplicating identities is what manufactures a collision, so a
# caller can manufacture one and then read the colliding vault paths straight
# out of the error text — for pages it may hold no release decision over.
# --------------------------------------------------------------------------


def _collide(tmp_path: Path, *names: str) -> tuple[Path, str]:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    identity = memory_refs.new_id()
    for name in names:
        target = notes / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_page(identity), encoding="utf-8")
    return vault, identity


@pytest.mark.parametrize("resolver", ["index", "read_only"])
def test_ambiguous_reference_carries_a_count_not_the_colliding_paths(
    tmp_path: Path, resolver: str
) -> None:
    vault, identity = _collide(tmp_path, "secret-alpha.md", "secret-beta.md")
    index = memory_refs.ReferenceIndex(vault)
    index.rebuild_all()

    with pytest.raises(memory_refs.ReferenceError) as excinfo:
        if resolver == "index":
            index.resolve(identity)
        else:
            memory_refs.resolve_identifier_read_only(
                vault, memory_refs.memory_ref(identity)
            )

    error = excinfo.value
    assert error.code == "AMBIGUOUS_REFERENCE"
    text = f"{error.code}: {error.reason}"
    for leaked in (
        "secret-alpha",
        "secret-beta",
        ".md",
        "Knowledge Base",
        "Notes/",
        "[",
    ):
        assert leaked not in text, f"collision error leaked {leaked!r}: {text!r}"
    assert "2" in text, f"the match COUNT is what an owner needs: {text!r}"


@pytest.mark.parametrize("resolver", ["index", "read_only"])
def test_ambiguous_reference_text_does_not_depend_on_which_pages_collide(
    tmp_path: Path, resolver: str
) -> None:
    """Indistinguishability. Two vaults whose colliding pages share nothing but
    the identity and the match count must produce byte-identical error text —
    otherwise the message is an oracle for what is stored where."""

    def _reason(root: Path, *names: str) -> str:
        vault, identity = _collide(root, *names)
        # Same identity in both vaults, so only the PATHS differ.
        index = memory_refs.ReferenceIndex(vault)
        index.rebuild_all()
        with pytest.raises(memory_refs.ReferenceError) as excinfo:
            if resolver == "index":
                index.resolve(identity)
            else:
                memory_refs.resolve_identifier_read_only(
                    vault, memory_refs.memory_ref(identity)
                )
        return excinfo.value.reason.replace(identity, "<id>")

    first = _reason(tmp_path / "a", "Patterns/kill-switch.md", "Patterns/copy.md")
    second = _reason(tmp_path / "b", "public.md", "Insights/other-name.md")
    assert first == second


def test_backfill_duplicate_refusal_carries_a_count_not_the_identities(
    tmp_path: Path,
) -> None:
    vault, _identity = _collide(tmp_path, "first.md", "second.md")
    (vault / "Knowledge Base" / "Notes" / "legacy.md").write_text(
        _page(None), encoding="utf-8"
    )

    with pytest.raises(memory_refs.ReferenceError) as excinfo:
        memory_refs.backfill_ids(vault, dry_run=False)

    error = excinfo.value
    assert error.code == "AMBIGUOUS_REFERENCE"
    assert "[" not in error.reason, f"identity list leaked: {error.reason!r}"
    assert "1" in error.reason
    # The repair detail an owner needs still exists — behind the governed
    # command, where an audience is resolved and a decision applies.
    planned = memory_refs.backfill_ids(vault)
    assert {item["kind"] for item in planned["identity_issues"]} == {"duplicate"}


def test_backfill_refuses_existing_duplicate_identity(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    identity = memory_refs.new_id()
    (notes / "first.md").write_text(_page(identity), encoding="utf-8")
    (notes / "second.md").write_text(_page(identity), encoding="utf-8")
    legacy = notes / "legacy.md"
    legacy.write_text(_page(None), encoding="utf-8")

    planned = memory_refs.backfill_ids(vault)
    assert planned["would_update"] == ["Knowledge Base/Notes/legacy.md"]
    assert {item["kind"] for item in planned["identity_issues"]} == {"duplicate"}
    try:
        memory_refs.backfill_ids(vault, dry_run=False)
    except memory_refs.ReferenceError as exc:
        assert exc.code == "AMBIGUOUS_REFERENCE"
    else:
        raise AssertionError("backfill unexpectedly wrote through duplicate identity")
    assert "exomem_id" not in legacy.read_text(encoding="utf-8")


def test_backfill_is_dry_run_by_default_and_preserves_content(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = vault / "Knowledge Base" / "Notes" / "legacy.md"
    path.parent.mkdir(parents=True)
    original = _page(None, "# Legacy\n\nExact body.\n")
    path.write_text(original, encoding="utf-8")

    planned = memory_refs.backfill_ids(vault)
    assert planned["dry_run"] is True
    assert planned["would_update"] == ["Knowledge Base/Notes/legacy.md"]
    assert path.read_text(encoding="utf-8") == original

    applied = memory_refs.backfill_ids(vault, dry_run=False)
    updated = path.read_text(encoding="utf-8")
    fm, body, _ = vault_module.parse_frontmatter(updated)
    assert memory_refs.normalize_id(fm["exomem_id"])
    assert body == "# Legacy\n\nExact body.\n"
    assert applied["updated"] == ["Knowledge Base/Notes/legacy.md"]


def test_governed_writers_return_canonical_refs(
    vault: Path, source_schema
) -> None:
    results = [
        add.add(
            vault,
            source_schema,
            content="Captured material.",
            source_type="article",
            title="Reference source",
            url="https://example.com/reference-source",
            today=TODAY,
        ),
        note.note(
            vault,
            content="# Reference note\n\n## Claim\n\nStable identity survives paths.\n",
            note_type="insight",
            title="Reference note",
            status="draft",
            today=TODAY,
        ),
        link.link(
            vault,
            entity_type="concept",
            name="Stable Identity",
            summary="Path-independent memory identity.",
            today=TODAY,
        ),
        preserve.preserve(
            vault,
            scope="Reference",
            category="proof",
            filename="artifact.txt",
            content="evidence",
            description="Evidence sidecar with identity.",
            today=TODAY,
        ),
    ]

    for result in results:
        assert result.ref and result.ref.startswith(memory_refs.REF_PREFIX)
        page_path = result.sidecar_path if hasattr(result, "sidecar_path") else result.path
        raw = (vault / page_path).read_text(encoding="utf-8")
        fm = yaml.safe_load(raw.split("\n---\n", 1)[0].removeprefix("---\n"))
        assert memory_refs.memory_ref(str(fm["exomem_id"])) == result.ref


def test_read_and_edit_accept_canonical_reference(vault: Path) -> None:
    created = note.note(
        vault,
        content="# Referenced command\n\nBefore.\n",
        note_type="insight",
        title="Referenced command",
        status="draft",
        today=TODAY,
    )
    fetched = commands.op_get(vault, path=created.ref)
    assert fetched["path"] == created.path
    assert fetched["ref"] == created.ref

    edited = commands.op_edit(
        vault,
        path=created.ref,
        why="exercise stable reference resolution",
        old_string="Before.",
        new_string="After.",
    )
    assert edited["path"] == created.path
    assert "After." in (vault / created.path).read_text(encoding="utf-8")


def test_product_move_preserves_identity_and_heals_reference(vault: Path) -> None:
    created = note.note(
        vault,
        content="# Move identity\n\nStable across a governed move.\n",
        note_type="insight",
        title="Move identity",
        status="draft",
        today=TODAY,
    )
    before = yaml.safe_load(
        (vault / created.path)
        .read_text(encoding="utf-8")
        .split("\n---\n", 1)[0]
        .removeprefix("---\n")
    )["exomem_id"]
    destination = "Knowledge Base/Notes/Insights/moved-identity.md"

    moved = commands.op_move_file(vault, old_path=created.ref, new_path=destination)
    read = commands.op_read_memory(vault, path=created.ref)

    assert moved["new_path"] == destination
    assert read["path"] == destination
    assert read["ref"] == created.ref
    assert read["frontmatter"]["exomem_id"] == before
