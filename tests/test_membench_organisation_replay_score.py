"""The f34 scorer: the scoring branches whose miss would flip a situation's result.

Every situation, key, name and value here is an invented placeholder. Each test
builds the end state it scores, so the case reads on its own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from membench.utility.organisation_replay.__main__ import main as replay_main
from membench.utility.organisation_replay.score import (
    ScoreRefused,
    bounded_match,
    newcombe_paired,
    score,
    statistics,
)

KB = "Knowledge Base"
CASE_BYTES = b"placeholder case file"
CASE_SHA = hashlib.sha256(CASE_BYTES).hexdigest()

RELATIONS = {
    "relation-r": {
        "parent_chain": [],
        "generic": False,
        "direction": "directed",
        "inverse": "relation-q",
        "aliases": [],
    },
    "relation-q": {
        "parent_chain": [],
        "generic": False,
        "direction": "directed",
        "inverse": "relation-r",
        "aliases": [],
    },
    "relation-s": {
        "parent_chain": [],
        "generic": False,
        "direction": "directed",
        "inverse": None,
        "aliases": [],
    },
    "relates_to": {
        "parent_chain": [],
        "generic": True,
        "direction": "symmetric",
        "inverse": None,
        "aliases": [],
    },
    "mentions": {
        "parent_chain": [],
        "generic": False,
        "direction": "directed",
        "inverse": None,
        "aliases": [],
    },
}


def entity(stem: str, title: str, entity_type: str = "concept", *, in_seed: bool = True) -> dict:
    return {
        "path": f"{KB}/Entities/{stem}.md",
        "title": title,
        "stem": stem,
        "role": "entity",
        "type": "entity",
        "entity_type": entity_type,
        "in_seed": in_seed,
    }


def note(folder: str, stem: str, *, in_seed: bool = False) -> dict:
    return {
        "path": f"{KB}/{folder}/{stem}.md",
        "title": stem,
        "stem": stem,
        "role": None,
        "type": "pattern",
        "in_seed": in_seed,
    }


def edge(subject: dict, relation: str, target: dict) -> dict:
    return {
        "holder": subject["path"],
        "subject": subject["path"],
        "relation": relation,
        "target_path": target["path"],
    }


def write_run(
    out: Path,
    situation: str,
    arm: str,
    *,
    pages: list[dict],
    edges: list[dict] = (),
    changed: list[str] = (),
    answer: str = "",
    tool_results: list[str] = (),
    vault_types: dict | None = None,
    status: str = "ok",
    shape: str = "hookless",
) -> None:
    run_dir = out / situation / arm
    run_dir.mkdir(parents=True)
    (run_dir / "transcript.jsonl").write_text("", encoding="utf-8")
    calls = [
        {
            "tool": "search",
            "result": {"content": [{"type": "text", "text": text}]},
            "refusal_code": None,
        }
        for text in tool_results
    ]
    run = {
        "situation_id": situation,
        "situation_sha256": CASE_SHA,
        "arm": arm,
        "client_shape": shape,
        "status": status,
        "sessions": [
            {"role": "capture", "calls": [], "transcript_path": "transcript.jsonl"},
            {
                "role": "fresh",
                "calls": calls,
                "final_answer": answer,
                "transcript_path": "transcript.jsonl",
            },
        ],
        "end_state": {
            "pages": pages,
            "pages_changed": list(changed),
            "edges": list(edges),
            "relations": RELATIONS,
            "entity_types": {"core": ["concept", "person"], "vault": vault_types or {}},
            "operation_log": {"edge_review_reasons": {"status": "unavailable"}},
        },
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")


def write_key(keys: Path, key: dict) -> None:
    keys.mkdir(exist_ok=True)
    body = {"case_sha256": CASE_SHA, **key}
    (keys / f"dk{key['id'][1:]}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def answer_key(value: str) -> dict:
    return {
        "question": "Placeholder question?",
        "key_fact": "Placeholder fact.",
        "accepted_values": [value],
    }


def pair_key(situation: str, variant: str, supported: list[dict], rule: str) -> dict:
    return {
        "id": situation,
        "category": "relation",
        "variant": variant,
        "endpoints": {"A": "Entity Alpha", "B": "Entity Beta"},
        "state_success": {"rule": rule, "pair": ["A", "B"]},
        "supported_edges": supported,
        "false_precision": {"pairs": [{"between": ["A", "B"], "supported_edges": supported}]},
        "answer": answer_key("beta"),
    }


def baseline_row(tmp_path: Path, key: dict, **run) -> dict:
    """Score one situation whose candidate run repeats the baseline's, and return the baseline row."""

    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, key)
    write_run(out, key["id"], "x1", **run)
    write_run(out, key["id"], "x2", **run)
    report = score(out, keys, baseline="x1", candidate="x2")
    return report["rows"][0]["baseline"]


