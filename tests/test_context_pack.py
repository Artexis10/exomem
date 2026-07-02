"""Unit tests for the reasoning-ready context pack (`find(pack=true)`).

Torch-free: builds its own tiny inter-linked vault per test (so it never perturbs
the shared fixture vault), and exercises `context_pack.assemble_pack` directly. The
embedding-dependent `tension` path is tested by monkeypatching
`corpus_aware._best_cosine_per_file` with injected cosines — no model load.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import context_pack, corpus_aware
from exomem import find as find_module
from exomem.find import Hit

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


def _write(vault: Path, rel: str, body: str) -> None:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _hit(rel: str) -> Hit:
    return Hit(path=rel, type=None, scope=None, title="", updated="", excerpt="")


ALPHA_P = "Knowledge Base/Notes/Alpha.md"
BETA_P = "Knowledge Base/Notes/Beta.md"
CHARLIE_P = "Knowledge Base/Notes/Charlie.md"
HUB_P = "Knowledge Base/Notes/Hub.md"
DELTA_P = "Knowledge Base/Notes/Delta.md"
OLD_P = "Knowledge Base/Notes/Old.md"
NEW_P = "Knowledge Base/Notes/NewView.md"


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
