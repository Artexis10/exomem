"""corpus_contradictions ordering: turn the flat band-pair sweep into a usable
review queue.

The raw sweep surfaces every in-band pair in flat cosine order, dominated by
same-family `Notes/Research/<X>/` architecture adjacency. This suite checks the
ordering layer on top of it:

- priority sorts by cosine (closer first) and by ACT-R dormancy (a forgotten note
  in a close pair lifts the pair),
- same-family pairs are demoted below cross-family pairs,
- the surfaced set is capped at EXOMEM_CONTRADICTION_TOP_N with an explicit
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

from exomem import audit as audit_module
from exomem import embeddings
from exomem import find as find_module

_TODAY = dt.date(2026, 6, 27)
_BODY = "Zylo narwhal quokka substrate measure-not-judge corpus contradiction body."


def _seed(vault: Path, rel: str, project: str | None = None) -> str:
    """Write a minimal active read-write compiled page. Returns the sidecar key
    (vault-relative WITH 'Knowledge Base/' and .md)."""
    p = vault / "Knowledge Base" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    project_line = f"project: {project}\n" if project else ""
    p.write_text(
        "---\ntype: insight\nstatus: active\n"
        f"{project_line}created: 2026-01-01\nupdated: 2026-01-01\n---\n\n# {rel}\n\n{_BODY}\n",
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
    projects: dict[str, str] | None = None,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Seed each pair's two notes and patch `all_vectors` so the planted pair has
    exactly the given cosine (orthonormal per-pair 2-D blocks → zero cross-pair
    cosine). Returns {(rel_a, rel_b): (key_a, key_b)} sorted-key form."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", str(floor))
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", str(ceiling))

    dim = 2 * len(pairs)
    vecs: dict[str, np.ndarray] = {}
    order: list[str] = []
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for i, (rel_a, rel_b, cos) in enumerate(pairs):
        key_a = _seed(vault, rel_a, (projects or {}).get(rel_a))
        key_b = _seed(vault, rel_b, (projects or {}).get(rel_b))
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

    metadata = [(key, 0) for key in order]  # numpy-lite: (file_path, chunk_idx)
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
    monkeypatch.delenv("EXOMEM_DISABLE_RELEVANCE_CHECK", raising=False)
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
        projects={
            "Notes/Research/Q/svc-architecture.md": "q",
            "Notes/Research/Q/api-architecture.md": "q",
        },
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


def test_project_key_decides_the_family_not_the_folder(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One project's notes filed in two folders are one family; two untagged notes
    # that share a folder are not.
    _install(
        vault,
        monkeypatch,
        [
            ("Notes/Equipment/p-a.md", "Notes/Insights/p-b.md", 0.9),
            ("Notes/Research/Q/u-a.md", "Notes/Research/Q/u-b.md", 0.8),
        ],
        projects={"Notes/Equipment/p-a.md": "q", "Notes/Insights/p-b.md": "q"},
    )
    by = {_pair_key(f): f for f in _pair_findings(_run(vault))}
    tagged = tuple(sorted(("Knowledge Base/Notes/Equipment/p-a.md", "Knowledge Base/Notes/Insights/p-b.md")))
    untagged = tuple(sorted(("Knowledge Base/Notes/Research/Q/u-a.md", "Knowledge Base/Notes/Research/Q/u-b.md")))
    assert by[tagged].meta["same_family"] is True
    assert by[untagged].meta["same_family"] is False


# ---------------- cap + explicit count ----------------


def test_cap_surfaces_top_n_and_reports_omitted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs = [
        (f"Notes/Insights/c{i}-a.md", f"Notes/Insights/c{i}-b.md", 0.6 + i * 0.05)
        for i in range(5)
    ]
    _install(vault, monkeypatch, pairs, ceiling=0.95)
    monkeypatch.setenv("EXOMEM_CONTRADICTION_TOP_N", "3")

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
    monkeypatch.setenv("EXOMEM_CONTRADICTION_TOP_N", "0")

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
    # Suite default EXOMEM_DISABLE_EMBEDDINGS=1 → short-circuit even with vectors
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


def test_a_remembered_sweep_is_dropped_when_the_vectors_change(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page's remembered pairs hold only while its vectors are the same bytes.
    Kept for longer, a page rewritten into a near-duplicate kept reporting its
    old contradiction."""
    planted = _install(vault, monkeypatch, [("Notes/Insights/memo-a.md", "Notes/Insights/memo-b.md", 0.8)])
    pair = next(iter(planted))
    keys = list(planted[pair])
    assert pair in {_pair_key(f) for f in _pair_findings(_run(vault))}

    # Rewritten into a near-duplicate: a new matrix, above the ceiling.
    duplicate = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    monkeypatch.setattr(
        embeddings.EmbeddingIndex,
        "all_vectors",
        lambda self: ([(key, 0) for key in keys], duplicate),
    )

    assert pair not in {_pair_key(f) for f in _pair_findings(_run(vault))}


