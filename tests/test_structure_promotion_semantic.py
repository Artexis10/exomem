"""Semantic scope-divergence sensor: geometry decides, vocabulary only labels.

The lexical v1 advisory (`structure_promotion`) compares a page's declared
vocabulary against its units' terms. It is blind precisely where divergence
hides behind shared vocabulary — the 2026-08-29 case, where a licence-
administration note absorbed stopping-physics analysis and every unit still
carried the parent domain's words.

These fixtures build the geometry directly: units are placed on an orthonormal
basis so that within-group cosine, between-group cosine, and group mass are
exact, known quantities rather than artefacts of a real embedder. Every angle is
derived FROM the module's own thresholds, so moving a PROVISIONAL constant moves
the fixtures with it and the test intent survives (design D3).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import audit as audit_module
from exomem import embedding_index, semantic_index, structure_promotion
from exomem import find as find_module

CATEGORY = "scope_divergence_semantic"


@pytest.fixture(autouse=True)
def _read_stored_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the suite-wide embedding kill switch for this module.

    `conftest` sets `EXOMEM_DISABLE_EMBEDDINGS=1` so the suite never pays for the
    bge-base load. This sensor loads no model — it reads vectors the pipeline
    already stored — so the switch only stands between these fixtures and the
    code under test. The sweep still HONOURS the switch in production, and
    `test_sweep_honours_the_embedding_kill_switch` is what holds that.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)


# --------------------------------------------------------------- synthetic geometry


def _axis(index: int) -> np.ndarray:
    """One orthonormal basis vector of the sidecar's vector space."""
    if not 0 <= index < embedding_index.VECTOR_DIM:
        raise AssertionError(
            f"fixture axis {index} is outside the {embedding_index.VECTOR_DIM}-dim "
            "vector space; give the group a lower offset"
        )
    vector = np.zeros(embedding_index.VECTOR_DIM, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _member(base: int, offset: int, cohesion: float) -> np.ndarray:
    """A unit vector whose cosine with any sibling sharing `base` is exactly `cohesion`.

    ``v_i = sqrt(c)·e_base + sqrt(1-c)·e_i`` with distinct ``e_i``: unit norm, and
    ``v_i · v_j = c`` for ``i != j``. Two groups built on different bases with
    disjoint member axes are exactly orthogonal, so their separation cosine is 0.
    """
    vector = np.sqrt(cohesion) * _axis(base) + np.sqrt(1.0 - cohesion) * _axis(offset)
    return vector.astype(np.float32)


def _group(base: int, first_offset: int, count: int, cohesion: float) -> list[np.ndarray]:
    return [_member(base, first_offset + i, cohesion) for i in range(count)]


def _dispersed(first_offset: int, count: int) -> list[np.ndarray]:
    """Mutually orthogonal units: a page whose own material is not one cluster.

    Needed to isolate the MASS gate. When the remainder is itself a tight group,
    lowering the mass threshold promotes the remainder to a candidate too, the
    page is left with nothing outside a candidate group, and retained scope
    rejects it — so the page stays quiet either way and the mass gate proves
    nothing. Dispersed identity units are singletons at any threshold, so mass is
    the only thing deciding.
    """
    return [_axis(first_offset + i) for i in range(count)]


#: Comfortably inside / outside the cohesion gate, expressed against the constant
#: itself so a threshold move does not silently invert a fixture's meaning.
def _cohesive() -> float:
    from exomem import structure_promotion_semantic as sensor

    return min(sensor.COHESION_MIN_COSINE + 0.06, 0.99)


def _incohesive() -> float:
    """Loose enough to fail cohesion, tight enough to still form one group."""
    from exomem import structure_promotion_semantic as sensor

    return (sensor.COHESION_MIN_COSINE + sensor.LINK_MIN_COSINE) / 2.0 - 0.02


# ------------------------------------------------------------------- vault fixtures


def _write_page(
    root: Path,
    rel_path: str,
    *,
    title: str,
    tags: list[str],
    units: list[tuple[str, str, list[str]]],
    page_type: str = "insight",
    exomem_id: str = "11111111-1111-4111-8111-111111111111",
) -> Path:
    """Write a compiled page whose units carry anchors (so each gets a `unit_ref`)."""
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"type: {page_type}",
        f"title: {title}",
        f"tags: [{', '.join(tags)}]",
        f"exomem_id: {exomem_id}",
        "updated: 2026-08-29",
        "---",
        f"# {title}",
        "",
    ]
    for anchor, content, unit_tags in units:
        rendered = " ".join(f"#{tag}" for tag in unit_tags)
        lines.append(f"- [observation] {content} {rendered} ^{anchor}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    find_module.clear_cache()
    return path


def _seed_vectors(root: Path, page: Path, vectors: list[np.ndarray]) -> None:
    """Seed `semantic_unit_vectors` through the real write seam. No embedder."""
    state = semantic_index.build_parent_index_state(root, page)
    matrix = np.stack(vectors).astype(np.float32)
    embedding_index.EmbeddingIndex(root).upsert_semantic_units(
        state, matrix, page.stat().st_mtime
    )


#: The 2026-08-29 shape. Four units hold the page's declared subject; five form a
#: separate vector group. EVERY unit carries the parent domain's vocabulary, and
#: each divergent unit carries as many in-scope terms as out-of-scope ones, so the
#: LEXICAL detector counts it as retained and never speaks — that blindness is
#: asserted in the acceptance test, not assumed.
#:
#: The divergent group carries `CLUSTER_MIN_TERMS` distinguishing terms because
#: v1's term floor applies to the label (design D5); a group that cannot clear it
#: has too little vocabulary of its own to route by.
_SHARED_TAGS = ("driving-licence", "liikluslab", "eksam")
_PAGE_TAGS = ["driving-licence", "liikluslab", "eksam"]
_DIVERGENT_EXTRA = ("braking", "stopping", "deceleration", "grip")
_DIVERGENT_LABEL = sorted(_DIVERGENT_EXTRA)

_IDENTITY_UNITS = [
    ("a1", "renewal window opens sixty days out", list(_SHARED_TAGS)),
    ("a2", "medical certificate precedes the booking", list(_SHARED_TAGS)),
    ("a3", "exam slots release on a weekly cadence", list(_SHARED_TAGS)),
    ("a4", "fee is paid before the slot is confirmed", list(_SHARED_TAGS)),
]
_DIVERGENT_UNITS = [
    ("b1", "stopping distance grows with the square of speed", [*_SHARED_TAGS, *_DIVERGENT_EXTRA]),
    ("b2", "reaction time dominates at low speed", [*_SHARED_TAGS, *_DIVERGENT_EXTRA]),
    ("b3", "wet tarmac lengthens the braking phase", [*_SHARED_TAGS, *_DIVERGENT_EXTRA]),
    ("b4", "tyre compound changes the deceleration curve", [*_SHARED_TAGS, *_DIVERGENT_EXTRA]),
    ("b5", "load transfer limits usable grip", [*_SHARED_TAGS, *_DIVERGENT_EXTRA]),
]


def _divergent_vault(
    root: Path,
    *,
    identity_units=None,
    divergent_units=None,
    divergent_cohesion: float | None = None,
    tags: list[str] | None = None,
    title: str = "Driving licence administration",
    page_type: str = "insight",
    rel_path: str = "Knowledge Base/Notes/licence.md",
) -> Path:
    identity_units = _IDENTITY_UNITS if identity_units is None else identity_units
    divergent_units = _DIVERGENT_UNITS if divergent_units is None else divergent_units
    cohesion = _cohesive() if divergent_cohesion is None else divergent_cohesion
    page = _write_page(
        root,
        rel_path,
        title=title,
        tags=list(_PAGE_TAGS) if tags is None else tags,
        units=list(identity_units) + list(divergent_units),
        page_type=page_type,
    )
    vectors = _group(0, 100, len(identity_units), _cohesive()) + _group(
        1, 200, len(divergent_units), cohesion
    )
    _seed_vectors(root, page, vectors)
    return page


def _findings(root: Path) -> list:
    report = audit_module.audit(root, categories=[CATEGORY])
    return [f for f in report.findings if f.category == CATEGORY]


# ---------------------------------------------------------------- 1.1 the seam


def test_seeded_unit_vectors_read_back_through_the_corpus_accessor(tmp_path: Path) -> None:
    """1.1 — the synthetic-geometry seam is real: what is written is what is read.

    Seeding goes through the production write seam (`upsert_semantic_units`) and
    read-back goes through the new corpus accessor, so a fixture can never pass
    against a shape the pipeline does not actually store.
    """
    page = _divergent_vault(tmp_path)
    state = semantic_index.build_parent_index_state(tmp_path, page)

    grouped = embedding_index.EmbeddingIndex(tmp_path).all_semantic_unit_vectors()

    assert set(grouped) == {state.path}
    rows = grouped[state.path]
    assert [row.unit_ref for row in rows] == [u.unit_ref for u in state.document.units]
    assert [row.source_order for row in rows] == list(range(len(state.document.units)))
    # The geometry survives the round trip exactly — this is what the gates read.
    expected = _group(0, 100, 4, _cohesive()) + _group(1, 200, 5, _cohesive())
    for row, want in zip(rows, expected, strict=True):
        assert row.vector.shape == (embedding_index.VECTOR_DIM,)
        np.testing.assert_allclose(row.vector, want, rtol=0, atol=1e-6)


def test_corpus_accessor_groups_every_parent_and_pages_the_read(tmp_path: Path) -> None:
    """One corpus-level read returns every parent, across more rows than one batch."""
    first = _divergent_vault(tmp_path)
    second = _write_page(
        tmp_path,
        "Knowledge Base/Notes/other.md",
        title="Second page",
        tags=["second"],
        units=[(f"c{i}", f"unit {i}", ["second"]) for i in range(3)],
        exomem_id="22222222-2222-4222-8222-222222222222",
    )
    _seed_vectors(tmp_path, second, _group(2, 300, 3, _cohesive()))

    index = embedding_index.EmbeddingIndex(tmp_path)
    grouped = index.all_semantic_unit_vectors()
    assert len(grouped) == 2
    assert sum(len(rows) for rows in grouped.values()) == 12

    # Pagination is an internal batching detail, never a truncation.
    batched = index.all_semantic_unit_vectors(batch_size=2)
    assert {path: [r.unit_ref for r in rows] for path, rows in batched.items()} == {
        path: [r.unit_ref for r in rows] for path, rows in grouped.items()
    }
    assert first.exists() and second.exists()


# ------------------------------------------------- 1.2 the 2026-08-29 acceptance case


def test_shared_vocabulary_divergence_is_detected(tmp_path: Path) -> None:
    """1.2 — the acceptance fixture. Vocabulary hides it; geometry does not."""
    page = _divergent_vault(tmp_path)

    # The premise: the shipped LEXICAL detector is blind to this page. If this
    # ever starts firing, the fixture no longer demonstrates what it claims.
    assert structure_promotion.suggest_for_page(tmp_path, "Knowledge Base/Notes/licence.md") is None

    findings = _findings(tmp_path)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.path == "Knowledge Base/Notes/licence.md"
    assert finding.meta["reasons"] == [structure_promotion_semantic_reason()]
    assert finding.meta["off_scope_units"] == 5
    # Extractive labels drawn from the divergent group's OWN units.
    assert finding.meta["cluster_terms"] == _DIVERGENT_LABEL
    assert page.exists()


def structure_promotion_semantic_reason() -> str:
    from exomem import structure_promotion_semantic as sensor

    return sensor.REASON_SEMANTIC_CLUSTER


# ----------------------------------------------------------- 1.3 synonym-swap survival


def test_advisory_survives_a_total_vocabulary_swap(tmp_path: Path) -> None:
    """1.3 — same geometry, every recurring term replaced. The signal is structural."""
    swapped_shared = ["permis-conduire", "autokool", "sooit"]
    swapped_extra = ["freinage", "arret", "ralenti", "adherence"]
    swapped_identity = [
        (anchor, content, list(swapped_shared))
        for anchor, content, _tags in _IDENTITY_UNITS
    ]
    swapped_divergent = [
        (anchor, content, [*swapped_shared, *swapped_extra])
        for anchor, content, _tags in _DIVERGENT_UNITS
    ]
    _divergent_vault(
        tmp_path,
        identity_units=swapped_identity,
        divergent_units=swapped_divergent,
        tags=list(swapped_shared),
        title="Permis de conduire administration",
    )

    findings = _findings(tmp_path)
    assert len(findings) == 1
    # The labels follow the new vocabulary; the DETECTION did not depend on it.
    assert findings[0].meta["cluster_terms"] == sorted(swapped_extra)


# ------------------------------------------------------------------- 1.4 the twins


def test_bounded_scope_page_stays_quiet(tmp_path: Path) -> None:
    """Twin 1 — the SAME page vocabulary as the firing fixture, one coherent subject.

    Word-for-word identical to the acceptance fixture, including the divergent
    group's `braking/stopping/deceleration/grip` tags; only the geometry differs,
    with every unit on one base. If this fires, the sensor is reading vocabulary
    rather than structure — which is the exact failure f20 pre-registers against.
    """
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/bounded.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=list(_IDENTITY_UNITS) + list(_DIVERGENT_UNITS),
    )
    _seed_vectors(tmp_path, page, _group(0, 100, 9, _cohesive()))
    assert _findings(tmp_path) == []

    # The contrast is the point, and it has to be in the same test or neither half
    # proves anything: the SAME page, the SAME vocabulary, split geometry — fires.
    # Anything that stops the gates reading geometry breaks this pair.
    split = _write_page(
        tmp_path,
        "Knowledge Base/Notes/bounded.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=list(_IDENTITY_UNITS) + list(_DIVERGENT_UNITS),
    )
    _seed_vectors(
        tmp_path,
        split,
        _group(0, 100, len(_IDENTITY_UNITS), _cohesive())
        + _group(1, 200, len(_DIVERGENT_UNITS), _cohesive()),
    )
    assert len(_findings(tmp_path)) == 1


