"""Structural relation suggestions derived from authored semantic units.

Three generators read the typed graph sidecar through one shared read snapshot
and propose page-level relation candidates that the page-level graph cannot
already see:

* ``unit_relation_lift`` — a typed unit relation (``- relations: answers: [[Q]]``)
  is a block-level edge with no page-level counterpart, so the authored kind is
  lifted to a page-level proposal.
* ``shared_open_question`` — two pages carrying the same normalized question.
* ``shared_resolution_target`` — two pages each answering/resolving the same
  target.

Everything here is propose-only: nothing writes Markdown or graph edges.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import corpus_aware, epistemic_graph, relation_queue

KB = "Knowledge Base/Notes/Insights"


def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _page(vault: Path, name: str, body: str) -> Path:
    return _write(
        vault,
        f"{KB}/{name}.md",
        # The H1 is deliberately NOT a bare `{name}`: an H1 whose normalized
        # title matches a `semantic_blocks.BLOCK_TYPES` entry (`source`,
        # `claim`, ...) becomes a rich unit that swallows the whole page.
        f"---\ntype: insight\nstatus: active\n---\n# {name} note\n\n{body}\n",
    )


def _build(vault: Path) -> epistemic_graph.EpistemicGraphIndex:
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    return index


def _candidates(vault: Path, rel_path: str, *, limit: int = 50) -> list[dict]:
    return epistemic_graph.suggest_relations(vault, path=rel_path, limit=limit)[
        "candidates"
    ]


def _by_method(candidates: list[dict], method: str) -> list[dict]:
    return [c for c in candidates if c["method"] == method]


# --------------------------------------------------------------------------
# The seam the lift rests on: two authoring forms behave oppositely.
# --------------------------------------------------------------------------


def test_metadata_relation_lifts_and_plain_bullet_does_not(tmp_path: Path) -> None:
    """`- relations: k: [[T]]` is block-level only; `- k [[T]]` is already page-level.

    A metadata relation row produces an edge whose `src_key` is the BLOCK key
    and no page-level edge at all, so the page-level graph, relation-filtered
    recall, and contract inference cannot see the author's typed claim. A plain
    relation bullet inside a block produces exactly the opposite — a page-level
    edge that is absent from `unit.relations` — so lifting it would duplicate an
    edge that already exists.
    """
    vault = tmp_path / "vault"
    _page(vault, "target-answer", "The answer.")
    _page(vault, "target-support", "The supported claim.")
    _page(
        vault,
        "source",
        "## Claim\n"
        "- id: c-1\n"
        f"- relations: answers: [[{KB}/target-answer]]\n"
        "\n"
        "A typed unit relation.\n"
        "\n"
        "## Finding\n"
        "- id: f-1\n"
        "\n"
        f"- supports [[{KB}/target-support]]\n",
    )
    _build(vault)

    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")

    assert [(c["relation_type"], c["to"]) for c in lifted] == [
        ("answers", f"{KB}/target-answer.md")
    ]
    assert all(c["relation_type"] != "supports" for c in lifted)


def test_lift_is_suppressed_when_the_page_already_authored_the_edge(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _page(vault, "target-answer", "The answer.")
    _page(vault, "target-open", "The unpromoted answer.")
    _page(
        vault,
        "source",
        "## Claim\n"
        "- id: c-1\n"
        f"- relations: answers: [[{KB}/target-answer]], answers: [[{KB}/target-open]]\n"
        "\n"
        "One typed unit relation already promoted by hand, one not.\n"
        "\n"
        "## Relations\n"
        "\n"
        f"- answers [[{KB}/target-answer]]\n",
    )
    _build(vault)

    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")

    assert [c["to"] for c in lifted] == [f"{KB}/target-open.md"]


# --------------------------------------------------------------------------
# Lift honesty: it may only promote a kind the author already typed.
# --------------------------------------------------------------------------


def test_lift_only_proposes_kinds_present_on_the_pages_own_unit_edges(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        "## Claim\n"
        "- id: c-1\n"
        f"- relations: answers: [[{KB}/target]], refines: [[{KB}/target]]\n"
        "\n"
        "A claim with two typed unit relations.\n",
    )
    index = _build(vault)

    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")
    assert lifted

    authored_raw = {
        edge["raw_relation"]
        for edge in index.edges()
        if edge["source_path"] == f"{KB}/source.md"
        and edge["origin"] == "semantic_relation"
        and edge["src_key"] != f"file:{KB}/source.md"
    }
    for candidate in lifted:
        assert candidate["relation_type"] in authored_raw


def test_lift_never_proposes_causality_or_unregistered_kinds(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        "## Claim\n"
        "- id: c-1\n"
        f"- relations: causes: [[{KB}/target]], caused_by: [[{KB}/target]], "
        f"made_up_relation: [[{KB}/target]], mentions: [[{KB}/target]], "
        f"supports: [[{KB}/target]]\n"
        "\n"
        "A claim carrying four kinds the lift must refuse and one it must take.\n",
    )
    _build(vault)

    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")

    # `supports` is the positive control: without it an empty result would pass
    # for the wrong reason (a generator that emits nothing at all).
    assert [c["relation_type"] for c in lifted] == ["supports"]


def test_lift_carries_the_authoring_unit_identity_in_evidence(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        "## Claim\n"
        "- id: c-1\n"
        f"- relations: answers: [[{KB}/target]]\n"
        "\n"
        "A typed unit relation.\n",
    )
    _build(vault)

    candidate = _by_method(
        _candidates(vault, f"{KB}/source.md"), "unit_relation_lift"
    )[0]
    evidence = candidate["evidence"]

    assert evidence["relation_family"] == "answer"
    assert [unit["anchor"] for unit in evidence["units"]] == ["c-1"]
    assert evidence["units"][0]["unit_ref"].endswith("#c-1")
    assert evidence["units"][0]["raw_relation"] == "answers"


def test_lift_folds_several_authoring_units_into_one_candidate(
    tmp_path: Path,
) -> None:
    """`_dedupe_candidates` excludes evidence, so a per-row emitter loses rows."""
    vault = tmp_path / "vault"
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        "## Claim\n"
        "- id: c-1\n"
        f"- relations: answers: [[{KB}/target]]\n"
        "\n"
        "First claim.\n"
        "\n"
        "## Finding\n"
        "- id: f-1\n"
        f"- relations: answers: [[{KB}/target]]\n"
        "\n"
        "Second unit, same authored kind and target.\n",
    )
    _build(vault)

    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")

    assert len(lifted) == 1
    assert [unit["anchor"] for unit in lifted[0]["evidence"]["units"]] == ["c-1", "f-1"]


# --------------------------------------------------------------------------
# Shared open questions: two indexed axes, UNIONed.
# --------------------------------------------------------------------------


def test_shared_open_question_spans_both_question_axes(tmp_path: Path) -> None:
    """A rich `## Open Question`, a `- category:` override, and a compact
    `- [question]` land on different indexed columns; all three must match."""
    vault = tmp_path / "vault"
    question = "Why is the tail latency spiky?"
    _page(vault, "rich", f"## Open Question\n- id: q-rich\n\n{question}\n")
    _page(
        vault,
        "override",
        f"## Open Question\n- id: q-over\n- category: risk\n\n{question}\n",
    )
    _page(vault, "compact", f"- [question] {question}\n")
    _build(vault)

    for name in ("rich", "override", "compact"):
        candidates = _by_method(
            _candidates(vault, f"{KB}/{name}.md"), "shared_open_question"
        )
        others = {c["to"] for c in candidates}
        assert others == {
            f"{KB}/{other}.md"
            for other in ("rich", "override", "compact")
            if other != name
        }, name
        assert all(c["relation_type"] == "relates_to" for c in candidates)


def test_shared_open_question_evidence_carries_the_other_pages_unit_identity(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    question = "Why is the tail latency spiky?"
    _page(vault, "alpha", f"## Open Question\n- id: q-alpha\n\n{question}\n")
    _page(vault, "beta", f"## Open Question\n- id: q-beta\n\n{question}\n")
    _build(vault)

    candidate = _by_method(
        _candidates(vault, f"{KB}/alpha.md"), "shared_open_question"
    )[0]
    match = candidate["evidence"]["matches"][0]

    assert match["question"] == "why is the tail latency spiky"
    assert match["anchor"] == "q-alpha"
    assert match["other_anchor"] == "q-beta"
    assert match["other_unit_ref"].endswith("#q-beta")


def test_shared_open_question_folds_several_matches_into_one_candidate(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _page(
        vault,
        "alpha",
        "## Open Question\n- id: q-a1\n\nWhy is the tail latency spiky?\n\n"
        "## Open Question\n- id: q-a2\n\nWhere does the retry storm start?\n",
    )
    _page(
        vault,
        "beta",
        "## Open Question\n- id: q-b1\n\nWhy is the tail latency spiky?\n\n"
        "## Open Question\n- id: q-b2\n\nWhere does the retry storm start?\n",
    )
    _build(vault)

    candidates = _by_method(
        _candidates(vault, f"{KB}/alpha.md"), "shared_open_question"
    )

    assert len(candidates) == 1
    assert [m["question"] for m in candidates[0]["evidence"]["matches"]] == [
        "where does the retry storm start",
        "why is the tail latency spiky",
    ]


def test_shared_open_question_normalization_is_ascii_lower_and_trailing_question_mark(
    tmp_path: Path,
) -> None:
    """Documented deterministic recall limits: SQLite's ASCII-only `lower()` and
    trailing-`?` stripping. A non-ASCII case difference is NOT normalized away."""
    vault = tmp_path / "vault"
    _page(vault, "alpha", "## Open Question\n- id: q-a\n\nWhy Is The Tail Spiky?\n")
    _page(vault, "beta", "## Open Question\n- id: q-b\n\nwhy is the tail spiky\n")
    _page(vault, "gamma", "## Open Question\n- id: q-g\n\nÉlan vital ou éclat?\n")
    _page(vault, "delta", "## Open Question\n- id: q-d\n\nélan vital ou éclat\n")
    _build(vault)

    alpha = _by_method(_candidates(vault, f"{KB}/alpha.md"), "shared_open_question")
    gamma = _by_method(_candidates(vault, f"{KB}/gamma.md"), "shared_open_question")

    assert [c["to"] for c in alpha] == [f"{KB}/beta.md"]
    assert gamma == []


# --------------------------------------------------------------------------
# Shared resolution target: co-participation over the resolution graph.
# --------------------------------------------------------------------------


def test_shared_resolution_target_pairs_pages_answering_the_same_target(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _page(vault, "question", "The open question page.")
    _page(
        vault,
        "alpha",
        "## Claim\n- id: c-alpha\n"
        f"- relations: answers: [[{KB}/question]]\n\nOne answer.\n",
    )
    _page(
        vault,
        "beta",
        "## Claim\n- id: c-beta\n"
        f"- relations: resolves: [[{KB}/question]]\n\nA competing answer.\n",
    )
    _build(vault)

    candidates = _by_method(
        _candidates(vault, f"{KB}/alpha.md"), "shared_resolution_target"
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["to"] == f"{KB}/beta.md"
    assert candidate["relation_type"] == "relates_to"
    match = candidate["evidence"]["matches"][0]
    assert match["target"] == f"{KB}/question.md"
    assert match["relation"] == "answers"
    assert match["other_relation"] == "resolves"
    assert match["other_anchor"] == "c-beta"
    assert match["other_unit_ref"].endswith("#c-beta")


def test_shared_resolution_target_ignores_page_level_resolution_edges(
    tmp_path: Path,
) -> None:
    """Adjacency is defined over UNIT-level resolution edges on both sides."""
    vault = tmp_path / "vault"
    _page(vault, "question", "The open question page.")
    _page(
        vault,
        "alpha",
        "## Claim\n- id: c-alpha\n"
        f"- relations: answers: [[{KB}/question]]\n\nOne answer.\n",
    )
    _page(
        vault,
        "beta",
        f"## Relations\n\n- answers [[{KB}/question]]\n",
    )
    _page(
        vault,
        "gamma",
        "## Claim\n- id: c-gamma\n"
        f"- relations: answers: [[{KB}/question]]\n\nA unit-level answer.\n",
    )
    _build(vault)

    candidates = _by_method(
        _candidates(vault, f"{KB}/alpha.md"), "shared_resolution_target"
    )

    # gamma is the positive control: beta's page-level edge must be the only
    # thing missing, not the generator itself.
    assert [c["to"] for c in candidates] == [f"{KB}/gamma.md"]


# --------------------------------------------------------------------------
# Bounds, ordering, soft-fail, purity.
# --------------------------------------------------------------------------


class _CountingConnection:
    """Counts `execute` calls made by the structural generators."""

    def __init__(self, inner, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls

    def execute(self, sql, *args, **kwargs):
        self._calls.append(sql)
        return self._inner.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._inner.close()


def _count_snapshot_queries(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

    def patched(self, **kwargs):
        conn = real(self, **kwargs)
        return None if conn is None else _CountingConnection(conn, calls)

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", patched
    )
    return calls


def test_structural_candidates_are_bounded_and_query_count_is_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    question = "Why is the tail latency spiky?"
    _page(vault, "alpha", f"## Open Question\n- id: q-alpha\n\n{question}\n")
    for n in range(200):
        _page(vault, f"other-{n:03d}", f"## Open Question\n- id: q-{n:03d}\n\n{question}\n")
    _build(vault)
    small = tmp_path / "small"
    _page(small, "alpha", f"## Open Question\n- id: q-alpha\n\n{question}\n")
    _page(small, "beta", f"## Open Question\n- id: q-beta\n\n{question}\n")
    _build(small)

    # Patched only after both sidecars exist: the proxy covers the read path,
    # not the rebuild path.
    calls = _count_snapshot_queries(monkeypatch)
    candidates = _by_method(
        _candidates(vault, f"{KB}/alpha.md", limit=500), "shared_open_question"
    )
    big_corpus_queries = len(calls)
    calls.clear()
    _candidates(small, f"{KB}/alpha.md", limit=500)

    assert len(candidates) == 3
    assert len(candidates[0]["evidence"]["matches"]) <= 5
    assert big_corpus_queries == len(calls)


def test_structural_generators_run_after_deterministic_and_before_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration order is deliberate: `_wikilink_candidates` is unbounded and
    `suggest_relations` truncates at `limit`, so the position is load-bearing."""
    vault = tmp_path / "vault"
    _write(
        vault,
        "Knowledge Base/Sources/Articles/shared-source.md",
        "---\ntype: source\nsource_type: article\ningested_into: []\n---\n"
        "# Shared Source\n\n## Capture\n\nMaterial.\n",
    )
    question = "Why is the tail latency spiky?"
    _write(
        vault,
        f"{KB}/neighbour.md",
        "---\ntype: insight\nstatus: active\nsources:\n"
        '  - "[[Knowledge Base/Sources/Articles/shared-source]]"\n---\n'
        f"# neighbour note\n\n## Open Question\n- id: q-n\n\n{question}\n",
    )
    _page(vault, "target", "A target page.")
    _write(
        vault,
        f"{KB}/source.md",
        "---\ntype: insight\nstatus: active\nsources:\n"
        '  - "[[Knowledge Base/Sources/Articles/shared-source]]"\n---\n'
        f"# source note\n\nSee [[{KB}/target]].\n\n"
        f"## Open Question\n- id: q-s\n- relations: answers: [[{KB}/target]]\n\n"
        f"{question}\n\n"
        "## Claim\n- id: c-s\n"
        f"- relations: resolves: [[{KB}/target]]\n\nA claim.\n",
    )
    _write(
        vault,
        f"{KB}/rival.md",
        "---\ntype: insight\nstatus: active\n---\n# rival\n\n"
        "## Claim\n- id: c-r\n"
        f"- relations: answers: [[{KB}/target]]\n\nA rival answer.\n",
    )
    _build(vault)
    monkeypatch.setattr(
        corpus_aware,
        "_best_cosine_per_file",
        lambda *_args, **_kwargs: {f"{KB}/target": 0.9},
    )

    methods = [c["method"] for c in _candidates(vault, f"{KB}/source.md", limit=500)]
    first = {method: methods.index(method) for method in set(methods)}

    for method in (
        "wikilink",
        "frontmatter_sources",
        "shared_sources",
        "unit_relation_lift",
        "shared_open_question",
        "shared_resolution_target",
        "embedding_proximity",
    ):
        assert method in first, method
    assert (
        first["shared_sources"]
        < first["unit_relation_lift"]
        < first["shared_open_question"]
        < first["shared_resolution_target"]
        < first["embedding_proximity"]
    )


