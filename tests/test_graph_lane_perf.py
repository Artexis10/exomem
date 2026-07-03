"""Graph-lane performance regression (feat/graph-lane-perf).

The graph lane surfaces 1-hop wikilink neighbours of strong candidates, which
means resolving every `[[link]]` on the seed pages through a `WikilinkResolver`
— an in-memory index of the whole vault's paths, stems, and frontmatter titles.

Root cause this file guards: that resolver used to be keyed purely on the vault
freshness digest, so ANY `.md` change moved the digest and forced a full-vault
rebuild (read + YAML-parse EVERY note) on the next graph query. On the owner's
~1700-note, actively-synced vault that was ~14s per query — 82% of a `find`'s
total time — while every other lane stayed warm (measured via
`find(include_timings=True)`).

The fix makes the resolver an event-maintained index alongside the freshness
and inbound registries: built once, then incrementally patched by the file
watcher on each change (`find.on_resolver_files_changed` ->
`WikilinkResolver.on_files_changed`). A single edit now patches a handful of
map entries instead of re-reading the vault, so the graph lane stays in the
millisecond range across edits — and the patched maps are byte-for-byte
identical to a fresh rebuild, so the 1-hop recall is unchanged.

These tests generate a synthetic, densely-wikilinked vault (~1000 notes) so a
regression that drops the event maintenance rebuilds the whole resolver and
blows the sub-second graph-stage budget, failing loudly.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import freshness
from exomem import vault as vault_module
from exomem.vault import WikilinkResolver, normalize_wikilink, walk_vault_md

# Large enough that a full resolver rebuild (read + YAML-parse every note)
# clearly overshoots the 1s graph-stage budget by several-fold, so the guard
# is robust rather than marginal, but small enough to keep the suite quick.
N_NOTES = 1000
_FOLDERS = (
    "Notes/Insights", "Notes/Patterns", "Notes/Failures",
    "Entities/Concepts", "Entities/People", "Sources", "Experiments",
)


def _gen_dense_vault(root: Path, n: int, links_per_note: int = 25, seed: int = 7) -> list[str]:
    """Write `n` densely-cross-linked KB notes; return their vault-relative rels."""
    rng = random.Random(seed)
    kb = root / "Knowledge Base"
    for f in _FOLDERS:
        (kb / f).mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    names: list[str] = []
    for i in range(n):
        folder = _FOLDERS[i % len(_FOLDERS)]
        name = f"note-{i:05d}-topic-{rng.randint(0, 99999)}"
        names.append(name)
        rels.append(f"Knowledge Base/{folder}/{name}.md")
    for i, rel in enumerate(rels):
        targets = rng.sample(range(n), min(links_per_note, n))
        link_lines = []
        for t in targets:
            if t % 3 == 0:  # mix full-path and bare-stem link forms
                link_lines.append(f"- see [[{rels[t][:-3]}]] for context")
            else:
                link_lines.append(f"- ref [[{names[t]}]] inline")
        (root / rel).write_text(
            "---\n"
            "type: insight\n"
            f"title: Note {i} about topic {names[i]}\n"
            "tags: [synthetic, graph, dense]\n"
            f"updated: 2026-02-{(i % 28) + 1:02d}\n"
            "---\n\n"
            f"# Note {i}\n\n"
            "Prose paragraph so the note is realistically sized and body text "
            "gives BM25 something to rank on. topic topic topic.\n\n"
            "## Related\n\n" + "\n".join(link_lines) + "\n",
            encoding="utf-8",
        )
    return rels


def _seed_freshness_live(vault: Path) -> None:
    """Seed the event-maintained freshness registry the way the watcher does,
    so `freshness.triple()` is live and `on_files_changed` can patch it."""
    freshness.seed(
        vault, "vault",
        ((str(p), p.stat().st_mtime_ns) for p in walk_vault_md(vault)),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault, "kb",
        ((str(p), p.stat().st_mtime_ns) for p in find_module._walk_md(kb)),
    )


def _resolver_maps(r: WikilinkResolver) -> tuple:
    """Order-independent snapshot of the resolver's resolution maps. Order
    within a stem/title bucket is irrelevant to resolution (single match wins;
    multi-match reports ambiguity regardless of order), so parity is by set."""
    return (
        set(r.full_paths),
        set(r.kb_stripped),
        {k: set(v) for k, v in r.stems.items()},
        {k: set(v) for k, v in r.titles.items()},
    )


def _publish_change(vault: Path, changed_rels: list[str], deleted_rels: list[str]) -> None:
    """Drive the same registry-update path the file watcher runs for a batch."""
    freshness.on_files_changed(
        vault,
        changed=[vault / r for r in changed_rels],
        deleted=[vault / r for r in deleted_rels],
    )
    vault_module.on_inbound_files_changed(vault, changed_rels, deleted_rels)
    find_module.on_resolver_files_changed(vault, changed_rels, deleted_rels)


@pytest.fixture
def dense_vault(tmp_path: Path) -> tuple[Path, list[str]]:
    find_module.clear_cache()  # no bleed-through from a prior test's caches
    freshness.clear()
    vault = tmp_path / "vault"
    rels = _gen_dense_vault(vault, N_NOTES)
    _seed_freshness_live(vault)
    yield vault, rels
    find_module.clear_cache()
    freshness.clear()


def test_resolver_not_rebuilt_and_parity_after_edit(dense_vault) -> None:
    """After an edit batch is published through the watcher path, the cached
    resolver is PATCHED IN PLACE (same instance, no full rebuild) and its maps
    match a fresh full rebuild exactly — recall is preserved, cost is not."""
    vault, rels = dense_vault
    snap = find_module.FreshnessSnapshot(vault)

    # Warm the resolver once (this is what boot warm-up / the first query does).
    r_before = find_module._get_query_resolver(vault, freshness=snap.vault())

    # An edit batch touching every map: retitle, delete, create, rename.
    retitled = rels[5]
    (vault / retitled).write_text(
        "---\ntype: insight\ntitle: A Distinctive Retitled Heading ZZZ\n---\n"
        f"\n# x\n\n- [[{rels[10][:-3]}]]\n",
        encoding="utf-8",
    )
    deleted = rels[7]
    (vault / deleted).unlink()
    created = "Knowledge Base/Notes/Insights/freshly-created-note-qwx.md"
    (vault / created).write_text(
        "---\ntitle: Freshly Created Note Title\n---\n\n# new\n", encoding="utf-8"
    )
    old_ren = rels[9]
    new_ren = "Knowledge Base/Notes/Patterns/renamed-note-abc.md"
    (vault / old_ren).rename(vault / new_ren)

    _publish_change(vault, [retitled, created, new_ren], [deleted, old_ren])

    # Same instance => patched in place, NOT rebuilt from a full-vault re-read.
    r_after = find_module._get_query_resolver(
        vault, freshness=find_module.FreshnessSnapshot(vault).vault()
    )
    assert r_after is r_before, "resolver was rebuilt instead of incrementally patched"

    # Byte-for-byte parity with a fresh rebuild over the new on-disk state.
    fresh = WikilinkResolver(vault)
    assert _resolver_maps(r_after) == _resolver_maps(fresh), "patched maps drifted from a rebuild"

    # Functional recall spot-checks: retitle + rename resolve, delete does not.
    c1, w1 = normalize_wikilink("A Distinctive Retitled Heading ZZZ", vault, resolver=r_after)
    assert w1 is None and c1 == retitled[:-3]
    _, w2 = normalize_wikilink(new_ren[:-3], vault, resolver=r_after)
    assert w2 is None
    _, w3 = normalize_wikilink(deleted[:-3], vault, resolver=r_after)
    assert w3 is not None  # deleted target no longer resolves


def test_graph_stage_stays_under_budget_after_edit(dense_vault) -> None:
    """End-to-end: a `find` whose graph lane runs after a vault edit keeps the
    graph stage well under 1s, because the resolver stayed warm."""
    vault, rels = dense_vault

    # Warm everything a first query / boot warm-up would build: bm25 corpus +
    # pages via a real query, and the resolver explicitly (so the test never
    # hinges on the graph seed-gate firing for this particular warm query).
    find_module.find(vault, query="topic", limit=10)
    find_module._get_query_resolver(
        vault, freshness=find_module.FreshnessSnapshot(vault).vault()
    )

    # Edit a note and publish the change through the watcher path.
    edited = rels[3]
    (vault / edited).write_text(
        "---\ntype: insight\ntitle: Note 3 edited about topic\n---\n"
        f"\n# edited\n\ntopic topic\n\n- [[{rels[20][:-3]}]]\n",
        encoding="utf-8",
    )
    _publish_change(vault, [edited], [])

    timings = find_module.FindTimings()
    hits = find_module.find(vault, query="topic", limit=10, graph=True, timings=timings)

    graph_stage = timings.stages.get("graph", {})
    assert "ms" in graph_stage, f"graph lane did not run: {timings.stages}"
    assert graph_stage["ms"] < 1000.0, (
        f"graph stage took {graph_stage['ms']}ms (>=1s): the resolver was rebuilt "
        f"instead of staying warm. full timings: {timings.as_dict()}"
    )
    assert hits, "expected the dense vault to produce hits"


def test_kill_switch_falls_back_to_rebuild(dense_vault, monkeypatch) -> None:
    """With the event-index kill switch set, the resolver patch is a no-op and
    the getter falls back to a digest-keyed rebuild (the rollback contract)."""
    vault, rels = dense_vault
    snap = find_module.FreshnessSnapshot(vault)
    r_before = find_module._get_query_resolver(vault, freshness=snap.vault())

    monkeypatch.setenv("EXOMEM_DISABLE_EVENT_INDEXES", "1")
    (vault / rels[1]).write_text(
        "---\ntitle: kill switch edit\n---\n\n# k\n", encoding="utf-8"
    )
    # Patch is a no-op under the kill switch...
    find_module.on_resolver_files_changed(vault, [rels[1]], [])
    # ...so a fresh freshness key forces a rebuild (new instance).
    r_after = find_module._get_query_resolver(
        vault, freshness=find_module.FreshnessSnapshot(vault).vault()
    )
    assert r_after is not r_before
