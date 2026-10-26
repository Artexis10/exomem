"""An imported collection reaches the agent as batches it can group without reading each Source.

Findings carry the title, a bounded excerpt of the captured text, the grouping metadata
the import recorded and the derivatives. The compilation scaffold carries a bounded
excerpt of each Source, within one total bound.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, corpus_aware, find, source_coverage

pytestmark = pytest.mark.usefixtures("obligations_only")

DOCUMENTS = 20


@pytest.fixture
def imported(obligations_only: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    """Twenty documents in two folders, imported as one collection through adoption."""
    vault = obligations_only
    for index in range(DOCUMENTS):
        folder = vault / "Old Notes" / ("Reading" if index % 2 else "Work")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"doc-{index}.md").write_text(
            f"Text of document {index}. " + "Words follow. " * 200,
            encoding="utf-8",
        )
    find.clear_cache()
    run = commands.op_adoption_studio(vault, action="start", path="Old Notes")["run_id"]
    commands.op_adoption_studio(vault, action="select", run_id=run, include=["Old Notes"])
    plan = commands.op_adoption_studio(vault, action="plan", run_id=run)["plan"]["plan_id"]
    applied = commands.op_adoption_studio(vault, action="apply", run_id=run, plan_id=plan)
    written = applied["apply_result"]["written"]
    assert len(written) == DOCUMENTS
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *a, **k: [])
    return vault, written


def test_a_batch_of_imported_sources_arrives_ready_to_group(
    imported: tuple[Path, list[str]],
) -> None:
    vault, _written = imported

    review = commands.op_review_memory(vault, mode="unprocessed-sources", limit=10)

    assert len(review["items"]) == 10
    for item in review["items"]:
        meta = item["reasons"][0]["meta"]
        number = meta["excerpt"].split()[3].rstrip(".")
        assert meta["excerpt"].startswith(f"Text of document {number}.")
        assert "Copied from" not in meta["excerpt"], (
            "the import notice says nothing about the Source"
        )
        assert len(meta["excerpt"]) <= source_coverage.EXCERPT_CHARS
        assert meta["import_collection"] == "Old Notes"
        assert meta["collection_folder"] == ("Reading" if int(number) % 2 else "Work")
        assert meta["source_kind"] == "unclassified"
        assert meta["captured"]
        assert "derivatives" not in meta, "an import with no note yet has no derivatives to list"


def test_a_compilation_scaffold_carries_every_source_within_one_total_bound(
    imported: tuple[Path, list[str]],
) -> None:
    vault, written = imported

    scaffold = commands.op_review_memory(vault, mode="compilation", sources=written)

    excerpts = scaffold["source_excerpts"]
    assert [entry["path"] for entry in excerpts] == scaffold["suggested_sources"]
    assert all(entry["excerpt"] for entry in excerpts), "every Source keeps an excerpt"
    assert (
        sum(len(entry["excerpt"]) for entry in excerpts) <= source_coverage.SCAFFOLD_EXCERPT_CHARS
    )
    assert all(len(entry["excerpt"]) <= source_coverage.EXCERPT_CHARS for entry in excerpts)
    assert {entry["import_collection"] for entry in excerpts} == {"Old Notes"}


def test_a_scaffold_over_more_sources_than_its_bound_allows_says_which_excerpts_it_dropped(
    imported: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, written = imported
    monkeypatch.setattr(source_coverage, "SCAFFOLD_EXCERPT_CHARS", 20 * 50)

    scaffold = commands.op_review_memory(vault, mode="compilation", sources=written)

    excerpts = scaffold["source_excerpts"]
    assert len(excerpts) == DOCUMENTS, "a Source keeps its title and metadata without an excerpt"
    assert "excerpt" in excerpts[0] and "excerpt" not in excerpts[-1]
    assert sum(len(entry.get("excerpt", "")) for entry in excerpts) <= 20 * 50
    assert any("excerpt omitted for 4 source(s)" in warning for warning in scaffold["warnings"]), (
        scaffold["warnings"]
    )


def test_a_compilation_scaffold_leaves_out_the_excerpt_of_a_source_the_caller_may_not_read(
    tmp_path: Path,
) -> None:
    from exomem import find as find_module
    from exomem.governance import egress, membership, policy
    from exomem.governance.principal import RequestPrincipal, request_scope

    vault = tmp_path / "vault"
    sources = vault / "Knowledge Base" / "Sources"
    for folder, text in (
        ("Open", "The harvest plan is public."),
        ("Withheld", "The salary table is private."),
    ):
        (sources / folder).mkdir(parents=True)
        (sources / folder / "page.md").write_text(
            f"---\ntype: source\nsource_type: note\n---\n# {folder} page\n\n## Capture\n\n{text}\n",
            encoding="utf-8",
        )
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir(parents=True)
    scope_id, rule_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0"
    (governance / "scopes" / "withheld.yaml").write_text(
        f'governance_version: 1\nid: {scope_id}\nname: Withheld\npaths: ["Sources/Withheld/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "withheld.yaml").write_text(
        f'governance_version: 1\nid: {rule_id}\nscope_ids: ["{scope_id}"]\n'
        f"audience: external\nceiling: {egress.LEVEL_NONE}\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp", resolved=True)):
        scaffold = commands.op_propose_compilation(
            vault, sources=["Sources/Open/page", "Sources/Withheld/page"]
        )

    assert [entry["path"] for entry in scaffold["source_excerpts"]] == [
        "Knowledge Base/Sources/Open/page"
    ]
    assert "salary" not in str(scaffold["source_excerpts"])
    (sources / "Withheld" / "page.md").unlink()
    find_module.clear_cache()
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp", resolved=True)):
        absent = commands.op_propose_compilation(
            vault, sources=["Sources/Open/page", "Sources/Withheld/page"]
        )
    assert scaffold == absent
    assert scaffold["semantic_compilation"]["preparation_route"]["args"]["sources"] == [
        "Knowledge Base/Sources/Open/page.md",
    ]
