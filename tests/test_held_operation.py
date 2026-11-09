"""The held-items tool operations: what the review tool lists and what the repair tool resolves.

They are thin over ``vault_store.held``, whose own cases are in ``test_vault_store_held``. These
cases cover what the adapter adds: the dispatch of both modes, the answer an agent repeats to the
person, the refusal that names what to change, and the page that a caller may not read.
"""

# Fixtures imported from the sibling test modules are redefined by each test's arguments.
# ruff: noqa: F811

from __future__ import annotations

import sys

import pytest
from test_governance_egress import EXTERNAL, SCOPE_ID, _gov_dir, write_rule, write_scope
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_held import (  # noqa: F401
    ALPHA,
    EXOMEM,
    MINE,
    digest,
    exports,
    hold,
    hold_a_binary,
    hold_after_store_delete,
    queue,
    stored_page,
)
from test_vault_store_migration import DIAGRAM, vault  # noqa: F401
from test_vault_store_read_write import rows, stored, writer  # noqa: F401

from exomem import commands, held_operation, state_paths
from exomem.cli_ops import OpError
from exomem.collection_store import preview
from exomem.governance import egress, membership, policy, principal
from exomem.vault_store import held, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

WHY = "the owner asked"


def tagged(body):
    return body.replace(b"type: note\n", b"type: note\ntags: [restricted]\n")


def restrict_by_tag(root):
    """An external audience may not read a page that carries the tag `restricted`."""
    scope = _gov_dir(root) / "scopes" / "patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Restricted\ntags: [restricted]\n",
        encoding="utf-8",
    )
    write_rule(root, ceiling=egress.LEVEL_NONE)
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


OUTSIDER = principal.RequestPrincipal(audience_id=EXTERNAL, surface="mcp")


@pytest.fixture
def served(writer, monkeypatch):
    """The request's bound writer, as a served request holds it."""
    monkeypatch.setattr(preview, "bound_writer", lambda root: writer)


def listed(root):
    return commands.op_review_memory(root, mode=held_operation.LIST_MODE)


def repair(root, choice, **overrides):
    arguments = {
        "path": ALPHA,
        "choice": choice,
        "item_version": held.list_held(root)[0].item_version,
        "why": WHY,
    }
    return commands.op_maintain_memory(
        root, mode=held_operation.RESOLVE_MODE, **{**arguments, **overrides}
    )


def test_the_review_tool_lists_each_item_with_what_the_store_lists_and_a_sentence_to_repeat(
    stored, writer
):
    """A tool whose items differ from the store's, so an agent resolves with a stale item_version.

    Or one whose answer does not say plainly what waits for a choice.
    """
    hold(stored, writer, "edit")
    (item,) = held.list_held(stored)
    answer = listed(stored)
    (row,) = answer["items"]
    assert (row["path"], row["kind"], row["detected_at"], row["item_version"]) == (
        item.path,
        item.kind,
        item.detected_at,
        item.item_version,
    )
    assert row["file"] == {"exists": True, "sha256": item.file.sha256, "size": item.file.size}
    assert row["store"] == {"revision": item.store.revision, "sha256": item.store.sha256}
    assert answer["outcome"].startswith("1 file changed outside Exomem and waits for a choice.")
    assert (row["resolvable"], row["keep_mine_possible"]) == (True, True)
    assert row["choices"] == ["keep-mine", "keep-store"]
    assert answer["total"] == 1 and answer["truncated"] is False


def test_the_review_tool_offers_no_choice_for_a_held_binary_and_says_it_stays_held(stored, writer):
    """A sentence that promises the person a choice that the store cannot take yet."""
    hold_a_binary(stored, writer)
    answer = listed(stored)
    (row,) = answer["items"]
    assert (row["resolvable"], row["keep_mine_possible"], row["choices"]) == (False, False, [])
    assert "wait" not in answer["outcome"]
    assert "stay held until a later release adds that" in answer["outcome"]
    assert row["not_resolvable_because"] == held.BINARY_BLOCK


def test_the_review_tool_says_when_the_vault_is_not_in_store_mode_instead_of_reporting_none_held(
    vault,
):
    """An answer that tells an agent nothing waits in a vault that has no store to hold anything."""
    answer = listed(vault)
    assert answer["items"] == [] and "not in store mode" in answer["outcome"]


def test_the_repair_tool_keeps_the_stores_page_and_tells_where_the_persons_file_went(
    stored, writer, served
):
    """A resolve that changes the store but leaves the agent unable to say where the bytes are."""
    revision = hold(stored, writer, "edit")
    answer = repair(stored, held.KEEP_STORE)
    (exported,) = exports(stored)
    assert (
        answer["saved_copy"] == exported.relative_to(state_paths.vault_state_dir(stored)).as_posix()
    )
    assert exported.read_bytes() == MINE
    assert stored_page(stored)[0][1] == revision + 1 and queue(stored) == [(ALPHA, "write")]
    assert answer["outcome"].startswith("Kept Exomem's page for")
    assert answer["diagnostics"] == {"revision": revision + 1, "saved_copy_sha256": digest(MINE)}
    assert listed(stored)["items"] == []