def test_sub_mass_tangent_stays_quiet(tmp_path: Path) -> None:
    """Twin 2 — a real second group, one unit short of child-note mass.

    The page's own units are dispersed rather than clustered, so the mass gate is
    the ONLY thing standing between this page and an advisory: drop the threshold
    by one and it fires.
    """
    tangent = _DIVERGENT_UNITS[: structure_promotion.CLUSTER_MIN_UNITS - 1]
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/licence.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=list(_IDENTITY_UNITS) + list(tangent),
    )
    _seed_vectors(
        tmp_path,
        page,
        _dispersed(100, len(_IDENTITY_UNITS)) + _group(1, 200, len(tangent), _cohesive()),
    )
    assert _findings(tmp_path) == []


def test_a_page_whose_own_body_also_reaches_mass_is_not_judged(tmp_path: Path) -> None:
    """A consequence of design D2 worth stating out loud, not discovering later.

    "Retained scope" counts units outside EVERY candidate group, and any group at
    child-note mass is a candidate. So when a page's own body is itself a cohesive
    block of five or more units, both blocks become candidates, nothing is left
    outside them, and the page is not judged — even though a reader would call one
    of them the page and the other a divergence.

    This pins the behaviour the design specifies. Whether the identity remainder
    should instead be the LARGEST group is a calibration question the design flags
    for review (D7, "the identity-centroid definition when groups overlap"); it is
    deliberately not decided here.
    """
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/big.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=[
            (f"h{i}", f"admin detail {i}", list(_SHARED_TAGS))
            for i in range(6)
        ]
        + list(_DIVERGENT_UNITS),
    )
    _seed_vectors(
        tmp_path,
        page,
        _group(0, 100, 6, _cohesive()) + _group(1, 200, len(_DIVERGENT_UNITS), _cohesive()),
    )
    assert _findings(tmp_path) == []


