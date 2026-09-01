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

from exomem import (
    commands,
    corpus_aware,
    epistemic_graph,
    markdown_relations,
    relation_queue,
    relation_registry,
)

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
    """A kind authored on ANOTHER page must never surface here.

    `foreign` is load-bearing. Deriving the expected set from the same
    `source_path` filter the query uses proves nothing: relaxing that filter
    (`e.source_path = ? OR 1=1`) would make every page's authored relations
    liftable onto every page, and a fixture holding only one authoring page has
    no foreign kind to catch it.
    """
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
    _page(
        vault,
        "foreign",
        "## Claim\n"
        "- id: c-f\n"
        f"- relations: contradicts: [[{KB}/target]]\n"
        "\n"
        "A third in-allowlist kind, to the same target, on a different page.\n",
    )
    index = _build(vault)

    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")
    foreign = _by_method(_candidates(vault, f"{KB}/foreign.md"), "unit_relation_lift")

    assert sorted(c["relation_type"] for c in lifted) == ["answers", "refines"]
    assert [c["relation_type"] for c in foreign] == ["contradicts"]

    authored = {
        relation_registry.normalize_relation(edge["raw_relation"])
        for edge in index.edges()
        if edge["source_path"] == f"{KB}/source.md"
        and edge["origin"] == "semantic_relation"
        and edge["src_key"] != f"file:{KB}/source.md"
    }
    assert "contradicts" not in authored
    for candidate in lifted:
        assert candidate["relation_type"] in authored


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


def test_lift_normalizes_the_authored_label_so_the_proposal_is_acceptable(
    tmp_path: Path,
) -> None:
    """`- relations: Answers:` parses with no diagnostic and resolves to core
    standing, but the canonical relation-bullet grammar accepts only
    `[a-z][a-z0-9_.-]{1,80}`. A verbatim proposal would author `- Answers [[T]]`,
    which the governed edit refuses as `malformed_relation` — leaving a queue
    item that can never be accepted and recurs on every read. The normalized
    label is still the authored kind."""
    vault = tmp_path
    source = _page(
        vault,
        "source",
        f"## Claim\n- id: c-1\n- relations: Answers: [[{KB}/target]]\n\nA claim.\n",
    )
    _page(vault, "target", "A target page.")
    _build(vault)

    queue = relation_queue.build_queue(vault)
    group = next(g for g in queue["groups"] if g["path"] == f"{KB}/source.md")
    item = next(i for i in group["items"] if i["method"] == "unit_relation_lift")

    assert item["relation_type"] == "answers"
    assert item["bullet"] == f"- answers [[{KB}/target]]"

    # The whole governed accept path, not an injected writer.
    commands.op_connect_memory(
        vault,
        operation="accept-relation",
        ref=item["ref"],
        expected_hash=group["content_hash"],
        why="Accepted a lifted unit relation",
        expected_fingerprint=item["fingerprint"],
    )

    assert f"- answers [[{KB}/target]]" in source.read_text(encoding="utf-8")


def _write_extension_registry(vault: Path) -> None:
    path = vault / "Knowledge Base" / "_Schema" / "relation-registry.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: 1\n"
        "extensions:\n"
        "  lab.answers_partially:\n"
        "    parent: answers\n"
        "    description: Partially answers\n"
        "  lab.answers_retired:\n"
        "    parent: answers\n"
        "    description: Retired in favour of the parent\n"
        "    status: deprecated\n"
        "  lab.answers_sourceonly:\n"
        "    parent: answers\n"
        "    description: Only meaningful on source pages\n"
        "    scope:\n"
        "      page_types: [source]\n",
        encoding="utf-8",
    )


def test_lift_takes_a_vault_extension_kind_without_a_code_change(
    tmp_path: Path,
) -> None:
    """The family allowlist is resolved through the registry at call time, so an
    extension parented into an allowed family lifts with no code change."""
    vault = tmp_path / "vault"
    _write_extension_registry(vault)
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        "## Claim\n- id: c-1\n"
        f"- relations: lab.answers_partially: [[{KB}/target]]\n\nA claim.\n",
    )
    _build(vault)

    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")

    assert [c["relation_type"] for c in lifted] == ["lab.answers_partially"]
    assert lifted[0]["evidence"]["relation_family"] == "answer"


