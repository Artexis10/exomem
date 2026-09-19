"""The write-time `entity_candidate` block on a committed durable write.

Covers `capture_sweep.entity_candidate` -- the `write-time-identity-candidates`
capability's producer. The graph is real: pages are actual files on an actual
vault, indexed through a real `EpistemicGraphIndex.rebuild_all()`, so "this
write's own page already linked it" and "at most sixteen rows" are exercised
against the real dependency-lookup keys rather than a mock of them. The
write-time seam itself -- the freshly committed page's own `body_wikilinks`
and the corpus resolution/registry context -- stays the same lightweight
`SimpleNamespace` level `test_capture_sweep.py` already uses for `hints()`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from exomem import capture_sweep, epistemic_graph

A = "Knowledge Base/Notes/a.md"
B = "Knowledge Base/Notes/b.md"
NEW = "Knowledge Base/Notes/new.md"
NAME = "Harbour Studio"


def _seed(tmp_path: Path, pages: dict[str, str]) -> None:
    for rel, body in pages.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()


def _link_body(name: str = NAME) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# Note\n\nMet [[{name}]].\n"


def _page(path: str, wikilinks: tuple[tuple[str, int], ...]) -> SimpleNamespace:
    return SimpleNamespace(path=path, frontmatter={}, body_wikilinks=wikilinks)


def _page_state(path: str, *, title: str = "", status: str | None = "active") -> SimpleNamespace:
    """The `corpus.pages[...]` shape `_linking_page_eligible` reads."""
    return SimpleNamespace(path=path, title=title, status=status)


def _corpus(
    tmp_path: Path,
    *,
    titles: dict[str, tuple[str, ...]] | None = None,
    pages: dict[str, SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        vault_root=tmp_path,
        pages=pages or {},
        resolver_full_paths=frozenset(),
        resolver_kb_stripped=frozenset(),
        resolver_stems={},
        resolver_titles=titles or {},
    )


# ------------------------------------------------------------- the transition


def test_the_second_page_carries_the_block(tmp_path: Path) -> None:
    """One eligible page already links it; this write is the second."""
    _seed(tmp_path, {A: _link_body()})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(tmp_path, pages={A: _page_state(A, title="A")})

    result = capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus)

    assert result == {
        "identities": [
            {
                "name": NAME,
                "pages": sorted([A, NEW]),
                "near_matches": [],
                "routes": ["resolve-entity", "create-entity"],
            }
        ]
    }


def test_a_third_page_does_not_repeat_the_prompt(tmp_path: Path) -> None:
    """Two eligible pages already link it; this write is the third."""
    _seed(tmp_path, {A: _link_body(), B: _link_body()})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(
        tmp_path, pages={A: _page_state(A, title="A"), B: _page_state(B, title="B")}
    )

    assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


def test_editing_a_page_that_already_linked_it_does_not_fire(tmp_path: Path) -> None:
    """The write's OWN page is already among the indexed sources for this name."""
    _seed(tmp_path, {A: _link_body()})
    page = _page(A, ((NAME, 3),))  # same path the graph already indexed
    corpus = _corpus(tmp_path, pages={A: _page_state(A, title="A")})

    assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


def test_no_prior_eligible_page_is_still_only_one_page_total(tmp_path: Path) -> None:
    """Nobody links it yet: this write is the FIRST page, not the second."""
    corpus = _corpus(tmp_path, pages={})
    page = _page(NEW, ((NAME, 3),))
    # No seed at all -- the sidecar exists but the dependency index is empty.
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


# ---------------------------------------------------------------- disclosure


def test_withheld_when_structural_suggestions_is_off(
    monkeypatch, tmp_path: Path
) -> None:
    from exomem import envelope

    _seed(tmp_path, {A: _link_body()})
    monkeypatch.setattr(envelope, "active", lambda *a, **k: {"structural_suggestions": "off"})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(tmp_path, pages={A: _page_state(A, title="A")})

    assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


def test_withheld_while_a_mutation_batch_is_active(tmp_path: Path) -> None:
    """Two notes in one batch count once: the seam is silent inside the batch."""
    from exomem import due_state

    _seed(tmp_path, {A: _link_body()})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(tmp_path, pages={A: _page_state(A, title="A")})

    with due_state.batch_scope(tmp_path):
        assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


def test_a_warming_graph_sends_nothing(tmp_path: Path) -> None:
    """No rebuild at all -- the sidecar does not exist yet."""
    path = tmp_path / A
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_link_body(), encoding="utf-8")
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(tmp_path, pages={A: _page_state(A, title="A")})

    assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


def test_a_disabled_graph_index_sends_nothing(monkeypatch, tmp_path: Path) -> None:
    _seed(tmp_path, {A: _link_body()})
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(tmp_path, pages={A: _page_state(A, title="A")})

    assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


# ------------------------------------------------------- the page-level exclusions


def test_a_retired_source_is_not_counted(tmp_path: Path) -> None:
    retired = "Knowledge Base/Notes/retired.md"
    _seed(
        tmp_path,
        {
            A: _link_body(),
            retired: "---\ntype: insight\nstatus: superseded\n---\n# R\n\n"
            f"Met [[{NAME}]].\n",
        },
    )
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(
        tmp_path,
        pages={
            A: _page_state(A, title="A"),
            retired: _page_state(retired, title="R", status="superseded"),
        },
    )

    result = capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus)

    assert result is not None
    assert result["identities"][0]["pages"] == sorted([A, NEW])