SUPPORTED = [{"relation": "relation-r", "from": "A", "to": "B"}]


def test_newcombe_method_10_reproduces_the_preregistered_vector():
    result = newcombe_paired(36, 12, 2, 0)
    assert round(result["difference"], 4) == 0.2000
    assert (round(result["lower"], 4), round(result["upper"], 4)) == (0.0569, 0.3404)


def test_two_more_false_precision_situations_in_the_candidate_block_the_merge():
    def row(candidate_fp: bool) -> dict:
        return {
            "baseline": {"success": False, "false_precision": False},
            "candidate": {"success": True, "false_precision": candidate_fp},
        }

    one = statistics([row(True), row(False), row(False), row(False), row(False), row(False)])
    two = statistics([row(True), row(True), row(False), row(False), row(False), row(False)])
    assert one["decision_rule"]["merge"] is True
    assert (two["false_precision_excess"], two["decision_rule"]["merge"]) == (2, False)


def test_a_supported_edge_written_as_its_registered_inverse_is_the_same_fact(tmp_path):
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    row = baseline_row(
        tmp_path,
        pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported"),
        pages=[a, b],
        edges=[edge(b, "relation-q", a)],
        changed=[b["path"]],
        answer="Entity Beta.",
    )
    assert (row["state_outcome"], row["false_precision"], row["success"]) == (True, False, True)


def test_a_reversed_directed_edge_is_false_precision_and_no_state(tmp_path):
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    row = baseline_row(
        tmp_path,
        pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported"),
        pages=[a, b],
        edges=[edge(b, "relation-r", a)],
        changed=[b["path"]],
        answer="Entity Beta.",
    )
    assert row["state_outcome"] is False
    assert row["false_precision_claims"] == ["unsupported_edge:relation-r"]
    assert row["success"] is False


def test_only_a_generic_edge_where_a_specific_one_is_required_is_an_omission_not_false_precision(
    tmp_path,
):
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    row = baseline_row(
        tmp_path,
        pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported"),
        pages=[a, b],
        edges=[edge(a, "relates_to", b), edge(a, "mentions", b)],
        changed=[a["path"]],
        answer="Entity Beta.",
    )
    assert row["omissions"] == ["generic_edge_only"]
    assert row["false_precision"] is False


def test_a_typed_edge_where_only_a_generic_edge_fits_is_false_precision(tmp_path):
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    row = baseline_row(
        tmp_path,
        pair_key("d01", "generic_or_none", [], "none_required"),
        pages=[a, b],
        edges=[edge(a, "relation-s", b)],
        changed=[a["path"]],
        answer="Entity Beta.",
    )
    assert (row["state_outcome"], row["false_precision"]) == (True, True)


def test_a_referent_typed_as_a_ruled_out_core_type_fails_and_is_false_precision(tmp_path):
    anchor = entity("anchor-one", "Anchor One")
    referent = entity("token-zed", "Token Zed", "person", in_seed=False)
    key = {
        "id": "d01",
        "category": "referent",
        "variant": "positive",
        "endpoints": {"A": "Anchor One", "R": "the referent page"},
        "referent": {"match_tokens": ["token zed"], "wrong_core_types": ["person"]},
        "state_success": {"rule": "referent_entity_exists_with_fitting_type"},
        "false_precision": {"wrong_core_types": ["person"], "pairs": []},
        "answer": answer_key("token zed"),
    }
    row = baseline_row(
        tmp_path, key, pages=[anchor, referent], changed=[referent["path"]], answer="Token Zed"
    )
    assert row["state_outcome"] is False
    assert row["false_precision_claims"] == ["wrong_type:person"]


