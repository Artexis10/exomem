"""The f34 scorer: the scoring branches whose miss would flip a situation's result.

Every situation, key, name and value here is an invented placeholder. Each test
builds the end state it scores, so the case reads on its own.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml
from membench.utility.organisation_replay.__main__ import main as replay_main
from membench.utility.organisation_replay.score import (
    DASHES,
    ScoreRefused,
    bounded_match,
    newcombe_paired,
    score,
    statistics,
)

KB = "Knowledge Base"
CASE_SHA = hashlib.sha256(b"placeholder case file").hexdigest()


def relation(generic=False, direction="directed", inverse=None) -> dict:
    return {
        "parent_chain": [],
        "generic": generic,
        "direction": direction,
        "inverse": inverse,
        "aliases": [],
    }


RELATIONS = {
    "relation-r": relation(inverse="relation-q"),
    "relation-q": relation(inverse="relation-r"),
    "relation-s": relation(),
    "relates_to": relation(generic=True, direction="symmetric"),
    "mentions": relation(),
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


def edge(subject: dict, relation_key: str, target: dict) -> dict:
    return {
        "holder": subject["path"],
        "subject": subject["path"],
        "relation": relation_key,
        "target_path": target["path"],
    }


def call(text: str) -> dict:
    return {
        "turn": 0,
        "seq": 1,
        "tool": "search",
        "arguments": {},
        "request_bytes": 2,
        "result": {"content": [{"type": "text", "text": text}]},
        "response_bytes": len(text),
        "is_error": False,
        "refusal_code": None,
        "mutating": False,
        "target": None,
        "retry_hash": None,
    }


def session(role: str, *, answer: str = "", calls: list[dict] = ()) -> dict:
    return {
        "role": role,
        "session_id": f"session-{role}",
        "attempt": 1,
        "init": {},
        "transcript_path": "transcript.jsonl",
        "tap_log_path": "tap.jsonl",
        "calls": list(calls),
        "final_answer": answer,
        "turn_results": [],
        "guard_refusals": 0,
    }


def write_run(
    out: Path,
    situation: str,
    arm: str,
    *,
    pages: list[dict] = (),
    edges: list[dict] = (),
    changed: list[str] = (),
    answer: str = "",
    tool_results: list[str] = (),
    vault_types: dict | None = None,
    status: str = "ok",
    shape: str = "hookless",
    reasons: dict | None = None,
) -> dict:
    """A run file that conforms to SCHEMA.md section 2, with the given end state."""

    run_dir = out / situation / arm
    run_dir.mkdir(parents=True)
    for name in ("transcript.jsonl", "tap.jsonl"):
        (run_dir / name).write_text("", encoding="utf-8")
    run = {
        "situation_id": situation,
        "situation_sha256": CASE_SHA,
        "arm": arm,
        "client_shape": shape,
        "binding_ref": f"{arm}-binding",
        "seed": {
            "tree_digest": "t",
            "projection_digest": "p",
            "registry_overlay_digests": {},
            "check": "pass",
            "settled_promotion_ids": [],
        },
        "sessions": [
            session("capture"),
            session("fresh", answer=answer, calls=[call(t) for t in tool_results]),
        ],
        "end_state": {
            "pages": list(pages),
            "pages_changed": list(changed),
            "edges": list(edges),
            "relations": RELATIONS,
            "entity_types": {"core": ["concept", "person"], "vault": vault_types or {}},
            "registry_history": [],
            "homes": {},
            "operation_log": {"edge_review_reasons": reasons or {"status": "unavailable"}},
            "promotions": [],
        },
        "faults": [],
        "status": status,
    }
    if status == "struck":
        run["sessions"] = []
        run["faults"] = [{"session": "seed", "attempt": 1, "kind": "harness_fault", "detail": "x"}]
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return run


def write_key(keys: Path, key: dict) -> None:
    keys.mkdir(exist_ok=True)
    view = {
        "question": "Placeholder question?",
        "key_fact": "Placeholder fact.",
        "accepted_values": key.pop("accepted"),
    }
    body = {
        "case_sha256": CASE_SHA,
        "client_shape": "hookless",
        "answer": view,
        "judge_reference_view": dict(view),
        **key,
    }
    prefix = "k" if key["id"].startswith("h") else "dk"
    (keys / f"{prefix}{key['id'][1:]}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def pair_key(situation: str, variant: str, supported: list[dict], rule: str) -> dict:
    return {
        "id": situation,
        "category": "relation",
        "variant": variant,
        "endpoints": {"A": "Entity Alpha", "B": "Entity Beta"},
        "state_success": {"rule": rule, "pair": ["A", "B"]},
        "supported_edges": supported,
        "false_precision": {"pairs": [{"between": ["A", "B"], "supported_edges": supported}]},
        "accepted": ["beta"],
    }


def baseline_row(tmp_path: Path, key: dict, **run) -> dict:
    """Score one situation whose candidate run repeats the baseline's, and return the baseline row."""

    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, key)
    write_run(out, key["id"], "x1", **run)
    write_run(out, key["id"], "x2", **run)
    report = score(out, keys, baseline="x1", candidate="x2", expect=[key["id"]])
    return report["rows"][0]["baseline"]


