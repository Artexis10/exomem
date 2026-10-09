"""D1-T9: the hydration family, `upkeep_hydration`.

Newer facts about an entity live on other compiled pages that link it, from at
least two independent origins, and the entity's own page neither links nor
cites them. Detection uses the published graph and freshly admitted page
eligibility. The route is a curation work item over explicit paths, never a
`review_ref` (whose binding runs a whole-vault audit).
"""

from __future__ import annotations

from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import dreamer, dreamer_families, dreamer_store, freshness


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


def test_an_entity_linked_by_many_older_pages_still_hydrates(tmp_path: Path) -> None:
    """Pages dated before the entity are no contributors, so 64 of them ahead
    in path order do not hide the two newer origins."""
    vault = fx.build(tmp_path, with_graph=False)
    for index in range(64):
        fx.write(
            vault,
            f"{fx.KB}/Notes/Insights/a-old-{index:02d}.md",
            fx.insight(
                f"Old note {index:02d}",
                sources=["field-report-three"],
                updated="2026-01-05",
                links="Checked the [[Notes/Entities/orbit-pump]].",
            ),
        )
    fx.seed(vault)
    fx.publish_graph(vault)
    results = fx.run_to_quiet(vault, limit=200)
    assert all(result.stop_reason != "error" for result in results), results
    assert _candidate(vault)["measures"]["origins"] == 2


def test_a_relation_to_one_of_the_entitys_units_contributes(tmp_path: Path) -> None:
    """A relation target `[[entity#unit]]` lands on the unit; it still links the entity."""
    vault = fx.build(tmp_path, with_graph=False)
    fx.write(
        vault,
        fx.ENTITY,
        fx.entity(
            extra="\n## Observations\n\n- [finding] Runs at 40 litres a minute. ^pump-flow\n"
        ),
    )
    fx.write(
        vault,
        fx.SEAL_WEAR,
        fx.insight(
            "Pump seal wear",
            sources=["field-report-two"],
            updated="2026-05-02",
            observation="Seal wear doubles after a dry start.",
            extra="\n## Relations\n\n- supports [[Notes/Entities/orbit-pump#pump-flow]]\n",
        ),
    )
    fx.seed(vault)
    fx.publish_graph(vault)
    _quiet(vault)
    row = _candidate(vault)
    assert row is not None and row["measures"]["origins"] == 2
    contributors = {item["path"] for item in row["evidence"] if item["role"] == "contributor"}
    assert contributors == {fx.CAVITATION, fx.SEAL_WEAR}


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


def test_unsourced_contributors_count_as_one_origin_between_them(tmp_path: Path) -> None:
    """The graph carries no session key, so pages that declare no Source cannot
    be told apart by conversation: together they are one origin, never two."""
    vault = fx.build(tmp_path)
    for rel, title, updated, links in (
        (fx.CAVITATION, "Pump cavitation", "2026-05-01", "Cavitation on the"),
        (fx.SEAL_WEAR, "Pump seal wear", "2026-05-02", "Seal wear on the"),
    ):
        fx.edit(
            vault,
            rel,
            fx.insight(
                title,
                sources=[],
                updated=updated,
                links=f"{links} [[Notes/Entities/orbit-pump]] is recorded.",
            ),
        )
    _quiet(vault)
    assert _candidate(vault) is None

    # One sourced contributor beside them makes two independent origins.
    fx.edit(
        vault,
        fx.INLET,
        fx.insight(
            "Pump inlet pressure",
            sources=["field-report-one"],
            updated="2026-05-03",
            links="Inlet pressure on the [[Notes/Entities/orbit-pump]] falls first.",
        ),
    )
    _quiet(vault)
    row = _candidate(vault)
    assert row is not None and row["measures"]["origins"] == 2, row


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


