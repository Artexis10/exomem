"""A moved page keeps the paths it left, and links to them still find it.

OpenSpec add-curation-relocation-protocol, group 1. Each test names the failure it catches. The
vault is the canonical fixture in file mode with a current reference sidecar, as a live service
keeps it; the last tests bind the store.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from exomem import audit, create_file, memory_refs, move_file, page_lineage
from exomem import vault as vault_module

TODAY = dt.date(2026, 11, 16)
#: A compiled note of the fixture vault, which a governed move accepts as it stands.
A = "Knowledge Base/Notes/Insights/autovacuum-thresholds-prevent-table-bloat.md"
B = "Knowledge Base/Notes/Insights/Postgres/autovacuum-thresholds-prevent-table-bloat.md"
C = "Knowledge Base/Notes/Insights/Databases/autovacuum-scale-factors.md"
STEM = "autovacuum-thresholds-prevent-table-bloat"


def _note(title: str, *, exomem_id: str | None = None, body: str = "The ledger.\n") -> str:
    identity = f"exomem_id: {exomem_id}\n" if exomem_id else ""
    return f"---\ntitle: {title}\ntype: note\n{identity}---\n# {title}\n\n{body}"


def _with_identity(vault: Path, rel: str, *, source: str = A) -> str:
    """Write the fixture note at `source` to `rel` with a fresh `exomem_id`; return the id."""
    identity = str(uuid.uuid4())
    text = (vault / source).read_text(encoding="utf-8")
    _write(vault, rel, text.replace("---\n", f"---\nexomem_id: {identity}\n", 1))
    return identity


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _frontmatter(vault: Path, rel: str) -> dict:
    front, _body, _block = vault_module.parse_frontmatter((vault / rel).read_text(encoding="utf-8"))
    return front


def _resolve(vault: Path, target: str, **kwargs) -> tuple[str, str | None]:
    return vault_module.normalize_wikilink(target, vault, **kwargs)


def _prior_rows(vault: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(memory_refs.ReferenceIndex(vault).path)) as conn:
        return sorted(conn.execute("SELECT prior_path, path FROM prior_paths").fetchall())


def _move(vault: Path, old: str, new: str, **kwargs):
    kwargs.setdefault("update_wikilinks", False)
    return move_file.move_file(vault, old_path=old, new_path=new, today=TODAY, **kwargs)


@pytest.fixture
def moved(vault: Path) -> Path:
    """A note with an identity, moved from A to B and then to C, with a current sidecar."""
    (vault / C).parent.mkdir(parents=True, exist_ok=True)
    (vault / B).parent.mkdir(parents=True, exist_ok=True)
    _with_identity(vault, A)
    memory_refs.ReferenceIndex(vault).rebuild_all()
    _move(vault, A, B)
    _move(vault, B, C)
    return vault


def test_a_note_moved_twice_lists_both_prior_paths_and_links_to_them_resolve(moved: Path) -> None:
    """A move that records no lineage, records it out of order, or a resolver that ignores it."""
    entries = _frontmatter(moved, C)[page_lineage.LINEAGE_FIELD]

    assert [entry["path"] for entry in entries] == [A, B]
    for prior in (A, B, A.removeprefix("Knowledge Base/").removesuffix(".md")):
        assert _resolve(moved, prior) == (C.removesuffix(".md"), None)


def test_an_incremental_refresh_and_a_rebuild_give_the_same_prior_rows(moved: Path) -> None:
    """A refill that only the rebuild or only the refresh path performs, so the index drifts."""
    incremental = _prior_rows(moved)

    memory_refs.ReferenceIndex(moved).rebuild_all()

    assert incremental == sorted([(A, C), (B, C)])
    assert _prior_rows(moved) == incremental


def test_a_prior_stem_resolves_only_when_no_live_page_has_the_name(moved: Path) -> None:
    """A prior stem that wins over a live name, or that never resolves."""
    assert _resolve(moved, STEM) == (C.removesuffix(".md"), None)

    _write(moved, f"Knowledge Base/Notes/{STEM}.md", _note("Another note"))
    memory_refs.ReferenceIndex(moved).rebuild_all()

    assert _resolve(moved, STEM) == (f"Knowledge Base/Notes/{STEM}", None)


def test_a_page_without_frontmatter_moves_without_lineage(vault: Path) -> None:
    """A move that invents a frontmatter block in a page Exomem does not otherwise own."""
    plain = "Knowledge Base/Notes/plain.md"
    _write(vault, plain, "Only prose here.\n")

    _move(vault, plain, "Knowledge Base/Notes/plain-moved.md")

    assert (vault / "Knowledge Base/Notes/plain-moved.md").read_text(
        encoding="utf-8"
    ) == "Only prose here.\n"


def test_a_page_with_an_empty_frontmatter_block_moves_with_lineage(vault: Path) -> None:
    """A move that refuses a page whose frontmatter block is empty, or moves it without the entry
    that resolves its old path."""
    empty = "Knowledge Base/Notes/empty-block.md"
    _write(vault, empty, "---\n---\nOnly prose here.\n")

    _move(vault, empty, "Knowledge Base/Notes/empty-block-moved.md")

    entries = _frontmatter(vault, "Knowledge Base/Notes/empty-block-moved.md")
    assert [entry["path"] for entry in entries[page_lineage.LINEAGE_FIELD]] == [empty]


def test_a_relayout_old_path_resolves_after_a_later_move_and_a_rebuild(vault: Path) -> None:
    """A relayout row the refill ignores, or a reverted relayout that still claims its paths."""
    from exomem import relayout_record

    relaid = "Knowledge Base/Notes/Insights/Relaid/autovacuum-tuning.md"
    old = "Knowledge Base/Postgres/autovacuum-tuning.md"
    identity = _with_identity(vault, relaid)
    (vault / A).unlink()
    record = relayout_record.record_path(vault)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        "schema_version: 1\nrelayouts:\n  layout-1:\n    reverted: false\n    rows:\n"
        f"      - {{exomem_id: {identity}, old_path: '{old}', new_path: '{relaid}'}}\n"
        "  layout-0:\n    reverted: true\n    rows:\n"
        f"      - {{exomem_id: {identity}, old_path: 'Knowledge Base/Old/autovacuum-tuning.md', "
        f"new_path: '{old}'}}\n",
        encoding="utf-8",
    )
    memory_refs.ReferenceIndex(vault).rebuild_all()
    final = "Knowledge Base/Notes/Insights/autovacuum-tuning.md"
    _move(vault, relaid, final)

    assert _resolve(vault, old) == (final.removesuffix(".md"), None)
    memory_refs.ReferenceIndex(vault).rebuild_all()
    assert _resolve(vault, old) == (final.removesuffix(".md"), None)
    assert _resolve(vault, "Knowledge Base/Old/autovacuum-tuning")[1] is not None


def test_a_lineage_key_spelled_with_a_yaml_escape_claims_without_a_relayout_record(
    vault: Path,
) -> None:
    """A byte prefilter that skips the parse of a page whose `moved_from` key a double-quoted
    escape spells, so its prior path claims only once an unrelated relayout row exists."""
    old = "Knowledge Base/Old/escaped-ledger.md"
    rel = "Knowledge Base/Notes/escaped-ledger.md"
    _write(
        vault, rel,
        f'---\ntitle: Ledger\ntype: note\n"moved\\x5ffrom": [{{path: "{old}"}}]\n---\n# Ledger\n',
    )
    memory_refs.ReferenceIndex(vault).rebuild_all()

    assert (old, rel) in _prior_rows(vault)
    assert _resolve(vault, old.removesuffix(".md")) == (rel.removesuffix(".md"), None)


def test_a_prior_path_only_a_withheld_page_claims_answers_as_a_missing_path(moved: Path) -> None:
    """A visibility filter applied after counting claimants, so a hidden page's move leaks."""
    hidden = _resolve(moved, A, visible=lambda path: path != C)

    (moved / C).unlink()
    memory_refs.ReferenceIndex(moved).rebuild_all()

    assert hidden == _resolve(moved, A)
    assert hidden[1] is not None


