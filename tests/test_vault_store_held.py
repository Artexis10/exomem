"""Held items of a store-mode vault: the list and the two resolutions.

OpenSpec make-vault-store-canonical, design D5 and task 4.4. The vault is the migration suite's
generated vault in store mode, and each page's change is a real ``write.commit``. A held row is
inserted the way the projector writes one: Exomem changed a page, a person edited or deleted its
file, and the row records both.
"""

# Fixtures imported from the sibling test modules are redefined by each test's arguments.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, DIAGRAM, migrate_apply, vault  # noqa: F401
from test_vault_store_read_write import put, rows, stored, writer  # noqa: F401

from exomem import state_paths
from exomem.cli_ops import OpError
from exomem.curation import MAX_WHY_CHARS
from exomem.vault_store import held, projector, read, write

EXOMEM = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nThe page that Exomem changed.\n"
MINE = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nThe edit that a person made in the file.\n"
WHY = "the owner asked"


def digest(body):
    return hashlib.sha256(body).hexdigest()


def hold(root, writer, kind, *, body=True, exomem=EXOMEM, mine=MINE):
    """Exomem changed ALPHA to ``exomem`` and a person changed its file to ``mine``.

    The store, the file and the held row end as the projector leaves them.
    """
    manifest = rows(root, "SELECT sha256, revision FROM store_manifest WHERE path = ?", ALPHA)[0]
    revision = write.commit(writer, put(ALPHA, exomem)).revision
    if kind == "edit":
        (root / ALPHA).write_bytes(mine)
    else:
        (root / ALPHA).unlink()
    with writer.handle.transaction() as tx:
        tx.execute(
            "INSERT INTO store_held"
            "(path, kind, base_revision, store_revision, sha256, body, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ALPHA,
                kind,
                manifest[1],
                revision,
                digest(mine) if kind == "edit" else manifest[0],
                mine if kind == "edit" and body else None,
                "2026-11-09T09:00:00Z",
            ),
        )
    return revision


def hold_a_binary(root, writer):
    """A person changed the file of a binary that the store records but does not hold."""
    sha, revision = rows(
        root, "SELECT sha256, revision FROM store_binaries WHERE path = ?", DIAGRAM
    )[0]
    with writer.handle.transaction() as tx:
        tx.execute(
            "INSERT INTO store_held(path, kind, store_revision, sha256, detected_at) "
            "VALUES (?, 'edit', ?, ?, ?)",
            (DIAGRAM, revision, sha, "2026-11-09T09:00:00Z"),
        )


def hold_after_store_delete(root, writer, *, mine=MINE):
    """Exomem deleted ALPHA's page while a person edited its file, which stays with no page."""
    manifest = rows(root, "SELECT revision FROM store_manifest WHERE path = ?", ALPHA)[0][0]
    write.commit(
        writer, write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(ALPHA),))
    )
    (root / ALPHA).write_bytes(mine)
    with writer.handle.transaction() as tx:
        tx.execute(
            "INSERT INTO store_held(path, kind, base_revision, sha256, detected_at) "
            "VALUES (?, 'edit', ?, ?, ?)",
            (ALPHA, manifest, digest(mine), "2026-11-09T09:00:00Z"),
        )


def hold_through_the_projector(root, writer, kind):
    """A person changed ALPHA's file while Exomem changed its page; the real projector held it."""
    if kind == "edit":
        (root / ALPHA).write_bytes(MINE)
    else:
        (root / ALPHA).unlink()
    write.commit(writer, put(ALPHA, EXOMEM))
    projecting = projector.Projector(root, run=lambda work: work(writer))
    assert projecting.run_pass().held == 1
    return projecting


def only_item(root):
    (item,) = held.list_held(root)
    return item


def resolve(root, writer, choice, *, seen=None):
    return held.resolve_held(
        root, ALPHA, choice, why=WHY, seen=seen or only_item(root).item_version, writer=writer
    )


def exports(root):
    folder = state_paths.vault_state_dir(root) / held.EXPORT_FOLDER
    return sorted(path for path in folder.rglob("*") if path.is_file())


