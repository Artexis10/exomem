"""The catalogue loader: the shipped catalogue loads, and each refusal names its item.

The refusal cases start from one small catalogue and change one thing, so each case shows the
fault it is about.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from benchmarks.membench.world.loader import (
    CATALOGUE_DIR,
    CatalogueRefused,
    check_catalogue,
    load_catalogue,
)


def small_catalogue() -> dict:
    """A catalogue of two phenomena, a base and its twin, that loads clean."""
    probe_key = {"at": "probe", "value": {"read": "state_fold.current", "of": "item"}}
    return {
        "attributes": {
            "truth_mechanisms": {
                "kind_tag": {"readings": {"kind": "x"}},
                "state_fold": {"readings": {"current": "x", "replaced": "x"}},
                "judged": {"readings": {}},
                "run_record": {"readings": {}},
                "none": {"readings": {}},
            },
            "pipeline_order": ["kind_tag", "state_fold"],
            "world_operations": {
                "set": {"args": ["target", "value", "at"]},
                "state": {"args": ["by", "about", "value", "kind", "at", "basis"]},
            },
            "step_worlds": {"both": "x", "planted": "x", "mutation": "x"},
            "role_kinds": {"holder": "x", "attribute": "x", "value": "x"},
            "role_constraints": {"of": "x", "differs_from": "x", "volatility": "x"},
            "volatility_classes": {"durable": "x"},
            "key_parts": {"value": "x", "traps": "x", "premise": "x", "facts": "x"},
            "reading_arguments": {"of": "x", "when": "x"},
            "rule_shapes": {"default": "x"},
            "defeat_modes": {"rebut": "x"},
            "statement_bases": {"observed": "x"},
            "commitments": {"asserted": "x"},
            "norm_bases": {"stated": "x", "background": "x"},
            "behaviour_polarities": {"wanted": "x"},
            "key_classes": {
                "deterministic-oracle": {"truth": "pipeline"},
                "rubric-track": {"truth": "judged"},
                "run-relative": {"truth": "run_record"},
                "out-of-scope": {"truth": "none"},
            },
            "probe_forms": {"state": "x", "act": "x", "stable": "x"},
            "implicitness_levels": {"stated": "x", "silent": "x"},
            "stages": {"s1": "x"},
        },
        "behaviours": {"uses_current": {"polarity": "wanted"}},
        "hedges": {},
        "lineage": {},
        "norms": {"later-replaces": {"basis": "background"}, "stated-rule": {"basis": "stated"}},
        "statement_kinds": {"decision": {"commitment": "asserted"}},
        "realms": {"time": {}},
        "families": {
            "currency": {
                "realm": "time",
                "defaults": {
                    "truth": ["state_fold"],
                    "key": "deterministic-oracle",
                    "norms": ["later-replaces"],
                    "probes": ["state", "act"],
                    "expect": ["uses_current"],
                },
            }
        },
        "phenomena": [
            {"id": "currency.reversal", "stage": "s1"},
            {
                "id": "currency.unchanged",
                "stage": "s1",
                "twin_of": "currency.reversal",
                "probes": ["stable"],
            },
        ],
        "recipes": {
            "currency.reversal": {
                "needs": {
                    "owner": {"is": "holder"},
                    "item": {"is": "attribute", "volatility": "durable"},
                    "old": {"is": "value", "of": "item"},
                    "new": {"is": "value", "of": "item", "differs_from": "old"},
                },
                "slots": ["before", "change", "probe"],
                "steps": [
                    {"op": "set", "target": "item", "value": "old", "at": "before"},
                    {
                        "op": "state",
                        "id": "first",
                        "by": "owner",
                        "about": "item",
                        "value": "old",
                        "kind": "decision",
                        "at": "before",
                    },
                    {
                        "op": "set",
                        "target": "item",
                        "value": "new",
                        "at": "change",
                        "world": "planted",
                    },
                    {
                        "op": "state",
                        "id": "second",
                        "by": "owner",
                        "about": "item",
                        "value": "new",
                        "kind": "decision",
                        "at": "change",
                        "world": "planted",
                    },
                ],
                "keys": {"state": dict(probe_key), "act": dict(probe_key)},
                "twin": {"world": "mutation", "keys": {"stable": dict(probe_key)}},
            }
        },
    }


def refusals_of(raw: dict) -> list:
    with pytest.raises(CatalogueRefused) as refused:
        check_catalogue(raw)
    return refused.value.refusals


def assert_refused(raw: dict, code: str, *named: str) -> None:
    """Some refusal has this code, and its item and detail together name each of `named`."""
    for refusal in refusals_of(raw):
        text = f"{refusal.item} {refusal.detail}"
        if refusal.code == code and all(name in text for name in named):
            return
    pytest.fail(f"no `{code}` refusal names {named}")


def test_shipped_catalogue_loads_and_names_what_each_other_entry_lacks():
    catalogue = load_catalogue()

    assert "currency.reversal" in catalogue.buildable
    assert set(catalogue.buildable).isdisjoint(catalogue.lacking)
    assert set(catalogue.buildable) | set(catalogue.lacking) == set(catalogue.entries)
    assert catalogue.lacking["currency.revert"] == ("no recipe",)


def test_shipped_twin_entry_whose_base_has_no_twin_part_is_named_and_not_buildable():
    catalogue = load_catalogue()

    assert "identity.homonym" not in catalogue.buildable
    assert "identity.alias" in catalogue.lacking["identity.homonym"][0]
    assert "twin part" in catalogue.lacking["identity.homonym"][0]


def test_small_catalogue_lists_the_base_and_its_twin_as_buildable():
    catalogue = check_catalogue(small_catalogue())

    assert catalogue.buildable == ("currency.reversal", "currency.unchanged")


def test_generator_that_lacks_an_operation_makes_the_entry_not_buildable():
    catalogue = check_catalogue(small_catalogue(), implemented={"world_operations": ["set"]})

    assert catalogue.buildable == ()
    assert "state" in catalogue.lacking["currency.reversal"][0]


def test_entry_that_names_an_undeclared_mechanism_is_refused_by_phenomenon_and_mechanism():
    raw = small_catalogue()
    raw["phenomena"][0]["truth"] = ["warp_drive"]

    assert_refused(raw, "undeclared-value", "currency.reversal", "warp_drive")


def test_step_that_names_an_undeclared_operation_is_refused_by_step():
    raw = small_catalogue()
    raw["recipes"]["currency.reversal"]["steps"][1]["op"] = "teleport"

    assert_refused(raw, "undeclared-value", "currency.reversal", "first", "teleport")


def test_step_that_names_a_role_the_recipe_lacks_is_refused_by_step_and_role():
    raw = small_catalogue()
    raw["recipes"]["currency.reversal"]["steps"][1]["by"] = "stranger"

    assert_refused(raw, "unknown-reference", "currency.reversal", "first", "stranger")


def test_entry_that_names_an_unknown_norm_is_refused_by_phenomenon_and_norm():
    raw = small_catalogue()
    raw["phenomena"][0]["norms"] = ["no-such-norm"]

    assert_refused(raw, "unknown-reference", "currency.reversal", "no-such-norm")


def test_twin_entry_of_a_missing_base_is_refused_by_phenomenon_and_base():
    raw = small_catalogue()
    raw["phenomena"][1]["twin_of"] = "currency.vanished"

    assert_refused(raw, "unknown-reference", "currency.unchanged", "currency.vanished")


def test_phenomenon_id_given_twice_is_refused_by_id():
    raw = small_catalogue()
    raw["phenomena"].append({"id": "currency.reversal", "stage": "s1"})

    assert_refused(raw, "duplicate-id", "currency.reversal")


def test_mapping_key_given_twice_in_a_file_is_refused_by_key(tmp_path: Path):
    catalogue_dir = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE_DIR, catalogue_dir)
    with (catalogue_dir / "hedges.yaml").open("a", encoding="utf-8") as hedges:
        hedges.write('  tentative: {description: "Given a second time."}\n')

    with pytest.raises(CatalogueRefused) as refused:
        load_catalogue(catalogue_dir)

    assert any(r.code == "duplicate-id" and r.item == "tentative" for r in refused.value.refusals)


def test_step_id_used_twice_in_one_world_is_refused_by_step():
    raw = small_catalogue()
    raw["recipes"]["currency.reversal"]["steps"][3]["id"] = "first"

    assert_refused(raw, "duplicate-id", "currency.reversal", "first")


def test_rubric_entry_that_names_a_pipeline_mechanism_is_refused_by_phenomenon():
    raw = small_catalogue()
    raw["phenomena"][0]["key"] = "rubric-track"

    assert_refused(raw, "key-class-contradicts-mechanisms", "currency.reversal", "rubric-track")


def test_computed_entry_that_names_the_judge_is_refused_by_phenomenon():
    raw = small_catalogue()
    raw["phenomena"][0]["truth"] = ["judged"]

    assert_refused(raw, "key-class-contradicts-mechanisms", "currency.reversal", "judged")


def test_computed_silent_entry_without_a_stated_norm_is_refused_by_phenomenon():
    raw = small_catalogue()
    raw["phenomena"][0]["implicit"] = ["silent"]

    assert_refused(raw, "silent-without-stated-norm", "currency.reversal")


def test_computed_silent_entry_with_a_stated_norm_loads():
    raw = small_catalogue()
    raw["phenomena"][0].update({"implicit": ["silent"], "norms": ["later-replaces", "stated-rule"]})

    assert check_catalogue(raw).buildable


def test_recipe_that_leaves_a_declared_form_unkeyed_is_refused_by_phenomenon_and_form():
    raw = small_catalogue()
    del raw["recipes"]["currency.reversal"]["keys"]["act"]

    assert_refused(raw, "form-unkeyed", "currency.reversal", "act")


def test_recipe_that_keys_an_undeclared_form_is_refused_by_phenomenon_and_form():
    raw = small_catalogue()
    raw["recipes"]["currency.reversal"]["keys"]["stable"] = raw["recipes"]["currency.reversal"][
        "keys"
    ]["act"]

    assert_refused(raw, "form-undeclared", "currency.reversal", "stable")


def test_twin_part_that_leaves_a_form_of_its_twin_entry_unkeyed_is_refused_by_twin_entry_and_form():
    raw = small_catalogue()
    raw["phenomena"][1]["probes"] = ["stable", "state"]

    assert_refused(raw, "twin-form-unkeyed", "currency.unchanged", "state")


def test_twin_part_that_keys_a_form_its_twin_entry_does_not_declare_is_refused_by_twin_entry_and_form():
    raw = small_catalogue()
    raw["recipes"]["currency.reversal"]["twin"]["keys"]["act"] = raw["recipes"][
        "currency.reversal"
    ]["keys"]["act"]

    assert_refused(raw, "twin-form-undeclared", "currency.unchanged", "act")


def test_twin_entry_whose_base_recipe_has_no_twin_part_is_named_and_not_buildable():
    raw = small_catalogue()
    del raw["recipes"]["currency.reversal"]["twin"]

    catalogue = check_catalogue(raw)

    assert catalogue.buildable == ("currency.reversal",)
    assert "currency.reversal" in catalogue.lacking["currency.unchanged"][0]


def test_key_that_reads_a_step_absent_in_the_mutation_world_is_refused_by_step_and_world():
    raw = small_catalogue()
    raw["recipes"]["currency.reversal"]["keys"]["state"]["value"]["of"] = "second"

    assert_refused(raw, "step-absent-in-world", "currency.reversal", "second", "mutation")


def test_key_that_reads_a_step_absent_in_the_planted_world_is_refused_by_step_and_world():
    raw = small_catalogue()
    steps = raw["recipes"]["currency.reversal"]["steps"]
    steps.append(
        {
            "op": "state",
            "id": "alone",
            "by": "owner",
            "about": "item",
            "value": "old",
            "kind": "decision",
            "at": "before",
            "world": "mutation",
        }
    )
    raw["recipes"]["currency.reversal"]["keys"]["state"]["value"]["of"] = "alone"

    assert_refused(raw, "step-absent-in-world", "currency.reversal", "alone", "planted")


def test_key_that_reads_a_mechanism_the_phenomenon_does_not_name_is_refused_by_reading():
    raw = small_catalogue()
    raw["recipes"]["currency.reversal"]["keys"]["state"]["value"]["read"] = "kind_tag.kind"

    assert_refused(raw, "reading-not-named", "currency.reversal", "kind_tag.kind")