def test_an_excluded_access_tier_source_is_not_counted(tmp_path: Path) -> None:
    excluded = "Knowledge Base/Private/hidden.md"
    (tmp_path / "Knowledge Base" / "_access.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Private\n", encoding="utf-8"
    )
    _seed(tmp_path, {A: _link_body(), excluded: _link_body()})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(
        tmp_path,
        pages={A: _page_state(A, title="A"), excluded: _page_state(excluded, title="Hidden")},
    )

    result = capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus)

    assert result is not None
    assert result["identities"][0]["pages"] == sorted([A, NEW])


def test_a_navigation_source_is_not_counted(tmp_path: Path) -> None:
    index_page = "Knowledge Base/Notes/index.md"
    _seed(tmp_path, {A: _link_body(), index_page: _link_body()})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(
        tmp_path,
        pages={A: _page_state(A, title="A"), index_page: _page_state(index_page, title="Notes")},
    )

    result = capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus)

    assert result is not None
    assert result["identities"][0]["pages"] == sorted([A, NEW])


def test_an_entities_subtree_source_is_not_counted(tmp_path: Path) -> None:
    entity_page = "Knowledge Base/Entities/People/e.md"
    _seed(tmp_path, {A: _link_body(), entity_page: _link_body()})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(
        tmp_path,
        pages={
            A: _page_state(A, title="A"),
            entity_page: _page_state(entity_page, title="E"),
        },
    )

    result = capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus)

    assert result is not None
    assert result["identities"][0]["pages"] == sorted([A, NEW])


def test_a_page_whose_own_title_is_the_name_is_not_counted(tmp_path: Path) -> None:
    self_page = "Knowledge Base/Notes/self.md"
    _seed(tmp_path, {A: _link_body(), self_page: _link_body()})
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(
        tmp_path,
        pages={A: _page_state(A, title="A"), self_page: _page_state(self_page, title=NAME)},
    )

    result = capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus)

    assert result is not None
    assert result["identities"][0]["pages"] == sorted([A, NEW])


def test_a_suffixed_name_standing_on_a_real_file_carries_no_block(tmp_path: Path) -> None:
    scan = tmp_path / "Reference" / "scan-2026.pdf"
    scan.parent.mkdir(parents=True, exist_ok=True)
    scan.write_bytes(b"%PDF-1.4 fixture\n")
    target = "Reference/scan-2026.pdf"
    _seed(tmp_path, {A: f"---\ntype: insight\nstatus: active\n---\n# A\n\n[[{target}]]\n"})
    page = _page(NEW, ((target, 3),))
    corpus = _corpus(tmp_path, pages={A: _page_state(A, title="A")})

    assert capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus) is None


def test_at_most_sixteen_rows_are_evaluated(tmp_path: Path) -> None:
    """A 17th, otherwise-eligible source beyond the cap is never even looked at.

    Sixteen pages sort before the one truly eligible page below and are all
    ineligible (retired); a 17th page, sorting last, links the SAME name and IS
    eligible. Evaluating all seventeen would find two eligible pages (no
    block); evaluating only the first sixteen (all ineligible) finds zero, so
    the block stays silent either way here -- proving the cap applies by
    exercising the boundary the spec pins, not by asserting a number that
    would pass by accident.
    """
    pages: dict[str, str] = {}
    page_states: dict[str, SimpleNamespace] = {}
    for index in range(16):
        rel = f"Knowledge Base/Notes/r{index:02d}.md"
        pages[rel] = "---\ntype: insight\nstatus: superseded\n---\n# R\n\n" f"Met [[{NAME}]].\n"
        page_states[rel] = _page_state(rel, title=f"R{index}", status="superseded")
    seventeenth = "Knowledge Base/Notes/z-seventeenth.md"
    pages[seventeenth] = _link_body()
    page_states[seventeenth] = _page_state(seventeenth, title="Z")

    _seed(tmp_path, pages)
    write = _page(NEW, ((NAME, 3),))
    corpus = _corpus(tmp_path, pages=page_states)

    assert capture_sweep.entity_candidate(tmp_path, page_state=write, corpus=corpus) is None


def test_a_folder_qualified_second_link_yields_no_block(tmp_path: Path) -> None:
    """The dependency index sees only the SAME bare spelling (design D5)."""
    deep = "Knowledge Base/Notes/deep.md"
    _seed(
        tmp_path,
        {
            A: _link_body(),
            deep: "---\ntype: insight\nstatus: active\n---\n# Deep\n\n"
            f"See [[Notes/People/{NAME}]].\n",
        },
    )
    page = _page(NEW, ((NAME, 3),))
    corpus = _corpus(
        tmp_path, pages={A: _page_state(A, title="A"), deep: _page_state(deep, title="Deep")}
    )

    result = capture_sweep.entity_candidate(tmp_path, page_state=page, corpus=corpus)

    assert result is not None
    assert result["identities"][0]["pages"] == sorted([A, NEW])
