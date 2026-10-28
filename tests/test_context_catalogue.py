"""The context catalogue: raw declarations per entry, folded the same way however it is reached.

The workflow under test is a vault edit reaching a reader's memberships and
description coverage. The checks below name what only a real vault catches: a
topic declared through the wrong door, an incremental update that drifts from a
full build, a kill switch that still builds, and a stale parse published as
current.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from conftest import _drain_background_threads
from test_working_set_index import _seed_planning

from exomem import context_catalogue as cc
from exomem import (
    episode_memory,
    epistemic_graph,
    freshness,
    working_set_index,
    working_set_runtime,
)
from exomem.governance import principal

KB = "Knowledge Base"
INSIGHTS = f"{KB}/Notes/Insights"


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _page(vault: Path, name: str, frontmatter: str = "", body: str = "Body.\n", folder: str = INSIGHTS) -> str:
    return _write(vault, f"{folder}/{name}.md", f"---\ntype: insight\nstatus: active\n{frontmatter}---\n\n# {name}\n\n{body}")


def _seed(vault: Path) -> None:
    """One page for each way the spec says a page does, or does not, join a topic."""
    _write(
        vault,
        f"{KB}/_Schema/source-taxonomy.yaml",
        "schema_version: 1\n"
        "domains:\n"
        "  harbour: {label: Harbour, path_label: Harbour, description: Quays and moorings.}\n"
        "  fleet: {label: Fleet, path_label: Fleet}\n"
        "  old-harbour: {label: Old harbour, path_label: Old Harbour, status: deprecated, replaced_by: harbour}\n",
    )
    _write(
        vault,
        f"{KB}/_Schema/project-keys.yaml",
        "projects:\n"
        "  harbour-ops: {folder: Harbour Ops, category: domain, domain: harbour}\n"
        "  fleet-ops: {folder: Fleet Ops, category: domain, domain: [fleet, no-such-domain]}\n"
        "  plain: {folder: Plain, category: product}\n",
    )
    # A `topics` field, with an authored one-line summary.
    _page(vault, "mooring-lines", "topics: [harbour]\nsummary: How the quay lines are rated.\n")
    # Membership through a project key whose registry entry names a domain.
    _page(vault, "tide-work", "project: harbour-ops\n")
    # An authored `member_of` relation: it takes the topics its target declares.
    _page(vault, "dock-roster", body="## Relations\n\n- member_of [[Knowledge Base/Notes/Insights/mooring-lines]]\n")
    # One resolvable (a deprecated key maps to its replacement), one unresolved value.
    _page(vault, "crane-notes", "topics: [old-harbour, tug-schedules]\n")
    # A `domain` field is not a declaration; the page has no topics.
    _page(vault, "buoy-trial", "domain: ops\n")
    # A mention is not an edge.
    _page(vault, "quay-mention", body="See [[mooring-lines]] for the ratings.\n")
    # Membership does not chain: the first page joins the second, the second joins a topic.
    _page(vault, "chain-first", body="## Relations\n\n- member_of [[Knowledge Base/Notes/Insights/chain-second]]\n")
    _page(vault, "chain-second", body="## Relations\n\n- member_of [[Knowledge Base/Notes/Insights/mooring-lines]]\n")
    # Unfiled, and a project whose only domain does not resolve.
    _page(vault, "loose-end")
    _page(vault, "fleet-notes", "project: fleet-ops\n")
    # Navigation pages, whatever their case: activation keeps them from becoming anchors, so they are not notes.
    _page(vault, "index", "topics: [harbour]\n")
    _page(vault, "README", "topics: [harbour]\n")
    # An anchor, a page the walk skips, and raw material.
    _page(vault, "harbour-hub", "tags: [hub]\ntopics: [harbour]\n")
    _page(vault, "skipped", folder=f"{KB}/Templates")
    _write(vault, f"{KB}/Sources/Articles/raw-capture.md", "---\ntype: source\ntopics: [harbour]\n---\n\nRaw.\n")


def _graph(vault: Path) -> None:
    # A planning or episode write starts a background graph rebuild; a second owner would be refused.
    _drain_background_threads()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()


def _anchors(vault: Path) -> None:
    """The activation index the catalogue mirrors its anchors from; `ensure` brings it current first."""
    working_set_index.WorkingSetIndex(vault).update()


def _built(vault: Path) -> cc.ContextCatalogue:
    _graph(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    catalogue = cc.ContextCatalogue(vault)
    catalogue.rebuild()
    return catalogue


def _topics(catalogue: cc.ContextCatalogue) -> dict[str, tuple[str, ...]]:
    return {path.rsplit("/", 1)[-1].removesuffix(".md"): keys for path, keys in catalogue.memberships().topics.items()}


def test_a_page_joins_a_topic_only_by_the_declarations_the_spec_names(vault: Path) -> None:
    _seed(vault)
    catalogue = _built(vault)

    stored = {entry.path.rsplit("/", 1)[-1].removesuffix(".md"): entry for entry in catalogue.entries()}
    assert stored["crane-notes"].topics == ("old-harbour", "tug-schedules")  # raw, not resolved
    assert stored["tide-work"].projects == ("harbour-ops",)
    assert "raw-capture" not in stored and "skipped" not in stored
    assert "index" not in stored and "README" not in stored

    topics = _topics(catalogue)
    assert topics["mooring-lines"] == ("harbour",)
    assert topics["tide-work"] == ("harbour",)
    assert topics["dock-roster"] == ("harbour",)  # through its authored member_of
    assert topics["crane-notes"] == ("harbour",)  # deprecated key reads as its replacement
    assert topics["buoy-trial"] == ()  # `domain:` declares nothing
    assert topics["quay-mention"] == ()  # a mention does not spread
    assert topics["chain-second"] == ("harbour",)
    assert topics["chain-first"] == ()  # not transitive
    assert topics["loose-end"] == ()

    joined = catalogue.memberships()
    assert {path.rsplit("/", 1)[-1]: values for path, values in joined.unresolved.items()} == {
        "crane-notes.md": ("tug-schedules",)
    }
    assert dict(joined.unresolved_project_domains) == {"fleet-ops": ("no-such-domain",)}
    assert topics["fleet-notes"] == ("fleet",)


def test_an_incremental_update_equals_a_fresh_build_after_every_kind_of_edit(vault: Path, tmp_path: Path) -> None:
    _seed(vault)
    _seed_planning(vault)
    (vault / f"{KB}/Sources/Episodes").mkdir(parents=True, exist_ok=True)
    catalogue = _built(vault)
    kinds = {entry.kind for entry in catalogue.entries()}
    assert kinds == {cc.KIND_ANCHOR, cc.KIND_NOTE, cc.KIND_COLLECTION}  # a thread arrives in the last step

    def snapshot(target: cc.ContextCatalogue):
        return target.entries(), target.membership_edges(), target.pending()

    def fresh_build(label: str):
        reference = cc.ContextCatalogue(vault, path=tmp_path / f"{label}.sqlite")
        reference.rebuild()
        return snapshot(reference)

    def update(*args, **kwargs) -> None:
        _anchors(vault)
        catalogue.update(*args, **kwargs)

    def check(label: str) -> None:
        assert snapshot(catalogue) == fresh_build(label), label

    created = _page(vault, "new-berth", "topics: [fleet]\n")
    _graph(vault)
    update([created])
    check("create")

    edited = _page(vault, "loose-end", "topics: [harbour]\n", body="Now filed.\n")
    update([edited])
    check("set-topics")

    old, new = f"{INSIGHTS}/quay-mention.md", f"{INSIGHTS}/quay-notes.md"
    (vault / old).rename(vault / new)
    _graph(vault)
    update([new], [old])
    check("rename")

    (vault / f"{INSIGHTS}/buoy-trial.md").unlink()
    update(deleted=[f"{INSIGHTS}/buoy-trial.md"])
    check("delete")

    generation = catalogue.generation()
    _write(
        vault,
        f"{KB}/_Schema/project-keys.yaml",
        "projects:\n  harbour-ops: {folder: Harbour Ops, category: domain, domain: fleet}\n"
        "  fleet-ops: {folder: Fleet Ops, category: domain}\n  plain: {folder: Plain, category: product}\n",
    )
    update()
    check("project-key-domain")
    assert catalogue.generation() == generation  # nothing stored changed; the topics moved at read time
    assert _topics(catalogue)["tide-work"] == ("fleet",)

    for revision in ("First recap.", "Second recap."):
        with principal.request_scope(principal.owner_principal(surface="library")):
            episode_memory.record(
                vault, _source_schema(vault), episode="ep-" + "ab" * 16, subject="Berth planning", summary=revision,
                worked_on=[revision],
            )
        _graph(vault)
        update()
        check(f"episode: {revision}")
    threads = catalogue.entries(cc.KIND_THREAD)
    assert len(threads) == 2 and len({entry.thread_key for entry in threads}) == 1

    _write(vault, f"{KB}/Templates/skipped.md", "---\ntype: insight\n---\n\nEdited.\n")
    generation = catalogue.generation()
    update([f"{KB}/Templates/skipped.md"])
    assert catalogue.generation() == generation
    check("skipped-folder")


def _source_schema(vault: Path):
    from exomem import schema

    return schema.load_source_schema(vault)


def test_a_description_serves_no_part_of_a_page_it_links_to(vault: Path) -> None:
    """A link is removed whole before the cut, which would otherwise fall inside it and serve half a page name."""
    run_up = "a" * 230 + " see [[Harbour wiki page]] now"
    _page(vault, "authored-cut", f"summary: {run_up}\n")
    _page(vault, "lede-cut", body=run_up + "\n")
    catalogue = _built(vault)

    descriptions = {entry.path.rsplit("/", 1)[-1]: entry.description for entry in catalogue.entries()}

    assert descriptions["authored-cut.md"] == "a" * 230 + " see now"
    assert descriptions["lede-cut.md"] == "a" * 230 + " see now"


def test_the_kill_switch_builds_and_reads_nothing(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")

    state, catalogue, stale = cc.ensure(vault)

    assert (state, catalogue, stale) == ("disabled", None, False)
    assert cc.ContextCatalogue(vault).update()["disabled"] is True
    assert not cc.sidecar_path(vault).exists()


def test_a_file_that_changes_between_parse_and_commit_stays_pending(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    catalogue = _built(vault)
    target = _page(vault, "mooring-lines", "topics: [harbour]\nsummary: Before.\n")
    catalogue.update([target])
    read = cc._read_page

    def read_then_rewrite(absolute: Path, vault_root: Path):
        page = read(absolute, vault_root)
        absolute.write_text(absolute.read_text(encoding="utf-8") + "\nAppended after the parse.\n", encoding="utf-8")
        return page

    _write(vault, target, (vault / target).read_text(encoding="utf-8").replace("Before.", "After."))
    monkeypatch.setattr(cc, "_read_page", read_then_rewrite)
    catalogue.update([target])
    monkeypatch.undo()

    held = next(entry for entry in catalogue.entries() if entry.path == target)
    assert held.description == "Before." and catalogue.pending() == {target: cc.PENDING_CHANGED}

    catalogue.update([])
    settled = next(entry for entry in catalogue.entries() if entry.path == target)
    assert settled.description == "After." and catalogue.pending() == {}


def test_coverage_counts_descriptions_by_origin_and_serves_the_owner_only(vault: Path) -> None:
    _seed(vault)
    catalogue = _built(vault)

    coverage = catalogue.owner_coverage()

    assert coverage["available"] is True
    assert coverage["descriptions"][cc.KIND_NOTE][cc.ORIGIN_AUTHORED] == 1  # mooring-lines carries a summary
    assert coverage["unresolved_declarations"] == {
        "topics": 1, "project_domains": 1, "carrier_topics": 0, "home_topics": 0,
    }
    guest = principal.RequestPrincipal("principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:synthetic")
    with principal.request_scope(guest):
        assert catalogue.owner_coverage() == {"available": False, "reason": "audience_restricted"}


def test_ensure_builds_inline_when_unmanaged_and_in_the_background_when_managed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    monkeypatch.setattr(cc, "_managed", lambda: True)

    state, catalogue, _ = cc.ensure(vault)
    assert (state, catalogue) == ("index_warming", None)  # the request thread did not walk the vault
    working_set_runtime.join_builds(vault)
    state, catalogue, stale = cc.ensure(vault)
    assert state == cc.READY and catalogue is not None and catalogue.built()

    monkeypatch.setattr(cc, "_managed", lambda: False)
    cc.ContextCatalogue(vault).reset()
    state, catalogue, stale = cc.ensure(vault)
    assert state == cc.READY and catalogue is not None and catalogue.built() and not stale


def test_stopping_the_runtime_abandons_a_running_catalogue_pass(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A service that is shutting down must not wait for, or let through, a half-finished fold."""
    _seed(vault)
    _anchors(vault)
    monkeypatch.setattr(cc, "_managed", lambda: True)
    stop = threading.Event()
    read = cc._read_page

    def read_then_stop(absolute: Path, vault_root: Path):
        stop.set()  # the runtime stops while the first page is being folded
        return read(absolute, vault_root)

    monkeypatch.setattr(cc, "_read_page", read_then_stop)
    working_set_runtime.bind_shutdown(vault, stop)
    try:
        assert cc.ensure(vault)[0] == "index_warming"
        working_set_runtime.join_builds(vault)
    finally:
        working_set_runtime.reset_caches_for_tests()

    assert not cc.ContextCatalogue(vault).built()


def test_a_warm_managed_ensure_folds_the_pages_the_freshness_registry_saw(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    _graph(vault)
    pages = sorted((vault / KB).rglob("*.md"))
    freshness.seed(vault, "kb", [(str(page), freshness.stat_signature(page)) for page in pages])
    monkeypatch.setattr(cc, "_managed", lambda: True)
    cc.ensure(vault)
    working_set_runtime.join_builds(vault)

    target = _page(vault, "mooring-lines", "topics: [harbour]\nsummary: Rewritten while the service ran.\n")
    freshness.on_files_changed(vault, [vault / target])
    state, catalogue, stale = cc.ensure(vault)

    assert state == cc.READY and catalogue is not None and not stale
    held = next(entry for entry in catalogue.entries() if entry.path == target)
    assert held.description == "Rewritten while the service ran."