def _toward(page: int, cosine: float, fresh: int) -> np.ndarray:
    """A unit vector with `cosine` to the chunks of `page` and none to any other chunk."""
    vector = np.zeros(16, dtype=np.float32)
    vector[page] = cosine
    vector[fresh] = (1.0 - cosine * cosine) ** 0.5
    return vector


def _chunked_pages(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[list[np.ndarray]]]:
    """Eight pages of 1,400 chunks down to one. A page's plain chunks equal each other and have
    cosine 0 with every other page. The band is 0.55 to 0.7, and the pages are large enough for
    the sweep to need more than one product. Five chunks are planted:

    - Page 0 holds one chunk at 0.6 to page 5 and one at 0.95. Seen from page 0, each chunk of
      page 5 scores 0.95, which is outside the band. Seen from page 5, the first chunk scores 0.6.
    - Page 6 holds the same two chunks toward page 1, so that pair counts only from page 1.
    - The first chunk of page 2 and the first chunk of page 3 are at 0.6 and 0.65 to page 7.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.55")
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "0.7")
    monkeypatch.setenv("EXOMEM_CONTRADICTION_TOP_N", "0")
    keys = [_seed(vault, f"Notes/Insights/chunked-{page}.md") for page in range(8)]
    chunks = [
        [np.eye(16, dtype=np.float32)[page]] * count
        for page, count in enumerate((1400, 900, 500, 150, 40, 3, 2, 1))
    ]
    chunks[0][-2:] = [_toward(5, 0.6, 8), _toward(5, 0.95, 9)]
    chunks[6][:] = [_toward(1, 0.6, 12), _toward(1, 0.95, 13)]
    chunks[2][0] = _toward(7, 0.6, 10)
    chunks[3][0] = _toward(7, 0.65, 11)
    find_module.clear_cache()
    return keys, chunks


def _serve(monkeypatch: pytest.MonkeyPatch, keys: list[str], chunks: list[list[np.ndarray]]) -> None:
    metadata = [
        (key, index) for key, page in zip(keys, chunks, strict=True) for index in range(len(page))
    ]
    matrix = np.array([chunk for page in chunks for chunk in page], dtype=np.float32)
    monkeypatch.setattr(embeddings.EmbeddingIndex, "all_vectors", lambda self: (metadata, matrix))


def _reported(vault: Path, keys: list[str]) -> set[tuple[int, int]]:
    return {
        tuple(sorted(keys.index(path) for path in finding.paths))
        for finding in _pair_findings(_run(vault))
    }


def test_a_sweep_in_several_products_gives_each_chunk_to_its_own_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One product holds several pages and is split by page, along both of its sides. A wrong
    split gives a page's first chunk to its neighbour. A sweep that reads a product from one
    side only loses a pair that counts from the other."""
    keys, chunks = _chunked_pages(vault, monkeypatch)
    _serve(monkeypatch, keys, chunks)

    assert _reported(vault, keys) == {(0, 5), (1, 6), (2, 7), (3, 7)}