def test_declared_hub_stays_quiet(tmp_path: Path) -> None:
    """Twin 3 — breadth is declared, so dispersion is the page working as intended."""
    _divergent_vault(tmp_path, tags=["driving-licence", "hub"])
    assert _findings(tmp_path) == []


def test_heterogeneous_log_stays_quiet(tmp_path: Path) -> None:
    """Twin 4 — the natural FP hazard: a log page is heterogeneous by design."""
    _divergent_vault(tmp_path, rel_path="Knowledge Base/Notes/log.md")
    assert _findings(tmp_path) == []


# ---------------------------------------------------------------- absence semantics


def test_page_without_stored_vectors_is_not_judged(tmp_path: Path) -> None:
    """Absence of evidence is never evidence of divergence."""
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/licence.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=list(_IDENTITY_UNITS) + list(_DIVERGENT_UNITS),
    )
    # No _seed_vectors call: the page is parsed, but the sidecar holds nothing.
    assert _findings(tmp_path) == []


def test_vector_rows_that_no_longer_resolve_are_dropped_not_guessed(tmp_path: Path) -> None:
    """A stale generation loses its judgment, not its honesty (design D5)."""
    _divergent_vault(tmp_path)
    assert len(_findings(tmp_path)) == 1

    # Rewrite the page so the divergent units carry NEW anchors, WITHOUT
    # reindexing. The page still holds nine tagged units and the sidecar still
    # holds nine vectors — but five of those vectors now belong to unit refs the
    # current parse does not contain. Only four units can be judged, which is
    # under child-note mass, so nothing is said.
    #
    # Disclosure (same spirit as the log-twin note below): rewriting anchors
    # also moves parent_source_hash and therefore the generation, so the
    # generation gate skips this page before shape_from_parse ever runs — this
    # test would stay green with the drop branch deleted. It pins the
    # audit-level outcome (stale page says nothing); the drop branch itself is
    # held by test_shape_from_parse_drops_unresolvable_refs_rather_than_pairing_them.
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/licence.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=list(_IDENTITY_UNITS)
        + [
            (f"restated-{anchor}", content, tags)
            for anchor, content, tags in _DIVERGENT_UNITS
        ],
    )
    assert _findings(tmp_path) == []


