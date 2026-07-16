"""Unit tests for the reasoning-ready context pack (`find(pack=true)`).

Torch-free: builds its own tiny inter-linked vault per test (so it never perturbs
the shared fixture vault), and exercises `context_pack.assemble_pack` directly. The
embedding-dependent `tension` path is tested by monkeypatching
`corpus_aware._best_cosine_per_file` with injected cosines — no model load.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import (
    context_pack,
    corpus_aware,
    epistemic_graph,
    semantic_blocks,
    semantic_index,
)
from exomem import find as find_module
from exomem.find import Hit
from exomem.find_types import SemanticUnitHit

# --- a small cluster: Alpha + Beta packed; Hub (co-cited), Charlie, Delta neighbours ---

ALPHA = """\
---
type: insight
---
# Alpha Insight

Alpha is the lede paragraph that states the core claim plainly.

## Summary
- Alpha summarizes the key finding succinctly.

## Problem
The problem Alpha addresses is stated right here.

## Detail

```python
# not-a-heading inside a fenced code block
x = "[[Knowledge Base/Notes/NotALink]]"
```

## Connections
- [[Knowledge Base/Notes/Charlie]] — related leaf
- [[Knowledge Base/Notes/Hub]] — the hub
"""

BETA = """\
---
type: pattern
---
# Beta Pattern

Beta lede paragraph describing the pattern.

## Pattern
Beta's pattern body, linking to [[Knowledge Base/Notes/Hub]].
"""

CHARLIE = """\
---
type: insight
---
# Charlie

Charlie lede sentence. A second sentence that should not appear in a one-liner.
"""

HUB = """\
---
type: insight
---
# Hub

Hub lede paragraph describing the central hub note.
"""

DELTA = """\
---
type: note
---
# Delta

Delta references [[Knowledge Base/Notes/Alpha]] from outside the packed set.
"""

OLD = """\
---
type: insight
status: superseded
superseded_by: "[[Knowledge Base/Notes/NewView]]"
---
# Old View

The old lede that has since been replaced.
"""

NEW = """\
---
type: insight
---
# New View

The current lede that supersedes the old one.
"""

UNIT_CONTEXT = """\
---
type: insight
exomem_id: 11111111-1111-4111-8111-111111111111
status: superseded
updated: 2026-07-16
superseded_by: "[[Knowledge Base/Notes/NewView]]"
sources:
  - "[[Knowledge Base/Sources/Cache benchmark]]"
---
# Cache Policy

## Observations

- [config] Cache TTL is thirty seconds #runtime (edge cache) ^cache-ttl
- [rule] Evict least-recently-used entries first #runtime ^evict-lru

## Decision
- id: choose-lru
- category: architecture
- relations: evidenced_by: [[Knowledge Base/Sources/Cache benchmark]]

