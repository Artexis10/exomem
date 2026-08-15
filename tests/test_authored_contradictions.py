"""Authored contradictions: asserted queue entries, provenance, and the
competing-alternatives pair stance.

The strongest contradiction signal in a vault is an authored `contradicts` edge,
and until this change nothing consumed it: the `corpus_contradictions` queue only
measured embedding proximity, and cosine cannot separate "X works" from "X fails".
This suite covers the four moving parts:

- asserted entries sourced from the typed graph, emitted before every proximity
  pair and independent of the embedding sidecar,
- an explicit `meta.provenance` of `asserted` / `proximity` on every entry, with an
  asserted pair suppressing its own proximity duplicate,
- the fingerprint-bound competing-alternatives pair stance ("rivals; keep both"),
  recorded in the same review-state store as dismiss/snooze and honored by the
  queues and by the write-time draft warnings,
- the structural-pair exemption for pairs whose authored edges already declare them.

Torch-free throughout: the asserted lane needs no vectors, and the mixed
asserted/proximity tests patch `EmbeddingIndex.all_vectors` with synthetic unit
vectors exactly as `test_audit_contradiction_order.py` does.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from exomem import attention as attention_module
from exomem import audit as audit_module
from exomem import commands as commands_module
from exomem import context_pack as context_pack_module
from exomem import contradiction_stance as stance_module
from exomem import corpus_aware as corpus_aware_module
from exomem import embeddings as embeddings_module
from exomem import epistemic_graph as epistemic_graph_module
from exomem import find as find_module
from exomem import review_state as review_state_module
from exomem.audit import AuditFinding
from exomem.find import Hit

_TODAY = dt.date(2026, 6, 27)
_BODY = "Zylo narwhal quokka substrate measure-not-judge authored contradiction body."


# ----------------------------- helpers -----------------------------


def _seed(
    vault: Path,
    rel: str,
    *,
    relations: list[str] | None = None,
    type_: str = "insight",
    status: str = "active",
    body: str = _BODY,
) -> str:
    """Write one compiled page; return its vault-relative key (with .md)."""
    path = vault / "Knowledge Base" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\ntype: {type_}\nstatus: {status}\n"
        f"created: 2026-01-01\nupdated: 2026-01-01\n---\n\n# {rel}\n\n{body}\n"
    )
    if relations:
        text += "\n## Relations\n\n" + "".join(f"- {row}\n" for row in relations)
    path.write_text(text, encoding="utf-8")
    return f"Knowledge Base/{rel}"


def _link(key: str) -> str:
    return f"[[{key.removesuffix('.md')}]]"


def _build_graph(vault: Path) -> None:
    find_module.clear_cache()
    epistemic_graph_module.EpistemicGraphIndex(vault).rebuild_all()


def _cc(vault: Path, *, today: dt.date = _TODAY) -> list[AuditFinding]:
    return audit_module.audit(
        vault, categories=["corpus_contradictions"], today=today
    ).findings


def _pair_key(finding: AuditFinding) -> tuple[str, ...]:
    return tuple(sorted(finding.paths or []))


def _install_vectors(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    pairs: list[tuple[str, str, float]],
    *,
    floor: float = 0.5,
    ceiling: float = 0.95,
) -> None:
    """Patch `all_vectors` so each already-seeded key pair has exactly `cos`."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", str(floor))
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", str(ceiling))
    dim = 2 * len(pairs)
    vecs: dict[str, np.ndarray] = {}
    order: list[str] = []
    for index, (key_a, key_b, cos) in enumerate(pairs):
        va = np.zeros(dim, dtype=np.float32)
        vb = np.zeros(dim, dtype=np.float32)
        va[2 * index] = 1.0
        vb[2 * index] = cos
        vb[2 * index + 1] = float((1.0 - cos * cos) ** 0.5)
        for key, vec in ((key_a, va), (key_b, vb)):
            if key not in vecs:
                vecs[key] = vec
                order.append(key)
    metadata = [(key, 0) for key in order]
    matrix = np.array([vecs[key] for key in order], dtype=np.float32)
    monkeypatch.setattr(
        embeddings_module.EmbeddingIndex, "all_vectors", lambda self: (metadata, matrix)
    )