# ------------------------------------------------------------------ the gate edges


def test_group_that_forms_but_does_not_cohere_is_rejected(tmp_path: Path) -> None:
    """Cohesion is its own gate: linking is not the same as being one thing."""
    _divergent_vault(tmp_path, divergent_cohesion=_incohesive())
    assert _findings(tmp_path) == []


def test_page_with_no_retained_scope_is_rejected(tmp_path: Path) -> None:
    """Without a remainder the page has been renamed, not outgrown."""
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/moved.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=[
            (f"e{i}", f"stopping analysis {i}", [*_SHARED_TAGS, *_DIVERGENT_EXTRA])
            for i in range(6)
        ],
    )
    _seed_vectors(tmp_path, page, _group(1, 200, 6, _cohesive()))
    assert _findings(tmp_path) == []


def test_oversized_page_is_skipped_with_a_named_note(tmp_path: Path) -> None:
    """The O(units^2) pass is bounded, and the skip says so rather than staying silent."""
    from exomem import structure_promotion_semantic as sensor

    count = sensor.MAX_JUDGED_UNITS + 1
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/huge.md",
        title="Driving licence administration",
        tags=list(_PAGE_TAGS),
        units=[
            (f"f{i}", f"unit {i}", [*_SHARED_TAGS, *_DIVERGENT_EXTRA])
            for i in range(count)
        ],
    )
    # Axes 2.. so `MAX_JUDGED_UNITS + 1` members still fit the real vector space;
    # the cap is checked before any geometry is read, but the fixture stays honest.
    _seed_vectors(
        tmp_path,
        page,
        _group(0, 2, 4, _cohesive()) + _group(1, 6, count - 4, _cohesive()),
    )

    findings = _findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].meta["skipped"] == "unit_count_over_cap"
    assert findings[0].meta["units"] == count
    assert findings[0].severity == "info"


# -------------------------------------------------------- 2.3 resolve by state change


def _destination(root: Path, rel_path: str, title: str, tags: list[str], page_id: str) -> Path:
    return _write_page(
        root,
        rel_path,
        title=title,
        tags=tags,
        units=[("g1", "a durable observation", tags)],
        exomem_id=page_id,
    )


def test_covering_destinations_resolve_the_advisory_by_state_change(tmp_path: Path) -> None:
    """2.3 — acting on the advice silences it; undoing the action brings it back."""
    _divergent_vault(tmp_path)
    assert len(_findings(tmp_path)) == 1

    home = _destination(
        tmp_path,
        "Knowledge Base/Notes/braking.md",
        "Braking and stopping distance",
        ["braking", "stopping"],
        "33333333-3333-4333-8333-333333333333",
    )
    assert _findings(tmp_path) == []

    home.unlink()
    find_module.clear_cache()
    assert len(_findings(tmp_path)) == 1