def test_lift_refuses_deprecated_and_scope_violating_kinds_in_an_allowed_family(
    tmp_path: Path,
) -> None:
    """The family allowlist and the registry-standing gate are independent: an
    allowed family readily admits a deprecated or out-of-scope extension, and
    proposing one would author a bullet the writer rejects."""
    vault = tmp_path / "vault"
    _write_extension_registry(vault)
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        "## Claim\n- id: c-1\n"
        f"- relations: lab.answers_retired: [[{KB}/target]], "
        f"lab.answers_sourceonly: [[{KB}/target]], "
        f"lab.answers_partially: [[{KB}/target]]\n\nA claim.\n",
    )
    index = _build(vault)

    statuses = {
        edge["raw_relation"]: edge["registry_status"]
        for edge in index.edges()
        if edge["source_path"] == f"{KB}/source.md"
        and edge["origin"] == "semantic_relation"
    }
    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")

    # All three are in family `answer`; only their standing differs.
    assert statuses["lab.answers_retired"] == "deprecated"
    assert statuses["lab.answers_sourceonly"] == "scope_violation"
    assert [c["relation_type"] for c in lifted] == ["lab.answers_partially"]


#: `relation_registry._KEY_RE` is length-unbounded; the canonical relation
#: grammar caps the label at 81 characters. 84 characters is a valid extension
#: key that cannot be written as a bullet.
_OVERLONG_KEY = "lab." + ("a" * 80)


def test_lift_refuses_labels_the_canonical_relation_grammar_rejects(
    tmp_path: Path,
) -> None:
    """Registry standing is not the same question as bullet writability.

    Three vault-authored shapes resolve to `extension` or `alias` standing and
    would still author a bullet the governed write refuses: a key longer than
    the grammar's 81-character cap, a one-character alias (the canonical bullet
    grammar needs at least two), and an over-length alias. A candidate that can
    never be accepted is worse than no candidate, so the normalized label is
    checked against the canonical grammar itself before emission.

    A non-ASCII alias was a fourth shape here, and is kept as a fixture with the
    opposite expectation: the registry now refuses it at registration rather
    than recording an `invalid_alias` finding and registering it anyway, so it
    never reaches this guard. That is a narrower registry, not a weaker guard —
    the three shapes above pass the registry's own label grammar and are exactly
    what this check exists for.
    """
    vault = tmp_path / "vault"
    path = vault / "Knowledge Base" / "_Schema" / "relation-registry.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: 1\n"
        "extensions:\n"
        f"  {_OVERLONG_KEY}:\n"
        "    parent: answers\n"
        "    description: A key the bullet grammar cannot carry\n"
        "  lab.answers_short:\n"
        "    parent: answers\n"
        "    description: One-character alias\n"
        "    aliases: ['a']\n"
        "  lab.answers_accented:\n"
        "    parent: answers\n"
        "    description: Non-ASCII alias\n"
        "    aliases: ['ré']\n"
        "  lab.answers_verbose:\n"
        "    parent: answers\n"
        "    description: Over-length alias\n"
        f"    aliases: ['{'v' * 84}']\n"
        # Three writable controls, all sorting AFTER the two unwritable labels
        # that lead the ordering. If the guard ran after the per-generator cap
        # instead of per row, `a` and the over-long key would consume two of the
        # three slots and only one control would survive.
        "  lab.answers_partially:\n"
        "    parent: answers\n"
        "    description: A writable extension, the positive control\n"
        "  lab.answers_second:\n"
        "    parent: answers\n"
        "    description: A second writable extension\n"
        "  lab.answers_third:\n"
        "    parent: answers\n"
        "    description: A third writable extension\n",
        encoding="utf-8",
    )
    _page(vault, "target", "A target page.")
    _page(
        vault,
        "source",
        "## Claim\n- id: c-1\n"
        f"- relations: {_OVERLONG_KEY}: [[{KB}/target]], a: [[{KB}/target]], "
        f"ré: [[{KB}/target]], {'v' * 84}: [[{KB}/target]], "
        f"lab.answers_partially: [[{KB}/target]], "
        f"lab.answers_second: [[{KB}/target]], "
        f"lab.answers_third: [[{KB}/target]]\n\nA claim.\n",
    )
    index = _build(vault)

    standing = {
        edge["raw_relation"]: edge["registry_status"]
        for edge in index.edges()
        if edge["source_path"] == f"{KB}/source.md"
        and edge["origin"] == "semantic_relation"
    }
    lifted = _by_method(_candidates(vault, f"{KB}/source.md"), "unit_relation_lift")

    # Every refused shape carries standing the status gate admits.
    assert standing[_OVERLONG_KEY] == "extension"
    assert standing["a"] == "alias"
    assert standing["v" * 84] == "alias"
    # The non-ASCII alias is refused by the registry itself, so it never gains
    # the standing this guard would have had to strip.
    assert standing.get("ré") != "alias"
    assert [c["relation_type"] for c in lifted] == [
        "lab.answers_partially",
        "lab.answers_second",
        "lab.answers_third",
    ]

    # And what survives is writable by construction.
    for candidate in lifted:
        bullet = relation_queue._bullet(candidate)
        assert markdown_relations._CANONICAL_RE.match(bullet) is not None, bullet


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


