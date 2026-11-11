"""The planner, the backbone, the domain pack and the wordbank partition of the synthetic world.

The first case is the workflow: plan one world from a fixed seed with the shipped pack, build both
worlds of every thread, and write the world to a temporary directory. The others break one rule each:
a recipe that no pack thing can bind, a thread that would take a reserved attribute, a name that two
splits hold, and a pack that names something undeclared.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from membench.generate import BANNED_VOCABULARY, _lint_vocabulary

from benchmarks.membench.world import core, names
from benchmarks.membench.world.core import Vocabulary
from benchmarks.membench.world.loader import CATALOGUE_DIR, load_catalogue
from benchmarks.membench.world.manifest import catalogue_digest
from benchmarks.membench.world.pack import PACKS_DIR, PackRefused, check_pack, load_pack
from benchmarks.membench.world.planner import (
    MUTATION,
    PLANTED,
    PlanRefused,
    RoundSpec,
    plan_world,
)
from benchmarks.membench.world.validator import validate
from exomem.public_artifact_privacy import scan_artifact

VOCABULARY = Vocabulary.load()
CATALOGUE = load_catalogue(implemented=core.implemented())
PACK_FILE = PACKS_DIR / "operations.yaml"
PACK = load_pack(PACK_FILE)
SEED = 7
# The 29 entries of slice 1 that the catalogue can build.
SLICE_ONE = tuple(i for i in CATALOGUE.buildable if CATALOGUE.entries[i].stage == "s1")


def plan(*entries: str, split: str = "dev", pack=PACK, **dials: Any):
    return plan_world(
        RoundSpec(entries or SLICE_ONE, **dials), CATALOGUE, pack, VOCABULARY, split, SEED
    )


def pack_without(edit) -> Any:
    """The shipped pack after ``edit`` has changed its raw data."""
    raw = copy.deepcopy(yaml.safe_load(PACK_FILE.read_text(encoding="utf-8")))
    edit(raw)
    return check_pack(raw, "edited", VOCABULARY)


def banned_words(*paths: Path) -> list[str]:
    errors: list[str] = []
    for path in paths:
        _lint_vocabulary(path.read_text(encoding="utf-8"), path.name, errors)
    return errors


# -- the workflow ---------------------------------------------------------------------------------


def test_a_world_is_planned_from_a_seed_and_written_the_same_way_twice(tmp_path: Path) -> None:
    first, second = plan(), plan()
    digests = [p.write(tmp_path / name) for p, name in ((first, "one"), (second, "two"))]
    manifest = first.manifest()

    assert not first.drops and len(first.threads) == len(SLICE_ONE)
    assert digests[0] == digests[1]
    for name in ("manifest.json", "world.json"):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()
    assert (
        plan_world(RoundSpec(SLICE_ONE), CATALOGUE, PACK, VOCABULARY, "dev", SEED + 1).write(
            tmp_path / "other"
        )
        != digests[0]
    )
    assert manifest["projection_digest"] is None
    assert all(a["hash"] for a in manifest["artefacts"])

    # Every id of the world begins with the split and the index of the world.
    world = first.world()
    assert all(
        i.startswith("dev-00.") for i in [*world.entities, *world.statements, *world.artefacts]
    )

    # The shared world and the two worlds of each thread hold no broken invariant.
    assert validate(world) == []
    for thread in first.threads:
        assert [validate(w) for w in first.pair(thread)] == [[], []]

    # The pack and the written world pass the privacy gate and the banned-vocabulary lint.
    files = [PACK_FILE, tmp_path / "one" / "manifest.json", tmp_path / "one" / "world.json"]
    assert [f for f in files if scan_artifact(f, label=f.name)] == []
    assert banned_words(*files) == []
    assert BANNED_VOCABULARY  # the lint ran against a non-empty list


# -- reservation ----------------------------------------------------------------------------------


def test_no_attribute_belongs_to_two_threads_and_no_other_thread_states_it() -> None:
    planned = plan()
    world = planned.world()
    owner: dict[str, str] = {}
    for thread in planned.threads:
        for ref in thread.reserved:
            assert ref not in owner, f"{ref} is reserved by {owner[ref]} and {thread.id}"
            owner[ref] = thread.id
    for statement in world.statements.values():
        holder_thread = owner.get(statement.about or "")
        if holder_thread is not None:
            assert statement.id.startswith(f"{holder_thread}."), (statement.id, holder_thread)


def test_a_thread_whose_recipe_needs_a_reserved_attribute_is_dropped_and_the_drop_is_recorded() -> (
    None
):
    def one_service(raw: dict[str, Any]) -> None:
        raw["entity_types"] = [t for t in raw["entity_types"] if t["id"] in ("person", "service")]
        raw["entity_types"][1]["count"] = 1

    two_cascades = ("propagation.independent-support",) * 2
    planned = plan(*two_cascades, pack=pack_without(one_service))

    assert [t.entry for t in planned.threads] == ["propagation.independent-support"]
    assert [(d.entry, d.code) for d in planned.drops] == [
        ("propagation.independent-support", "unbound-role")
    ]
    assert "no free attribute" in planned.drops[0].detail
    assert planned.manifest()["drops"] == [
        {"entry": d.entry, "code": d.code, "detail": d.detail} for d in planned.drops
    ]


# -- role binding ---------------------------------------------------------------------------------


def test_a_recipe_whose_role_no_pack_thing_meets_is_dropped_and_the_drop_names_the_role() -> None:
    def no_permanent(raw: dict[str, Any]) -> None:
        for kind in raw["entity_types"]:
            kind["attributes"] = [
                a for a in kind.get("attributes", []) if a["volatility"] != "permanent"
            ]

    planned = plan("persistence.permanent", pack=pack_without(no_permanent))

    assert planned.threads == ()
    assert planned.drops[0].detail.startswith("role item: no free attribute")


def test_every_bound_role_meets_the_constraints_of_its_recipe() -> None:
    planned = plan()
    backbone = planned.backbone
    for thread in planned.threads:
        needs = CATALOGUE.recipes[thread.recipe]["needs"]
        for role, need in needs.items():
            value = thread.roles[role]
            where = f"{thread.entry} {role}"
            if need["is"] == "attribute":
                held = backbone.attributes[value]
                assert held.schema.volatility == need.get("volatility", "durable"), where
                if "of" in need:
                    assert held.entity == thread.roles[need["of"]], where
                generator = held.schema.domain.generator
                assert (generator == need["values"]) if "values" in need else generator != "holder"
            if need["is"] == "value":
                target = thread.roles[need["of"]]
                domain = (
                    backbone.attributes[target].schema.domain
                    if target in backbone.attributes
                    else backbone.events[target].type.domain
                )
                assert value in backbone.values(domain), where
            if need["is"] == "holder":
                assert value in backbone.holders(), where  # never the user
            if need["is"] == "user":
                assert value == backbone.user, where
            if need["is"] == "context" and "within" in need:
                assert backbone.contexts[value] == thread.roles[need["within"]], where
            if "outside" in need and need["is"] == "holder":
                assert value not in backbone.audiences[thread.roles[need["outside"]]], where
            if "differs_from" in need:
                assert value != thread.roles[need["differs_from"]], where


def test_a_name_that_a_step_gives_is_new_and_a_name_that_no_step_gives_is_the_entitys_own() -> None:
    planned = plan("identity.rename", "identity.merge")
    entities = planned.backbone.entities
    rename, merge = planned.threads

    assert rename.roles["former"] == entities[rename.roles["subject"]].name
    assert rename.roles["fresh"] not in {e.name for e in entities.values()}
    assert merge.roles["absorbed"] == entities[merge.roles["second"]].name
    assert merge.roles["joint"] not in {e.name for e in entities.values()}


# -- the twins dial -------------------------------------------------------------------------------


def test_a_thread_planted_in_the_mutation_world_leaves_its_phenomenon_out_of_the_shared_world() -> (
    None
):
    planted = plan("currency.reversal", twins=0)
    left_out = plan("currency.reversal", twins=1)
    (a,), (b,) = planted.threads, left_out.threads

    def statements(p, thread) -> int:
        return sum(s.startswith(thread.id) for s in p.world().statements)

    assert (a.variant, b.variant) == (PLANTED, MUTATION)
    assert statements(planted, a) == 2 and statements(left_out, b) == 1


def test_a_twin_entry_takes_the_world_that_its_recipe_keys_it_in() -> None:
    planned = plan("scope.general-rule-applies", "currency.unchanged", twins=0)

    assert {t.entry: t.variant for t in planned.threads} == {
        "scope.general-rule-applies": PLANTED,
        "currency.unchanged": MUTATION,
    }


# -- refusals before anything is planned ----------------------------------------------------------


def test_a_phenomenon_that_the_catalogue_cannot_build_is_refused_and_named() -> None:
    with pytest.raises(PlanRefused, match="hygiene.no-relevant-memory"):
        plan("hygiene.no-relevant-memory")


def test_a_split_that_attributes_does_not_declare_is_refused() -> None:
    with pytest.raises(PlanRefused, match="split final"):
        plan("currency.reversal", split="final")


# -- splits ---------------------------------------------------------------------------------------


def test_no_entity_name_belongs_to_two_splits() -> None:
    seen: dict[str, str] = {}
    for split in sorted(VOCABULARY.splits):
        world = plan(split=split).world()
        named = {e.name for e in world.entities.values()}
        named |= {n.name for n in world.namings}
        assert named.isdisjoint(seen), (split, named & set(seen))
        seen |= dict.fromkeys(named, split)


def test_the_name_spaces_that_attributes_declares_are_the_ones_the_wordbank_partition_builds() -> (
    None
):
    declared = yaml.safe_load((CATALOGUE_DIR / "attributes.yaml").read_text(encoding="utf-8"))
    assert set(declared["name_spaces"]) == set(names.implemented()["name_spaces"])
    assert set(declared["splits"]) == {"train", "dev", "heldout"}


def test_the_catalogue_digest_changes_when_a_catalogue_file_changes(tmp_path: Path) -> None:
    copied = tmp_path / "catalogue"
    copied.mkdir()
    for path in CATALOGUE_DIR.rglob("*.yaml"):
        target = copied / path.relative_to(CATALOGUE_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    assert catalogue_digest(copied) == catalogue_digest()

    (copied / "hedges.yaml").write_text(
        (copied / "hedges.yaml").read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )
    assert catalogue_digest(copied) != catalogue_digest()


# -- the pack -------------------------------------------------------------------------------------


def test_a_pack_that_names_an_undeclared_volatility_class_is_refused() -> None:
    def edit(raw: dict[str, Any]) -> None:
        raw["entity_types"][2]["attributes"][0]["volatility"] = "fleeting"

    with pytest.raises(PackRefused, match="approvals.*fleeting"):
        pack_without(edit)


def test_a_pack_whose_stable_facts_use_an_unregistered_statement_kind_is_refused() -> None:
    def edit(raw: dict[str, Any]) -> None:
        raw["facts"]["kind"] = "rumour"

    with pytest.raises(PackRefused, match="facts.*rumour"):
        pack_without(edit)


def test_a_pack_whose_audience_is_drawn_from_things_that_make_no_statement_is_refused() -> None:
    def edit(raw: dict[str, Any]) -> None:
        raw["audiences"]["from"] = "service"

    with pytest.raises(PackRefused, match="audiences.*service"):
        pack_without(edit)


def test_a_pack_attribute_whose_values_are_drawn_from_things_that_make_no_statement_is_refused() -> (
    None
):
    def edit(raw: dict[str, Any]) -> None:
        owner = next(a for a in raw["entity_types"][2]["attributes"] if a["id"] == "owner")
        owner["value"]["from"] = "service"

    with pytest.raises(PackRefused, match="attribute owner.*service"):
        pack_without(edit)