def test_a_single_shared_term_is_not_a_home(tmp_path: Path) -> None:
    """One incidental tag collision must not scavenge a real divergence quiet."""
    _divergent_vault(tmp_path)
    _destination(
        tmp_path,
        "Knowledge Base/Notes/incidental.md",
        "Braking only",
        ["braking"],
        "44444444-4444-4444-8444-444444444444",
    )
    assert len(_findings(tmp_path)) == 1


def test_sweep_honours_the_embedding_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that has switched embeddings off gets no vector sweep."""
    _divergent_vault(tmp_path)
    assert len(_findings(tmp_path)) == 1

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    assert _findings(tmp_path) == []


# ------------------------------------------------------------------ 3.x delivery


def test_category_is_registered_and_triageable(tmp_path: Path) -> None:
    """3.1/3.2 — the category exists, and a disposition may be recorded against it."""
    from exomem import attention as attention_module
    from exomem import review_state as review_state_module

    assert CATEGORY in audit_module.ALL_CATEGORIES
    assert CATEGORY in attention_module.ATTENTION_CATEGORIES
    assert CATEGORY in review_state_module.registered_families()
    # Opt-in: a brand-new structural queue must not displace the daily surface.
    assert CATEGORY not in attention_module.DEFAULT_ATTENTION_CATEGORIES


def test_finding_fingerprint_binds_to_the_label_set_not_the_page_bytes(tmp_path: Path) -> None:
    """3.1 — material change is a label-set change (design D4/D7).

    An incidental edit that leaves the divergent group's vocabulary alone must not
    reopen a dismissal, so the signal version is composed over the label terms.
    """
    _divergent_vault(tmp_path)
    before = _findings(tmp_path)[0].meta["signal_version"]

    # Same geometry, same labels, different bytes on an untouched identity unit.
    edited_identity = [
        ("a1", "renewal window opens sixty days out, revised", list(_SHARED_TAGS)),
        *_IDENTITY_UNITS[1:],
    ]
    _divergent_vault(tmp_path, identity_units=edited_identity)
    assert _findings(tmp_path)[0].meta["signal_version"] == before

    # ...and the other half, without which "stable" would be satisfied by a
    # constant: a different divergent group MUST get a different version.
    _divergent_vault(
        tmp_path,
        divergent_units=[
            (anchor, content, [*_SHARED_TAGS, "insurance", "premium", "renewal", "broker"])
            for anchor, content, _tags in _DIVERGENT_UNITS
        ],
    )
    assert _findings(tmp_path)[0].meta["signal_version"] != before


# ------------------------------------------------- the sensor's own exclusion rules
#
# The audit's eligibility gate already drops `log.md` before the sensor is asked
# (`_is_active_compiled_rw`), so the audit-level twin above cannot exercise the
# sensor's OWN navigation rule — it would pass with that rule deleted. These
# address the sensor directly, so each exclusion is held by something.


def _shape(**overrides):
    """A shape that fires, so an exclusion test is measuring the exclusion."""
    from exomem import structure_promotion_semantic as sensor

    units = []
    for offset, (anchor, _content, tags) in enumerate(_IDENTITY_UNITS):
        units.append(sensor.JudgedUnit(f"ref-{anchor}", tuple(tags), _member(0, 100 + offset, _cohesive())))
    for offset, (anchor, _content, tags) in enumerate(_DIVERGENT_UNITS):
        units.append(sensor.JudgedUnit(f"ref-{anchor}", tuple(tags), _member(1, 200 + offset, _cohesive())))
    fields = {
        "title": "Driving licence administration",
        "tags": tuple(_SHARED_TAGS),
        "projects": (),
        "basename": "licence.md",
        "path": "Knowledge Base/Notes/licence.md",
        "units": tuple(units),
    }
    fields.update(overrides)
    return sensor.SemanticPageShape(**fields)


def test_sensor_fires_on_the_bare_shape() -> None:
    """The control: without it, every exclusion test below could pass vacuously."""
    from exomem import structure_promotion_semantic as sensor

    advisory = sensor.detect(_shape())
    assert advisory is not None
    assert advisory["reasons"] == [sensor.REASON_SEMANTIC_CLUSTER]
    assert advisory["cluster_terms"] == _DIVERGENT_LABEL


@pytest.mark.parametrize("basename", sorted(structure_promotion.NAVIGATION_BASENAMES))
def test_sensor_exempts_navigation_pages(basename: str) -> None:
    """Navigation pages are containers by definition; dispersion is their job."""
    from exomem import structure_promotion_semantic as sensor

    assert sensor.detect(_shape(basename=basename)) is None


@pytest.mark.parametrize("tag", sorted(structure_promotion.BREADTH_TAGS))
def test_sensor_exempts_declared_breadth(tag: str) -> None:
    """A page that announces breadth has already answered the question."""
    from exomem import structure_promotion_semantic as sensor

    assert sensor.detect(_shape(tags=(*_SHARED_TAGS, tag))) is None


def test_sensor_reuses_v1_child_note_mass() -> None:
    """Mass is v1's constant, not a second opinion about what a note deserves."""
    from exomem import structure_promotion_semantic as sensor

    shape = _shape()
    trimmed = shape.units[: len(_IDENTITY_UNITS) + structure_promotion.CLUSTER_MIN_UNITS - 1]
    assert sensor.detect(sensor.SemanticPageShape(
        title=shape.title, tags=shape.tags, projects=shape.projects,
        basename=shape.basename, path=shape.path, units=tuple(trimmed),
    )) is None