def test_shared_open_question_evidence_fold_is_capped(tmp_path: Path) -> None:
    """Eight shared questions, five folded matches, and an honest total.

    A fixture where every candidate carries one match cannot tell a cap of five
    from a cap of a thousand.
    """
    questions = [f"Why does subsystem {n} stall?" for n in range(8)]
    blocks = "\n\n".join(
        f"## Open Question\n- id: q-{side}-{n}\n\n{question}"
        for side in ("x",)
        for n, question in enumerate(questions)
    )
    vault = tmp_path / "vault"
    _page(vault, "alpha", blocks)
    _page(vault, "beta", blocks.replace("q-x-", "q-y-"))
    _build(vault)

    candidate = _by_method(
        _candidates(vault, f"{KB}/alpha.md"), "shared_open_question"
    )[0]

    assert candidate["evidence"]["shared_questions"] == 8
    assert len(candidate["evidence"]["matches"]) == 5


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
    # A plain relation bullet INSIDE a block body, NOT under `## Relations`.
    # That form keeps `origin = 'semantic_relation'` and is excluded only by the
    # `src_key <> file_key` guard; the `## Relations` form would be excluded by
    # the origin filter alone and would prove nothing about the guard.
    _page(
        vault,
        "beta",
        f"## Claim\n- id: c-beta\n\n- answers [[{KB}/question]]\n",
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


def test_co_participation_generators_do_not_propose_the_same_edge_twice(
    tmp_path: Path,
) -> None:
    """Two pages that share a question usually also answer the same thing, and
    both generators then propose the identical `relates_to` bullet. Keeping both
    spends two scarce slots on one edge the reviewer can accept only once."""
    vault = tmp_path / "vault"
    question = "Why is the tail latency spiky?"
    _page(vault, "topic", "The thing being answered.")
    _page(
        vault,
        "alpha",
        f"## Open Question\n- id: q-alpha\n\n{question}\n\n"
        f"## Claim\n- id: c-alpha\n- relations: answers: [[{KB}/topic]]\n\nOne answer.\n",
    )
    _page(
        vault,
        "beta",
        f"## Open Question\n- id: q-beta\n\n{question}\n\n"
        f"## Claim\n- id: c-beta\n- relations: answers: [[{KB}/topic]]\n\nAnother answer.\n",
    )
    _build(vault)

    candidates = _candidates(vault, f"{KB}/alpha.md")
    to_beta = [
        c
        for c in candidates
        if c["to"] == f"{KB}/beta.md" and c["relation_type"] == "relates_to"
    ]

    assert [c["method"] for c in to_beta] == ["shared_open_question"]


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
    # Structural first: an author-written typed relation outranks a body
    # wikilink, and `limit` makes that a budget rather than a preference.
    assert (
        first["unit_relation_lift"]
        < first["shared_open_question"]
        < first["shared_resolution_target"]
        < first["wikilink"]
    )
    # ...and the pre-existing four keep their relative order among themselves.
    assert (
        first["wikilink"]
        < first["frontmatter_sources"]
        < first["shared_sources"]
        < first["embedding_proximity"]
    )


def test_structural_candidates_survive_a_link_heavy_page(tmp_path: Path) -> None:
    """The motivating case: a dense compiled note with more wikilinks than the
    response limit. Registered after `_wikilink_candidates`, every structural
    candidate would be truncated away and the change would be silent on exactly
    the pages it exists to serve."""
    vault = tmp_path / "vault"
    _page(vault, "target", "A target page.")
    for n in range(12):
        _page(vault, f"link-{n:02d}", "A linked page.")
    links = " ".join(f"[[{KB}/link-{n:02d}]]" for n in range(12))
    _page(
        vault,
        "dense",
        f"A dense compiled note citing {links}.\n\n"
        f"## Claim\n- id: c-1\n- relations: answers: [[{KB}/target]]\n\nA claim.\n",
    )
    _build(vault)

    # `relation_queue._DEFAULT_LIMIT_PER_PAGE` is 10; use it verbatim.
    candidates = _candidates(
        vault, f"{KB}/dense.md", limit=relation_queue._DEFAULT_LIMIT_PER_PAGE
    )

    assert len([c for c in candidates if c["method"] == "wikilink"]) >= 1
    assert [c["relation_type"] for c in _by_method(candidates, "unit_relation_lift")] == [
        "answers"
    ]


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