Use LRU eviction for the bounded edge cache.
"""


def _write(vault: Path, rel: str, body: str) -> None:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _hit(rel: str) -> Hit:
    return Hit(path=rel, type=None, scope=None, title="", updated="", excerpt="")


def _unit_hit(rel: str, unit_ref: str) -> SemanticUnitHit:
    return SemanticUnitHit(
        unit_ref=unit_ref,
        form="compact",
        category_raw="config",
        category_key="config",
        category="config",
        kind="observation",
        content="selected",
        excerpt="selected",
        tags=[],
        context=None,
        source_anchor=None,
        source_span={},
        source_hash="hash",
        parent_path=rel,
        parent_ref=None,
        parent_title="",
        parent_type="insight",
        parent_status="active",
        parent_updated="",
    )


ALPHA_P = "Knowledge Base/Notes/Alpha.md"
BETA_P = "Knowledge Base/Notes/Beta.md"
CHARLIE_P = "Knowledge Base/Notes/Charlie.md"
HUB_P = "Knowledge Base/Notes/Hub.md"
DELTA_P = "Knowledge Base/Notes/Delta.md"
OLD_P = "Knowledge Base/Notes/Old.md"
NEW_P = "Knowledge Base/Notes/NewView.md"
UNIT_CONTEXT_P = "Knowledge Base/Notes/CachePolicy.md"


@pytest.fixture
def cluster(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    _write(vault, ALPHA_P, ALPHA)
    _write(vault, BETA_P, BETA)
    _write(vault, CHARLIE_P, CHARLIE)
    _write(vault, HUB_P, HUB)
    _write(vault, DELTA_P, DELTA)
    _write(vault, OLD_P, OLD)
    _write(vault, NEW_P, NEW)
    _write(vault, UNIT_CONTEXT_P, UNIT_CONTEXT)
    find_module.clear_cache()
    return vault


# ----------------------------- claims -----------------------------

def test_claims_are_structural_lede_sections_outline(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P), _hit(BETA_P)])
    claims = pack["claims"][ALPHA_P]

    assert claims["type"] == "insight"
    # lede is the first content paragraph, NOT the H1 title.
    assert claims["lede"].startswith("Alpha is the lede paragraph")
    assert "Alpha Insight" not in claims["lede"]
    # recognized headline sections are captured with their heading label.
    joined = " | ".join(claims["sections"])
    assert "Summary:" in joined and "Alpha summarizes" in joined
    assert "Problem:" in joined and "problem Alpha addresses" in joined
    # outline is the ## skeleton, in order.
    assert claims["outline"] == ["Summary", "Problem", "Detail", "Connections"]


def test_claims_ignore_fenced_code(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P)])
    claims = pack["claims"][ALPHA_P]
    # The `# not-a-heading` inside the code fence is not a heading.
    assert "not-a-heading" not in " ".join(claims["outline"])
    assert all("not-a-heading" not in s for s in claims["sections"])
    # The `[[...NotALink]]` inside the fence is not an outbound neighbour.
    assert all("NotALink" not in n["path"] for n in pack["neighborhood"])


def test_claim_lede_capped(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P)], )
    # Force a tiny cap and confirm an ellipsis marks the truncation (not silent).
    pack_small = context_pack.assemble_pack(cluster, [_hit(ALPHA_P)], max_hits=1)
    # default claim chars is generous; explicitly cap via env-independent kwarg path:
    capped = context_pack._extract_claims(
        find_module._CACHE.get(cluster / ALPHA_P, cluster), claim_chars=20
    )
    assert len(capped["lede"]) <= 21  # 20 chars + ellipsis
    assert capped["lede"].endswith("…")
    assert pack["claims"][ALPHA_P]["lede"]  # sanity: default not truncated
    assert pack_small["packed_paths"] == [ALPHA_P]


# -------------------------- neighbourhood --------------------------

def test_neighbourhood_co_citation_order_and_exclusion(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P), _hit(BETA_P)])
    neigh = pack["neighborhood"]
    paths = [n["path"] for n in neigh]

    # Hub is linked by BOTH Alpha and Beta → co-citation 2 → ranks first.
    assert paths[0] == HUB_P
    hub = neigh[0]
    assert set(hub["referenced_by"]) == {ALPHA_P, BETA_P}
    assert hub["direction"] == "out"
    # Charlie and Delta are each linked by one packed note.
    assert CHARLIE_P in paths and DELTA_P in paths
    # Packed notes never appear in their own neighbourhood.
    assert ALPHA_P not in paths and BETA_P not in paths
    # Inbound link (Delta → Alpha) is tagged direction "in".
    delta = next(n for n in neigh if n["path"] == DELTA_P)
    assert delta["direction"] == "in"
    assert delta["referenced_by"] == [ALPHA_P]
    # one-sentence lede only.
    charlie = next(n for n in neigh if n["path"] == CHARLIE_P)
    assert "second sentence" not in charlie["lede"]


def test_neighbourhood_cap_reports_truncation(cluster: Path) -> None:
    pack = context_pack.assemble_pack(
        cluster, [_hit(ALPHA_P), _hit(BETA_P)], max_neighbors=1
    )
    assert len(pack["neighborhood"]) == 1
    assert pack["neighborhood"][0]["path"] == HUB_P
    assert any("neighborhood" in t for t in pack["truncation"])


# ---------------------- contradictions / supersession ----------------------

def test_supersession_edge_from_frontmatter(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(OLD_P), _hit(NEW_P)])
    edges = pack["contradictions"]["superseded"]
    assert {"from": OLD_P, "to": NEW_P, "kind": "supersession"} in edges
    # supersession needs no embeddings.
    assert pack["embeddings_available"] is False


def test_embeddings_off_degrades_gracefully(cluster: Path) -> None:
    # Default suite env has embeddings disabled (conftest autouse).
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P), _hit(BETA_P)])
    assert pack["embeddings_available"] is False
    assert pack["contradictions"]["tension"] == []
    # The non-embedding parts are still populated.
    assert pack["claims"] and pack["neighborhood"]


def test_tension_pairs_only_in_band(cluster: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_bcpf(vault_root, *, title, body, k: int = 15):
        if title.startswith("Alpha"):
            # Beta in band [0.82,0.90); Charlie above (a near-dup, excluded).
            return {BETA_P: 0.85, CHARLIE_P: 0.95}
        if title.startswith("Beta"):
            return {ALPHA_P: 0.85}
        return {}

    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", fake_bcpf)
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P), _hit(BETA_P)])

    tension = pack["contradictions"]["tension"]
    assert pack["embeddings_available"] is True
    assert len(tension) == 1
    pair = tension[0]
    assert {pair["a"], pair["b"]} == {ALPHA_P, BETA_P}
    assert pair["cosine"] == 0.85
    assert "polarity" in pair["note"]


# ------------------------------ bounds / determinism ------------------------------

def test_packed_paths_bounded_by_max_hits(cluster: Path) -> None:
    hits = [_hit(ALPHA_P), _hit(BETA_P), _hit(HUB_P)]
    pack = context_pack.assemble_pack(cluster, hits, max_hits=2)
    assert pack["packed_paths"] == [ALPHA_P, BETA_P]
    assert any("hits" in t for t in pack["truncation"])


def test_deterministic_on_rerun(cluster: Path) -> None:
    hits = [_hit(ALPHA_P), _hit(BETA_P)]
    assert context_pack.assemble_pack(cluster, hits) == context_pack.assemble_pack(
        cluster, hits
    )


def test_empty_hits_yield_empty_pack(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [])
    assert pack["packed_paths"] == []
    assert pack["claims"] == {}
    assert pack["neighborhood"] == []
    assert pack["contradictions"] == {"superseded": [], "tension": []}


def test_duplicate_hits_are_deduped(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(OLD_P), _hit(OLD_P), _hit(NEW_P)])
    assert pack["packed_paths"] == [OLD_P, NEW_P]
    # the supersession edge is not double-counted.
    assert pack["contradictions"]["superseded"] == [
        {"from": OLD_P, "to": NEW_P, "kind": "supersession"}
    ]


def test_missing_hit_file_is_reported_not_silent(cluster: Path) -> None:
    gone = "Knowledge Base/Notes/DoesNotExist.md"
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P), _hit(gone)])
    assert pack["packed_paths"] == [ALPHA_P]
    assert any("unreadable or missing" in t for t in pack["truncation"])


# ------------------------------ integration via op_find ------------------------------

def test_op_find_pack_false_returns_bare_list(vault: Path) -> None:
    from exomem import commands

    result = commands.op_find(vault, query="insulin", pack=False)
    assert isinstance(result, list)


def test_op_find_pack_true_returns_hits_and_pack(vault: Path) -> None:
    from exomem import commands

    result = commands.op_find(vault, query="insulin", pack=True)
    assert isinstance(result, dict)
    assert set(result) == {"hits", "pack"}
    assert isinstance(result["hits"], list)
    pack = result["pack"]
    assert set(pack) >= {
        "packed_paths",
        "claims",
        "neighborhood",
        "contradictions",
        "embeddings_available",
        "truncation",
    }

def test_pack_without_graph_enrichment_preserves_shape(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P), _hit(BETA_P)])

    assert "graph" not in pack
    assert set(pack) == {
        "packed_paths",
        "claims",
        "semantic_units",
        "semantic_blocks",
        "neighborhood",
        "contradictions",
        "embeddings_available",
        "truncation",
    }


def test_pack_parses_each_readable_page_once_and_preserves_exact_json(
    cluster: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [_hit(ALPHA_P), _hit(BETA_P)]
    parse_calls = 0
    original_parse = context_pack.semantic_units.parse_semantic_units

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(
        context_pack.semantic_units, "parse_semantic_units", counted_parse
    )

    actual = context_pack.assemble_pack(cluster, hits)
    legacy_block_map = {}
    for rel_path in (ALPHA_P, BETA_P):
        page = find_module._CACHE.get(cluster / rel_path, cluster)
        blocks = semantic_blocks.parse_semantic_blocks(
            page.body, validate=False
        ).blocks
        if blocks:
            legacy_block_map[rel_path] = [block.to_dict() for block in blocks]
    expected_legacy = {
        "packed_paths": actual["packed_paths"],
        "claims": actual["claims"],
        "semantic_blocks": legacy_block_map,
        "neighborhood": actual["neighborhood"],
        "contradictions": actual["contradictions"],
        "embeddings_available": actual["embeddings_available"],
        "truncation": actual["truncation"],
    }

    assert parse_calls == 2
    legacy_actual = {key: value for key, value in actual.items() if key != "semantic_units"}
    assert json.dumps(legacy_actual, ensure_ascii=False, sort_keys=False) == json.dumps(
        expected_legacy, ensure_ascii=False, sort_keys=False
    )
    assert set(actual["semantic_units"]) == {BETA_P}
    assert actual["semantic_units"][BETA_P]["units"][0]["kind"] == "pattern"


def test_pack_includes_bounded_citable_compact_and_rich_semantic_units(
    cluster: Path,
) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(UNIT_CONTEXT_P)])

    entry = pack["semantic_units"][UNIT_CONTEXT_P]
    units = entry["units"]
    assert [unit["form"] for unit in units] == ["compact", "compact", "rich"]

    compact = units[0]
    assert compact["category"] == "config"
    assert compact["kind"] == "observation"
    assert compact["excerpt"] == "Cache TTL is thirty seconds"
    assert compact["source_anchor"] == "cache-ttl"
    assert compact["unit_ref"].endswith("#cache-ttl")
    assert "text" not in compact["source_span"]
    assert entry["parent"] == {
        "path": UNIT_CONTEXT_P,
        "ref": "exomem://memory/11111111-1111-4111-8111-111111111111",
        "title": "Cache Policy",
        "type": "insight",
        "status": "superseded",
        "updated": "2026-07-16",
        "supersedes": [],
        "superseded_by": ["[[Knowledge Base/Notes/NewView]]"],
        "sources": ["[[Knowledge Base/Sources/Cache benchmark]]"],
        "evidence": [],
    }

    rich = units[-1]
    assert rich["kind"] == "decision"
    assert rich["category"] == "architecture"
    assert rich["source_anchor"] == "choose-lru"
    assert rich["relations"] == [
        {
            "kind": "evidenced_by",
            "target": "[[Knowledge Base/Sources/Cache benchmark]]",
            "line": rich["source_span"]["start_line"] + 3,
            "origin": "authored_rich_unit",
            "direction": "outbound",
            "source_anchor": "choose-lru",
        }
    ]

    expected_legacy = [
        block.to_dict()
        for block in semantic_blocks.parse_semantic_blocks(
            find_module._CACHE.get(cluster / UNIT_CONTEXT_P, cluster).body,
            validate=False,
        ).blocks
    ]
    assert pack["semantic_blocks"][UNIT_CONTEXT_P] == expected_legacy


def test_semantic_unit_caps_are_explicit_and_bound_legacy_projection(
    cluster: Path,
) -> None:
    pack = context_pack.assemble_pack(
        cluster,
        [_hit(UNIT_CONTEXT_P)],
        max_units_per_page=1,
        max_units=1,
    )

    assert len(pack["semantic_units"][UNIT_CONTEXT_P]["units"]) == 1
    assert pack["semantic_blocks"] == {}
    assert any(
        "2 semantic units omitted" in item
        for item in pack["truncation"]
    )


def test_semantic_unit_character_cap_is_hard_and_legacy_body_uses_same_excerpt(
    cluster: Path,
) -> None:
    bounded = context_pack.assemble_pack(
        cluster,
        [_hit(UNIT_CONTEXT_P)],
        unit_chars=12,
    )
    rich = bounded["semantic_units"][UNIT_CONTEXT_P]["units"][-1]
    assert rich["excerpt"].endswith("…")
    assert bounded["semantic_blocks"][UNIT_CONTEXT_P][0]["body"] == rich["excerpt"]

    total_cap = 2_500
    total_bounded = context_pack.assemble_pack(
        cluster,
        [_hit(UNIT_CONTEXT_P)],
        max_unit_total_chars=total_cap,
    )
    semantic_payload = json.dumps(
        {
            "semantic_units": total_bounded["semantic_units"],
            "semantic_blocks": total_bounded["semantic_blocks"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert total_bounded["semantic_units"]
    assert len(semantic_payload) <= total_cap

    exhausted = context_pack.assemble_pack(
        cluster,
        [_hit(UNIT_CONTEXT_P)],
        max_unit_total_chars=0,
    )
    assert exhausted["semantic_units"] == {}
    assert exhausted["semantic_blocks"] == {}
    assert any("character cap" in item for item in exhausted["truncation"])


def test_parent_grouping_precedes_hit_cap_and_stale_selection_is_explicit(
    cluster: Path,
) -> None:
    stale = "exomem://memory/11111111-1111-4111-8111-111111111111#gone"
    pack = context_pack.assemble_pack(
        cluster,
        [_unit_hit(UNIT_CONTEXT_P, stale), _hit(UNIT_CONTEXT_P), _hit(BETA_P)],
        max_hits=2,
    )

    assert pack["packed_paths"] == [UNIT_CONTEXT_P, BETA_P]
    assert any("selected semantic unit(s) stale or missing" in item for item in pack["truncation"])


def test_pack_wide_selected_units_precede_fillers_from_earlier_parents(
    cluster: Path,
) -> None:
    beta_ref = semantic_index.build_parent_index_state(
        cluster, cluster / BETA_P
    ).document.units[0].unit_ref
    assert beta_ref is not None

    pack = context_pack.assemble_pack(
        cluster,
        [_hit(UNIT_CONTEXT_P), _unit_hit(BETA_P, beta_ref)],
        max_units=1,
    )

    assert set(pack["semantic_units"]) == {BETA_P}
    assert pack["semantic_units"][BETA_P]["units"][0]["unit_ref"] == beta_ref
    assert not any(
        "selected semantic unit(s) omitted" in item
        for item in pack["truncation"]
    )


def test_deleted_selected_unit_is_reported_when_parent_has_no_units(
    cluster: Path,
) -> None:
    pack = context_pack.assemble_pack(
        cluster,
        [_unit_hit(ALPHA_P, "path:Knowledge Base/Notes/Alpha.md#gone")],
    )

    assert pack["semantic_units"] == {}
    assert any(
        "1 selected semantic unit(s) stale or missing" in item
        for item in pack["truncation"]
    )


def test_unit_level_find_can_return_deep_context_seeded_by_selected_unit(
    cluster: Path,
) -> None:
    from exomem import commands

    result = commands.op_find(
        cluster,
        query="bounded edge cache",
        kinds=["decision"],
        result_level="unit",
        pack=True,
        mode="keyword",
        graph=False,
        scope="kb-only",
        prefer_active=False,
    )

    selected = result["hits"][0]
    packed = result["pack"]["semantic_units"][UNIT_CONTEXT_P]["units"]
    assert selected["result_type"] == "semantic_unit"
    assert packed[0]["unit_ref"] == selected["unit_ref"]
    assert packed[0]["kind"] == "decision"
    assert packed[0]["relations"][0]["kind"] == "evidenced_by"


def test_deep_ask_memory_supports_unit_level_context(cluster: Path) -> None:
    from exomem import commands

    result = commands.op_ask_memory(
        cluster,
        query="bounded edge cache",
        kinds=["decision"],
        result_level="unit",
        deep=True,
        mode="keyword",
        graph=False,
        scope="kb-only",
        prefer_active=False,
    )

    assert result["hits"][0]["result_type"] == "semantic_unit"
    assert result["pack"]["semantic_units"][UNIT_CONTEXT_P]["units"][0][
        "kind"
    ] == "decision"


def test_graph_enriched_pack_includes_typed_neighborhood(cluster: Path) -> None:
    epistemic_graph.EpistemicGraphIndex(cluster).rebuild_all()

    pack = context_pack.assemble_pack(
        cluster, [_hit(ALPHA_P), _hit(BETA_P)], graph_enrich=True
    )

    graph = pack["graph"]
    assert graph["available"] is True
    assert graph["nodes"]
    assert any(edge["relation_type"] == "links_to" for edge in graph["edges"])
    assert pack["packed_paths"] == [ALPHA_P, BETA_P]


def test_graph_enriched_pack_missing_sidecar_soft_fails(cluster: Path) -> None:
    pack = context_pack.assemble_pack(cluster, [_hit(ALPHA_P)], graph_enrich=True)

    assert pack["graph"]["available"] is False
    assert pack["graph"]["reason"] == "graph sidecar unavailable"
    assert pack["claims"]
    assert pack["neighborhood"]