def test_two_pages_claiming_one_prior_path_leave_its_link_unresolved_and_audit_names_both(
    vault: Path,
) -> None:
    """A link that silently picks one of two claimants, or an audit that misses the conflict."""
    left = "Knowledge Base/Notes/left.md"
    right = "Knowledge Base/Notes/right.md"
    for rel in (left, right):
        _write(
            vault,
            rel,
            f"---\ntitle: {rel}\ntype: note\nmoved_from:\n  - path: {A}\n---\nBody.\n",
        )
    memory_refs.ReferenceIndex(vault).rebuild_all()

    canonical, warning = _resolve(vault, A)
    findings = [
        finding
        for finding in audit._check_reference_identity(vault)
        if (finding.meta or {}).get("kind") == "prior_path_conflict"
    ]

    assert canonical == A.removesuffix(".md") and warning is not None
    assert [(finding.path, sorted(finding.paths)) for finding in findings] == [
        (A, sorted([left, right]))
    ]


def _prior_findings(vault: Path, kind: str) -> list[tuple[str, list[str]]]:
    return [
        (finding.path, finding.paths)
        for finding in audit._check_reference_identity(vault)
        if (finding.meta or {}).get("kind") == kind
    ]


def test_a_conflict_that_names_a_withheld_claimant_is_absent_from_a_restricted_audit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit that names every claimant of a prior path to a caller that may not see one."""
    from exomem.governance import egress

    left = "Knowledge Base/Notes/left.md"
    hidden = "Knowledge Base/Notes/Withheld/right.md"
    for rel in (left, hidden):
        _write(vault, rel, f"---\ntitle: {rel}\ntype: note\nmoved_from:\n  - path: {A}\n---\nBody.\n")
    memory_refs.ReferenceIndex(vault).rebuild_all()
    monkeypatch.setattr(egress, "caller_restricted", lambda _root: True)
    monkeypatch.setattr(egress, "restricted_release_filter", lambda _root: lambda path: path != hidden)

    assert _prior_findings(vault, "prior_path_conflict") == []


def test_an_identified_page_at_a_prior_path_is_reported_to_the_owner_only(
    moved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page with its own identity that a sync or an outside editor drops at a moved page's old
    path, which takes the old links with no finding, or whose finding pairs two pages' histories
    for a caller whose view is partial. A bound store gives the owner a release filter too, so
    an audit that reads restriction from that filter hides the finding from the owner."""
    from exomem.governance import egress

    _write(moved, A, _note("Harbour ledger", exomem_id=str(uuid.uuid4())))
    memory_refs.ReferenceIndex(moved).rebuild_all()
    monkeypatch.setattr(egress, "restricted_release_filter", lambda _root: lambda _path: True)

    owner = _prior_findings(moved, "reserved_path_identified")
    monkeypatch.setattr(egress, "caller_restricted", lambda _root: True)
    restricted = _prior_findings(moved, "reserved_path_identified")

    assert owner == [(A, [C])]
    assert restricted == []


