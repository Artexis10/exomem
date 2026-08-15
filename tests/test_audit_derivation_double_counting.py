"""`derivation_double_counting` — an opt-in, read-only audit category that walks
`sources:` (`derived_from`) chains for two epistemic-integrity problems ordinary
checks cannot see:

- Support collapse: a page cites two or more sources as independent support, but
  those sources themselves trace back to a shared ancestor — the classic
  "one blog post laundered into multiple sources agree" failure.
- Circular derivation: a `sources:` chain that loops back on itself.

Observe-before-enforce: every finding is informational or warn, nothing is ever
written, and the traversal is bounded (depth + total edges) with the cap made
visible in a dedicated finding whenever it is hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import audit as audit_module

_FOLDER_BY_TYPE = {
    "insight": "Knowledge Base/Notes/Insights",
    "research-note": "Knowledge Base/Notes/Research/personal",
    "failure": "Knowledge Base/Notes/Failures",
    "pattern": "Knowledge Base/Notes/Patterns",
    "experiment": "Knowledge Base/Notes/Experiments/health",
    "production-log": "Knowledge Base/Notes/Productions/video",
    "entity": "Knowledge Base/Entities",
    "source": "Knowledge Base/Sources/Articles",
}


def _write(
    vault: Path,
    name: str,
    *,
    page_type: str = "insight",
    sources: list[str] | None = None,
    status: str = "active",
    tags: str = "[]",
) -> str:
    folder = _FOLDER_BY_TYPE[page_type]
    rel = f"{folder}/{name}.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    sources_literal = (
        "[]" if not sources else "[" + ", ".join(f'"[[{s}]]"' for s in sources) + "]"
    )
    extra = "project: personal\n" if page_type == "research-note" else ""
    path.write_text(
        f"---\ntype: {page_type}\nstatus: {status}\ncreated: 2026-07-10\n"
        f"updated: 2026-07-10\nsources: {sources_literal}\ntags: {tags}\n{extra}---\n"
        f"\n## Finding\n\nBody.\n",
        encoding="utf-8",
    )
    return rel.removesuffix(".md")


def _findings(vault: Path):
    return audit_module.audit(vault, categories=["derivation_double_counting"]).findings


def _by_kind(findings, kind: str):
    return [f for f in findings if (f.meta or {}).get("kind") == kind]


# ---------------- registration ----------------


def test_category_is_optional_and_absent_from_the_default_sweep(tmp_path: Path) -> None:
    _write(tmp_path, "s", page_type="source")

    assert "derivation_double_counting" in audit_module.OPTIONAL_CATEGORIES
    assert "derivation_double_counting" not in audit_module.ALL_CATEGORIES

    default_categories = {
        finding.category for finding in audit_module.audit(tmp_path).findings
    }
    assert "derivation_double_counting" not in default_categories


# ---------------- support collapse (the diamond) ----------------


def test_diamond_support_collapse_is_flagged(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    b = _write(tmp_path, "derived-b", sources=[s])
    c = _write(tmp_path, "citing-both", sources=[a, b])

    findings = _by_kind(_findings(tmp_path), "support_collapse")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.path == c + ".md"
    assert finding.severity == "info"
    assert finding.meta["shared_ancestor"] == s + ".md"
    assert sorted(finding.meta["via_sources"]) == sorted([a + ".md", b + ".md"])
    assert "double-count" in finding.detail.lower()


def test_clean_chain_with_independent_sources_produces_no_finding(tmp_path: Path) -> None:
    """The false-positive guard: genuinely independent sources must not be flagged."""
    s1 = _write(tmp_path, "root-source-1", page_type="source")
    s2 = _write(tmp_path, "root-source-2", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s1])
    b = _write(tmp_path, "derived-b", sources=[s2])
    _write(tmp_path, "citing-both", sources=[a, b])

    assert _by_kind(_findings(tmp_path), "support_collapse") == []


def test_single_source_is_not_a_collapse_candidate(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    _write(tmp_path, "citing-one", sources=[a])

    assert _by_kind(_findings(tmp_path), "support_collapse") == []


def test_direct_duplicate_shared_source_also_collapses(tmp_path: Path) -> None:
    """A page whose two direct sources cite the same source directly (not
    transitively) still counts as double-counting the shared ancestor."""
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    c = _write(tmp_path, "citing-both", sources=[a, s])

    findings = _by_kind(_findings(tmp_path), "support_collapse")

    assert len(findings) == 1
    assert findings[0].path == c + ".md"
    assert findings[0].meta["shared_ancestor"] == s + ".md"


def test_unresolved_shared_ancestor_gets_a_reconstructed_vault_path(
    tmp_path: Path,
) -> None:
    """The shared ancestor here is never written as a real page -- `sources:`
    can legitimately point at material this vault has no parsed page for.
    `shared_ancestor` must still be a vault-relative rel_path an agent could
    open (or get an honest "not found" from), not the internal lowercase,
    extension-stripped canon key every other lookup in this module uses.
    """
    ghost = "Knowledge Base/Sources/Articles/ghost-page"
    a = _write(tmp_path, "derived-a", sources=[ghost])
    b = _write(tmp_path, "derived-b", sources=[ghost])
    _write(tmp_path, "citing-both", sources=[a, b])

    findings = _by_kind(_findings(tmp_path), "support_collapse")

    assert len(findings) == 1
    shared = findings[0].meta["shared_ancestor"]
    assert shared == f"{ghost}.md"
    assert shared != ghost.lower()  # not the internal canon key
    assert shared.startswith("Knowledge Base/")


def test_origin_page_is_never_its_own_shared_ancestor(tmp_path: Path) -> None:
    """The origin cites two sources that each cite the origin back (forming
    two 2-cycles as a byproduct -- unavoidable, since "the origin is
    reachable from a source" and "that source is one of the origin's own
    direct sources" together mean a path back to the origin by construction).
    A naive intersection lists the origin itself as its own "shared
    ancestor"; `collapse_roots` must exclude the citing page.

    Names are chosen so the origin sorts FIRST among the three canon keys
    (`aaa-origin` < `zzz-src-a` < `zzz-src-b`). This is load-bearing for the
    test, not decoration: when all three candidates end up mutually
    dominated (as they do here -- see `_nearest_shared_roots`), the
    deterministic single-survivor fallback picks `min(keys)`. With the
    origin-exclusion fix removed, that fallback would otherwise coincidentally
    pick a source rather than the origin if the origin didn't sort first,
    silently passing this test while the underlying bug (the origin reported
    as its own shared ancestor) remained.
    """
    origin_key = "Knowledge Base/Notes/Insights/aaa-origin"
    a_key = "Knowledge Base/Notes/Insights/zzz-src-a"
    b_key = "Knowledge Base/Notes/Insights/zzz-src-b"
    edges = {origin_key: [a_key, b_key], a_key: [origin_key], b_key: [origin_key]}
    for key, targets in edges.items():
        rel = f"{key}.md"
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        sources_literal = "[" + ", ".join(f'"[[{t}]]"' for t in targets) + "]"
        (tmp_path / rel).write_text(
            f"---\ntype: insight\nstatus: active\ncreated: 2026-07-10\n"
            f"updated: 2026-07-10\nsources: {sources_literal}\ntags: []\n---\n"
            f"\n## Finding\n\nBody.\n",
            encoding="utf-8",
        )

    findings = _by_kind(_findings(tmp_path), "support_collapse")

    origin_rel = origin_key + ".md"
    assert not any(f.meta["shared_ancestor"] == origin_rel for f in findings)
    assert not any(
        f.path == origin_rel and origin_rel in f.meta["via_sources"] for f in findings
    )


def test_one_converging_tail_produces_one_finding_not_one_per_node(
    tmp_path: Path,
) -> None:
    """E is cited by D, D is cited by C, and both A and B cite C directly
    (so both trace up through the same C -> D -> E tail). Z cites A and B.
    This is ONE collapse situation -- everything converges at C -- and must
    produce exactly one finding naming the nearest shared ancestor, not one
    finding per node (C, D, and E) in the shared tail.
    """
    e = _write(tmp_path, "tail-e", page_type="source")
    d = _write(tmp_path, "tail-d", sources=[e])
    c = _write(tmp_path, "tail-c", sources=[d])
    a = _write(tmp_path, "tail-a", sources=[c])
    b = _write(tmp_path, "tail-b", sources=[c])
    z = _write(tmp_path, "tail-z", sources=[a, b])

    findings = _by_kind(_findings(tmp_path), "support_collapse")

    assert len(findings) == 1
    assert findings[0].path == z + ".md"
    assert findings[0].meta["shared_ancestor"] == c + ".md"
    assert sorted(findings[0].meta["via_sources"]) == sorted([a + ".md", b + ".md"])


def test_only_provenance_bearing_types_originate_a_collapse_finding(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    b = _write(tmp_path, "derived-b", sources=[s])
    _write(tmp_path, "citing-both", page_type="production-log", sources=[a, b])

    assert _by_kind(_findings(tmp_path), "support_collapse") == []


def test_inactive_status_does_not_originate_a_collapse_finding(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    b = _write(tmp_path, "derived-b", sources=[s])
    _write(tmp_path, "citing-both", sources=[a, b], status="archived")

    assert _by_kind(_findings(tmp_path), "support_collapse") == []


def test_architecture_slug_suffix_is_excluded_from_origination(tmp_path: Path) -> None:
    """A convention-named hub/snapshot page is EXPECTED to fan its `sources:`
    out from a shared root (`relation_debt`/`missing_sources` already carry
    this exact exclusion) -- without it, this diamond would otherwise be
    flagged exactly like `test_diamond_support_collapse_is_flagged`.
    """
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    b = _write(tmp_path, "derived-b", sources=[s])
    _write(tmp_path, "retrieval-architecture", sources=[a, b])

    assert _by_kind(_findings(tmp_path), "support_collapse") == []


def test_hub_tag_is_excluded_from_origination(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    b = _write(tmp_path, "derived-b", sources=[s])
    _write(tmp_path, "citing-both", sources=[a, b], tags="[hub]")

    assert _by_kind(_findings(tmp_path), "support_collapse") == []


# ---------------- circular derivation ----------------


def test_direct_self_reference_is_flagged_as_a_cycle(tmp_path: Path) -> None:
    a_rel = f"{_FOLDER_BY_TYPE['insight']}/self-loop.md"
    (tmp_path / a_rel).parent.mkdir(parents=True, exist_ok=True)
    a = "Knowledge Base/Notes/Insights/self-loop"
    (tmp_path / a_rel).write_text(
        f"---\ntype: insight\nstatus: active\ncreated: 2026-07-10\n"
        f"updated: 2026-07-10\nsources: [\"[[{a}]]\"]\ntags: []\n---\n\n## Finding\n\nBody.\n",
        encoding="utf-8",
    )

    findings = _by_kind(_findings(tmp_path), "cycle")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "warn"
    assert finding.meta["cycle"][0] == finding.meta["cycle"][-1]
    assert len(finding.meta["cycle"]) == 2


def test_two_node_cycle_is_detected_and_deduplicated(tmp_path: Path) -> None:
    a_rel = "Knowledge Base/Notes/Insights/loop-a.md"
    b_rel = "Knowledge Base/Notes/Insights/loop-b.md"
    a_key = "Knowledge Base/Notes/Insights/loop-a"
    b_key = "Knowledge Base/Notes/Insights/loop-b"
    for rel, target in ((a_rel, b_key), (b_rel, a_key)):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(
            f"---\ntype: insight\nstatus: active\ncreated: 2026-07-10\n"
            f"updated: 2026-07-10\nsources: [\"[[{target}]]\"]\ntags: []\n---\n"
            f"\n## Finding\n\nBody.\n",
            encoding="utf-8",
        )

    findings = _by_kind(_findings(tmp_path), "cycle")

    # One cycle exists in the graph; detected from both endpoints but reported once.
    assert len(findings) == 1
    finding = findings[0]
    assert finding.meta["cycle"][0] == finding.meta["cycle"][-1]
    assert len(finding.meta["cycle"]) == 3
    assert {finding.meta["cycle"][0], finding.meta["cycle"][1]} == {
        a_key + ".md", b_key + ".md",
    }


def test_cycle_not_involving_the_walk_start_terminates_without_spurious_truncation(
    tmp_path: Path,
) -> None:
    """X cites A but is not itself part of the A<->B cycle. Walking from X must
    terminate cleanly (the `seen` guard stops re-enqueueing a node already
    visited on this walk) rather than bouncing between A and B until the
    depth/edge cap is hit and produces a spurious `truncated` finding.

    2- and 3-node cycles that loop straight back to their own start terminate
    via the `target == start` shortcut regardless of `seen` (see the tests
    above) -- this fixture is the one shape that actually exercises the
    `seen` guard's necessity: a cycle reachable FROM the walk's start, but
    not containing the start itself.
    """
    a_key = "Knowledge Base/Notes/Insights/seen-guard-a"
    b_key = "Knowledge Base/Notes/Insights/seen-guard-b"
    for key, target in ((a_key, b_key), (b_key, a_key)):
        rel = f"{key}.md"
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(
            f"---\ntype: insight\nstatus: active\ncreated: 2026-07-10\n"
            f"updated: 2026-07-10\nsources: [\"[[{target}]]\"]\ntags: []\n---\n"
            f"\n## Finding\n\nBody.\n",
            encoding="utf-8",
        )
    _write(tmp_path, "seen-guard-x", sources=[a_key])

    findings = _findings(tmp_path)

    assert len(_by_kind(findings, "cycle")) == 1
    assert _by_kind(findings, "truncated") == []


def test_cycle_detection_terminates_on_a_longer_self_referential_chain(tmp_path: Path) -> None:
    """A -> B -> C -> A must terminate (not loop forever) and be reported once."""
    keys = {
        "a": "Knowledge Base/Notes/Insights/chain-a",
        "b": "Knowledge Base/Notes/Insights/chain-b",
        "c": "Knowledge Base/Notes/Insights/chain-c",
    }
    edges = {"a": "b", "b": "c", "c": "a"}
    for name, target in edges.items():
        rel = f"{keys[name]}.md"
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(
            f"---\ntype: insight\nstatus: active\ncreated: 2026-07-10\n"
            f"updated: 2026-07-10\nsources: [\"[[{keys[target]}]]\"]\ntags: []\n---\n"
            f"\n## Finding\n\nBody.\n",
            encoding="utf-8",
        )

    findings = _by_kind(_findings(tmp_path), "cycle")

    assert len(findings) == 1
    assert findings[0].meta["cycle"][0] == findings[0].meta["cycle"][-1]
    assert len(findings[0].meta["cycle"]) == 4


# ---------------- bounded traversal / cap visibility ----------------


def test_truncation_is_visible_when_the_depth_cap_is_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DERIVATION_MAX_DEPTH", "2")

    # A chain of 6 hops, well past the depth-2 cap.
    prior: str | None = None
    for i in range(6):
        name = f"chain-{i}"
        sources = [prior] if prior else None
        prior = _write(tmp_path, name, sources=sources)

    findings = _findings(tmp_path)
    truncated = _by_kind(findings, "truncated")

    assert len(truncated) == 1
    assert truncated[0].severity == "info"
    assert truncated[0].meta["max_depth"] == 2
    assert truncated[0].meta["reasons"] == ["depth"]


def test_edge_budget_cap_is_reported_and_is_the_sole_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolates the shared edge budget from the depth cap: this chain (4 hops)
    is well under the default depth cap (12), so if the edge budget were
    removed entirely (mutation: `_DerivationBudget.take` always returning
    True) every walk would complete cleanly and no `truncated` finding would
    ever be emitted -- there would be nothing left to bound it.
    """
    monkeypatch.setenv("EXOMEM_DERIVATION_MAX_EDGES", "5")

    prior: str | None = None
    for i in range(5):  # 4 edges per full walk of the deepest node; well under depth=12
        name = f"edge-chain-{i}"
        sources = [prior] if prior else None
        prior = _write(tmp_path, name, sources=sources)

    findings = _findings(tmp_path)
    truncated = _by_kind(findings, "truncated")

    assert len(truncated) == 1
    assert truncated[0].meta["max_edges"] == 5
    assert truncated[0].meta["reasons"] == ["edges"]


def test_no_truncation_finding_when_the_graph_is_small(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    _write(tmp_path, "citing-one", sources=[a])

    assert _by_kind(_findings(tmp_path), "truncated") == []


# ---------------- hard constraints: read-only, severity ----------------


def test_severity_is_never_error(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    b = _write(tmp_path, "derived-b", sources=[s])
    _write(tmp_path, "citing-both", sources=[a, b])
    a_rel = "Knowledge Base/Notes/Insights/self-loop.md"
    (tmp_path / a_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / a_rel).write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-07-10\n"
        "updated: 2026-07-10\n"
        'sources: ["[[Knowledge Base/Notes/Insights/self-loop]]"]\ntags: []\n---\n'
        "\n## Finding\n\nBody.\n",
        encoding="utf-8",
    )

    findings = _findings(tmp_path)

    assert findings, "expected at least one finding to check severities on"
    assert {f.severity for f in findings} <= {"info", "warn"}


def test_audit_is_read_only(tmp_path: Path) -> None:
    s = _write(tmp_path, "root-source", page_type="source")
    a = _write(tmp_path, "derived-a", sources=[s])
    b = _write(tmp_path, "derived-b", sources=[s])
    _write(tmp_path, "citing-both", sources=[a, b])

    def _snapshot() -> dict[str, tuple[float, bytes] | None]:
        # `rglob("*")`, not `*.md`: a no-op check that only globbed Markdown
        # would still pass a vacuous `return []` implementation that creates
        # a new non-.md file. Directories snapshot as None (no content/mtime
        # comparison meaningful for them, but their presence/absence still
        # participates in the `keys()` equality check below).
        return {
            str(p): (
                None if p.is_dir() else (p.stat().st_mtime, p.read_bytes())
            )
            for p in sorted(tmp_path.rglob("*"))
        }

    before = _snapshot()
    findings = _findings(tmp_path)
    _findings(tmp_path)
    after = _snapshot()

    assert findings, "expected at least one finding — a no-op check must not pass vacuously"
    assert before.keys() == after.keys()
    assert before == after