def test_the_repair_tool_tells_a_kept_delete_apart_from_a_kept_edit(stored, writer, served):
    """A keep-mine on a person's delete that says it kept a file that is not there."""
    hold(stored, writer, "delete")
    answer = repair(stored, held.KEEP_MINE)
    assert answer["outcome"].startswith("Removed ") and "saved_copy" not in answer
    assert rows(stored, "SELECT path FROM store_pages WHERE path = ?", ALPHA) == []


def test_the_repair_tool_names_the_argument_to_add(stored, writer, served):
    """A refusal that leaves the agent to guess what is missing."""
    hold(stored, writer, "edit")
    with pytest.raises(OpError) as missing:
        repair(stored, held.KEEP_STORE, item_version=None)
    assert (
        missing.value.code == held_operation.INVALID and "item_version" in missing.value.remediation
    )
    assert len(held.list_held(stored)) == 1


def test_a_policy_that_restricts_one_folder_hides_its_held_item_from_an_outside_caller_only(
    stored, writer, served
):
    """A visibility check on the page's bytes on disk hides every held item from everyone, the owner
    too, because a held edit always differs from the store and a held delete has no file. A caller
    who may not read the folder must still see nothing, and not even a count."""
    hold(stored, writer, "edit")
    version = held.list_held(stored)[0].item_version
    write_scope(stored, paths="Notes/**")
    write_rule(stored, ceiling=0)
    policy._CACHE.clear()
    egress.clear_decision_memo()
    assert len(listed(stored)["items"]) == 1
    outsider = principal.RequestPrincipal(audience_id=EXTERNAL, surface="mcp")
    with principal.request_scope(outsider):
        answer = listed(stored)
        assert answer["items"] == [] and answer["total"] == 0
        assert "withheld" not in answer and answer["outcome"].startswith("Nothing is held")
        with pytest.raises(write.StoreConflict):
            repair(stored, held.KEEP_MINE, item_version=version)
    assert stored_page(stored)[0][0] != MINE
    assert repair(stored, held.KEEP_MINE, item_version=version)["choice"] == held.KEEP_MINE
    assert stored_page(stored)[0][0] == MINE


@pytest.mark.parametrize("stored_page_is_tagged", [True, False])
def test_a_tag_scope_hides_a_held_item_from_an_outside_caller_only_when_the_stored_page_is_tagged(
    stored, writer, served, stored_page_is_tagged
):
    """A scope that reads frontmatter left a level from the path alone at the minimum for every
    caller, so the owner saw nothing held and could not resolve. The stored page decides, never the
    person's file: here the file carries the tag exactly when the stored page does not."""
    hold(
        stored,
        writer,
        "edit",
        exomem=tagged(EXOMEM) if stored_page_is_tagged else EXOMEM,
        mine=MINE if stored_page_is_tagged else tagged(MINE),
    )
    version = held.list_held(stored)[0].item_version
    restrict_by_tag(stored)
    with principal.request_scope(OUTSIDER):
        assert len(listed(stored)["items"]) == (0 if stored_page_is_tagged else 1)
    assert len(listed(stored)["items"]) == 1
    assert repair(stored, held.KEEP_MINE, item_version=version)["choice"] == held.KEEP_MINE
    assert held.list_held(stored) == []


def test_a_held_edit_of_a_page_the_store_deleted_is_seen_and_resolved_by_the_owner_only(
    stored, writer, served
):
    """The store holds no page there, so a decision on its bytes has nothing to read. The item
    still belongs to the owner, who must see and resolve it, and a path scope must still hide it
    from a caller outside that policy."""
    hold_after_store_delete(stored, writer)
    version = held.list_held(stored)[0].item_version
    write_scope(stored, paths="Notes/**")
    write_rule(stored, ceiling=0)
    policy._CACHE.clear()
    egress.clear_decision_memo()
    with principal.request_scope(OUTSIDER):
        answer = listed(stored)
        assert answer["items"] == [] and answer["outcome"].startswith("Nothing is held")
        with pytest.raises(write.StoreConflict):
            repair(stored, held.KEEP_STORE, item_version=version)
    assert [row["path"] for row in listed(stored)["items"]] == [ALPHA]
    assert repair(stored, held.KEEP_STORE, item_version=version)["choice"] == held.KEEP_STORE
    assert held.list_held(stored) == []


@pytest.mark.parametrize("file_is_tagged", [True, False])
def test_a_tag_scope_judges_an_item_with_no_stored_page_on_the_persons_file_for_an_outside_caller(
    stored, writer, file_is_tagged
):
    """With no stored page the person's file is the only bytes of the page, and the owner sees it
    either way."""
    hold_after_store_delete(stored, writer, mine=tagged(MINE) if file_is_tagged else MINE)
    restrict_by_tag(stored)
    with principal.request_scope(OUTSIDER):
        assert len(listed(stored)["items"]) == (0 if file_is_tagged else 1)
    assert len(listed(stored)["items"]) == 1


def test_a_held_binary_stays_listed_for_the_owner_under_a_tag_scope_and_not_for_an_outside_caller(
    stored, writer
):
    """A binary has no stored page and no Markdown to read: only the owner's access settles it."""
    hold_a_binary(stored, writer)
    restrict_by_tag(stored)
    with principal.request_scope(OUTSIDER):
        assert listed(stored)["items"] == []
    (row,) = listed(stored)["items"]
    assert (row["path"], row["resolvable"], row["choices"]) == (DIAGRAM, False, [])
    assert row["not_resolvable_because"] == held.BINARY_BLOCK