def test_an_entity_created_again_under_its_moved_and_retitled_name_refuses(vault: Path) -> None:
    """An entity create that skips the reservation, so a new page under a moved and retitled
    entity's old name takes every link written to the entity before the move."""
    from exomem import link as link_module

    created = link_module.link(
        vault, entity_type="concept", name="Tidal Ledger", summary="A ledger of tide readings.",
        today=TODAY,
    )
    memory_refs.ReferenceIndex(vault).rebuild_all()
    renamed = created.path.replace("Tidal Ledger.md", "Tidal Ledger Archive.md")
    _move(vault, created.path, renamed)
    page = vault / renamed
    page.write_text(
        page.read_text(encoding="utf-8")
        .replace("title: Tidal Ledger\n", "title: Tidal Ledger Archive\n")
        .replace("# Tidal Ledger\n", "# Tidal Ledger Archive\n"),
        encoding="utf-8",
    )
    memory_refs.ReferenceIndex(vault).rebuild_all()

    with pytest.raises(link_module.LinkError) as refused:
        link_module.link(
            vault, entity_type="concept", name="Tidal Ledger", summary="A new ledger.", today=TODAY,
        )

    assert refused.value.code == "ENTITY_EXISTS"
    assert not (vault / created.path).exists()
    assert _resolve(vault, created.path.removesuffix(".md")) == (renamed.removesuffix(".md"), None)


