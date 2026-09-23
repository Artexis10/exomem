"""D1-T9: the hydration family, `upkeep_hydration`.

Newer facts about an entity live on other compiled pages that link it, from at
least two independent origins, and the entity's own page neither links nor
cites them. Detection is SQL over the published graph snapshot: it reads no
Markdown. The route is a curation work item over explicit paths, never a
`review_ref` (whose binding runs a whole-vault audit).
"""

from __future__ import annotations

from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import dreamer, dreamer_families, dreamer_store, find_corpus, freshness


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _candidate(vault: Path, *, state: str = "open") -> dict | None:
    conn = dreamer_store.DreamerStore(vault).connect()
    try:
        row = dreamer_store.DreamerStore.candidate(
            conn, dreamer_store.candidate_id(dreamer_families.HYDRATION_KIND, fx.ENTITY, "")
        )
    finally:
        conn.close()
    if row is None or row["state"] != state:
        return None
    return row


def _quiet(vault: Path) -> None:
    results = fx.run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results


def test_two_independent_newer_unit_links_make_a_candidate(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    row = _candidate(vault)
    assert row is not None
    assert row["family"] == dreamer_families.HYDRATION_FAMILY
    assert row["kind"] == dreamer_families.HYDRATION_KIND
    assert row["subject_path"] == fx.ENTITY
    assert row["measures"]["origins"] == 2
    assert row["measures"]["contributors"] == 2
    contributors = {item["path"] for item in row["evidence"] if item["role"] == "contributor"}
    assert contributors == {fx.CAVITATION, fx.SEAL_WEAR}
    assert len({item["origin"] for item in row["evidence"] if item["role"] == "contributor"}) == 2
    assert all(item["sig"] for item in row["evidence"])

    # The entity page updated after those facts is not behind them.
    fx.edit(vault, fx.ENTITY, fx.entity(updated="2026-06-01"))
    _quiet(vault)
    assert _candidate(vault) is None
    assert _candidate(vault, state="resolved") is not None


def test_one_source_fanned_out_counts_once(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    fx.edit(
        vault,
        fx.SEAL_WEAR,
        fx.insight(
            "Pump seal wear",
            sources=["field-report-one"],
            updated="2026-05-02",
            links="Seal wear on the [[Notes/Entities/orbit-pump]] doubles after a dry start.",
            observation="Seal wear doubles after a dry start.",
        ),
    )
    fx.edit(
        vault,
        fx.INLET,
        fx.insight(
            "Pump inlet pressure",
            sources=["field-report-one"],
            updated="2026-05-03",
            links="Inlet pressure on the [[Notes/Entities/orbit-pump]] falls first.",
            observation="Inlet pressure falls before cavitation begins.",
        ),
    )
    _quiet(vault)
    # Three notes linking the entity, all from one Source: one origin.
    assert _candidate(vault) is None


def test_entity_linking_back_resolves(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    assert _candidate(vault) is not None
    fx.edit(
        vault,
        fx.ENTITY,
        fx.entity(extra="\nSee [[Notes/Insights/pump-seal-wear]] for wear.\n"),
    )
    _quiet(vault)
    assert _candidate(vault) is None
    assert _candidate(vault, state="resolved") is not None


def test_superseded_entity_yields_nothing(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    fx.edit(vault, fx.ENTITY, fx.entity(status="superseded"))
    _quiet(vault)
    assert _candidate(vault) is None


def test_sources_and_evidence_are_not_hydration(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    # The seal-wear note stops linking; a Source page links the entity instead.
    fx.edit(
        vault,
        fx.SEAL_WEAR,
        fx.insight(
            "Pump seal wear",
            sources=["field-report-two"],
            updated="2026-05-02",
            observation="Seal wear doubles after a dry start.",
        ),
        graph=False,
    )
    fx.edit(
        vault,
        fx.SOURCE_THREE,
        fx.source("Field report three")
        + "\nThe [[Notes/Entities/orbit-pump]] was replaced on day two.\n",
        graph=False,
    )
    fx.publish_graph(vault)
    _quiet(vault)
    assert _candidate(vault) is None


def test_route_uses_explicit_paths_never_review_ref(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    route = _candidate(vault)["route"]
    assert route["tool"] == "maintain_memory"
    assert route["args"]["mode"] == "curation"
    assert route["args"]["curation_action"] == "work-item"
    assert "review_ref" not in route["args"]
    paths = route["args"]["paths"]
    assert paths[0] == fx.ENTITY
    assert set(paths[1:]) == {fx.CAVITATION, fx.SEAL_WEAR}
    assert len(paths) <= 8


def test_detection_reads_no_markdown(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)
    monkeypatch.setattr(dreamer_families, "REGISTRY", [dreamer_families.HYDRATION])
    reads: list[str] = []
    real_get = find_corpus.CACHE.get
    monkeypatch.setattr(
        find_corpus.CACHE,
        "get",
        lambda path, root: (reads.append(str(path)), real_get(path, root))[1],
    )
    _quiet(vault)
    assert _candidate(vault) is not None
    assert reads == []


def test_revalidate_reads_only_the_candidate_pages(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    row = _candidate(vault)
    allowed = {fx.ENTITY, *(item["path"] for item in row["evidence"])}
    looked_up: list[str] = []
    real_signature = freshness.live_signature
    monkeypatch.setattr(
        freshness,
        "live_signature",
        lambda root, scope, path: (
            looked_up.append(Path(path).relative_to(vault).as_posix()),
            real_signature(root, scope, path),
        )[1],
    )
    monkeypatch.setattr(
        find_corpus.CACHE, "get", lambda *_a: pytest.fail("revalidation read Markdown")
    )
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    ctx = dreamer_families.Context(vault_root=vault, store=store, conn=conn, now=1.0)
    with store.write(conn):
        dreamer_families.HYDRATION.revalidate(ctx, row)
    ctx.close()
    conn.close()
    assert looked_up
    assert set(looked_up) <= allowed
    assert _candidate(vault)["fingerprint"] == row["fingerprint"]
