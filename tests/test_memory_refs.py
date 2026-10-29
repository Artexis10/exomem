from __future__ import annotations

import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from exomem import (
    add,
    call_spans,
    commands,
    freshness,
    link,
    memory_refs,
    note,
    preserve,
    semantic_contract,
)
from exomem import vault as vault_module

TODAY = dt.date(2026, 7, 9)


def _page(identity: str | None, body: str = "# Page\n") -> str:
    id_line = f"\nexomem_id: {identity}" if identity is not None else ""
    return f"---\ntype: insight\ncreated: 2026-07-09{id_line}\n---\n\n{body}"


def test_a_legacy_bom_negative_row_rebuilds_without_rewriting_the_page(tmp_path: Path) -> None:
    import hashlib
    import sqlite3

    path = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    path.parent.mkdir(parents=True)
    identity = memory_refs.new_id()
    raw = ("\ufeff" + _page(identity)).encode("utf-8")
    path.write_bytes(raw)
    rel = path.relative_to(tmp_path).as_posix()
    index = memory_refs.ReferenceIndex(tmp_path)
    index.rebuild_all()
    with sqlite3.connect(index.path) as connection:
        # Version 3 could record a BOM page as missing while retaining its exact byte hash.
        connection.execute("UPDATE ref_meta SET value='3' WHERE key='schema_version'")
        connection.execute(
            "UPDATE identities SET exomem_id=NULL,raw_id='',status='missing' WHERE path=?",
            (rel,),
        )

    assert memory_refs.holds_content_identities(
        tmp_path, {rel: hashlib.sha256(raw).hexdigest()}
    ) == {rel: False}
    assert index.ref_for_path(rel) == memory_refs.memory_ref(identity)
    assert path.read_bytes() == raw


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


def test_a_crlf_page_is_held_at_the_digest_of_its_bytes(tmp_path: Path) -> None:
    """Recall custody proves a page by its byte digest; CRLF must not change the answer."""
    import hashlib

    vault = tmp_path / "vault"
    page = vault / "Knowledge Base" / "Evidence" / "capture.md"
    page.parent.mkdir(parents=True)
    identity = memory_refs.new_id()
    page.write_bytes(_page(identity).replace("\n", "\r\n").encode("utf-8"))
    rel = "Knowledge Base/Evidence/capture.md"

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all()["indexed"] == 1
    digest = hashlib.sha256(page.read_bytes()).hexdigest()

    assert memory_refs.holds_content_identities(vault, {rel: digest}) == {rel: True}
    assert index.resolve(identity) == rel
    assert memory_refs.drift(vault) == []


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