def only_revision(root):
    return rows(root, "SELECT revision FROM store_pages WHERE path = ?", ALPHA)[0][0]


def exports_exist(root):
    return (state_paths.vault_state_dir(root) / held.EXPORT_FOLDER).exists()


def stored_page(root):
    return rows(root, "SELECT body, revision FROM store_pages WHERE path = ?", ALPHA)


def queue(root):
    return rows(root, "SELECT path, operation FROM store_projection_queue")


def test_a_vault_not_in_store_mode_lists_nothing_and_refuses_a_resolution(vault):
    """A list that reads an unmigrated vault's missing store as an error, or a resolve that acts."""
    assert held.list_held(vault) == []
    with pytest.raises(OpError) as refused:
        held.resolve_held(vault, ALPHA, held.KEEP_STORE, why=WHY, seen="x")
    assert refused.value.code == read.NOT_ACTIVE


def test_the_list_gives_each_item_the_persons_file_and_the_stores_page_as_they_are_now(
    stored, writer
):
    """A list that shows what was held and not what the file and the store hold now.

    The agent would choose without knowing that the person's file has since moved on.
    """
    revision = hold(stored, writer, "edit", body=False)
    item = only_item(stored)
    assert (item.path, item.kind, item.detected_at) == (ALPHA, "edit", "2026-11-09T09:00:00Z")
    assert (item.file.exists, item.file.sha256, item.file.size) == (True, digest(MINE), len(MINE))
    assert (item.store.revision, item.store.sha256) == (revision, digest(EXOMEM))
    (stored / ALPHA).write_bytes(MINE + b"more\n")
    assert only_item(stored).file.sha256 == digest(MINE + b"more\n")
    assert only_item(stored).item_version != item.item_version


def test_the_list_offers_only_the_choices_that_a_resolve_takes(stored, writer, monkeypatch):
    """A list that offers keep-mine after Exomem changed the page again, or any choice for a file
    too large to read whole, so the agent tries a resolution that the store must refuse."""
    hold(stored, writer, "edit")
    before = only_item(stored)
    assert (before.resolvable, before.keep_mine_possible) == (True, True)
    assert before.choices == (held.KEEP_MINE, held.KEEP_STORE)
    write.commit(writer, put(ALPHA, EXOMEM + b"A later Exomem change.\n"))
    after = only_item(stored)
    assert (after.resolvable, after.keep_mine_possible) == (True, False)
    assert after.choices == (held.KEEP_STORE,)
    monkeypatch.setattr(projector, "HELD_BODY_LIMIT", len(MINE) - 1)
    large = only_item(stored)
    assert (large.blocked, large.choices) == (held.LARGE_BLOCK, ())
    with pytest.raises(OpError) as refused:
        resolve(stored, writer, held.KEEP_STORE, seen=large.item_version)
    assert refused.value.code == held.TOO_LARGE


def test_a_held_binary_is_listed_as_not_resolvable_and_refuses_both_choices(stored, writer):
    """A held binary that offers choices it cannot take, or whose refusal sends an agent to
    rollback commands that the person never asked for."""
    hold_a_binary(stored, writer)
    (item,) = held.list_held(stored)
    assert (item.resolvable, item.keep_mine_possible, item.choices) == (False, False, ())
    for choice in held.CHOICES:
        with pytest.raises(OpError) as refused:
            held.resolve_held(
                stored, DIAGRAM, choice, why=WHY, seen=item.item_version, writer=writer
            )
        assert refused.value.code == write.NOT_OWNED
        assert "until a later release adds that" in refused.value.remediation
        assert "rollback" not in refused.value.remediation
    assert len(rows(stored, "SELECT path FROM store_held")) == 1


