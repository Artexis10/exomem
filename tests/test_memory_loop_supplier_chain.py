"""Operator/site and supplier-chain acceptance fixtures (close-memory-loop 1.5).

Three things are pinned here and kept visibly apart:

**Frozen before capture.** Every episode's actor input, pre-capture world and
evaluator expectations are digested separately and the digests are pinned
below, so an expectation edited after a capture run voids that run instead of
rescoring it. The actor view carries turns only.

**Expected red on the current runtime.** An untouched world fails every
episode that needs a capture, and the public entity writer refuses a site
that shares its exact name with an organization (``ENTITY_EXISTS``). Those are
falsification targets for the identity and episode work, recorded as
evidence here rather than hidden.

**Not vacuous.** Scripted writes through the product's own writers (plumbing,
never ordinary-agent evidence) make each episode's positives pass, and a
matched wrong capture fails the specific predicate it should.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from epistemic.memory_loop import contract
from epistemic.memory_loop import supplier_chain as sc
from epistemic.memory_loop.contract import FixtureError, read_state

from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(600)

#: Frozen digests. Editing an episode, a world or an expectation moves one of
#: these; re-pinning is a deliberate act that voids earlier capture runs.
FIXTURE_SET_SHA256 = "0e1470e28450f11799cbe1b15df15cf877292a1bcde5ded02b1a475ac2cb1bb7"
EVALUATOR_SHA256 = "1f07c92812374a2f118d01402fb7369b72b3e2fb1928289635c70b3cdc722656"
MODULE = Path(sc.__file__)


# --------------------------------------------------------------------------- #
# The manifest (pure)
# --------------------------------------------------------------------------- #


def test_the_manifest_is_consistent_and_covers_every_facet() -> None:
    sc.assert_manifest_consistent()
    covered = {facet for item in sc.MANIFEST for facet in item.covers}
    assert covered == set(sc.FACETS)
    assert {
        "distinct_organization_and_site",
        "one_organization_several_roles",
        "shared_ambiguous_name",
        "independently_useful_brand",
        "incidental_brand",
        "mixed_producer_purchase",
        "supplier_to_producer_provenance",
        "unknown_lot_origin",
        "operator_succession",
    } == set(sc.FACETS)


def test_the_digests_are_frozen() -> None:
    assert sc.fixture_set_sha256() == FIXTURE_SET_SHA256
    assert sc.evaluator_sha256() == EVALUATOR_SHA256


def test_editing_an_expectation_moves_the_evaluator_digest_but_not_the_actor() -> None:
    item = sc.MANIFEST[3]
    edited = replace(item, expectations=item.expectations[:-1])
    assert contract.sha256_json(edited) != contract.sha256_json(item)
    # The actor digest is computed from the actor view alone.
    assert sc.actor_sha256(item.episode_id) == contract.sha256_json(sc.actor_view(item.episode_id))


def test_the_actor_view_withholds_every_evaluator_field() -> None:
    for item in sc.EPISODES:
        view = sc.actor_view(item.episode_id)
        assert set(view) == {"episode_id", "turns", "later_turn"}
        text = repr(view)
        expectations = sc.expectations_for(item.episode_id)
        for expectation in expectations.expectations:
            assert expectation.key not in text
            assert expectation.reason not in text
        assert expectations.later_use.useful not in text
        for word in ("positive", "negative", "expected", "polarity", "routed", "disposition"):
            assert word not in text


def test_actor_turns_carry_no_save_recall_or_routing_nudge() -> None:
    for item in sc.EPISODES:
        for turn in (*item.turns, item.later_turn):
            assert contract.find_nudges(turn) == (), (item.episode_id, turn)


def test_every_proper_name_is_declared_as_invented() -> None:
    allowed = (*sc.INVENTED_NAMES, *sc.ORDINARY_CAPITALIZED)
    texts: list[str] = []
    for item in sc.EPISODES:
        texts.extend((*item.turns, item.later_turn))
        world = item.world
        for seed in world.entities:
            texts.extend((seed.name, seed.summary, *seed.aliases))
        for seed in world.notes:
            texts.extend((seed.title, seed.observation))
        for seed in world.records:
            texts.append(seed.title)
            texts.extend(value for _key, row in seed.items for _field, value in row)
        for seed in world.types:
            texts.extend((seed.label, seed.guidance))
    for text in texts:
        assert contract.undeclared_capitalized_words(text, allowed) == (), text


def test_the_fixture_module_passes_the_privacy_gate_and_names_nothing_clinical() -> None:
    assert_public_artifacts_clean([MODULE, Path(contract.__file__)])
    for item in sc.EPISODES:
        for turn in (*item.turns, item.later_turn):
            assert contract.find_medical_terms(turn) == ()


def test_every_expected_edge_carries_frozen_evidence_from_its_own_input() -> None:
    for item in sc.MANIFEST:
        turns = " ".join(sc.episode(item.episode_id).turns)
        for evidence in item.edge_evidence:
            assert evidence.episode_id == item.episode_id
            assert evidence.span in turns


def test_every_episode_pairs_a_positive_with_a_guard() -> None:
    """A manifest of only negatives would pass an agent that did nothing."""

    for item in sc.MANIFEST:
        polarities = {expectation.polarity for expectation in item.expectations}
        assert "positive" in polarities, item.episode_id
        assert len(item.expectations) >= 4, item.episode_id


def test_the_pre_capture_seeds_do_not_already_satisfy_a_role_positive() -> None:
    summary = sc.ORG_MERROW.summary
    for marker in ("grow", "producer", "run", "operat", "sell", "supplier", "slope", "acre"):
        assert marker not in summary.casefold()


def test_only_the_shared_name_episodes_pin_an_activation_status() -> None:
    pinned = {
        item.episode_id: item.later_use.expected_status
        for item in sc.MANIFEST
        if item.later_use.expected_status is not None
    }
    assert pinned == {"multi-role": "ambiguous", "shared-name": "ambiguous"}


def test_unknown_episode_ids_are_refused() -> None:
    with pytest.raises(FixtureError):
        sc.episode("no-such-episode")
    with pytest.raises(FixtureError):
        sc.expectations_for("no-such-episode")


# --------------------------------------------------------------------------- #
# Worlds through the product's writers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, contract.BuiltWorld]]:
    built: dict[str, tuple[Path, contract.BuiltWorld]] = {}
    for item in sc.EPISODES:
        root = tmp_path_factory.mktemp(f"world-{item.episode_id}") / "vault"
        built[item.episode_id] = (root, sc.build_pre_capture(root, item.episode_id))
    return built


def _copy(worlds, episode_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, world = worlds[episode_id]
    root = tmp_path / "vault"
    shutil.copytree(source, root)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    return root, world


def test_every_world_builds_its_seeds_through_the_product_writers(worlds) -> None:
    for item in sc.EPISODES:
        root, world = worlds[item.episode_id]
        assert set(world.key_to_path) == set(item.world.keys())
        state = read_state(root)
        for seed in item.world.entities:
            page = state.pages[world.key_to_path[seed.key]]
            assert (page.page_type, page.entity_type, page.title) == ("entity", seed.entity_type, seed.name)
            assert sorted(page.aliases) == sorted(seed.aliases)
        for seed in item.world.records:
            items = [record for record in state.records if record.collection == seed.manifest_path]
            assert len(items) == len(seed.items)
        assert world.spec_sha256 == sc.pre_capture_spec_sha256(item.episode_id)


def test_a_world_build_is_deterministic(worlds, tmp_path: Path) -> None:
    _root, first = worlds["mixed-purchase"]
    again = sc.build_pre_capture(tmp_path / "again", "mixed-purchase")

    assert again.logical_sha256 == first.logical_sha256
    assert again.key_to_path == first.key_to_path


def test_the_shared_name_is_carried_by_both_identities_before_capture(worlds) -> None:
    root, world = worlds["shared-name"]
    state = read_state(root)
    carriers = [
        page.path
        for page in state.entities()
        if any(name.casefold() == "merrow farm" for name in page.names)
    ]
    assert sorted(carriers) == sorted([world.key_to_path["org_merrow"], world.key_to_path["site_merrow"]])


# --------------------------------------------------------------------------- #
# Expected red on the current runtime
# --------------------------------------------------------------------------- #


def test_current_runtime_an_untouched_world_fails_every_capture_episode(worlds) -> None:
    """No capture happened: every episode that needs one is red, for its own reason."""

    red = {}
    for item in sc.EPISODES:
        root, world = worlds[item.episode_id]
        state = read_state(root)
        check = sc.check_capture(item.episode_id, world, state, state)
        red[item.episode_id] = check.failed()
    # Abstention is the correct outcome of the shared-name episode, so an
    # untouched world passes it; every other episode needs a capture.
    assert red.pop("shared-name") == ()
    assert all(failed for failed in red.values()), red
    assert "org-and-site/site-identity" in red["org-and-site"]
    assert "multi-role/producer-role" in red["multi-role"]
    assert "mixed-purchase/unknown-origin-lot" in red["mixed-purchase"]
    assert "operator-succession/current-operator" in red["operator-succession"]
    assert "brand/brand-identity" in red["brand"]


def test_current_runtime_refuses_a_same_name_site_beside_its_organization(tmp_path: Path) -> None:
    """Expected red. A justified distinct identity must stay expressible
    through the public writer despite an overlapping surface name; today the
    entity writer refuses it as ``ENTITY_EXISTS``. When the identity work
    lands, this build succeeds and this test flips to assert it."""

    with pytest.raises(FixtureError, match="ENTITY_EXISTS"):
        contract.build_world(tmp_path / "vault", sc.SAME_NAME_WORLD, world_id="same-name")


def test_current_runtime_reports_the_bare_shared_name_as_ambiguous(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, lexstore, working_set_index, working_set_runtime

    root, _world = _copy(worlds, "shared-name", tmp_path, monkeypatch)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    lexstore.ensure_fresh(root)
    working_set_runtime.reset_caches_for_tests()

    packet = commands.op_activate_context(root, turn=sc.episode("shared-name").later_turn)

    assert packet.get("turn_status") == "ambiguous" or (
        packet["abstained"] and packet["abstention"]["reason"] == "ambiguous"
    ), packet


# --------------------------------------------------------------------------- #
# Not vacuous: scripted product writes (plumbing, not agent evidence)
# --------------------------------------------------------------------------- #

TODAY = dt.date(2026, 9, 24)


def _command(name: str):
    from exomem import commands

    return next(item for item in commands.PRODUCT_COMMANDS if item.name == name)


def _edit(root: Path, path: str, operation: dict) -> None:
    from exomem import writer_lease
    from exomem.vault import content_hash

    text = (root / path).read_text(encoding="utf-8")
    writer_lease.invoke_command(
        _command("edit_memory"),
        root,
        path=path,
        why="scripted fixture capture",
        operation={**operation, "expected_hash": content_hash(text)},
    )


def _entity(root: Path, entity_type: str, name: str, summary: str) -> str:
    from exomem import link

    return link.link(root, entity_type=entity_type, name=name, summary=summary, today=TODAY).path


def _append(root: Path, collection: str, item: dict, key: str) -> None:
    from exomem import commands

    snapshot = commands.op_record_memory(root, action="inspect", collection=collection)["snapshot"]
    commands.op_record_memory(
        root,
        action="append",
        collection=collection,
        item=item,
        item_key=key,
        expected_container_hash=snapshot,
        why="scripted fixture capture",
    )


def _check(episode_id: str, root: Path, world, before) -> contract.CaptureCheck:
    return sc.check_capture(episode_id, world, before, read_state(root))


def test_scripted_org_and_site_capture_passes_and_a_misrouted_facet_fails(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "org-and-site", tmp_path, monkeypatch)
    before = read_state(root)
    site = _entity(root, "site", "Merrow Farm Orchard", "About forty acres on the north slope of Tarrow valley.")

    check = _check("org-and-site", root, world, before)
    assert check.accepted, check.results

    _edit(root, site, {"kind": "replace_string", "old_string": "valley.", "new_string": "valley, by Mill Street."})
    assert _check("org-and-site", root, world, before).failed() == ("org-and-site/office-not-on-site",)


def test_a_site_named_without_the_shared_name_is_still_the_site(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "org-and-site", tmp_path, monkeypatch)
    before = read_state(root)
    _entity(root, "site", "North Slope Orchard", "About forty acres on the north slope of Tarrow valley.")

    assert _check("org-and-site", root, world, before).accepted


def test_a_duplicate_organization_fails_the_org_and_site_episode(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "org-and-site", tmp_path, monkeypatch)
    before = read_state(root)
    _entity(root, "site", "Merrow Farm Orchard", "About forty acres on the north slope of Tarrow valley.")
    _entity(root, "organization", "Merrow Farm Company", "The company behind the orchard.")

    assert "org-and-site/one-organization" in _check("org-and-site", root, world, before).failed()


def test_scripted_multi_role_capture_passes_and_a_trading_name_entity_fails(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "multi-role", tmp_path, monkeypatch)
    before = read_state(root)
    _edit(
        root,
        world.key_to_path["org_merrow"],
        {
            "kind": "replace_string",
            "old_string": "Apple business based in Tarrow valley.",
            "new_string": (
                "Apple business based in Tarrow valley. Grows most of what it sells, runs "
                "the north-slope orchard and sells at the Saturday market stall."
            ),
        },
    )
    assert _check("multi-role", root, world, before).accepted

    _entity(root, "brand", "Merrow Gold", "Name on the stall chalkboard.")
    assert _check("multi-role", root, world, before).failed() == ("multi-role/no-trading-name-entity",)


def test_the_shared_name_claim_on_either_identity_fails(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "shared-name", tmp_path, monkeypatch)
    before = read_state(root)
    _edit(
        root,
        world.key_to_path["site_merrow"],
        {
            "kind": "replace_string",
            "old_string": "Orchard on the north slope of Tarrow valley.",
            "new_string": "Orchard on the north slope of Tarrow valley. Changing hands next spring.",
        },
    )

    assert _check("shared-name", root, world, before).failed() == ("shared-name/claim-not-on-site",)


PURCHASES = sc.PURCHASES.manifest_path


def _lots(root: Path, *, b_producer: str, c_producer: str, c_origin: str) -> None:
    rows = (
        ("0921-A", "Merrow Farm", "label", ""),
        ("0921-B", b_producer, "reported", "stall worker at Merrow Farm"),
        ("0921-C", c_producer, c_origin, ""),
    )
    for index, (lot, producer, origin, source) in enumerate(rows, start=1):
        item = {"lot": lot, "bought_on": "2026-09-21", "seller": "Merrow Farm", "origin": origin}
        if producer:
            item["producer"] = producer
        if source:
            item["origin_source"] = source
        _append(root, PURCHASES, item, f"20000000-0000-4000-8000-00000000009{index}")


def test_scripted_mixed_purchase_keeps_each_lot_origin(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "mixed-purchase", tmp_path, monkeypatch)
    before = read_state(root)
    _lots(root, b_producer="Pellow Orchards", c_producer="", c_origin="unknown")

    check = _check("mixed-purchase", root, world, before)
    assert check.accepted, check.results


def test_a_seller_substituted_for_the_producer_fails_the_partner_lot(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "mixed-purchase", tmp_path, monkeypatch)
    before = read_state(root)
    _lots(root, b_producer="Merrow Farm", c_producer="", c_origin="unknown")

    assert _check("mixed-purchase", root, world, before).failed() == ("mixed-purchase/partner-lot",)


def test_an_inferred_producer_for_the_unknown_lot_fails(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "mixed-purchase", tmp_path, monkeypatch)
    before = read_state(root)
    _lots(root, b_producer="Pellow Orchards", c_producer="Pellow Orchards", c_origin="reported")

    assert _check("mixed-purchase", root, world, before).failed() == ("mixed-purchase/unknown-origin-lot",)


OPERATORS = sc.OPERATORS.manifest_path


def test_scripted_succession_keeps_the_site_and_its_history(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "operator-succession", tmp_path, monkeypatch)
    before = read_state(root)
    _entity(root, "organization", "Callow Cider Company", "Cider maker running the north-slope orchard.")
    _append(
        root,
        OPERATORS,
        {"site": "Merrow Farm Orchard", "operator": "Callow Cider Company", "since": "2026-09-01", "source": "Merrow Farm stall"},
        "20000000-0000-4000-8000-000000000022",
    )

    check = _check("operator-succession", root, world, before)
    assert check.accepted, check.results


def test_a_succession_without_the_new_assignment_leaves_the_old_operator_current(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "operator-succession", tmp_path, monkeypatch)
    before = read_state(root)
    _entity(root, "organization", "Callow Cider Company", "Cider maker running the north-slope orchard.")

    failed = _check("operator-succession", root, world, before).failed()
    assert set(failed) == {"operator-succession/transition-recorded", "operator-succession/current-operator"}


def test_a_new_site_for_the_new_operator_fails_the_succession(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "operator-succession", tmp_path, monkeypatch)
    before = read_state(root)
    _entity(root, "organization", "Callow Cider Company", "Cider maker.")
    _entity(root, "site", "Merrow Farm North Slope", "The orchard under its new operator.")
    _append(
        root,
        OPERATORS,
        {"site": "Merrow Farm Orchard", "operator": "Callow Cider Company", "since": "2026-09-01"},
        "20000000-0000-4000-8000-000000000022",
    )

    assert _check("operator-succession", root, world, before).failed() == ("operator-succession/one-site",)


def _save_relation(root: Path, relation: str, description: str) -> None:
    from exomem import commands, relation_registry

    commands.op_schema_memory(
        root,
        subject="relations",
        operation="save-relations",
        proposal={
            "upsert": {
                relation: {
                    "parent": "relates_to",
                    "description": description,
                    "direction": "directed",
                    "aliases": [],
                }
            }
        },
        expected_hash=relation_registry.load_registry(root).extension_hash,
        why="scripted fixture capture",
    )


def _relate(root: Path, path: str, relation: str, target: str) -> None:
    text = (root / path).read_text(encoding="utf-8")
    _edit(root, path, {"kind": "replace_body", "new_body": text.split("\n---\n", 1)[1].rstrip() + f"\n\n## Relations\n\n- {relation} [[{target}]]\n"})


def test_scripted_brand_capture_passes_and_a_generic_relation_does_not(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world = _copy(worlds, "brand", tmp_path, monkeypatch)
    before = read_state(root)
    _entity(root, "brand", "Tarrow Crown", "The valley growers' quality mark.")
    _save_relation(root, "vault.sells_under", "Sells its produce under a mark or label.")
    _relate(root, world.key_to_path["org_merrow"], "vault.sells_under", "Tarrow Crown")
    _relate(root, world.key_to_path["org_pellow"], "relates_to", "Tarrow Crown")

    check = _check("brand", root, world, before)
    assert check.failed() == ("brand/pellow-sells-under",), check.results
    assert check.outcome("brand/merrow-sells-under") == "pass"