def test_hot_bulk_reference_lookup_uses_one_readonly_sidecar_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    page_path = "Knowledge Base/Notes/current.md"
    page = vault / page_path
    page.parent.mkdir(parents=True)
    identity = memory_refs.new_id()
    page.write_text(_page(identity), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    index.rebuild_all()
    readonly_connections = 0
    connect_readonly = index._connect_readonly

    def counted_readonly():
        nonlocal readonly_connections
        readonly_connections += 1
        return connect_readonly()

    def refuse_write_connection():
        raise AssertionError("a current reference lookup opened the writable sidecar")

    monkeypatch.setattr(index, "_connect_readonly", counted_readonly)
    monkeypatch.setattr(index, "_connect", refuse_write_connection)

    assert index.refs_for_paths([page_path]) == {
        page_path: memory_refs.memory_ref(identity)
    }
    assert readonly_connections == 1


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
    folder = vault / "Knowledge Base" / "Notes" / "ACME"
    folder.mkdir(parents=True)
    identity = memory_refs.new_id()
    (folder / "terms.md").write_text(_page(identity), encoding="utf-8")
    on_disk = "Knowledge Base/Notes/ACME/terms.md"
    phantom = "Knowledge Base/Notes/Acme/terms.md"

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

    On NTFS `.../Acme/terms.md` and `.../ACME/terms.md` open one file, but the
    sidecar keys rows as case-sensitive text: a stale row survives the delete and
    then reads back as a second owner of the identity.
    """
    vault = tmp_path / "vault"
    folder = vault / "Knowledge Base" / "Notes" / "ACME"
    folder.mkdir(parents=True)
    identity = memory_refs.new_id()
    page = folder / "terms.md"
    page.write_text(_page(identity), encoding="utf-8")

    index = memory_refs.ReferenceIndex(vault)
    assert index.rebuild_all() == {"indexed": 1, "duplicates": 0, "malformed": 0}
    assert index.resolve(identity) == "Knowledge Base/Notes/ACME/terms.md"

    page.unlink()
    index.delete_paths(["Knowledge Base/Notes/Acme/terms.md"])

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
    from exomem import due_state

    # The first recall of a vault builds its due-state projection, which resolves refs too.
    due_state.reconcile(vault)
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
    token = call_spans.MCP_CALL_TOKEN.set("reference-read-diagnostics")
    try:
        fetched = commands.op_get(vault, path=created.ref)
        spans = {
            span["name"]: span
            for span in call_spans.pop_call_spans("reference-read-diagnostics")
        }
    finally:
        call_spans.MCP_CALL_TOKEN.reset(token)
    assert fetched["path"] == created.path
    assert fetched["ref"] == created.ref
    assert {
        "read.resolve_identifier", "read.prepare", "read.page",
        "read.reference", "read.release", "memory_refs.identity_census",
    } <= spans.keys()
    census = spans["memory_refs.identity_census"]["fields"]
    # The suite runs with the corpus cache off, so the scan answered.
    assert census["snapshot"] == 0
    assert census["pages"] >= 1
    assert census["cpu_ms"] >= 0
    assert all(
        isinstance(value, int)
        for span in spans.values()
        for value in span.get("fields", {}).values()
    )

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


class _CountingConnection:
    """Records how many SQL variables each query binds, then delegates."""

    def __init__(self, inner, binds: list[int]) -> None:
        self._inner = inner
        self._binds = binds

    def execute(self, sql, parameters=()):
        self._binds.append(len(parameters))
        return self._inner.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_bulk_lookup_is_chunked_so_one_query_never_binds_the_whole_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bind per path, and SQLite has a ceiling on binds.

    `WHERE path IN (?,...)` binds one variable per path, and SQLite refuses past
    `SQLITE_MAX_VARIABLE_NUMBER` — measured on this build as fine at 32,766 and
    `OperationalError: too many SQL variables` at 32,767. Any caller that
    derives its path list from the vault can cross that, and one of them
    (`review_memory(mode="dispositions")`) does it outside the try that promises
    a count never breaks the view.

    Asserted with a chunk of 3 over 10 paths, because the property is "no single
    query carries the whole list", not "the ceiling is 32,767": a test that had
    to build 32,767 pages would be measuring SQLite rather than this code.
    """
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    expected = {}
    for index_number in range(10):
        identity = memory_refs.new_id()
        relative = f"Knowledge Base/Notes/chunk-{index_number:02d}.md"
        (vault / relative).write_text(_page(identity), encoding="utf-8")
        expected[relative] = memory_refs.memory_ref(identity)

    index = memory_refs.ReferenceIndex(vault)
    index.rebuild_all()

    binds: list[int] = []
    real = memory_refs.ReferenceIndex._refs_from_connection

    def counting(self, conn, wanted):
        return real(self, _CountingConnection(conn, binds), wanted)

    monkeypatch.setattr(memory_refs, "REFS_QUERY_CHUNK", 3)
    monkeypatch.setattr(memory_refs.ReferenceIndex, "_refs_from_connection", counting)

    resolved = index.refs_for_paths(list(expected))

    # Chunking must not lose or reorder anything: the whole mapping comes back.
    assert resolved == expected
    assert binds, "no query was observed"
    assert max(binds) <= 3, binds
    # ceil(10 / 3) == 4 batches, each running the path query at least once.
    assert len([count for count in binds if count > 0]) >= 4, binds


def test_an_unchunked_lookup_would_bind_every_path(tmp_path: Path) -> None:
    """The other half: without the chunk the single query binds all ten.

    Pins that the chunk is what bounds the query, and not some upstream caller
    that happened to pass a short list.
    """
    vault = tmp_path / "vault"
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    paths = []
    for index_number in range(10):
        relative = f"Knowledge Base/Notes/whole-{index_number:02d}.md"
        (vault / relative).write_text(_page(memory_refs.new_id()), encoding="utf-8")
        paths.append(relative)

    index = memory_refs.ReferenceIndex(vault)
    index.rebuild_all()

    binds: list[int] = []
    real = memory_refs.ReferenceIndex._refs_from_connection

    def counting(self, conn, wanted):
        return real(self, _CountingConnection(conn, binds), wanted)

    original = memory_refs.ReferenceIndex._refs_from_connection
    memory_refs.ReferenceIndex._refs_from_connection = counting
    try:
        # Default chunk (500) is above ten, so this is the unchunked shape.
        index.refs_for_paths(paths)
    finally:
        memory_refs.ReferenceIndex._refs_from_connection = original

    assert max(binds) == 10, binds


# --------------------------------------------------------------------------
# Resolution from the server's current identity projection
# --------------------------------------------------------------------------


def _withhold_hidden_notes_from_external(root: Path) -> None:
    """Release `Notes/Open/**` to `external`; withhold `Notes/Hidden/**` (ceiling 0)."""
    base = root / "Knowledge Base" / "_Governance"
    (base / "scopes").mkdir(parents=True, exist_ok=True)
    (base / "rules").mkdir(parents=True, exist_ok=True)
    (base / "scopes" / "open.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FE1\n"
        'name: open\npaths: ["Notes/Open/**"]\n',
        encoding="utf-8",
    )
    (base / "rules" / "open.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FE2\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FE1"]\naudience: external\nceiling: 6\n',
        encoding="utf-8",
    )
    (base / "scopes" / "hidden.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FE3\n"
        'name: hidden\npaths: ["Notes/Hidden/**"]\n',
        encoding="utf-8",
    )
    (base / "rules" / "hidden.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FE4\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FE3"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )


def _write_page(root: Path, relative: str, markdown: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")


def test_the_projection_resolves_twins_exactly_as_the_scan_does(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicated identity keeps every owner in the projection.

    The projection's canonical refs drop a duplicated id altogether. Answering
    from them would turn a visible page with a hidden twin into "not found",
    which tells the caller that a hidden page shares the id, and would turn two
    visible twins into "not found" instead of ambiguous.
    """
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal

    hidden_twin = memory_refs.new_id()
    visible_twins = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/Open/shown.md", _page(hidden_twin))
    _write_page(tmp_path, "Knowledge Base/Notes/Hidden/withheld.md", _page(hidden_twin))
    _write_page(tmp_path, "Knowledge Base/Notes/Open/first.md", _page(visible_twins))
    _write_page(tmp_path, "Knowledge Base/Notes/Open/second.md", _page(visible_twins))
    _withhold_hidden_notes_from_external(tmp_path)
    external = RequestPrincipal(audience_id="external", surface="mcp")

    def resolve(identity: str) -> str | dict[str, str]:
        try:
            return egress.resolve_visible_identifier(
                tmp_path, memory_refs.memory_ref(identity), principal=external
            )
        except memory_refs.ReferenceError as error:
            return error.as_dict()

    # No projection is current yet, so these read through the scan.
    through_scan = {identity: resolve(identity) for identity in (hidden_twin, visible_twins)}

    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)

    def no_scan(vault_root: Path) -> list:
        raise AssertionError("a current projection must answer without a scan")

    monkeypatch.setattr(memory_refs, "_scan_pages", no_scan)
    through_projection = {
        identity: resolve(identity) for identity in (hidden_twin, visible_twins)
    }

    assert through_projection == through_scan
    assert through_projection[hidden_twin] == "Knowledge Base/Notes/Open/shown.md"
    assert through_projection[visible_twins] == {
        "code": "AMBIGUOUS_REFERENCE",
        "reason": f"memory id {visible_twins} appears in 2 pages",
    }


def test_artifact_redaction_borrows_the_current_identity_projection(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two release passes preserve redaction without reparsing the corpus per token."""
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, request_scope

    visible, hidden, twin, absent = (memory_refs.new_id() for _ in range(4))
    for path, identity in (
        ("Notes/Open/shown.md", visible), ("Notes/Hidden/withheld.md", hidden),
        ("Notes/Open/twin.md", twin), ("Notes/Hidden/twin.md", twin),
    ):
        _write_page(tmp_path, f"Knowledge Base/{path}", _page(identity))
    _withhold_hidden_notes_from_external(tmp_path)
    refs = [memory_refs.memory_ref(identity) for identity in (visible, hidden, twin, absent)]
    payload = {"handoff_prompt": " ".join([*refs, "exomem://memory/invalid"])}
    external = RequestPrincipal(audience_id="external", surface="mcp")
    with request_scope(external):
        through_scan = egress.postfilter(
            "ask_memory", egress.postfilter("ask_memory", payload, tmp_path), tmp_path,
        )

    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)

    def no_scan(root: Path) -> list:
        raise AssertionError("artifact release must borrow the current identity projection")

    monkeypatch.setattr(memory_refs, "_scan_pages", no_scan)
    with request_scope(external):
        through_projection = egress.postfilter(
            "ask_memory", egress.postfilter("ask_memory", payload, tmp_path), tmp_path,
        )
    assert through_projection == through_scan
    assert refs[1] not in through_projection["handoff_prompt"]
    assert all(ref in through_projection["handoff_prompt"] for ref in (refs[0], refs[2], refs[3]))
    assert "exomem://memory/invalid" in through_projection["handoff_prompt"]


def test_through_the_projection_a_hidden_only_id_reads_as_an_absent_one(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two vaults that differ only by one withheld page answer the same."""
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal

    identity = memory_refs.new_id()
    held = tmp_path / "held"
    empty = tmp_path / "empty"
    for root in (held, empty):
        _write_page(root, "Knowledge Base/Notes/Open/other.md", _page(memory_refs.new_id()))
        _withhold_hidden_notes_from_external(root)
    _write_page(held, "Knowledge Base/Notes/Hidden/withheld.md", _page(identity))
    for root in (held, empty):
        freshness.rebaseline(root)
        semantic_contract.build_corpus_context(root)

    def no_scan(vault_root: Path) -> list:
        raise AssertionError("a current projection must answer without a scan")

    monkeypatch.setattr(memory_refs, "_scan_pages", no_scan)
    external = RequestPrincipal(audience_id="external", surface="mcp")
    answers = []
    for root in (held, empty):
        with pytest.raises(memory_refs.ReferenceError) as caught:
            egress.resolve_visible_identifier(
                root, memory_refs.memory_ref(identity), principal=external
            )
        answers.append(caught.value.as_dict())

    assert answers[0] == answers[1] == {
        "code": "REFERENCE_NOT_FOUND",
        "reason": f"memory id not found: {identity}",
    }


def test_excluded_owners_do_not_change_visible_reference_resolution(tmp_path: Path) -> None:
    from exomem.governance import egress

    shared, hidden_only = memory_refs.new_id(), memory_refs.new_id()
    visible = "Knowledge Base/Notes/Open/shown.md"
    _write_page(tmp_path, visible, _page(shared))
    _write_page(tmp_path, "Knowledge Base/Notes/Hidden/twin.md", _page(shared))
    _write_page(tmp_path, "Knowledge Base/Notes/Hidden/only.md", _page(hidden_only))
    (tmp_path / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Notes/Hidden\n")
    ref = memory_refs.memory_ref(shared)
    hidden_ref = memory_refs.memory_ref(hidden_only)

    assert egress.resolve_visible_identifier(tmp_path, ref) == visible
    assert egress.visible_memory_ref_paths(tmp_path, [ref, hidden_ref]) == {ref: visible}
    with pytest.raises(memory_refs.ReferenceError) as caught:
        egress.resolve_visible_identifier(tmp_path, hidden_ref)
    assert caught.value.as_dict() == {
        "code": "REFERENCE_NOT_FOUND",
        "reason": f"memory id not found: {hidden_only}",
    }


def test_a_projection_behind_an_external_edit_falls_back_to_the_scan(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page written behind the server resolves once the change is pending.

    The projection was built before the page existed, so only the scan can
    answer for it.
    """
    known = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/known.md", _page(known))
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)
    late = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/late.md", _page(late))
    freshness.mark_external_pending(tmp_path)

    scans: list[str] = []
    real = memory_refs._scan_pages

    def counted(vault_root: Path) -> list:
        scans.append(str(vault_root))
        return real(vault_root)

    monkeypatch.setattr(memory_refs, "_scan_pages", counted)
    resolved = memory_refs.paths_for_ids_read_only(tmp_path, [known, late])

    assert resolved == {
        known: ("Knowledge Base/Notes/known.md",),
        late: ("Knowledge Base/Notes/late.md",),
    }
    assert len(scans) == 1


def test_the_projection_and_the_scan_resolve_upper_case_and_cr_only_pages_alike(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sources read a `.MD` page and a page with CR-only line endings.

    The scan once matched `*.md` case-sensitively on POSIX, and the census read
    raw bytes, where a CR-only page shows no frontmatter. Either gap made the
    same reference resolve through one source and read as absent through the
    other.
    """
    upper = memory_refs.new_id()
    carriage = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/Upper.MD", _page(upper))
    cr_only = tmp_path / "Knowledge Base" / "Notes" / "carriage.md"
    cr_only.write_bytes(_page(carriage).replace("\n", "\r").encode("utf-8"))
    expected = {
        upper: ("Knowledge Base/Notes/Upper.MD",),
        carriage: ("Knowledge Base/Notes/carriage.md",),
    }

    # No projection is current yet, so this reads through the scan.
    assert memory_refs.paths_for_ids_read_only(tmp_path, [upper, carriage]) == expected

    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)

    def no_scan(vault_root: Path) -> list:
        raise AssertionError("a current projection must answer without a scan")

    monkeypatch.setattr(memory_refs, "_scan_pages", no_scan)
    assert memory_refs.paths_for_ids_read_only(tmp_path, [upper, carriage]) == expected


def test_a_trash_owned_reference_resolves_from_the_projection_without_a_scan(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page under `Knowledge Base/trash/` keeps the ref the scan hands out.

    The identity census prunes that directory, while the scan reads it and a
    read by path returns the page's ref. The projection keeps those owners in a
    table that only reference resolution reads, so the vault answers from it.
    """
    live = memory_refs.new_id()
    trashed = memory_refs.new_id()
    shared = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/live.md", _page(live))
    _write_page(tmp_path, "Knowledge Base/trash/gone.md", _page(trashed))
    _write_page(tmp_path, "Knowledge Base/Notes/twin.md", _page(shared))
    _write_page(tmp_path, "Knowledge Base/trash/twin.md", _page(shared))
    expected = {
        live: ("Knowledge Base/Notes/live.md",),
        trashed: ("Knowledge Base/trash/gone.md",),
        shared: ("Knowledge Base/Notes/twin.md", "Knowledge Base/trash/twin.md"),
    }
    through_scan = memory_refs.paths_for_ids_read_only(tmp_path, list(expected))
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)

    def no_scan(vault_root: Path) -> list:
        raise AssertionError("a trash owner must resolve from the projection")

    monkeypatch.setattr(memory_refs, "_scan_pages", no_scan)
    through_projection = memory_refs.paths_for_ids_read_only(tmp_path, list(expected))

    assert through_scan == through_projection == expected


def test_a_page_moved_into_trash_keeps_resolving_after_the_projection_is_patched(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reference-only table follows the watcher's patch, in and out of trash."""
    identity = memory_refs.new_id()
    live = tmp_path / "Knowledge Base/Notes/page.md"
    trashed = tmp_path / "Knowledge Base/trash/page.md"
    _write_page(tmp_path, "Knowledge Base/Notes/page.md", _page(identity))
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)

    def no_scan(vault_root: Path) -> list:
        raise AssertionError("a patched projection must answer without a scan")

    monkeypatch.setattr(memory_refs, "_scan_pages", no_scan)
    trashed.parent.mkdir(parents=True)
    live.rename(trashed)
    semantic_contract.publish_corpus_files_changed(tmp_path, changed=[trashed], deleted=[live])
    assert memory_refs.paths_for_ids_read_only(tmp_path, [identity]) == {
        identity: ("Knowledge Base/trash/page.md",)
    }

    trashed.rename(live)
    semantic_contract.publish_corpus_files_changed(tmp_path, changed=[live], deleted=[trashed])
    assert memory_refs.paths_for_ids_read_only(tmp_path, [identity]) == {
        identity: ("Knowledge Base/Notes/page.md",)
    }


def test_a_stale_projection_resolves_a_read_without_a_scan(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read answers from the last published projection while the watcher catches up.

    A caller that writes from the answer keeps the default and scans until the
    projection is current again.
    """
    known = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/known.md", _page(known))
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)
    _write_page(tmp_path, "Knowledge Base/Notes/other.md", _page(memory_refs.new_id()))
    freshness.mark_external_pending(tmp_path)
    scans: list[str] = []
    real = memory_refs._scan_pages

    def counted(vault_root: Path) -> list:
        scans.append(str(vault_root))
        return real(vault_root)

    monkeypatch.setattr(memory_refs, "_scan_pages", counted)

    stale = memory_refs.paths_for_ids_read_only(tmp_path, [known], serve_stale=True)
    assert stale == {known: ("Knowledge Base/Notes/known.md",)}
    assert scans == []

    strict = memory_refs.paths_for_ids_read_only(tmp_path, [known])
    assert strict == stale
    assert len(scans) == 1


def test_a_stale_read_still_decides_release_live(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release decision over a stale owner is taken at read time.

    The page is released when the projection is built. A rule written after that
    withholds it, and the read answers as it would for a page that never was.
    """
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal

    identity = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/Hidden/withheld.md", _page(identity))
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)
    external = RequestPrincipal(audience_id="external", surface="mcp")
    ref = memory_refs.memory_ref(identity)

    def no_scan(vault_root: Path) -> list:
        raise AssertionError("a stale read must answer from the projection")

    monkeypatch.setattr(memory_refs, "_scan_pages", no_scan)
    freshness.mark_external_pending(tmp_path)
    assert (
        egress.resolve_visible_identifier(tmp_path, ref, principal=external, serve_stale=True)
        == "Knowledge Base/Notes/Hidden/withheld.md"
    )

    _withhold_hidden_notes_from_external(tmp_path)
    with pytest.raises(memory_refs.ReferenceError) as caught:
        egress.resolve_visible_identifier(tmp_path, ref, principal=external, serve_stale=True)
    assert caught.value.as_dict() == {
        "code": "REFERENCE_NOT_FOUND",
        "reason": f"memory id not found: {identity}",
    }


@pytest.mark.parametrize("edit", ["deleted", "id_changed"])
def test_a_stale_read_never_returns_a_page_that_no_longer_holds_the_id(
    tmp_path: Path, corpus_cache_on: None, edit: str
) -> None:
    """A removal or a re-identification made behind the server applies at once.

    The stale projection still names the page. The read checks the page's own id
    before it returns the path, and otherwise takes the strict lookup.
    """
    from exomem.governance import egress

    identity = memory_refs.new_id()
    page = tmp_path / "Knowledge Base/Notes/page.md"
    _write_page(tmp_path, "Knowledge Base/Notes/page.md", _page(identity))
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)
    if edit == "deleted":
        page.unlink()
    else:
        page.write_text(_page(memory_refs.new_id()), encoding="utf-8")
    freshness.mark_external_pending(tmp_path)

    with pytest.raises(memory_refs.ReferenceError) as caught:
        egress.resolve_visible_identifier(
            tmp_path, memory_refs.memory_ref(identity), serve_stale=True
        )
    assert caught.value.code == "REFERENCE_NOT_FOUND"


