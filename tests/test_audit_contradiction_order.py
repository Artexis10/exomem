"""corpus_contradictions ordering: turn the flat band-pair sweep into a usable
review queue.

The raw sweep surfaces every in-band pair in flat cosine order, dominated by
same-family `Notes/Research/<X>/` architecture adjacency. This suite checks the
ordering layer on top of it:

- priority sorts by cosine (closer first) and by ACT-R dormancy (a forgotten note
  in a close pair lifts the pair),
- same-family pairs are demoted below cross-family pairs,
- the surfaced set is capped at KB_MCP_CONTRADICTION_TOP_N with an explicit
  omitted-count summary finding (no silent truncation),
- nothing is mutated.

It is torch-free: `EmbeddingIndex.all_vectors` is patched with synthetic unit
vectors (orthonormal per-pair blocks) so each planted pair has an exact, isolated
cosine — no model, no sidecar.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from kb_mcp import audit as audit_module
from kb_mcp import embeddings
from kb_mcp import find as find_module

_TODAY = dt.date(2026, 6, 27)
_BODY = "Zylo narwhal quokka substrate measure-not-judge corpus contradiction body."


def _seed(vault: Path, rel: str) -> str:
    """Write a minimal active read-write compiled page. Returns the sidecar key
    (vault-relative WITH 'Knowledge Base/' and .md)."""
    p = vault / "Knowledge Base" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: insight\nstatus: active\n"
        f"created: 2026-01-01\nupdated: 2026-01-01\n---\n\n# {rel}\n\n{_BODY}\n",
        encoding="utf-8",
    )
    return f"Knowledge Base/{rel}"


def _install(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    pairs: list[tuple[str, str, float]],
    *,
    floor: float = 0.5,
    ceiling: float = 0.95,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Seed each pair's two notes and patch `all_vectors` so the planted pair has
    exactly the given cosine (orthonormal per-pair 2-D blocks → zero cross-pair
    cosine). Returns {(rel_a, rel_b): (key_a, key_b)} sorted-key form."""
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("KB_MCP_CONTRADICTION_FLOOR", str(floor))
    monkeypatch.setenv("KB_MCP_DUP_THRESHOLD", str(ceiling))

    dim = 2 * len(pairs)
    vecs: dict[str, np.ndarray] = {}
    order: list[str] = []
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for i, (rel_a, rel_b, cos) in enumerate(pairs):
        key_a = _seed(vault, rel_a)
        key_b = _seed(vault, rel_b)
        va = np.zeros(dim, dtype=np.float32)
        vb = np.zeros(dim, dtype=np.float32)
        va[2 * i] = 1.0
        vb[2 * i] = cos
        vb[2 * i + 1] = float((1.0 - cos * cos) ** 0.5)
        for key, vec in ((key_a, va), (key_b, vb)):
            if key not in vecs:
                vecs[key] = vec
                order.append(key)
        out[tuple(sorted((key_a, key_b)))] = (key_a, key_b)

    metadata = [(key, 0, "chunk") for key in order]
    matrix = np.array([vecs[key] for key in order], dtype=np.float32)
    monkeypatch.setattr(
        embeddings.EmbeddingIndex, "all_vectors", lambda self: (metadata, matrix)
    )
    find_module.clear_cache()
    return out


def _run(vault: Path, *, today: dt.date = _TODAY):
    return audit_module.audit(
        vault, categories=["corpus_contradictions"], today=today
    ).findings


def _pair_findings(findings):
    return [f for f in findings if f.paths]


def _pair_key(f) -> tuple[str, str]:
    return tuple(sorted(f.paths))


# ---------------- priority: cosine ----------------