def test_a_page_created_for_a_durability_twins_one_off_referent_is_false_precision(tmp_path):
    referent = entity("token-once", "Token Once", in_seed=False)
    key = {
        "id": "d01",
        "category": "referent",
        "variant": "durability_twin",
        "endpoints": {"R": "the referent page"},
        "referent": {"match_tokens": ["token once"]},
        "state_success": {"rule": "none_required"},
        "false_precision": {"twin_promoted": ["token once"]},
        "answer": answer_key("value-v"),
    }
    row = baseline_row(
        tmp_path, key, pages=[referent], changed=[referent["path"]], answer="value-v"
    )
    assert (row["state_outcome"], row["answer_match"], row["false_precision"]) == (True, True, True)


def test_a_note_written_outside_the_expected_home_names_that_omission(tmp_path):
    elsewhere = note("Notes/Home Other", "note-n")
    key = {
        "id": "d01",
        "category": "placement",
        "variant": "under_expected_home",
        "endpoints": {},
        "state_success": {"rule": "placed_under_home", "folder": "Notes/Home H"},
        "false_precision": {"wrong_homes": ["Notes/Home W"]},
        "answer": answer_key("value-v"),
    }
    row = baseline_row(
        tmp_path, key, pages=[elsewhere], changed=[elsewhere["path"]], answer="value-v"
    )
    assert row["omissions"] == ["placed_outside_expected_home"]
    assert row["false_precision"] is False


def test_a_note_created_in_an_unrelated_home_fails_placement_and_is_false_precision(tmp_path):
    wrong = note("Notes/Home W", "note-n")
    key = {
        "id": "d01",
        "category": "placement",
        "variant": "outside_unrelated_homes",
        "endpoints": {},
        "state_success": {"rule": "placed_outside_homes", "folders": ["Notes/Home W"]},
        "false_precision": {"wrong_homes": ["Notes/Home W"]},
        "answer": answer_key("value-v"),
    }
    row = baseline_row(tmp_path, key, pages=[wrong], changed=[wrong["path"]], answer="value-v")
    assert (row["state_outcome"], row["false_precision"]) == (False, True)


def test_a_referent_typed_as_a_child_of_the_promoted_type_uses_the_promoted_entry(tmp_path):
    referent = entity("token-zed", "Token Zed", "type-child", in_seed=False)
    key = {
        "id": "d01",
        "category": "freshness",
        "variant": "promoted_entity_type",
        "endpoints": {"R": "the referent page"},
        "referent": {"match_tokens": ["token zed"], "wrong_core_types": ["person"]},
        "promoted_entry": {"registry": "entity-types", "key": "type-x"},
        "state_success": {"rule": "referent_type_lineage_includes_promoted"},
        "false_precision": {"wrong_core_types": ["person"]},
        "answer": answer_key("value-v"),
    }
    row = baseline_row(
        tmp_path,
        key,
        pages=[referent],
        changed=[referent["path"]],
        answer="value-v",
        vault_types={"type-child": {"parent_chain": ["type-x"]}, "type-x": {"parent_chain": []}},
    )
    assert row["success"] is True


def test_a_promoted_relation_edge_in_the_wrong_orientation_does_not_hold(tmp_path):
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    required = {"relation": "relation-s", "from": "A", "to": "B"}
    key = {
        "id": "d01",
        "category": "freshness",
        "variant": "promoted_relation",
        "endpoints": {"A": "Entity Alpha", "B": "Entity Beta"},
        "promoted_entry": {"registry": "relations", "key": "relation-s"},
        "state_success": {"rule": "pair_edge_lineage_includes_promoted", "required_edge": required},
        "false_precision": {"pairs": [{"between": ["A", "B"], "supported_edges": [required]}]},
        "answer": answer_key("beta"),
    }
    row = baseline_row(
        tmp_path,
        key,
        pages=[a, b],
        edges=[edge(b, "relation-s", a)],
        changed=[b["path"]],
        answer="beta",
    )
    assert row["state_outcome"] is False
    assert row["omissions"] == ["promoted_entry_not_used"]