def test_structural_generators_soft_fail_when_the_snapshot_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        f"See [[{KB}/target]].\n\n## Claim\n- id: c-1\n"
        f"- relations: answers: [[{KB}/target]]\n\nA claim.\n",
    )
    _build(vault)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_open_read_snapshot",
        lambda self, **kwargs: None,
    )

    result = epistemic_graph.suggest_relations(vault, path=f"{KB}/source.md", limit=50)

    assert result["warnings"] == []
    assert any(c["method"] == "wikilink" for c in result["candidates"])
    assert not [
        c
        for c in result["candidates"]
        if c["method"]
        in {"unit_relation_lift", "shared_open_question", "shared_resolution_target"}
    ]


def test_structural_suggestions_are_propose_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    question = "Why is the tail latency spiky?"
    _page(vault, "target", "A target page.")
    _page(vault, "beta", f"## Open Question\n- id: q-b\n\n{question}\n")
    _page(
        vault,
        "source",
        f"## Open Question\n- id: q-s\n- relations: answers: [[{KB}/target]]\n\n"
        f"{question}\n",
    )
    index = _build(vault)
    before_markdown = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*.md")
    }
    before_edges = index.edges()

    result = epistemic_graph.suggest_relations(vault, path=f"{KB}/source.md", limit=50)

    structural = {
        c["method"]
        for c in result["candidates"]
        if c["method"]
        in {"unit_relation_lift", "shared_open_question", "shared_resolution_target"}
    }
    assert structural == {"unit_relation_lift", "shared_open_question"}
    assert result["mutated"] is False
    assert result["model_suggestions_available"] is False
    assert index.edges() == before_edges
    assert {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*.md")
    } == before_markdown


