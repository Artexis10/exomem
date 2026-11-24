"""The corpus that a governed move judges its after-state against is the before corpus patched by
the pages the move changes, never the whole vault derived again (OpenSpec
judge-relocations-by-what-they-change).

Each case is a move that changes how some link resolves. The patched corpus must be the one that a
derivation of the whole vault gives: the same pages, relation facts, inbound and outbound edges,
identity census, activation census and bound prior-path claims.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    memory_refs,
    page_lineage,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    semantic_writes,
)

NOTES = "Knowledge Base/Notes/Insights"
SHARED_ID = "3b8f61d2-9a47-4c05-8e1b-5d2c7a9e04f6"


def page(title: str, body: str = "Body.\n", *, identity: str | None = None) -> str:
    extra = f"{memory_refs.ID_FIELD}: {identity}\n" if identity else ""
    front = f"title: {title}\ntype: insight\nstatus: active\nproject: atlas\n{extra}"
    return f"---\n{front}---\n\n{body}"


def relations(target: str) -> str:
    return f"## Relations\n- supports [[{target}]]\n"


def write(root: Path, name: str, text: str) -> None:
    (root / NOTES / name).parent.mkdir(parents=True, exist_ok=True)
    (root / NOTES / name).write_text(text, encoding="utf-8")


def seed(root: Path) -> None:
    write(root, "slug-4f2a.md", page("Harbour Pilot", "- [claim] Tides set the schedule ^tides\n"))
    write(root, "path-linker.md", page("Path Linker", relations(f"{NOTES}/slug-4f2a")))
    write(root, "anchor-linker.md", page("Anchor Linker", relations("slug-4f2a#^tides")))
    write(root, "title-linker.md", page("Title Linker", relations("Harbour Pilot")))
    # `[[lantern]]` resolves to the page titled Lantern until some page takes `lantern` as its stem.
    write(root, "beacon.md", page("Lantern"))
    write(root, "bare-linker.md", page("Bare Linker", relations("lantern")))
    write(root, "drifter.md", page("Drifter"))
    write(root, "copy-a.md", page("Copy A", relations("Lantern"), identity=SHARED_ID))
    # Two holders of one id number their identical links by path order, which a move can flip.
    write(root, "copy-b.md", page("Copy B", relations("Lantern"), identity=SHARED_ID))
    # An earlier move took spare.md to spare-kept.md; its lineage claims the path it left.
    write(root, "spare-kept.md", page_lineage.append_entry(page("Spare"), f"{NOTES}/spare.md"))
    write(root, "spare-linker.md", page("Spare Linker", relations("spare")))
    # A page without a title takes its stem's; the link to it is typed decomposed (NFD).
    write(root, "untitled.md", "---\ntype: insight\nstatus: active\nproject: atlas\n---\n\nBody.\n")
    write(root, "nfd-linker.md", page("NFD Linker", relations("Cafe\u0301")))


def move(root: Path, corpus, old: str, new: str):
    """The corpus once `old` moved to `new` as a governed move writes it, lineage entry and all."""
    source = (root / NOTES / old).read_text(encoding="utf-8")
    return semantic_writes._move_state_map(
        root,
        before_corpus=corpus,
        old_path=f"{NOTES}/{old}",
        new_path=f"{NOTES}/{new}",
        source=source,
        moved_source=page_lineage.append_entry(source, f"{NOTES}/{old}"),
        rewrites=(),
        registry=relation_registry.load_registry(root),
        language=semantic_language_registry.load_registry(root),
    )[0]


def full_build(root: Path, before, patched):
    """The corpus that deriving every page of `patched` gives, from the before corpus's census."""
    census = before.identity_census.with_pages(
        {path: patched.pages.get(path) for path in {*before.pages, *patched.pages}
         if before.pages.get(path) is not patched.pages.get(path)}
    )
    return semantic_contract.SemanticCorpusContext.from_states(
        root,
        patched.pages.values(),
        registry=before.registry,
        identity_census=census,
        prior_claims=patched.prior_claims,
    )


def facts_of(corpus, author: str) -> set[tuple[str, str | None]]:
    return {
        (fact.identity, fact.resolved_target_path) for fact in corpus.relation_facts
        if fact.authored_path == f"{NOTES}/{author}"
    }


#: Each move (old name, new name), and the page whose link facts it must change.
MOVES = {
    "a move into another folder": ("slug-4f2a.md", "Harbour/slug-4f2a.md", "path-linker.md"),
    "a rename to the title's stem": ("slug-4f2a.md", "harbour-pilot.md", "anchor-linker.md"),
    "a new stem that captures a bare-title link": ("drifter.md", "lantern.md", "bare-linker.md"),
    "a move of one of two pages sharing an id": ("copy-a.md", "wharf/copy-a.md", "copy-b.md"),
    "a path an earlier move vacated, filled again": ("drifter.md", "spare.md", "spare-linker.md"),
    "a new title that a decomposed link names": ("untitled.md", "Caf\u00e9.md", "nfd-linker.md"),
    "a decomposed new stem that a decomposed link names": (
        "drifter.md", "Cafe\u0301.md", "nfd-linker.md"
    ),
}


@pytest.mark.parametrize("case", MOVES)
def test_a_move_patches_the_corpus_to_what_a_full_derivation_gives(tmp_path, monkeypatch, case):
    """Defect that this guards: the after corpus of a move step either derives every relation in
    the vault again (seconds per step on a large vault, so a relocation of many pages times out) or,
    patched, keeps a link's stale resolution, so the move is judged on links no build would give."""
    seed(tmp_path)
    old, new, linker = MOVES[case]
    before = semantic_contract.build_corpus_context(tmp_path)

    def forbidden(*_arguments, **_keywords):
        raise AssertionError("the whole corpus was derived for one move")

    with monkeypatch.context() as patch:
        patch.setattr(semantic_contract, "_context_from_state_map", forbidden)
        after = move(tmp_path, before, old, new)
    full = full_build(tmp_path, before, after)

    assert after.as_dict() == full.as_dict(), case
    assert after.activation_census.candidates == full.activation_census.candidates, case
    assert after.prior_claims is full.prior_claims
    assert facts_of(after, linker) != facts_of(before, linker), case