@pytest.mark.parametrize(
    "why",
    ["", "   ", "two\nlines", "x" * (MAX_WHY_CHARS + 1)],
    ids=["empty", "blank", "lines", "long"],
)
def test_a_reason_that_is_empty_has_several_lines_or_is_long_is_refused_before_anything_changes(
    stored, writer, why
):
    """A reason that fills the append-only audit trail with a page of text, or with nothing."""
    hold(stored, writer, "edit")
    with pytest.raises(OpError) as refused:
        held.resolve_held(
            stored,
            ALPHA,
            held.KEEP_STORE,
            why=why,
            seen=only_item(stored).item_version,
            writer=writer,
        )
    assert refused.value.code == "INVALID_INPUT" and "`why`" in refused.value.remediation
    assert len(rows(stored, "SELECT path FROM store_held")) == 1
    assert not exports_exist(stored)


def test_keep_mine_on_an_edit_makes_the_persons_bytes_the_page_without_projecting_again(
    stored, writer
):
    """A resolution that keeps the edit in the file but not in the store, or queues a projection.

    The projection would overwrite the edit. The held row carries no body: the bytes come from the
    file.
    """
    revision = hold(stored, writer, "edit", body=False)
    resolved = resolve(stored, writer, held.KEEP_MINE)
    assert resolved.revision == revision + 1
    assert stored_page(stored) == [(MINE, revision + 1)]
    assert (stored / ALPHA).read_bytes() == MINE
    assert queue(stored) == []
    assert rows(stored, "SELECT sha256, revision FROM store_manifest WHERE path = ?", ALPHA) == [
        (digest(MINE), revision + 1)
    ]
    assert rows(stored, "SELECT path FROM store_held") == []
    assert rows(
        stored, "SELECT operation, origin, reason FROM store_audit WHERE revision = ?", revision + 1
    ) == [("import", "outside editor", f"held keep-mine: {WHY}")]
    assert held.list_held(stored) == []


def test_keep_mine_refuses_when_exomem_changed_the_page_after_the_edit_was_held(stored, writer):
    """A keep-mine that overwrites a newer Exomem change with a person's older edit."""
    hold(stored, writer, "edit")
    seen = only_item(stored).item_version
    newer = EXOMEM + b"A later Exomem change.\n"
    write.commit(writer, put(ALPHA, newer))
    with pytest.raises(write.StoreConflict) as refused:
        resolve(stored, writer, held.KEEP_MINE, seen=seen)
    assert refused.value.remediation
    after = only_item(stored)
    with pytest.raises(write.StoreConflict) as refused:
        resolve(stored, writer, held.KEEP_MINE, seen=after.item_version)
    assert "revision" in refused.value.message
    assert stored_page(stored)[0][0] == newer
    assert (stored / ALPHA).read_bytes() == MINE
    assert len(rows(stored, "SELECT path FROM store_held")) == 1


def test_keep_store_saves_the_persons_exact_bytes_outside_the_vault_and_queues_the_stores_page(
    stored, writer
):
    """A keep-store that loses the person's bytes, saves them inside the vault, or leaves nothing
    queued, so the store's page never reaches the file. The held body is stale, so only the file can
    be the source of the export."""
    revision = hold(stored, writer, "edit")
    with writer.handle.transaction() as tx:
        tx.execute("UPDATE store_held SET body = ? WHERE path = ?", (b"stale", ALPHA))
    resolved = resolve(stored, writer, held.KEEP_STORE)
    (exported,) = exports(stored)
    assert exported == resolved.exported_to
    assert exported.relative_to(state_paths.vault_state_dir(stored)).parts[0] == held.EXPORT_FOLDER
    assert exported.parts[-len(ALPHA.split("/")) :] == tuple(ALPHA.split("/"))
    assert datetime.now(UTC).strftime("%Y-%m-%d") in exported.parts
    assert digest(exported.read_bytes()) == digest(MINE) == resolved.exported_sha256
    assert stored_page(stored) == [(EXOMEM, revision + 1)]
    assert queue(stored) == [(ALPHA, "write")]
    assert rows(stored, "SELECT sha256 FROM store_manifest WHERE path = ?", ALPHA) == [
        (digest(MINE),)
    ]
    assert rows(stored, "SELECT path FROM store_held") == []
    copy = exported.relative_to(state_paths.vault_state_dir(stored)).as_posix()
    assert rows(
        stored, "SELECT operation, reason FROM store_audit WHERE revision = ?", revision + 1
    ) == [
        (
            "write",
            f"held keep-store: {WHY}; the person's file is saved as {copy} (sha256 {digest(MINE)})",
        )
    ]
    assert (stored / ALPHA).read_bytes() == MINE, (
        "the projector, not the resolution, replaces the file"
    )