def test_a_create_at_a_reserved_path_refuses_and_a_move_back_commits(moved: Path) -> None:
    """A create that takes a moved page's links, or a refusal that blocks the page's own way back."""
    with pytest.raises(create_file.CreateFileError) as refused:
        create_file.create_file(moved, path=A, content=_note("Squatter"), today=TODAY)


    assert refused.value.code == "FILE_EXISTS"
    assert C in refused.value.reason
    assert not (moved / A).exists()

    _move(moved, C, A)

    assert (moved / A).exists()
    assert [entry["path"] for entry in _frontmatter(moved, A)["moved_from"]] == [A, B, C]
    assert _resolve(moved, B) == (A.removesuffix(".md"), None)


@pytest.mark.parametrize(
    ("moved_from", "spelling"),
    [
        (A, A.replace(STEM, STEM.title())),
        (
            "Knowledge Base/Notes/Insights/caf\u00e9-ledger.md",
            "Knowledge Base/Notes/Insights/cafe\u0301-ledger.md",
        ),
    ],
    ids=["case", "unicode-form"],
)
def test_a_create_at_another_spelling_of_a_reserved_path_refuses(
    vault: Path, moved_from: str, spelling: str
) -> None:
    """A reservation keyed by exact bytes, which a case or Unicode-form variant walks around."""
    moved_to = "Knowledge Base/Notes/Insights/Databases/ledger.md"
    _with_identity(vault, moved_from)
    if moved_from != A:
        (vault / A).unlink()
    memory_refs.ReferenceIndex(vault).rebuild_all()
    (vault / moved_to).parent.mkdir(parents=True, exist_ok=True)
    _move(vault, moved_from, moved_to)

    with pytest.raises(create_file.CreateFileError) as refused:
        create_file.create_file(vault, path=spelling, content=_note("Squatter"), today=TODAY)

    assert refused.value.code == "FILE_EXISTS"
    assert moved_to in refused.value.reason


def test_a_new_note_named_like_a_reserved_path_takes_the_next_free_name(moved: Path) -> None:
    """`unique_path` handing out a name that a moved page's links still resolve to."""
    picked = vault_module.unique_path((moved / A).parent, STEM, vault_root=moved)

    assert picked.name == f"{STEM}-2.md"


def test_an_editor_file_without_identity_at_a_prior_path_does_not_take_its_links(
    moved: Path,
) -> None:
    """A file an editor drops at the old path stealing every link to the moved page."""
    _write(moved, A, "")
    memory_refs.ReferenceIndex(moved).rebuild_all()

    findings = [
        finding
        for finding in audit._check_reference_identity(moved)
        if (finding.meta or {}).get("kind") == "reserved_path_occupied"
    ]

    assert _resolve(moved, A) == (C.removesuffix(".md"), None)
    assert [(finding.path, finding.paths) for finding in findings] == [(A, [C])]