def test_closer_pair_ranks_first(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two cross-family pairs, equal (gated) dormancy → higher cosine first.
    _install(
        vault,
        monkeypatch,
        [
            ("Notes/Insights/far-a.md", "Notes/Insights/far-b.md", 0.6),
            ("Notes/Insights/near-a.md", "Notes/Insights/near-b.md", 0.9),
        ],
    )
    pf = _pair_findings(_run(vault))
    order = [_pair_key(f) for f in pf]
    near = tuple(sorted(("Knowledge Base/Notes/Insights/near-a.md",
                         "Knowledge Base/Notes/Insights/near-b.md")))
    far = tuple(sorted(("Knowledge Base/Notes/Insights/far-a.md",
                        "Knowledge Base/Notes/Insights/far-b.md")))
    assert order.index(near) < order.index(far), order
    # cosine preserved; new priority/dormancy/same_family meta present.
    near_f = next(f for f in pf if _pair_key(f) == near)
    assert near_f.meta["cosine"] == 0.9
    assert "priority" in near_f.meta and "dormancy" in near_f.meta
    assert near_f.meta["same_family"] is False


# ---------------- priority: ACT-R dormancy ----------------


def test_dormant_note_lifts_equally_close_pair(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two cross-family pairs at the SAME cosine. P-fresh's notes are recently and
    # repeatedly surfaced (low dormancy); P-dormant's notes are never accessed
    # (max dormancy). The dormant pair must sort first.
    _install(
        vault,
        monkeypatch,
        [
            ("Notes/Insights/dormant-a.md", "Notes/Insights/dormant-b.md", 0.8),
            ("Notes/Insights/fresh-a.md", "Notes/Insights/fresh-b.md", 0.8),
        ],
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    recent = (_TODAY - dt.timedelta(days=1)).isoformat() + "T10:00:00"
    surfacings = []
    for note in ("fresh-a", "fresh-b"):
        for _ in range(5):
            surfacings.append(json.dumps({
                "ts": recent,
                "query": f"q-{note}",
                "top_k": [{"path": f"Knowledge Base/Notes/Insights/{note}"}],
            }))
    (logs / "queries.jsonl").write_text("\n".join(surfacings) + "\n", encoding="utf-8")
    monkeypatch.delenv("KB_MCP_DISABLE_RELEVANCE_CHECK", raising=False)
    monkeypatch.setattr(audit_module, "_RELEVANCE_LOGS_DIR", logs)

    pf = _pair_findings(_run(vault))
    order = [_pair_key(f) for f in pf]
    dormant = tuple(sorted(("Knowledge Base/Notes/Insights/dormant-a.md",
                            "Knowledge Base/Notes/Insights/dormant-b.md")))
    fresh = tuple(sorted(("Knowledge Base/Notes/Insights/fresh-a.md",
                          "Knowledge Base/Notes/Insights/fresh-b.md")))
    assert order.index(dormant) < order.index(fresh), order
    by = {_pair_key(f): f for f in pf}
    # The forgotten pair is maximally dormant; the fresh pair is much less so.
    assert by[dormant].meta["dormancy"] == 1.0
    assert by[fresh].meta["dormancy"] < by[dormant].meta["dormancy"]
    assert by[dormant].meta["priority"] > by[fresh].meta["priority"]


# ---------------- same-family demotion ----------------


def test_same_family_pair_demoted_below_lower_priority_cross_family(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same-family architecture pair has the HIGHER cosine (0.9) but is demoted
    # below the lower-cosine (0.6) cross-family pair.
    _install(
        vault,
        monkeypatch,
        [
            ("Notes/Research/Q/svc-architecture.md",
             "Notes/Research/Q/api-architecture.md", 0.9),
            ("Notes/Insights/x-a.md", "Notes/Insights/x-b.md", 0.6),
        ],
    )
    pf = _pair_findings(_run(vault))
    order = [_pair_key(f) for f in pf]
    family = tuple(sorted(("Knowledge Base/Notes/Research/Q/svc-architecture.md",
                           "Knowledge Base/Notes/Research/Q/api-architecture.md")))
    cross = tuple(sorted(("Knowledge Base/Notes/Insights/x-a.md",
                          "Knowledge Base/Notes/Insights/x-b.md")))
    assert order.index(cross) < order.index(family), order
    by = {_pair_key(f): f for f in pf}
    assert by[family].meta["same_family"] is True
    assert by[cross].meta["same_family"] is False
    # Demoted despite the higher raw priority.
    assert by[family].meta["priority"] > by[cross].meta["priority"]
    assert "Same-family" in by[family].detail


# ---------------- cap + explicit count ----------------


def test_cap_surfaces_top_n_and_reports_omitted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs = [
        (f"Notes/Insights/c{i}-a.md", f"Notes/Insights/c{i}-b.md", 0.6 + i * 0.05)
        for i in range(5)
    ]
    _install(vault, monkeypatch, pairs, ceiling=0.95)
    monkeypatch.setenv("KB_MCP_CONTRADICTION_TOP_N", "3")

    findings = _run(vault)
    pf = _pair_findings(findings)
    assert len(pf) == 3, [f.as_dict() for f in findings]
    summary = [f for f in findings if not f.paths]
    assert len(summary) == 1
    assert summary[0].meta["truncated"] == 2
    assert summary[0].meta["shown"] == 3
    assert summary[0].meta["total"] == 5
    assert "2 more" in summary[0].detail
    assert "not shown" in summary[0].detail
    # The 3 surfaced are the highest-cosine (priority) pairs.
    shown_cos = sorted(f.meta["cosine"] for f in pf)
    assert shown_cos == [0.7, 0.75, 0.8]


def test_top_n_zero_disables_cap(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs = [
        (f"Notes/Insights/u{i}-a.md", f"Notes/Insights/u{i}-b.md", 0.6 + i * 0.05)
        for i in range(5)
    ]
    _install(vault, monkeypatch, pairs, ceiling=0.95)
    monkeypatch.setenv("KB_MCP_CONTRADICTION_TOP_N", "0")

    findings = _run(vault)
    assert len(_pair_findings(findings)) == 5
    assert [f for f in findings if not f.paths] == []  # no summary when uncapped


# ---------------- measurement-only ----------------


def test_audit_does_not_mutate_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        vault,
        monkeypatch,
        [
            ("Notes/Insights/m-a.md", "Notes/Insights/m-b.md", 0.8),
            ("Notes/Research/Q/p-architecture.md",
             "Notes/Research/Q/q-architecture.md", 0.85),
        ],
    )

    def _snapshot() -> dict[str, str]:
        snap: dict[str, str] = {}
        for p in sorted(vault.rglob("*")):
            if p.is_file():
                snap[str(p.relative_to(vault))] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
        return snap

    before = _snapshot()
    findings = _run(vault)
    after = _snapshot()
    assert before == after  # no file created, modified, moved, or deleted
    assert _pair_findings(findings)  # sanity: it did surface the planted pairs


# ---------------- gating preserved ----------------


def test_noop_when_embeddings_disabled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Suite default KB_MCP_DISABLE_EMBEDDINGS=1 → short-circuit even with vectors
    # patched in. The ordering layer never runs.
    _seed(vault, "Notes/Insights/d-a.md")
    _seed(vault, "Notes/Insights/d-b.md")
    monkeypatch.setattr(
        embeddings.EmbeddingIndex,
        "all_vectors",
        lambda self: ([], np.zeros((0, 3), dtype=np.float32)),
    )
    find_module.clear_cache()
    assert _run(vault) == []