def _tilted_member(tilt: float, offset: int, cohesion: float) -> np.ndarray:
    """A member whose group base is tilted `tilt` away from the identity base.

    Separation has to be provable on its own, and that needs geometry no other
    gate would have rejected first: a group built on the SAME base links straight
    into the remainder and fails retained scope instead, which would leave the
    separation gate untested while the test still passed. Tilting the base
    decouples the two — members stay below the link threshold (so the group forms
    separately and keeps a remainder) while the CENTROIDS stay close enough to be
    the same subject.
    """
    base = tilt * _axis(0) + np.sqrt(1.0 - tilt**2) * _axis(1)
    vector = np.sqrt(cohesion) * base + np.sqrt(1.0 - cohesion) * _axis(offset)
    return vector.astype(np.float32)


def test_sensor_requires_a_group_to_separate_from_the_page() -> None:
    """A group that points where the page already points is a sub-topic, not a rival.

    The arithmetic is deliberate: with cohesion c on both sides and a base tilt t,
    cross-group member cosine is c·t and centroid cosine is ~0.88·t. At c=0.66 and
    t=0.6 that is 0.40 (below the 0.50 link threshold, so the group is genuinely
    separate and the page keeps its remainder) against ~0.53 (above the 0.35
    separation ceiling). Only the separation gate can reject this page.
    """
    from exomem import structure_promotion_semantic as sensor

    units = list(_shape().units[: len(_IDENTITY_UNITS)])
    for offset, (anchor, _content, tags) in enumerate(_DIVERGENT_UNITS):
        units.append(
            sensor.JudgedUnit(f"ref-{anchor}", tuple(tags), _tilted_member(0.6, 300 + offset, _cohesive()))
        )
    shape = _shape(units=tuple(units))

    # Every OTHER gate is satisfied, so a pass here can only come from separation.
    matrix = sensor._unit_matrix(shape.units)
    similarity = matrix @ matrix.T
    groups = [g for g in sensor._groups(similarity, sensor.LINK_MIN_COSINE) if len(g) >= structure_promotion.CLUSTER_MIN_UNITS]
    assert len(groups) == 1, "the divergent group must form on its own"
    claimed = set(groups[0])
    remainder = [i for i in range(len(shape.units)) if i not in claimed]
    assert len(remainder) >= structure_promotion.MIN_RETAINED_UNITS, "the page must retain its scope"
    assert sensor._cohesion(similarity, groups[0]) >= sensor.COHESION_MIN_COSINE, "the group must cohere"
    separation = float(sensor._centroid(matrix, groups[0]) @ sensor._centroid(matrix, remainder))
    assert separation > sensor.SEPARATION_MAX_COSINE, "and it must NOT separate"

    assert sensor.detect(shape) is None


# -------------------------------------------------------------- 3.2 S6 integration


def _items(root: Path, *, state: str = "open") -> list:
    from exomem import attention as attention_module

    report = attention_module.attention(
        root, categories=[CATEGORY], limit=0, state=state, record_surfacing=False
    )
    return list(report.items)


def test_family_disposition_off_silences_the_queue(tmp_path: Path) -> None:
    """A user who says "stop suggesting this kind of thing" is obeyed."""
    from exomem import review_state as review_state_module

    _divergent_vault(tmp_path)
    assert len(_items(tmp_path)) == 1

    review_state_module.ReviewStateStore(tmp_path).set_disposition(
        CATEGORY, "off", why="intentional: structural advice is not what I want here"
    )
    assert _items(tmp_path) == []


def test_dismissal_survives_an_incidental_edit_and_reopens_on_a_label_change(
    tmp_path: Path,
) -> None:
    """3.2 — the material-change contract, end to end.

    A dismissal binds to the fingerprint, and the fingerprint is composed over the
    label terms. So an unrelated edit must NOT resurrect the advice, and a genuinely
    different divergent group must not inherit the decision.
    """
    from exomem import review_state as review_state_module

    _divergent_vault(tmp_path)
    item = _items(tmp_path)[0]
    review_state_module.apply_for_item(
        tmp_path, item, action="dismiss", why="intentional: this material belongs here"
    )
    assert _items(tmp_path) == []

    # Incidental edit: same geometry, same divergent vocabulary, different bytes.
    _divergent_vault(
        tmp_path,
        identity_units=[
            ("a1", "renewal window opens sixty days out, revised", list(_SHARED_TAGS)),
            *_IDENTITY_UNITS[1:],
        ],
    )
    assert _items(tmp_path) == [], "an unrelated edit must not reopen a dismissal"

    # Material change: the divergent group is now about something else.
    _divergent_vault(
        tmp_path,
        divergent_units=[
            (anchor, content, [*_SHARED_TAGS, "insurance", "premium", "renewal", "broker"])
            for anchor, content, _tags in _DIVERGENT_UNITS
        ],
    )
    reopened = _items(tmp_path)
    assert len(reopened) == 1, "a different divergent group is a different signal"


# ------------------------------------------------------------------ 4.2 cost bound