def test_an_accepted_value_inside_a_longer_number_does_not_match():
    assert bounded_match("30", "It held at 30.")
    assert not bounded_match("30", "It held at 300.")
    assert not bounded_match("30", "It held at 130.")
    assert bounded_match("value-v", "VALUE–V")


def test_a_residual_answer_goes_to_the_judge_and_its_verdict_completes_the_table(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    write_run(out, "d01", "x1", pages=[a, b], answer="Entity Beta.")
    write_run(
        out,
        "d01",
        "x2",
        pages=[a, b],
        answer="The other one.",
        tool_results=["... Entity Beta ..."],
    )

    pending = score(out, keys, baseline="x1", candidate="x2")
    assert pending["statistics"].startswith("unavailable")
    [item] = pending["judge_items"]
    assert set(item) == {"id", "question", "key_fact", "accepted_values", "answer"}
    assert pending["judge_key"][item["id"]] == {"situation_id": "d01", "arm": "x2"}

    judged = score(
        out,
        keys,
        baseline="x1",
        candidate="x2",
        judgements={"judge_model": "judge-model-id", "verdicts": {item["id"]: False}},
    )
    assert judged["rows"][0]["candidate"]["omissions"] == ["answer_lacks_fact"]
    assert judged["statistics"]["success"]["c"] == 1


def test_an_answer_the_fresh_session_never_received_fails_without_a_judge(tmp_path):
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    row = baseline_row(
        tmp_path,
        pair_key("d01", "generic_or_none", [], "none_required"),
        pages=[a, b],
        answer="The other one.",
    )
    assert (row["residual"], row["success"], row["omissions"]) == (
        False,
        False,
        ["answer_lacks_fact"],
    )


def test_a_struck_situation_leaves_both_arms_without_opening_its_key(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    keys.mkdir()
    write_run(out, "d01", "x1", pages=[], status="struck")
    write_run(out, "d01", "x2", pages=[], answer="anything")
    report = score(out, keys, baseline="x1", candidate="x2")
    assert (report["rows"], report["struck"], report["void"]) == ([], ["d01"], False)


def test_a_run_of_another_case_file_than_the_keys_is_refused(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(
        keys, {**pair_key("d01", "generic_or_none", [], "none_required"), "case_sha256": "0" * 64}
    )
    write_run(out, "d01", "x1", pages=[])
    write_run(out, "d01", "x2", pages=[])
    with pytest.raises(ScoreRefused, match="case file"):
        score(out, keys, baseline="x1", candidate="x2")


def test_the_score_command_writes_the_paired_table_and_the_decision_rule(tmp_path):
    out, keys, to = tmp_path / "out", tmp_path / "keys", tmp_path / "scores"
    a, b = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")
    for situation in ("d01", "d02"):
        write_key(keys, pair_key(situation, "specific_fits", SUPPORTED, "pair_edge_supported"))
        write_run(out, situation, "x1", pages=[a, b], answer="Entity Beta.")
        write_run(
            out,
            situation,
            "x2",
            pages=[a, b],
            edges=[edge(a, "relation-r", b)],
            changed=[a["path"]],
            answer="Entity Beta.",
        )

    code = replay_main(
        [
            "score",
            "--out",
            str(out),
            "--keys",
            str(keys),
            "--baseline",
            "x1",
            "--candidate",
            "x2",
            "--to",
            str(to),
        ]
    )

    assert code == 0
    stats = json.loads((to / "scores.json").read_text(encoding="utf-8"))["statistics"]
    assert {k: stats["success"][k] for k in "abcd"} == {"a": 0, "b": 2, "c": 0, "d": 0}
    assert stats["decision_rule"] == {
        "gain_established": True,
        "false_precision_guard": True,
        "merge": True,
    }
    assert json.loads((to / "judge-items.json").read_text(encoding="utf-8")) == []