def test_a_delete_check_finds_a_link_to_a_prior_path_while_the_index_is_stale(
    moved: Path,
) -> None:
    """A delete check that trusts a stale derived index and misses a link to an old path."""
    linker = "Knowledge Base/Notes/harbour-log.md"
    _write(moved, linker, _note("Harbour log", body=f"See [[{A.removesuffix('.md')}]].\n"))
    with closing(sqlite3.connect(memory_refs.ReferenceIndex(moved).path)) as conn, conn:
        conn.execute("DELETE FROM prior_paths")

    hits = vault_module.find_inbound_wikilinks(moved, C)

    assert [hit.path for hit in hits] == [linker]


def test_a_move_judges_and_indexes_a_link_to_the_old_path_as_a_link_to_the_new_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move judged before its lineage is indexed, so the page loses the facts linking to it."""
    from exomem import epistemic_graph, semantic_writes

    linker = "Knowledge Base/Notes/Insights/harbour-log.md"
    _with_identity(vault, A)
    _write(vault, linker, _note("Harbour log", body=f"- supports [[{A.removesuffix('.md')}]]\n"))
    (vault / B).parent.mkdir(parents=True, exist_ok=True)
    memory_refs.ReferenceIndex(vault).rebuild_all()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    judged = []
    commit = semantic_writes.commit_move

    def spy(vault_root, *, preflight, mutate):
        judged.append(preflight.after_corpus)
        return commit(vault_root, preflight=preflight, mutate=mutate)

    monkeypatch.setattr(semantic_writes, "commit_move", spy)

    _move(vault, A, B)

    assert [(fact.authored_path, fact.resolved_target_path) for fact in judged[0].inbound[B]] == [
        (linker, B)
    ]
    with closing(sqlite3.connect(epistemic_graph.sidecar_path(vault))) as conn:
        edges = conn.execute(
            "SELECT DISTINCT dst_key FROM graph_edges WHERE source_path = ?", (linker,)
        ).fetchall()
    assert edges == [(f"file:{B}",)]


def test_reading_a_moved_page_by_a_path_it_left_finds_it_and_a_hidden_one_reads_as_missing(
    moved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read of an old path that answers NOT_FOUND while a link to the same path finds the page,
    or that finds a page the caller may not see and so tells them it exists."""
    from exomem import commands
    from exomem.governance import egress

    assert commands.read_memory_stored(moved, A)["path"] == C
    assert commands.read_memory_stored(moved, A.removesuffix(".md"))["path"] == C

    monkeypatch.setattr(egress, "caller_restricted", lambda _root, **_kw: True)
    monkeypatch.setattr(
        egress, "restricted_release_filter", lambda _root, **_kw: lambda path: path != C
    )
    with pytest.raises(ValueError) as hidden:
        commands.read_memory_stored(moved, A)
    with pytest.raises(ValueError) as absent:
        commands.read_memory_stored(moved, "Knowledge Base/Notes/Insights/never-there.md")
    assert str(hidden.value).split(":", 1)[0] == str(absent.value).split(":", 1)[0]


def _link_findings(vault: Path, linker: str) -> list[str]:
    report = audit.audit(vault, categories=["broken_wikilink", "forward_reference"])
    return [finding.category for finding in report.findings if finding.path == linker]


def test_a_link_to_a_path_a_page_left_is_no_forward_reference_unless_the_page_is_hidden(
    moved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit that calls a link to a moved page's old path a link to a page not yet written,
    while the resolver follows it; or that drops the finding for a caller who may not see the page,
    and so tells them a hidden page exists."""
    from exomem.governance import egress

    linker = "Knowledge Base/Notes/Insights/linker.md"
    _write(moved, linker, _note("Linker", body=f"See [[{A.removesuffix('.md')}]].\n"))
    memory_refs.ReferenceIndex(moved).rebuild_all()

    assert _link_findings(moved, linker) == []

    monkeypatch.setattr(egress, "caller_restricted", lambda _root, **_kw: True)
    monkeypatch.setattr(
        egress, "restricted_release_filter", lambda _root, **_kw: lambda path: path != C
    )
    assert _link_findings(moved, linker) == ["forward_reference"]