def test_keep_store_never_replaces_an_earlier_export_of_the_same_path(stored, writer):
    """A second resolution on one day that overwrites the first export's bytes with its own."""
    hold(stored, writer, "edit")
    resolve(stored, writer, held.KEEP_STORE)
    first = exports(stored)[0].read_bytes()
    (stored / ALPHA).write_bytes(b"A second edit.\n")
    with writer.handle.transaction() as tx:
        tx.execute(
            "INSERT INTO store_held(path, kind, store_revision, sha256, detected_at) "
            "VALUES (?, 'edit', ?, ?, ?)",
            (ALPHA, only_revision(stored), digest(b"A second edit.\n"), "2026-11-09T10:00:00Z"),
        )
    resolve(stored, writer, held.KEEP_STORE)
    saved = exports(stored)
    assert [path.name for path in saved] == ["Alpha.1.md", "Alpha.md"]
    assert {path.read_bytes() for path in saved} == {first, b"A second edit.\n"}


def test_keep_mine_on_a_delete_removes_the_page_and_projects_nothing(stored, writer):
    """A keep-mine on a person's delete that restores the file.

    Or one that leaves a manifest row and an intent, so the projector holds the missing file again.
    """
    revision = hold(stored, writer, "delete")
    resolved = resolve(stored, writer, held.KEEP_MINE)
    assert resolved.revision == revision + 1
    assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", ALPHA) == []
    assert rows(stored, "SELECT path FROM store_manifest WHERE path = ?", ALPHA) == []
    assert queue(stored) == []
    assert rows(stored, "SELECT path FROM store_held") == []
    assert rows(stored, "SELECT operation FROM store_audit WHERE revision = ?", revision + 1) == [
        ("delete",)
    ]
    assert not (stored / ALPHA).exists()


def test_keep_store_on_a_delete_queues_the_page_and_exports_nothing(stored, writer):
    """A keep-store on a person's delete that invents an export for a file that is not there.

    Or one that leaves the manifest row, so the projector holds the missing file again instead of
    writing it.
    """
    revision = hold(stored, writer, "delete")
    resolved = resolve(stored, writer, held.KEEP_STORE)
    assert (resolved.exported_to, exports_exist(stored)) == (None, False)
    assert stored_page(stored) == [(EXOMEM, revision + 1)]
    assert queue(stored) == [(ALPHA, "write")]
    assert rows(stored, "SELECT path FROM store_manifest WHERE path = ?", ALPHA) == []
    assert rows(stored, "SELECT path FROM store_held") == []


def test_keep_store_on_an_edit_of_a_page_the_store_deleted_removes_the_file_after_exporting_it(
    stored, writer
):
    """A person's edit that raced Exomem's delete: keep-store must queue the file's removal, and the
    person's bytes must be saved first."""
    hold_after_store_delete(stored, writer)
    resolve(stored, writer, held.KEEP_STORE)
    assert [digest(path.read_bytes()) for path in exports(stored)] == [digest(MINE)]
    assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", ALPHA) == []
    assert queue(stored) == [(ALPHA, "delete")]
    assert rows(stored, "SELECT sha256 FROM store_manifest WHERE path = ?", ALPHA) == [
        (digest(MINE),)
    ]
    assert rows(stored, "SELECT path FROM store_held") == []


def test_a_resolution_refuses_when_the_held_item_changed_after_it_was_listed(stored, writer):
    """A resolution that acts on what the agent read an hour ago, after a newer edit was held."""
    hold(stored, writer, "edit")
    seen = only_item(stored).item_version
    with writer.handle.transaction() as tx:
        tx.execute(
            "UPDATE store_held SET detected_at = '2026-11-09T11:00:00Z', sha256 = ?",
            (digest(b"x"),),
        )
    with pytest.raises(write.StoreConflict):
        resolve(stored, writer, held.KEEP_STORE, seen=seen)
    assert len(rows(stored, "SELECT path FROM store_held")) == 1
    assert not exports_exist(stored)
    assert stored_page(stored)[0][0] == EXOMEM