def _hit(rel: str) -> Hit:
    return Hit(path=rel, type=None, scope=None, title="", updated="", excerpt="")


# ----------------------------- pure logic: pair identity -----------------------------


def test_pair_key_is_order_independent_and_md_normalized() -> None:
    a = "Knowledge Base/Notes/Insights/a"
    b = "Knowledge Base/Notes/Insights/b.md"
    assert stance_module.pair_key(a, b) == stance_module.pair_key(b, a)
    assert stance_module.pair_key(a, b) == (
        "Knowledge Base/Notes/Insights/a.md",
        "Knowledge Base/Notes/Insights/b.md",
    )


def test_pair_identity_is_order_independent(vault: Path) -> None:
    a = _seed(vault, "Notes/Insights/pi-a.md")
    b = _seed(vault, "Notes/Insights/pi-b.md")
    find_module.clear_cache()
    assert stance_module.pair_identity(vault, a, b) == stance_module.pair_identity(
        vault, b, a
    )


def test_pair_fingerprint_changes_when_an_endpoint_changes(vault: Path) -> None:
    a = _seed(vault, "Notes/Insights/pf-a.md")
    b = _seed(vault, "Notes/Insights/pf-b.md")
    find_module.clear_cache()
    before = stance_module.pair_identity(vault, a, b)
    assert before is not None
    (vault / "Knowledge Base" / "Notes/Insights/pf-a.md").write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-01-01\nupdated: 2026-01-02\n"
        "---\n\n# pf-a\n\nA materially different claim now lives here.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    after = stance_module.pair_identity(vault, a, b)
    assert after is not None
    assert after[0] == before[0], "the pair review id is stable across edits"
    assert after[1] != before[1], "the pair fingerprint tracks endpoint content"


def test_pair_identity_is_none_for_a_missing_page(vault: Path) -> None:
    a = _seed(vault, "Notes/Insights/pm-a.md")
    find_module.clear_cache()
    assert stance_module.pair_identity(vault, a, "Knowledge Base/Notes/nope.md") is None


# ----------------------------- pure logic: review state -----------------------------


def test_review_state_accepts_competing_as_action_and_view() -> None:
    assert "competing" in review_state_module.VALID_ACTIONS
    assert "competing" in review_state_module.VALID_VIEWS
    assert "competing" in review_state_module.VALID_STATES


def test_competing_decision_resolves_to_a_competing_state(vault: Path) -> None:
    store = review_state_module.ReviewStateStore(vault)
    result = store.apply("a" * 24, "b" * 24, action="competing", why="rivals; keep both")
    assert result["state"] == "competing"
    effective, decision = store.effective_state("a" * 24, "b" * 24, today=_TODAY)
    assert effective == "competing"
    assert decision is not None and decision.action == "competing"


def test_competing_refuses_an_until_date(vault: Path) -> None:
    store = review_state_module.ReviewStateStore(vault)
    with pytest.raises(ValueError, match="INVALID_REVIEW_ACTION"):
        store.apply("a" * 24, "b" * 24, action="competing", until="2026-09-01")


def test_reopen_clears_a_competing_record(vault: Path) -> None:
    store = review_state_module.ReviewStateStore(vault)
    store.apply("a" * 24, "b" * 24, action="competing", why="rivals")
    store.apply("a" * 24, "b" * 24, action="reopen")
    effective, decision = store.effective_state("a" * 24, "b" * 24, today=_TODAY)
    assert effective == "open"
    assert decision is None


def test_a_competing_fingerprint_mismatch_does_not_apply(vault: Path) -> None:
    store = review_state_module.ReviewStateStore(vault)
    store.apply("a" * 24, "b" * 24, action="competing", why="rivals")
    effective, _decision = store.effective_state("a" * 24, "c" * 24, today=_TODAY)
    assert effective == "open"


# ----------------------------- pure logic: graph readers -----------------------------