def test_a_stale_read_rechecks_every_visible_owner_before_calling_it_ambiguous(
    tmp_path: Path, corpus_cache_on: None
) -> None:
    """Two pages held the id and an outside edit took it off one of them.

    The stale projection still names both. The read answers what the strict
    lookup answers, the one page that holds the id, not an ambiguity.
    """
    from exomem.governance import egress

    identity = memory_refs.new_id()
    _write_page(tmp_path, "Knowledge Base/Notes/one.md", _page(identity))
    _write_page(tmp_path, "Knowledge Base/Notes/two.md", _page(identity))
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)
    (tmp_path / "Knowledge Base/Notes/two.md").write_text(
        _page(memory_refs.new_id()), encoding="utf-8"
    )
    freshness.mark_external_pending(tmp_path)

    stale = egress.resolve_visible_identifier(
        tmp_path, memory_refs.memory_ref(identity), serve_stale=True
    )
    assert stale == "Knowledge Base/Notes/one.md"


def test_a_stale_lookup_does_the_same_work_whether_or_not_the_id_exists(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id that is held, held only by a withheld page, or absent costs the same.

    The batch takes one source, chosen from vault state, so what a caller can
    time or count does not tell them whether a page they cannot see holds the id.
    """
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal

    shown, withheld, absent = (memory_refs.new_id() for _ in range(3))
    _write_page(tmp_path, "Knowledge Base/Notes/Open/shown.md", _page(shown))
    _write_page(tmp_path, "Knowledge Base/Notes/Hidden/withheld.md", _page(withheld))
    _withhold_hidden_notes_from_external(tmp_path)
    freshness.rebaseline(tmp_path)
    semantic_contract.build_corpus_context(tmp_path)
    freshness.mark_external_pending(tmp_path)
    scans: list[str] = []

    def counted(vault_root: Path) -> list:
        scans.append(str(vault_root))
        raise AssertionError("a stale lookup must not scan for any id")

    monkeypatch.setattr(memory_refs, "_scan_pages", counted)
    external = RequestPrincipal(audience_id="external", surface="mcp")
    spans = {}
    for name, identity in (("shown", shown), ("withheld", withheld), ("absent", absent)):
        token = f"stale-neutrality-{name}"
        handle = call_spans.MCP_CALL_TOKEN.set(token)
        try:
            try:
                egress.resolve_visible_identifier(
                    tmp_path, memory_refs.memory_ref(identity), principal=external,
                    serve_stale=True,
                )
            except memory_refs.ReferenceError:
                pass
            rows = call_spans.pop_call_spans(token)
        finally:
            call_spans.pop_call_spans(token)
            call_spans.MCP_CALL_TOKEN.reset(handle)
        fields = next(r["fields"] for r in rows if r["name"] == "memory_refs.identity_census")
        timings = {"cpu_ms", "thread_cpu_us", "process_cpu_us"}
        spans[name] = {key: value for key, value in fields.items() if key not in timings}

    assert scans == []
    assert spans["shown"] == spans["withheld"] == spans["absent"] == {"snapshot": 1}


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("warm", None),
        ("unobserved", "events_not_live"),
        ("cold", "corpus_absent"),
        ("pending", "external_pending"),
        ("moved", "checkpoint_differs"),
        ("revalidation", "snapshot_changed"),
    ],
)
def test_identity_resolution_names_the_reason_for_a_full_scan(
    tmp_path: Path, corpus_cache_on: None, monkeypatch: pytest.MonkeyPatch,
    state: str, reason: str | None,
) -> None:
    """A slow reference read must distinguish why it could not reuse its snapshot."""
    identity = memory_refs.new_id()
    relative = "Knowledge Base/Notes/known.md"
    page = tmp_path / relative
    _write_page(tmp_path, relative, _page(identity))
    if state != "unobserved":
        freshness.rebaseline(tmp_path)
    if state not in ("cold", "unobserved"):
        semantic_contract.build_corpus_context(tmp_path)
    if state == "pending":
        freshness.mark_external_pending(tmp_path)
    elif state == "moved":
        page.write_text(_page(identity, "# Changed\n"), encoding="utf-8")
        freshness.on_files_changed(tmp_path, changed=(page,))
    elif state == "revalidation":
        acquire = semantic_contract.current_reference_identity_snapshot

        def changed_snapshot(root: Path, **kwargs):
            snapshot = acquire(root, **kwargs)
            freshness.mark_external_pending(root)
            return snapshot

        monkeypatch.setattr(
            semantic_contract, "current_reference_identity_snapshot", changed_snapshot
        )

    token = "identity-scan-reason"
    handle = call_spans.MCP_CALL_TOKEN.set(token)
    try:
        assert memory_refs.paths_for_ids_read_only(tmp_path, [identity]) == {
            identity: (relative,),
        }
        rows = call_spans.pop_call_spans(token)
        fields = next(row["fields"] for row in rows if row["name"] == "memory_refs.identity_census")
        assert fields["snapshot"] == int(reason is None)
        declines = {key: value for key, value in fields.items() if key.startswith("declined_")}
        assert declines == ({} if reason is None else {f"declined_{reason}": 1})
    finally:
        call_spans.pop_call_spans(token)
        call_spans.MCP_CALL_TOKEN.reset(handle)