def test_a_status_registry_save_and_restore_change_hydration_without_page_rewrites(
    tmp_path: Path,
) -> None:
    from urllib.parse import quote

    from exomem import commands, upkeep
    from exomem.governance.principal import library_scope

    vault = fx.build(tmp_path)
    fx.edit(
        vault,
        fx.SEAL_WEAR,
        fx.insight(
            "Pump seal wear",
            sources=["field-report-two"],
            updated="2026-05-02",
            links="Seal wear on the [[Notes/Entities/orbit-pump]] doubles after a dry start.",
            observation="Seal wear doubles after a dry start.",
            status="bench-checked",
        ),
    )
    with library_scope():
        _quiet(vault)
        ref = upkeep.upkeep_ref(_candidate(vault)["id"])
        pages = {path: (vault / path).read_bytes() for path in (fx.ENTITY, fx.CAVITATION, fx.SEAL_WEAR)}

        def contributors() -> set[str] | None:
            try:
                item = commands.op_review_memory(vault, mode="item", ref=ref)["item"]
            except ValueError as error:
                assert str(error).startswith("REVIEW_ITEM_NOT_FOUND"), error
                return None
            return {entry["ref"] for entry in item["evidence"]}

        both = {
            f"exomem://vault/{quote(fx.CAVITATION)}",
            f"exomem://vault/{quote(fx.SEAL_WEAR)}",
        }
        assert contributors() == both
        inspected = commands.op_schema_memory(vault, subject="statuses", operation="inspect")
        commands.op_schema_memory(
            vault,
            subject="statuses",
            operation="save",
            proposal={"upsert": {"bench-checked": {"attributes": {"class": "pending"}}}},
            expected_hash=inspected["content_hash"],
            why="bench checks are not yet confirmed findings",
        )
        # One origin is left, below the family's minimum, so the proposal lapses.
        assert contributors() is None
        history = commands.op_schema_memory(vault, subject="statuses", operation="history")
        commands.op_schema_memory(
            vault,
            subject="statuses",
            operation="restore",
            version=history["versions"][0]["version"],
            expected_hash=history["content_hash"],
            why="bench checks count as findings again",
        )
        assert contributors() == both
    assert {path: (vault / path).read_bytes() for path in pages} == pages


def test_hidden_retired_and_unfamiliar_contributors_do_not_spend_hydration_cap(
    tmp_path: Path,
) -> None:
    from urllib.parse import quote

    from test_governance_egress import _external, _reset_caches, write_rule, write_scope

    from exomem import commands, upkeep
    from exomem.governance.principal import library_scope, request_scope

    vault = fx.build(tmp_path)
    with library_scope():
        _quiet(vault)
    ref = upkeep.upkeep_ref(_candidate(vault)["id"])
    write_scope(vault, paths="Notes/Withheld/**", name="Private contributors")
    write_rule(vault, ceiling=0)
    governance = vault / "Knowledge Base/_Governance"
    (governance / "scopes/statuses.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\npaths: ["_Schema/statuses.yaml"]\n'
    )
    (governance / "rules/statuses.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB2\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FB1"]\naudience: external\nceiling: 0\n'
    )
    _reset_caches()
    fx.seed(vault)
    with request_scope(_external()):
        absent = commands.op_review_memory(vault, mode="item", ref=ref)["item"]
    for index in range(70):
        fx.write(
            vault,
            f"Knowledge Base/Notes/Withheld/a-private-{index:02d}.md",
            fx.insight(
                f"Private observation {index}",
                sources=["field-report-three"],
                updated="2026-05-03",
                links="See [[Notes/Entities/orbit-pump]].",
                status="private-review" if index % 2 else "archived",
            ),
        )
    fx.seed(vault)
    fx.publish_graph(vault)
    with request_scope(_external()):
        present = commands.op_review_memory(vault, mode="item", ref=ref)["item"]
    assert present["evidence"] == absent["evidence"]
    assert present["fingerprint"] == absent["fingerprint"]
    assert {entry["ref"] for entry in present["evidence"]} == {
        f"exomem://vault/{quote(fx.CAVITATION)}",
        f"exomem://vault/{quote(fx.SEAL_WEAR)}",
    }