def test_structural_evidence_is_json_serializable_and_deterministic(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    question = "Why is the tail latency spiky?"
    _page(vault, "target", "A target page.")
    _page(vault, "beta", f"## Open Question\n- id: q-b\n\n{question}\n")
    _page(
        vault,
        "source",
        f"## Open Question\n- id: q-s\n- relations: answers: [[{KB}/target]]\n\n"
        f"{question}\n",
    )
    _build(vault)

    first = _candidates(vault, f"{KB}/source.md")
    second = _candidates(vault, f"{KB}/source.md")
    structural = [
        c
        for c in first
        if c["method"]
        in {"unit_relation_lift", "shared_open_question", "shared_resolution_target"}
    ]

    assert len(structural) == 2
    assert first == second
    assert json.dumps(structural, sort_keys=True)


# --------------------------------------------------------------------------
# The registry gate is a non-issue — proved empirically, not designed around.
# --------------------------------------------------------------------------


def test_accepted_lift_bullet_reenters_as_a_core_page_level_edge(
    tmp_path: Path,
) -> None:
    vault = tmp_path
    _page(vault, "target", "A target page.")
    source = _page(
        vault,
        "source",
        f"## Claim\n- id: c-1\n- relations: answers: [[{KB}/target]]\n\nA claim.\n",
    )
    _build(vault)

    queue = relation_queue.build_queue(vault)
    group = next(g for g in queue["groups"] if g["path"] == f"{KB}/source.md")
    item = next(i for i in group["items"] if i["method"] == "unit_relation_lift")

    def _append_bullet(vault_root, *, path, new_string, **_kwargs):
        target = Path(vault_root) / path
        target.write_text(
            target.read_text(encoding="utf-8") + f"\n## Relations\n\n{new_string}\n",
            encoding="utf-8",
        )
        return {"path": path}

    relation_queue.accept(
        vault,
        ref=item["ref"],
        expected_hash=group["content_hash"],
        why="Accepted a lifted unit relation",
        expected_fingerprint=item["fingerprint"],
        edit_memory=_append_bullet,
    )

    assert f"- answers [[{KB}/target]]" in source.read_text(encoding="utf-8")
    index = _build(vault)
    page_level = [
        edge
        for edge in index.edges()
        if edge["src_key"] == f"file:{KB}/source.md"
        and edge["dst_key"] == f"file:{KB}/target.md"
        and edge["relation_type"] == "answers"
    ]
    assert page_level
    assert all(edge["registry_status"] == "core" for edge in page_level)