def test_sweep_loads_the_vector_table_exactly_once_regardless_of_page_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.2 — amortization is the whole reason this runs in the audit.

    The shape being prevented is a per-page `WHERE parent_path = ?`, which turns a
    sweep into one query per compiled page. Counting the accessor across a
    multi-page vault is what makes "exactly one load per run" provable rather than
    asserted: with a per-page query the count would track the page count.
    """
    from exomem import embedding_index as embedding_index_module

    for index in range(3):
        page = _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page{index}.md",
            title="Driving licence administration",
            tags=list(_PAGE_TAGS),
            units=list(_IDENTITY_UNITS) + list(_DIVERGENT_UNITS),
            exomem_id=f"5555555{index}-5555-4555-8555-555555555555",
        )
        _seed_vectors(
            tmp_path,
            page,
            _group(0, 100, len(_IDENTITY_UNITS), _cohesive())
            + _group(1, 200, len(_DIVERGENT_UNITS), _cohesive()),
        )

    calls = []
    original = embedding_index_module.EmbeddingIndex.all_semantic_unit_vectors

    def counted(self, **kwargs):
        calls.append(1)
        return original(self, **kwargs)

    monkeypatch.setattr(
        embedding_index_module.EmbeddingIndex, "all_semantic_unit_vectors", counted
    )

    findings = _findings(tmp_path)
    assert len(findings) == 3, "every page must actually be judged"
    assert len(calls) == 1, f"one corpus load per sweep, got {len(calls)}"


def test_sweep_never_reaches_for_the_knn_unit_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kNN path validates parents against Markdown; a sweep must not pay it."""
    from exomem import embedding_index as embedding_index_module

    _divergent_vault(tmp_path)

    def forbidden(self, *args, **kwargs):
        raise AssertionError("the sweep must not call search_semantic_units")

    monkeypatch.setattr(
        embedding_index_module.EmbeddingIndex, "search_semantic_units", forbidden
    )
    assert len(_findings(tmp_path)) == 1


# ------------------------------------------ 2.4 staleness, robustness, term floor


def test_in_place_rewrite_without_reindex_is_not_judged(tmp_path: Path) -> None:
    """Geometry may only be read against the parse it was written for.

    An anchored `unit_ref` is `{parent_ref}#{anchor}` — content-INDEPENDENT. So
    rewriting a unit's text and tags under the same anchor leaves every row
    joinable while the vectors still describe deleted text. Joining by ref alone,
    the sensor relabels the group from the old geometry and the new vocabulary:
    a finding derived from text that no longer exists, whose changed label set
    REOPENS a dismissal the reader already settled.

    The generation is the guard: it binds `parent_source_hash`, so any edit moves
    it and the page drops out of judgment until the pipeline catches up.
    """
    _divergent_vault(tmp_path)
    assert len(_findings(tmp_path)) == 1

    _write_page(
        tmp_path,
        "Knowledge Base/Notes/licence.md",
        title="Driving licence administration",
        tags=_PAGE_TAGS,
        units=list(_IDENTITY_UNITS)
        + [
            (
                anchor,
                "entirely different content",
                # Four off-identity terms, so this page WOULD fire on its new text
                # once reindexed. Fewer and the term floor would silence it and
                # the generation check would never be the thing under test.
                [*_SHARED_TAGS, "checklist", "reminder", "memo", "todo"],
            )
            for anchor, _content, _tags in _DIVERGENT_UNITS
        ],
    )
    assert _findings(tmp_path) == [], "stale geometry must not be relabelled as live"

    # And the page is not silenced forever: once the pipeline catches up, it is
    # judged on the text it actually holds now.
    _seed_vectors(
        tmp_path,
        tmp_path / "Knowledge Base" / "Notes" / "licence.md",
        _group(0, 100, len(_IDENTITY_UNITS), _cohesive())
        + _group(1, 200, len(_DIVERGENT_UNITS), _cohesive()),
    )
    assert _findings(tmp_path)[0].meta["cluster_terms"] == [
        "checklist", "memo", "reminder", "todo",
    ]


def test_units_added_without_reindex_are_not_judged(tmp_path: Path) -> None:
    """Partial vector coverage is a stale generation, not a smaller page.

    Judging the vectored subset would evaluate mass and retained scope over a
    fraction of the page and call the result the page's shape.
    """
    _divergent_vault(tmp_path)
    assert len(_findings(tmp_path)) == 1

    _write_page(
        tmp_path,
        "Knowledge Base/Notes/licence.md",
        title="Driving licence administration",
        tags=_PAGE_TAGS,
        units=list(_IDENTITY_UNITS)
        + list(_DIVERGENT_UNITS)
        + [(f"extra{i}", f"appended later {i}", list(_SHARED_TAGS)) for i in range(6)],
    )
    assert _findings(tmp_path) == []