def test_asserted_pairs_dedupes_a_symmetric_edge(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/ap-b.md")
    a = _seed(vault, "Notes/Insights/ap-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    assert stance_module.asserted_pairs(vault) == [tuple(sorted((a, b)))]


def test_asserted_pairs_is_empty_without_a_built_graph(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/ng-b.md")
    _seed(vault, "Notes/Insights/ng-a.md", relations=[f"contradicts {_link(b)}"])
    find_module.clear_cache()
    assert stance_module.asserted_pairs(vault) == []


def test_asserted_pairs_ignores_other_relations(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/or-b.md")
    _seed(vault, "Notes/Insights/or-a.md", relations=[f"supports {_link(b)}"])
    _build_graph(vault)
    assert stance_module.asserted_pairs(vault) == []


def test_structural_pair_detects_an_authored_contradicts_edge(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/sp-b.md")
    a = _seed(vault, "Notes/Insights/sp-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    assert stance_module.structural_pair(vault, a, b) == "contradicts"
    assert stance_module.structural_pair(vault, b, a) == "contradicts"


def test_structural_pair_detects_two_answers_to_one_question(vault: Path) -> None:
    q = _seed(vault, "Notes/Insights/sq-question.md")
    a = _seed(vault, "Notes/Insights/sq-a.md", relations=[f"answers {_link(q)}"])
    b = _seed(vault, "Notes/Insights/sq-b.md", relations=[f"answers {_link(q)}"])
    _build_graph(vault)
    assert stance_module.structural_pair(vault, a, b) == "answers_same_question"


def test_structural_pair_is_none_for_an_undeclared_pair(vault: Path) -> None:
    a = _seed(vault, "Notes/Insights/su-a.md")
    b = _seed(vault, "Notes/Insights/su-b.md")
    _build_graph(vault)
    assert stance_module.structural_pair(vault, a, b) is None


# ----------------------------- asserted queue entries -----------------------------


def test_authored_edge_surfaces_without_embeddings(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/ae-b.md")
    a = _seed(vault, "Notes/Insights/ae-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    mine = [f for f in _cc(vault) if _pair_key(f) == tuple(sorted((a, b)))]
    assert len(mine) == 1, [f.as_dict() for f in _cc(vault)]
    finding = mine[0]
    assert finding.category == "corpus_contradictions"
    assert finding.severity == "info"
    assert finding.path == min(a, b)
    assert finding.meta["provenance"] == "asserted"
    assert finding.meta["signal_version"]
    assert "competing" in (finding.proposed_fix or "")


def test_asserted_entries_precede_every_proximity_entry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    near_a = _seed(vault, "Notes/Insights/pr-near-a.md")
    near_b = _seed(vault, "Notes/Insights/pr-near-b.md")
    asserted_b = _seed(vault, "Notes/Insights/pr-zz-b.md")
    asserted_a = _seed(
        vault, "Notes/Insights/pr-zz-a.md", relations=[f"contradicts {_link(asserted_b)}"]
    )
    _build_graph(vault)
    _install_vectors(vault, monkeypatch, [(near_a, near_b, 0.9)])

    findings = [f for f in _cc(vault) if f.paths]
    provenance = [f.meta["provenance"] for f in findings]
    assert provenance == ["asserted", "proximity"], [f.as_dict() for f in findings]
    assert _pair_key(findings[0]) == tuple(sorted((asserted_a, asserted_b)))
    assert "priority" not in findings[0].meta
    assert "same_family" not in findings[0].meta
    assert findings[1].meta["provenance"] == "proximity"
    assert findings[1].meta["cosine"] == 0.9


def test_an_asserted_pair_suppresses_its_proximity_duplicate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = _seed(vault, "Notes/Insights/sd-b.md")
    a = _seed(vault, "Notes/Insights/sd-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    _install_vectors(vault, monkeypatch, [(a, b, 0.9)])

    mine = [f for f in _cc(vault) if _pair_key(f) == tuple(sorted((a, b)))]
    assert len(mine) == 1
    assert mine[0].meta["provenance"] == "asserted"
    assert "cosine" not in mine[0].meta


def test_asserted_signal_version_differs_from_the_proximity_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = _seed(vault, "Notes/Insights/sv-b.md")
    a = _seed(vault, "Notes/Insights/sv-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    asserted = next(f for f in _cc(vault) if _pair_key(f) == tuple(sorted((a, b))))

    plain_b = _seed(vault, "Notes/Insights/sv-plain-b.md")
    plain_a = _seed(vault, "Notes/Insights/sv-plain-a.md")
    _install_vectors(vault, monkeypatch, [(plain_a, plain_b, 0.9)])
    proximity = next(
        f for f in _cc(vault) if _pair_key(f) == tuple(sorted((plain_a, plain_b)))
    )
    assert asserted.meta["signal_version"] != proximity.meta["signal_version"]


def test_an_ineligible_endpoint_is_not_surfaced(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/el-b.md", status="archived")
    a = _seed(vault, "Notes/Insights/el-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    assert not [f for f in _cc(vault) if _pair_key(f) == tuple(sorted((a, b)))]


def test_asserted_entries_are_not_capped(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_CONTRADICTION_TOP_N", "1")
    keys = []
    for index in range(3):
        other = _seed(vault, f"Notes/Insights/cap-b{index}.md")
        own = _seed(
            vault,
            f"Notes/Insights/cap-a{index}.md",
            relations=[f"contradicts {_link(other)}"],
        )
        keys.append(tuple(sorted((own, other))))
    _build_graph(vault)
    surfaced = {_pair_key(f) for f in _cc(vault) if f.paths}
    assert set(keys) <= surfaced
    assert not [f for f in _cc(vault) if f.meta and "truncated" in f.meta]


# ----------------------------- attention composition -----------------------------


def _finding(path: str, other: str, provenance: str, version: str) -> AuditFinding:
    return AuditFinding(
        category="corpus_contradictions",
        severity="info",
        path=path,
        detail=f"{provenance} contradiction with {other}",
        proposed_fix="review only",
        paths=sorted([path, other]),
        meta={"signal_version": version, "provenance": provenance},
    )


def test_asserted_reason_outranks_a_proximity_reason() -> None:
    report = attention_module._rank(
        [
            _finding("a.md", "b.md", "asserted", "v1"),
            _finding("c.md", "d.md", "proximity", "v2"),
        ],
        categories={"corpus_contradictions"},
        limit=0,
    )
    assert [item.path for item in report.items] == ["a.md", "c.md"]
    assert report.items[0].score > report.items[1].score
    assert report.items[0].reasons[0]["meta"]["provenance"] == "asserted"


def test_attention_surfaces_an_asserted_pair_with_a_stable_ref(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/at-b.md")
    a = _seed(vault, "Notes/Insights/at-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    report = attention_module.attention(
        vault, categories=["corpus_contradictions"], limit=0, today=_TODAY
    )
    item = next(i for i in report.items if i.path == min(a, b))
    assert item.ref and item.fingerprint and item.state == "open"
    assert sorted(item.reasons[0]["related_paths"]) == sorted([a, b])
    assert item.reasons[0]["meta"]["provenance"] == "asserted"


def test_state_summary_counts_competing(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/ss-b.md")
    _seed(vault, "Notes/Insights/ss-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    report = attention_module.attention(
        vault, categories=["corpus_contradictions"], limit=0, state="all", today=_TODAY
    )
    assert "competing" in (report.state_summary or {})


# ----------------------------- the competing stance end to end -----------------------------


@pytest.fixture
def rivals(vault: Path) -> tuple[Path, str, str, str]:
    """A surfaced asserted pair plus its review ref."""
    b = _seed(vault, "Notes/Insights/rv-b.md")
    a = _seed(vault, "Notes/Insights/rv-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    report = attention_module.attention(
        vault, categories=["corpus_contradictions"], limit=0, today=_TODAY
    )
    item = next(i for i in report.items if i.path == min(a, b))
    assert item.ref is not None
    return vault, a, b, item.ref


def _open_paths(vault: Path, *, state: str = "open") -> list[str]:
    report = attention_module.attention(
        vault, categories=["corpus_contradictions"], limit=0, state=state, today=_TODAY
    )
    return [item.path for item in report.items]


def test_competing_stance_removes_the_pair_from_the_open_view(rivals) -> None:
    vault, a, b, ref = rivals
    result = commands_module.op_triage_memory(
        vault, ref=ref, action="competing", why="rivals; keep both"
    )
    assert result["state"] == "competing"
    assert sorted(result["pair"]) == sorted([a, b])
    assert min(a, b) not in _open_paths(vault)
    assert min(a, b) in _open_paths(vault, state="competing")


def test_competing_stance_is_recorded_on_the_pair_identity(rivals) -> None:
    vault, a, b, ref = rivals
    commands_module.op_triage_memory(vault, ref=ref, action="competing", why="rivals")
    assert stance_module.is_competing(vault, a, b)
    assert stance_module.is_competing(vault, b, a)


def test_editing_a_rival_resurfaces_the_pair(rivals) -> None:
    vault, a, b, ref = rivals
    commands_module.op_triage_memory(vault, ref=ref, action="competing", why="rivals")
    assert min(a, b) not in _open_paths(vault)

    target = vault / a
    target.write_text(
        target.read_text(encoding="utf-8") + "\nA materially revised claim.\n",
        encoding="utf-8",
    )
    _build_graph(vault)
    assert min(a, b) in _open_paths(vault)
    assert not stance_module.is_competing(vault, a, b)


def test_reopen_clears_the_pair_stance(rivals) -> None:
    vault, a, b, ref = rivals
    commands_module.op_triage_memory(vault, ref=ref, action="competing", why="rivals")
    commands_module.op_triage_memory(vault, ref=ref, action="reopen")
    assert min(a, b) in _open_paths(vault)
    assert not stance_module.is_competing(vault, a, b)


def test_item_level_dismissal_beats_the_pair_stance(rivals) -> None:
    vault, a, b, ref = rivals
    commands_module.op_triage_memory(vault, ref=ref, action="competing", why="rivals")
    commands_module.op_triage_memory(vault, ref=ref, action="dismiss", why="handled")
    report = attention_module.attention(
        vault, categories=["corpus_contradictions"], limit=0, state="all", today=_TODAY
    )
    item = next(i for i in report.items if i.path == min(a, b))
    assert item.state == "dismissed"


def test_competing_is_refused_for_an_item_without_a_pair(vault: Path) -> None:
    _seed(vault, "Notes/Insights/nopair.md")
    find_module.clear_cache()
    report = attention_module.attention(
        vault, categories=["relation_debt"], limit=0, today=_TODAY
    )
    item = next(
        i for i in report.items if i.path == "Knowledge Base/Notes/Insights/nopair.md"
    )
    with pytest.raises(ValueError, match="INVALID_REVIEW_ACTION"):
        commands_module.op_triage_memory(
            vault, ref=item.ref, action="competing", why="not a pair"
        )


@pytest.mark.parametrize(
    "ref",
    [
        "exomem://review/adoption/" + "a" * 24,
        "exomem://review/relation/" + "a" * 24,
    ],
)
def test_competing_is_refused_for_a_namespaced_single_sided_queue(
    vault: Path, ref: str
) -> None:
    with pytest.raises(ValueError, match="INVALID_REVIEW_ACTION"):
        commands_module.op_triage_memory(
            vault, ref=ref, action="competing", why="not a pair"
        )


def test_recording_a_stance_mutates_no_note(rivals) -> None:
    vault, a, b, ref = rivals
    before = {
        path: path.read_bytes() for path in (vault / "Knowledge Base").rglob("*.md")
    }
    commands_module.op_triage_memory(vault, ref=ref, action="competing", why="rivals")
    after = {path: path.read_bytes() for path in (vault / "Knowledge Base").rglob("*.md")}
    assert before == after


# ----------------------------- write-time draft warnings -----------------------------


def _overlap(vault: Path, self_key: str, other_key: str, cosine: float) -> list:
    return corpus_aware_module.detect_contradictions(
        vault,
        title="Draft",
        body=_BODY,
        self_path=self_key,
        precomputed={other_key: cosine},
    )


def _dups(vault: Path, self_key: str, other_key: str, cosine: float) -> list:
    return corpus_aware_module.detect_duplicates(
        vault,
        title="Draft",
        body=_BODY,
        self_path=self_key,
        precomputed={other_key: cosine},
        threshold=0.9,
    )


@pytest.fixture
def write_time(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.5")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "0.95")
    return vault


def test_an_undeclared_pair_still_warns(write_time: Path) -> None:
    a = _seed(write_time, "Notes/Insights/wu-a.md")
    b = _seed(write_time, "Notes/Insights/wu-b.md")
    _build_graph(write_time)
    assert [c.path for c in _overlap(write_time, a, b, 0.8)] == [b]
    assert [c.path for c in _dups(write_time, a, b, 0.99)] == [b]


def test_an_authored_contradicts_edge_exempts_the_pair(write_time: Path) -> None:
    b = _seed(write_time, "Notes/Insights/wc-b.md")
    a = _seed(write_time, "Notes/Insights/wc-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(write_time)
    assert _overlap(write_time, a, b, 0.8) == []
    assert _dups(write_time, a, b, 0.99) == []


def test_two_answers_to_one_question_exempt_the_pair(write_time: Path) -> None:
    q = _seed(write_time, "Notes/Insights/wq-question.md")
    a = _seed(write_time, "Notes/Insights/wq-a.md", relations=[f"answers {_link(q)}"])
    b = _seed(write_time, "Notes/Insights/wq-b.md", relations=[f"answers {_link(q)}"])
    _build_graph(write_time)
    assert _overlap(write_time, a, b, 0.8) == []
    assert _dups(write_time, a, b, 0.99) == []


def test_a_competing_stance_exempts_the_pair(write_time: Path) -> None:
    a = _seed(write_time, "Notes/Insights/ws-a.md")
    b = _seed(write_time, "Notes/Insights/ws-b.md")
    _build_graph(write_time)
    identity = stance_module.pair_identity(write_time, a, b)
    assert identity is not None
    review_state_module.ReviewStateStore(write_time).apply(
        identity[0], identity[1], action="competing", why="rivals"
    )
    assert _overlap(write_time, a, b, 0.8) == []
    assert _dups(write_time, a, b, 0.99) == []


def test_a_draft_with_no_page_identity_still_warns(write_time: Path) -> None:
    b = _seed(write_time, "Notes/Insights/wd-b.md")
    _build_graph(write_time)
    candidates = corpus_aware_module.detect_contradictions(
        write_time, title="Draft", body=_BODY, self_path=None, precomputed={b: 0.8}
    )
    assert [c.path for c in candidates] == [b]


# ----------------------------- deep-pack tension provenance -----------------------------


def test_pack_tension_carries_asserted_provenance_without_embeddings(vault: Path) -> None:
    b = _seed(vault, "Notes/Insights/pk-b.md")
    a = _seed(vault, "Notes/Insights/pk-a.md", relations=[f"contradicts {_link(b)}"])
    _build_graph(vault)
    pack = context_pack_module.assemble_pack(vault, [_hit(a), _hit(b)])
    tension = pack["contradictions"]["tension"]
    assert len(tension) == 1, tension
    assert {tension[0]["a"], tension[0]["b"]} == {a, b}
    assert tension[0]["provenance"] == "asserted"
    assert pack["embeddings_available"] is False


def test_pack_proximity_pairs_are_labelled_and_follow_asserted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = _seed(vault, "Notes/Insights/pp-b.md")
    a = _seed(vault, "Notes/Insights/pp-a.md", relations=[f"contradicts {_link(b)}"])
    c = _seed(vault, "Notes/Insights/pp-c.md")
    d = _seed(vault, "Notes/Insights/pp-d.md")
    _build_graph(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.5")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "0.95")

    cosines = {frozenset((c, d)): 0.85, frozenset((a, b)): 0.9}

    def fake_bcpf(vault_root, *, title, body, k: int = 15):
        # `_seed` writes `# {rel}`, so a packed page's title is its KB-relative path.
        del vault_root, body, k
        for key, score in cosines.items():
            for member in key:
                if title and member.endswith(title):
                    return {other: score for other in key if other != member}
        return {}

    monkeypatch.setattr(corpus_aware_module, "_best_cosine_per_file", fake_bcpf)
    pack = context_pack_module.assemble_pack(
        vault, [_hit(a), _hit(b), _hit(c), _hit(d)]
    )
    tension = pack["contradictions"]["tension"]
    provenance = [pair["provenance"] for pair in tension]
    assert provenance == ["asserted", "proximity"], tension
    assert {tension[0]["a"], tension[0]["b"]} == {a, b}
    assert {tension[1]["a"], tension[1]["b"]} == {c, d}
    assert tension[1]["cosine"] == 0.85