def test_a_sweep_after_one_page_changed_follows_its_new_vectors(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep keeps the pairs of the pages whose vectors did not change and multiplies only the
    changed page. The pair that the page left must go, and the pair that it joined must come."""
    keys, chunks = _chunked_pages(vault, monkeypatch)
    _serve(monkeypatch, keys, chunks)
    _reported(vault, keys)

    chunks[3][0] = _toward(4, 0.6, 14)  # page 3 rewritten: away from page 7, toward page 4
    _serve(monkeypatch, keys, chunks)

    assert _reported(vault, keys) == {(0, 5), (1, 6), (2, 7), (3, 4)}


def test_audit_queues_snapshot_claims_and_drops_a_label_after_claim_edit(
    vault: Path, monkeypatch
) -> None:
    import sensing_fixture as sf

    from exomem import claims, sensed_model, sensor_worker

    sf.enable(monkeypatch)
    _install(vault, monkeypatch, [("Notes/Insights/claim-a.md", "Notes/Insights/claim-b.md", 0.8)])
    store = sensed_model.ProjectionStore(vault)
    conn = store.connect()
    with store.write(conn):
        sensed_model._set_meta(conn, "seeding", None)
    conn.close()
    monkeypatch.setattr(
        claims,
        "claim_text_for_page",
        lambda *_args, **_kwargs: pytest.fail("audit consulted the unverified claim cache"),
    )
    first = _pair_findings(_run(vault))
    assert len(first) == 1 and "polarity" not in first[0].meta
    conn = sensed_model.open_readonly(vault)
    [job] = list(sensed_model.iter_jobs(conn))
    conn.close()
    assert job.request.input_version == 2
    assert [item.extractor for item in job.inputs] == ["page-claim-v1", "page-claim-v1"]
    assert sf.sense(vault, sf.StubInstrument({job.texts: "contradicts"})) == sensor_worker.EXIT_IDLE
    labelled = _pair_findings(_run(vault))[0]
    assert labelled.meta["polarity"] == "contradict" and labelled.meta["polarity_reading"]
    assert labelled.meta["signal_version"] == first[0].meta["signal_version"]
    path = vault / first[0].paths[0]
    path.write_text(path.read_text().replace(_BODY, "A materially changed current claim."))
    find_module.clear_cache()
    edited = _pair_findings(_run(vault))[0]
    assert edited.meta["signal_version"] != labelled.meta["signal_version"]
    assert "polarity" not in edited.meta
    conn = sensed_model.open_readonly(vault)
    [current_job] = list(sensed_model.iter_jobs(conn))
    conn.close()
    assert current_job.request_key == job.request_key
    assert current_job.request.input_key != job.request.input_key


def test_audit_consumes_current_ledger_evidence_while_projection_is_unavailable(
    vault: Path, monkeypatch
) -> None:
    import sensing_fixture as sf

    from exomem import sensed_model, sensor_worker

    sf.enable(monkeypatch)
    _install(
        vault, monkeypatch, [("Notes/Insights/ledger-a.md", "Notes/Insights/ledger-b.md", 0.8)]
    )
    store = sensed_model.ProjectionStore(vault)
    conn = store.connect()
    with store.write(conn):
        sensed_model._set_meta(conn, "seeding", None)
    conn.close()
    _run(vault)
    assert sf.sense(vault, sf.StubInstrument()) == sensor_worker.EXIT_IDLE
    store.wipe()
    finding = _pair_findings(_run(vault))[0]
    assert finding.meta["polarity"] == "neutral"
    assert finding.meta["polarity_reading"]
    assert not sensed_model.projection_path(vault).exists()


def test_audit_captures_the_actual_published_release_scope_before_parsing(
    vault: Path, monkeypatch
) -> None:
    import sensing_fixture as sf

    from exomem import published_admission, sensed_model
    from exomem.governance import egress

    sf.enable(monkeypatch)
    store = sensed_model.ProjectionStore(vault)
    conn = store.connect()
    with store.write(conn):
        sensed_model._set_meta(conn, "seeding", None)
    conn.close()
    identity = ("release", ("admitted-scope",))
    keep = published_admission.AdmittedPredicate(
        published_admission.Admission(lambda: identity, lambda _path, _publish: (True, True))
    )
    monkeypatch.setattr(egress, "restricted_release_filter", lambda _root: keep)
    captured = []
    capture = sensed_model.capture_scope

    def before_parse(*args, **kwargs):
        token = capture(*args, **kwargs)
        captured.append(token)
        return token

    def parse(_kb, _root, *, keep):
        assert keep is expected_keep
        assert len(captured) == 1 and isinstance(captured[0], sensed_model.ScopeToken)
        return []

    expected_keep = keep
    monkeypatch.setattr(sensed_model, "capture_scope", before_parse)
    monkeypatch.setattr(audit_module, "_parse_all", parse)
    assert _run(vault) == []
    assert not captured[0].invocation


def test_completed_empty_audit_scope_retires_obsolete_candidates(vault: Path, monkeypatch) -> None:
    import sensing_fixture as sf

    from exomem import sensed_model

    sf.enable(monkeypatch)
    _install(
        vault, monkeypatch, [("Notes/Insights/active-a.md", "Notes/Insights/active-b.md", 0.8)]
    )
    store = sensed_model.ProjectionStore(vault)
    conn = store.connect()
    with store.write(conn):
        sensed_model._set_meta(conn, "seeding", None)
    conn.close()
    [finding] = _pair_findings(_run(vault))
    retired = vault / finding.paths[0]
    retired.write_text(retired.read_text().replace("status: active", "status: superseded"))
    find_module.clear_cache()
    assert _pair_findings(_run(vault)) == []
    conn = sensed_model.open_readonly(vault)
    assert list(sensed_model.iter_jobs(conn)) == []
    conn.close()