def test_one_malformed_vector_blob_does_not_silence_the_corpus(tmp_path: Path) -> None:
    """A corrupt row on one page must not cost every other page its judgment.

    `np.frombuffer` raises on a blob whose length is not a multiple of the dtype
    size, and that raise happens BEFORE any shape check. Unguarded it escapes the
    accessor, the sweep's blanket handler swallows it, and the whole category
    silently returns nothing — one bad row anywhere reads as a clean corpus.
    """
    import sqlite3

    _divergent_vault(tmp_path)
    other = _write_page(
        tmp_path,
        "Knowledge Base/Notes/other.md",
        title="Second page",
        tags=["second"],
        units=[(f"c{i}", f"unit {i}", ["second"]) for i in range(3)],
        exomem_id="66666666-6666-4666-8666-666666666666",
    )
    _seed_vectors(tmp_path, other, _group(2, 300, 3, _cohesive()))

    conn = sqlite3.connect(embedding_index.index_paths.sidecar_path(tmp_path))
    try:
        conn.execute(
            "UPDATE semantic_unit_vectors SET vector = ? WHERE parent_path = ?",
            (b"\x01\x02\x03", "Knowledge Base/Notes/other.md"),
        )
        conn.commit()
    finally:
        conn.close()

    rows = embedding_index.EmbeddingIndex(tmp_path).all_semantic_unit_vectors()
    assert "Knowledge Base/Notes/other.md" not in rows, "the corrupt rows drop out"
    assert len(_findings(tmp_path)) == 1, "and the rest of the corpus is still judged"


def test_partial_routing_resolves_rather_than_shrinking_the_label(tmp_path: Path) -> None:
    """v1's term floor applies to what routing leaves behind (design D5).

    Without it, a destination that covers part of a group's vocabulary shrinks the
    label instead of resolving the advice — and because the fingerprint is the
    label set, the shrunken label is a NEW signal that reopens a dismissal the
    reader already settled. Routing that leaves fewer surviving terms than the
    floor resolves the advisory outright; a shrink that stays at or above the
    floor still moves the label set, v1-consistent (design D5).
    """
    _divergent_vault(tmp_path)
    before = _findings(tmp_path)
    assert len(before) == 1
    assert len(before[0].meta["cluster_terms"]) >= structure_promotion.CLUSTER_MIN_TERMS

    # One destination covering two of the label's terms. Two survive, which is
    # under v1's floor, so the advisory resolves instead of re-fingerprinting.
    _destination(
        tmp_path,
        "Knowledge Base/Notes/braking.md",
        "Braking and stopping distance",
        ["braking", "stopping"],
        "77777777-7777-4777-8777-777777777777",
    )
    assert _findings(tmp_path) == []


def test_undeclared_heterogeneous_page_with_a_cohesive_thread_fires(tmp_path: Path) -> None:
    """D6 fixture 6 — undeclared heterogeneity is not an exemption.

    The protected quiet twin is the page that DECLARES breadth. A scratch page
    that never said it was a container, but has grown one coherent durable thread
    at child-note mass, is exactly the case the sensor exists for.
    """
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/scratch.md",
        title="Driving licence administration",
        tags=_PAGE_TAGS,
        units=[
            (f"m{i}", f"unrelated jotting {i}", [*_SHARED_TAGS, f"topic{i}"])
            for i in range(4)
        ]
        + list(_DIVERGENT_UNITS),
    )
    _seed_vectors(
        tmp_path,
        page,
        _dispersed(100, 4) + _group(1, 200, len(_DIVERGENT_UNITS), _cohesive()),
    )

    findings = _findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].meta["cluster_terms"] == _DIVERGENT_LABEL


def test_advisory_reports_its_strength(tmp_path: Path) -> None:
    """The v1-compatible shape carries strength, decided by v1's overlap rule."""
    _divergent_vault(tmp_path)
    finding = _findings(tmp_path)[0]
    assert finding.meta["strength"] in {"strong", "moderate"}
    # This page's group shares the parent domain's vocabulary, so its overlap with
    # the declared identity sits ABOVE v1's mismatch ceiling: moderate, not strong.
    assert finding.meta["strength"] == "moderate"


def test_shape_from_parse_drops_unresolvable_refs_rather_than_pairing_them() -> None:
    """The join is by `unit_ref`, never by position — proven at the seam itself.

    In the audit this is now defence in depth: the generation check rejects any
    page whose vectors were written for an earlier parse. The rule still has to
    hold on its own, because `shape_from_parse` decides which units carry
    geometry, and handing a unit some leftover vector would build the advisory
    on a page that does not exist.
    """
    from exomem import structure_promotion_semantic as sensor

    class _Unit:
        def __init__(self, ref, tags):
            self.unit_ref, self.tags = ref, tags

    # Six parsed units; only four of them have a stored vector.
    units = [_Unit(f"ref-unit-{i}", list(_SHARED_TAGS)) for i in range(6)]
    vectors = {f"ref-unit-{i}": _axis(100 + i) for i in range(4)}
    # Two vectors left over from a parse that no longer exists. Nothing may
    # adopt them: their refs match no unit on the page.
    vectors.update({f"ref-vanished-{i}": _axis(200 + i) for i in range(2)})

    shape = sensor.shape_from_parse(
        path="Knowledge Base/Notes/x.md",
        frontmatter={"title": "X", "tags": list(_PAGE_TAGS)},
        units=units,
        vectors_by_ref=vectors,
    )
    assert [u.unit_ref for u in shape.units] == [f"ref-unit-{i}" for i in range(4)]
    assert len(shape.units) == 4, "the two unvectored units must not be given a leftover"
    for judged in shape.units:
        assert np.array_equal(judged.vector, vectors[judged.unit_ref])