SUPPORTED = [{"relation": "relation-r", "from": "A", "to": "B"}]
ALPHA, BETA = entity("entity-alpha", "Entity Alpha"), entity("entity-beta", "Entity Beta")


# -- the statistics and the decision ------------------------------------------


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


def test_the_score_command_writes_the_paired_table_and_the_decision_rule(tmp_path):
    out, keys, to = tmp_path / "out", tmp_path / "keys", tmp_path / "scores"
    for situation in ("d01", "d02"):
        write_key(keys, pair_key(situation, "specific_fits", SUPPORTED, "pair_edge_supported"))
        write_run(out, situation, "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
        write_run(
            out,
            situation,
            "x2",
            pages=[ALPHA, BETA],
            edges=[edge(ALPHA, "relation-r", BETA)],
            changed=[ALPHA["path"]],
            answer="Entity Beta.",
        )
    argv = ["score", "--out", str(out), "--keys", str(keys), "--baseline", "x1"]
    argv += ["--candidate", "x2", "--expect", "d01", "--expect", "d02", "--to", str(to)]

    assert replay_main(argv) == 0
    report = json.loads((to / "scores.json").read_text(encoding="utf-8"))
    stats = report["statistics"]
    assert report["verdict"] == "decided"
    assert {k: stats["success"][k] for k in "abcd"} == {"a": 0, "b": 2, "c": 0, "d": 0}
    assert stats["decision_rule"] == {
        "gain_established": True,
        "false_precision_guard": True,
        "merge": True,
    }
    # Per-shape and per-category tables are description only.
    assert all("decision_rule" not in table for table in report["descriptive"].values())
    assert json.loads((to / "judge" / "judge-items.json").read_text(encoding="utf-8")) == []


def test_a_run_with_more_than_two_struck_situations_decides_nothing(tmp_path):
    out, keys, to = tmp_path / "out", tmp_path / "keys", tmp_path / "scores"
    for situation in ("d01", "d02", "d03"):
        write_run(out, situation, "x1", status="struck")
        write_run(out, situation, "x2")
    write_key(keys, pair_key("d04", "specific_fits", SUPPORTED, "pair_edge_supported"))
    write_run(out, "d04", "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
    write_run(
        out,
        "d04",
        "x2",
        pages=[ALPHA, BETA],
        edges=[edge(ALPHA, "relation-r", BETA)],
        changed=[ALPHA["path"]],
        answer="Entity Beta.",
    )
    argv = ["score", "--out", str(out), "--keys", str(keys), "--baseline", "x1"]
    argv += ["--candidate", "x2", "--to", str(to)]
    argv += [arg for s in ("d01", "d02", "d03", "d04") for arg in ("--expect", s)]

    assert replay_main(argv) == 1
    report = json.loads((to / "scores.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "void"
    assert isinstance(report["statistics"], str)
    assert sorted(report["struck"]) == ["d01", "d02", "d03"]


def test_a_declared_situation_with_one_arm_missing_leaves_the_run_undecided(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    write_run(out, "d01", "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
    write_run(out, "d01", "x2", pages=[ALPHA, BETA], answer="Entity Beta.")
    write_run(out, "d02", "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
    report = score(out, keys, baseline="x1", candidate="x2", expect=["d01", "d02", "d03"])
    assert (report["verdict"], report["incomplete"]) == ("incomplete", ["d02", "d03"])
    assert isinstance(report["statistics"], str)


# -- the key and run files ----------------------------------------------------


def test_a_key_field_the_scorer_does_not_know_is_refused_by_name(tmp_path):
    key = pair_key("d01", "generic_or_none", [], "none_required")
    key["false_precision"]["new_rule"] = ["anything"]
    with pytest.raises(ScoreRefused, match=r"key\.false_precision\.new_rule"):
        baseline_row(tmp_path, key, pages=[ALPHA, BETA])


def test_an_all_struck_held_out_run_still_checks_its_manifest(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    keys.mkdir()
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(f"{CASE_SHA}  h01.yaml\n", encoding="utf-8")
    write_run(out, "h01", "x1", status="struck")
    write_run(out, "h01", "x2", status="struck")
    with pytest.raises(ScoreRefused, match="preregistered"):
        score(out, keys, baseline="x1", candidate="x2", manifest=manifest)


def test_a_key_without_a_field_its_rule_reads_is_refused_by_name(tmp_path):
    key = pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported")
    del key["state_success"]["pair"]
    with pytest.raises(ScoreRefused, match=r"missing fields \['state_success\.pair'\]"):
        baseline_row(tmp_path, key, pages=[ALPHA, BETA])


def test_a_situation_the_run_did_not_declare_is_refused(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    for situation in ("d01", "d02"):
        write_run(out, situation, "x1", pages=[ALPHA, BETA])
        write_run(out, situation, "x2", pages=[ALPHA, BETA])
    with pytest.raises(ScoreRefused, match="undeclared"):
        score(out, keys, baseline="x1", candidate="x2", expect=["d01"])


def test_a_held_out_key_with_a_manifest_that_is_not_the_preregistered_one_is_refused(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, {**pair_key("h01", "generic_or_none", [], "none_required")})
    digest = hashlib.sha256((keys / "k01.yaml").read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(f"{digest}  k01.yaml\n{CASE_SHA}  h01.yaml\n", encoding="utf-8")
    write_run(out, "h01", "x1", pages=[ALPHA, BETA])
    write_run(out, "h01", "x2", pages=[ALPHA, BETA])
    with pytest.raises(ScoreRefused, match="preregistered"):
        score(out, keys, baseline="x1", candidate="x2", manifest=manifest)


def test_a_run_of_another_case_file_than_the_keys_is_refused(tmp_path):
    key = {**pair_key("d01", "generic_or_none", [], "none_required"), "case_sha256": "0" * 64}
    with pytest.raises(ScoreRefused, match="case file"):
        baseline_row(tmp_path, key, pages=[ALPHA, BETA])


def test_a_run_file_without_its_fresh_session_is_refused(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    write_run(out, "d01", "x1", pages=[ALPHA, BETA])
    run = write_run(out, "d01", "x2", pages=[ALPHA, BETA])
    run["sessions"] = run["sessions"][:1]
    (out / "d01" / "x2" / "run.json").write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(ScoreRefused, match="SCHEMA.md"):
        score(out, keys, baseline="x1", candidate="x2", expect=["d01"])


def test_a_struck_situation_leaves_both_arms_without_opening_its_key(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    keys.mkdir()
    write_run(out, "d01", "x1", status="struck")
    write_run(out, "d01", "x2", answer="anything")
    report = score(out, keys, baseline="x1", candidate="x2", expect=["d01"])
    assert (report["rows"], list(report["struck"])) == ([], ["d01"])
    assert report["struck"]["d01"] == {"x1": ["harness_fault: x"]}


def test_a_missing_key_file_and_a_foreign_kb_folder_refuse_with_exit_2(tmp_path, monkeypatch):
    out, keys = tmp_path / "out", tmp_path / "keys"
    keys.mkdir()
    write_run(out, "d01", "x1", pages=[ALPHA, BETA])
    write_run(out, "d01", "x2", pages=[ALPHA, BETA])
    argv = ["score", "--out", str(out), "--keys", str(keys), "--baseline", "x1"]
    argv += ["--candidate", "x2", "--expect", "d01", "--to", str(tmp_path / "scores")]
    assert replay_main(argv) == 2
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Another Folder")
    assert replay_main(argv) == 2


# -- the state outcomes and false precision -----------------------------------


def test_a_supported_edge_written_as_its_registered_inverse_is_the_same_fact(tmp_path):
    row = baseline_row(
        tmp_path,
        pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported"),
        pages=[ALPHA, BETA],
        edges=[edge(BETA, "relation-q", ALPHA)],
        changed=[BETA["path"]],
        answer="Entity Beta.",
    )
    assert (row["state_outcome"], row["false_precision"], row["success"]) == (True, False, True)


def test_a_reversed_directed_edge_is_false_precision_and_no_state(tmp_path):
    row = baseline_row(
        tmp_path,
        pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported"),
        pages=[ALPHA, BETA],
        edges=[edge(BETA, "relation-r", ALPHA)],
        changed=[BETA["path"]],
        answer="Entity Beta.",
    )
    assert row["state_outcome"] is False
    assert row["false_precision_claims"] == ["unsupported_edge:relation-r"]
    assert row["success"] is False


def test_only_a_generic_edge_where_a_specific_one_is_required_is_an_omission(tmp_path):
    row = baseline_row(
        tmp_path,
        pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported"),
        pages=[ALPHA, BETA],
        edges=[edge(ALPHA, "relates_to", BETA)],
        changed=[ALPHA["path"]],
        answer="Entity Beta.",
    )
    assert row["omissions"] == ["generic_edge_only"]
    assert row["false_precision"] is False


def test_a_typed_edge_where_only_a_generic_edge_fits_is_false_precision(tmp_path):
    row = baseline_row(
        tmp_path,
        pair_key("d01", "generic_or_none", [], "none_required"),
        pages=[ALPHA, BETA],
        edges=[edge(ALPHA, "relation-s", BETA)],
        changed=[ALPHA["path"]],
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
        "accepted": ["token zed"],
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
        "accepted": ["value-v"],
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
        "accepted": ["value-v"],
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
        "accepted": ["value-v"],
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
        "accepted": ["value-v"],
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


def test_a_promoted_relation_edge_in_the_wrong_orientation_holds_no_state_and_no_code(tmp_path):
    required = {"relation": "relation-s", "from": "A", "to": "B"}
    key = {
        "id": "d01",
        "category": "freshness",
        "variant": "promoted_relation",
        "endpoints": {"A": "Entity Alpha", "B": "Entity Beta"},
        "promoted_entry": {"registry": "relations", "key": "relation-s"},
        "state_success": {"rule": "pair_edge_lineage_includes_promoted", "required_edge": required},
        "false_precision": {"pairs": [{"between": ["A", "B"], "supported_edges": [required]}]},
        "accepted": ["beta"],
    }
    row = baseline_row(
        tmp_path,
        key,
        pages=[ALPHA, BETA],
        edges=[edge(BETA, "relation-s", ALPHA)],
        changed=[BETA["path"]],
        answer="beta",
    )
    assert (row["state_outcome"], row["omissions"]) == (False, [])


# -- the answer and the residual judge -----------------------------------------


def test_an_accepted_value_inside_a_longer_number_does_not_match():
    assert bounded_match("30", "It held at 30.")
    assert not bounded_match("30", "It held at 300.")
    assert not bounded_match("30", "It held at 130.")
    assert bounded_match("value-v", "VALUE−V")


def test_the_dash_set_is_the_unicode_dash_property():
    regex = pytest.importorskip("regex")
    dash = {chr(c) for c in range(sys.maxunicode + 1) if regex.match(r"\p{Dash}", chr(c))}
    assert dash == DASHES


def test_a_residual_answer_goes_to_the_judge_and_its_verdict_completes_the_table(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    key = pair_key("d01", "generic_or_none", [], "none_required")
    write_key(keys, key)
    written = yaml.safe_load((keys / "dk01.yaml").read_text(encoding="utf-8"))
    written["judge_reference_view"]["question"] = "The judge's own wording?"
    (keys / "dk01.yaml").write_text(yaml.safe_dump(written), encoding="utf-8")
    write_run(out, "d01", "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
    write_run(
        out, "d01", "x2", pages=[ALPHA, BETA], answer="The other one.", tool_results=["Entity Beta"]
    )

    pending = score(out, keys, baseline="x1", candidate="x2", expect=["d01"])
    assert pending["verdict"] == "awaiting_judge"
    [item] = pending["judge_items"]
    assert set(item) == {"id", "question", "key_fact", "accepted_values", "answer"}
    assert item["question"] == "The judge's own wording?"
    assert pending["judge_key"][item["id"]]["arm"] == "x2"

    judged = score(
        out,
        keys,
        baseline="x1",
        candidate="x2",
        expect=["d01"],
        judgements={"judge_model": "judge-model-id", "verdicts": {item["id"]: False}},
        judge_key=pending["judge_key"],
    )
    assert judged["rows"][0]["candidate"]["omissions"] == ["answer_lacks_fact"]
    assert (judged["statistics"]["success"]["c"], judged["judge_model"]) == (1, "judge-model-id")


def test_a_verdict_on_an_answer_that_changed_since_its_item_was_issued_is_refused(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    write_run(out, "d01", "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
    write_run(out, "d01", "x2", pages=[ALPHA, BETA], answer="Other.", tool_results=["Entity Beta"])
    pending = score(out, keys, baseline="x1", candidate="x2", expect=["d01"])
    [item] = pending["judge_items"]
    run_file = out / "d01" / "x2" / "run.json"
    run = json.loads(run_file.read_text(encoding="utf-8"))
    run["sessions"][1]["final_answer"] = "Another answer."
    run_file.write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(ScoreRefused, match="answer changed"):
        score(
            out,
            keys,
            baseline="x1",
            candidate="x2",
            expect=["d01"],
            judgements={"judge_model": "judge-model-id", "verdicts": {item["id"]: True}},
            judge_key=pending["judge_key"],
        )


def test_generic_edge_reasons_are_counted_per_arm_or_unavailable(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    write_run(out, "d01", "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
    record = {"status": "available", "generic_added_edges": 3, "reasons_recorded": 2}
    write_run(out, "d01", "x2", pages=[ALPHA, BETA], answer="Entity Beta.", reasons=record)
    report = score(out, keys, baseline="x1", candidate="x2", expect=["d01"])
    counts = report["diagnostics"]["edge_review_reasons"]
    assert counts["baseline"] == {"status": "unavailable"}
    assert counts["candidate"] == {
        "status": "available",
        "generic_added_edges": 3,
        "with_reason": 2,
        "without_reason": 1,
    }


def test_a_judge_pass_never_clears_false_precision(tmp_path):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    for arm in ("x1", "x2"):
        write_run(
            out,
            "d01",
            arm,
            pages=[ALPHA, BETA],
            edges=[edge(ALPHA, "relation-s", BETA)],
            changed=[ALPHA["path"]],
            answer="The other one.",
            tool_results=["Entity Beta"],
        )
    pending = score(out, keys, baseline="x1", candidate="x2", expect=["d01"])
    verdicts = {item["id"]: True for item in pending["judge_items"]}
    judged = score(
        out,
        keys,
        baseline="x1",
        candidate="x2",
        expect=["d01"],
        judgements={"judge_model": "judge-model-id", "verdicts": verdicts},
        judge_key=pending["judge_key"],
    )
    assert judged["rows"][0]["baseline"]["success"] is False


def test_an_answer_without_the_state_outcome_never_reaches_the_judge(tmp_path):
    row = baseline_row(
        tmp_path,
        pair_key("d01", "specific_fits", SUPPORTED, "pair_edge_supported"),
        pages=[ALPHA, BETA],
        answer="The other one.",
        tool_results=["Entity Beta"],
    )
    assert (row["residual"], row["success"]) == (False, False)
    assert row["omissions"] == ["no_edge_between_pair", "answer_lacks_fact"]


def test_an_answer_the_fresh_session_never_received_fails_without_a_judge(tmp_path):
    row = baseline_row(
        tmp_path,
        pair_key("d01", "generic_or_none", [], "none_required"),
        pages=[ALPHA, BETA],
        answer="The other one.",
    )
    assert (row["residual"], row["success"], row["omissions"]) == (
        False,
        False,
        ["answer_lacks_fact"],
    )


@pytest.mark.parametrize(
    ("verdicts", "match"),
    [({"ID": "false"}, "JSON booleans"), ({"not-an-item": True}, "no residual item")],
)
def test_a_verdict_that_is_not_a_boolean_or_names_no_item_is_refused(tmp_path, verdicts, match):
    out, keys = tmp_path / "out", tmp_path / "keys"
    write_key(keys, pair_key("d01", "generic_or_none", [], "none_required"))
    write_run(out, "d01", "x1", pages=[ALPHA, BETA], answer="Entity Beta.")
    write_run(out, "d01", "x2", pages=[ALPHA, BETA], answer="Other.", tool_results=["Entity Beta"])
    pending = score(out, keys, baseline="x1", candidate="x2", expect=["d01"])
    [item] = pending["judge_items"]
    verdicts = {item["id"] if k == "ID" else k: v for k, v in verdicts.items()}
    with pytest.raises(ScoreRefused, match=match):
        score(
            out,
            keys,
            baseline="x1",
            candidate="x2",
            expect=["d01"],
            judgements={"judge_model": "judge-model-id", "verdicts": verdicts},
            judge_key=pending["judge_key"],
        )