def test_a_resolution_refuses_when_the_persons_file_changed_after_it_was_listed(stored, writer):
    """A keep-mine that commits bytes the agent never saw."""
    hold(stored, writer, "edit")
    seen = only_item(stored).item_version
    (stored / ALPHA).write_bytes(MINE + b"The person kept typing.\n")
    with pytest.raises(write.StoreConflict):
        resolve(stored, writer, held.KEEP_MINE, seen=seen)
    assert stored_page(stored)[0][0] == EXOMEM


def test_a_held_row_that_goes_between_the_read_and_the_commit_rolls_the_whole_resolution_back(
    stored, writer, monkeypatch
):
    """Two agents resolving one item at once: the second must not also commit the page it read."""
    revision = hold(stored, writer, "edit")
    seen = only_item(stored).item_version
    observe = held._observe

    def observe_then_lose_the_row(*arguments, **keywords):
        found = observe(*arguments, **keywords)
        with writer.handle.transaction() as tx:
            tx.execute("DELETE FROM store_held")
        return found

    monkeypatch.setattr(held, "_observe", observe_then_lose_the_row)
    with pytest.raises(write.StoreConflict):
        resolve(stored, writer, held.KEEP_MINE, seen=seen)
    assert stored_page(stored) == [(EXOMEM, revision)]
    assert read.current_revision(stored) == revision


def test_an_unknown_choice_and_an_item_that_is_not_held_each_name_what_to_change(stored, writer):
    """A refusal that leaves the agent to guess the choices, or that another agent resolved it."""
    hold(stored, writer, "edit")
    with pytest.raises(OpError) as bad:
        held.resolve_held(stored, ALPHA, "keep-both", why=WHY, seen="x", writer=writer)
    assert bad.value.code == held.BAD_CHOICE and held.KEEP_MINE in bad.value.remediation
    resolve(stored, writer, held.KEEP_STORE)
    with pytest.raises(write.StoreConflict) as gone:
        held.resolve_held(stored, ALPHA, held.KEEP_STORE, why=WHY, seen="x", writer=writer)
    assert "List the held items" in gone.value.remediation


def test_after_keep_store_the_projector_replaces_the_persons_file_and_holds_nothing_again(
    stored, writer
):
    """A resolution whose manifest facts leave the projector to hold the same file again, so the
    item returns after every pass and the store's page never reaches the file."""
    projecting = hold_through_the_projector(stored, writer, "edit")
    resolve(stored, writer, held.KEEP_STORE)
    report = projecting.run_pass()
    assert (report.projected, report.held) == (1, 0)
    assert (stored / ALPHA).read_bytes() == EXOMEM
    assert [path.read_bytes() for path in exports(stored)] == [MINE]
    assert held.list_held(stored) == [] and queue(stored) == []


def test_after_keep_mine_the_projector_leaves_the_persons_file_alone_and_holds_nothing_again(
    stored, writer
):
    """A keep-mine that leaves a queued write behind, so the projector overwrites the edit that the
    person asked to keep."""
    projecting = hold_through_the_projector(stored, writer, "edit")
    resolve(stored, writer, held.KEEP_MINE)
    assert projecting.run_pass().settled == 0
    assert (stored / ALPHA).read_bytes() == MINE
    assert stored_page(stored)[0][0] == MINE
    assert held.list_held(stored) == [] and queue(stored) == []


def test_after_keep_store_on_a_deleted_file_the_projector_writes_the_page_back(stored, writer):
    """A keep-store on a person's delete that the projector holds again, because the manifest still
    names a file that is not there."""
    projecting = hold_through_the_projector(stored, writer, "delete")
    resolve(stored, writer, held.KEEP_STORE)
    report = projecting.run_pass()
    assert (report.projected, report.held) == (1, 0)
    assert (stored / ALPHA).read_bytes() == EXOMEM
    assert held.list_held(stored) == [] and queue(stored) == []
