"""The corpus that a save judges its page against, without deriving the whole vault again
(OpenSpec make-vault-store-canonical, task 2.2).

A page that a save adds joins the corpus by the page delta, and a restricted caller's corpus is cut from
the enrichment that every caller shares. Each must give the corpus that a derivation of the whole vault
gives, in a vault of files and in a vault in store mode.
"""

from __future__ import annotations

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_readers import EXTERNAL, restrict_external

from exomem import memory_refs, semantic_contract
from exomem.governance.principal import request_scope
from exomem.vault_store import write

stored, writer, served = read_write.stored, read_write.writer, write_path.served

NOTES = "Knowledge Base/Notes"
SHARED_ID = "7e2c9b4a-5d1f-4a83-b6c0-3f8e1d2a9c57"


def page(title, body="", *, identity=None, tags=None):
    lines = [f"title: {title}", "type: insight", "status: active"]
    if identity:
        lines.append(f"{memory_refs.ID_FIELD}: {identity}")
    if tags:
        lines.append(f"tags: [{tags}]")
    return "---\n" + "\n".join(lines) + f"\n---\n# {title}\n\n{body}"


class Kb:
    """A vault that holds its pages as files, or in store mode, and the way to add a page to it."""

    def __init__(self, root, store_writer):
        self.root, self.writer = root, store_writer

    def add(self, rel, text):
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_text(text)
        if self.writer is not None:
            change = write.StoreChange(
                write.Origin.GOVERNED, "test", puts=(write.PagePut(rel, text.encode()),)
            )
            write.commit(self.writer, change)

    def build(self):
        """The corpus a build over the vault gives for the caller bound now."""
        return semantic_contract.build_corpus_context_with_census(self.root)[0]


@pytest.fixture(params=["file", "store"])
def kb(request):
    if request.param == "file":
        return Kb(request.getfixturevalue("vault"), None)
    return Kb(request.getfixturevalue("served"), request.getfixturevalue("writer"))


def seed(kb):
    kb.add(f"{NOTES}/target.md", page("Target", "- [claim] Wording ^claim\n"))
    kb.add(f"{NOTES}/plain.md", page("Plain Page"))
    kb.add(f"{NOTES}/stem-linker.md", page("Stem Linker", "## Relations\n- supports [[plain]]\n"))
    kb.add(f"{NOTES}/anchor-linker.md", page("Anchor Linker", "## Relations\n- supports [[Target#^claim]]\n"))
    kb.add(f"{NOTES}/dangling.md", page("Dangling", "## Relations\n- supports [[Shared Title]]\n"))
    kb.add(f"{NOTES}/copy-a.md", page("Copy A", identity=SHARED_ID))
    kb.add(f"{NOTES}/copy-b.md", page("Copy B", "## Relations\n- supports [[Target]]\n", identity=SHARED_ID))


#: What a save adds or changes, and whether it must move a link's resolution (so that the case is not vacuous).
CANDIDATES = {
    "a new title": ("new-page.md", page("Brand New"), False),
    "a title equal to an existing page's file stem": ("twin.md", page("plain"), False),
    "a title that makes an unresolved link resolve": ("shared.md", page("Shared Title"), True),
    "an exomem_id that two pages already share": (
        "copy-c.md", page("Copy C", "## Relations\n- supports [[Target]]\n", identity=SHARED_ID), True
    ),
    "an edit of the page a link names": (
        "target.md", page("Target", "- [claim] Reworded ^claim\n"), True
    ),
}


def test_a_candidate_page_joins_the_corpus_as_a_full_derivation_would_have_it(kb, monkeypatch):
    """Defect that this guards: a page that a save adds is judged against a corpus whose links resolve as
    they did before it, so a save is checked against a vault that no build would produce. For each kind
    of page, the corpus that the page delta gives is the one that a derivation of the whole corpus gives,
    and no whole corpus is derived to reach it."""
    seed(kb)
    before = kb.build()

    def forbidden(*_arguments, **_keywords):
        raise AssertionError("a whole corpus was derived for one page")

    for case, (name, text, moves_a_link) in CANDIDATES.items():
        candidate = semantic_contract.build_page_state(kb.root, f"{NOTES}/{name}", text)
        with monkeypatch.context() as patch:
            patch.setattr(semantic_contract, "_context_from_state_map", forbidden)
            patched = before.with_candidate(candidate)
        full = semantic_contract._context_from_state_map(
            kb.root,
            {**before.pages, candidate.path: candidate},
            before.registry,
            before.identity_census.with_page(candidate),
        )
        assert patched.as_dict() == full.as_dict(), case
        if moves_a_link:
            assert patched.relation_facts != before.relation_facts, case


def test_a_restricted_callers_corpus_is_a_full_filtered_build_and_the_shared_memo_stays_unfiltered(
    kb, corpus_cache_on, monkeypatch
):
    """Defect that this guards: the enrichment that every caller reads is kept with what a restricted caller
    was shown, or a page that caller may not see enriches into its corpus. A restricted caller's corpus is
    the one that a full build for it gives, its withheld page at its raw state, whether the shared memo was
    filled by it or by anyone else and whether a page changed since; the memo holds the enrichment of every
    page and no caller's view, and the owner's corpus is the one that a full build for the owner gives."""
    seed(kb)
    secret = f"{NOTES}/secret.md"
    secret_page = page("Secret", "- [claim] Withheld. ^w\n\n## Relations\n- supports [[Target]]\n", tags="secret")
    kb.add(secret, secret_page)
    restrict_external(kb.root, "tags: [secret]")
    memo_key = (semantic_contract._corpus_cache_key(kb.root), False)

    with request_scope(EXTERNAL):
        filled_by_restricted = kb.build()
    shared = semantic_contract._ENRICHED_CONTEXT_MEMO[memo_key][2]
    raw = semantic_contract._build_corpus_context_with_census(kb.root)[0].pages[secret]
    assert filled_by_restricted.pages[secret] == raw
    assert shared.pages[secret] != raw and shared.writer_visibility is None
    assert shared is not filled_by_restricted

    # A page changes, the withheld one too: the memo is patched, and the restricted caller's corpus with it.
    kb.add(f"{NOTES}/target.md", page("Target", "- [claim] Reworded ^claim\n"))
    kb.add(secret, secret_page.replace("Withheld.", "Withheld again."))
    with request_scope(EXTERNAL):
        restricted = kb.build()
    owner = kb.build()

    monkeypatch.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
    with request_scope(EXTERNAL):
        full_restricted = kb.build()
    full_owner = kb.build()
    assert restricted.pages[secret] == full_restricted.pages[secret]
    assert restricted.as_dict() == full_restricted.as_dict()
    assert owner.as_dict() == full_owner.as_dict()
    assert owner.pages[secret] != restricted.pages[secret]
