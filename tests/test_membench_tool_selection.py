"""The tool-selection evaluation: the behaviours whose miss would corrupt the decision.

Each test names the failure only it catches. Tools are named only through the
product's own resolver, so the same scoring serves every tool shape. The
recorded sessions under ``tests/fixtures/membench/tool_selection`` are real
``claude -p`` runs against the grouped surface, not hand-written transcripts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from membench.utility.switch_eval import protocol
from membench.utility.switch_eval.session import (
    PlannedSession,
    SessionInputs,
    _turn_record,
    classify,
    run_session,
)
from membench.utility.switch_eval.state import Layout, Slot, StateError
from membench.utility.switch_eval.tap import __file__ as TAP_SCRIPT
from membench.utility.tool_selection import cases as case_module
from membench.utility.tool_selection import executor as ex
from membench.utility.tool_selection import plan as planning
from membench.utility.tool_selection import readout as ro
from membench.utility.tool_selection import score
from membench.utility.tool_selection.__main__ import main
from membench.utility.tool_selection.cases import (
    DEV_PLAN,
    Case,
    build_case,
    dev_cases,
    dev_cases_from_plan,
    generate_heldout,
)
from membench.utility.tool_selection.resolve import grouped_resolver, product_resolver, resolve_call
from membench.utility.tool_selection.score import (
    CaseResult,
    answer_matches,
    parse_answer,
    score_session,
)
from membench.utility.tool_selection.templates import (
    PLAN_COMMAND,
    PLAN_PATH,
    RECORD_COMMAND,
    TEMPLATES,
    SeedStep,
    _manifest_text,
    records_path,
)
from membench.utility.tool_selection.vault import apply_steps, read_end_state, seed_vault

FIXTURES = Path(__file__).parent / "fixtures" / "membench" / "tool_selection"


# --------------------------------------------------------------------------
# Cases.
# --------------------------------------------------------------------------


def test_the_committed_dev_file_is_what_the_committed_plan_builds() -> None:
    # A template whose draw changed would silently change the measured cases;
    # this makes that a visible diff instead.
    assert [c.as_dict() for c in dev_cases()] == [c.as_dict() for c in dev_cases_from_plan()]


def test_the_dev_set_has_twenty_cases_over_at_least_six_template_groups() -> None:
    cases = dev_cases()
    per_template = Counter(case.template_id for case in cases)

    assert len(cases) == 20
    assert len(per_template) >= protocol.MIN_GROUPS
    assert set(per_template) == set(TEMPLATES)
    assert min(per_template.values()) >= 2
    assert len({case.case_id for case in cases}) == 20
    # The held-out generator draws above the floor, so no held-out case can reuse a dev seed.
    assert max(case.seed for case in cases) < case_module.HELDOUT_SEED_FLOOR


def test_the_held_out_generator_never_reuses_a_dev_seed_and_spreads_over_templates() -> None:
    dev_seeds = {seed for _template, seeds in DEV_PLAN for seed in seeds}

    generated = generate_heldout(96, master_seed=7)

    assert len(generated) == 96
    assert not dev_seeds & {case.seed for case in generated}
    assert len({case.case_id for case in generated}) == 96
    assert set(Counter(case.template_id for case in generated).values()) == {96 // len(TEMPLATES)}


def test_one_master_seed_regenerates_the_same_held_out_set_and_another_does_not() -> None:
    first = [case.case_id for case in generate_heldout(24, master_seed=7)]

    assert first == [case.case_id for case in generate_heldout(24, master_seed=7)]
    assert first != [case.case_id for case in generate_heldout(24, master_seed=8)]


def test_the_generate_stage_writes_a_held_out_file_once(tmp_path: Path) -> None:
    out = tmp_path / "heldout.json"

    assert main(["generate", "--count", "16", "--master-seed", "3", "--out", str(out)]) == 0
    assert len(case_module.load_cases(out)) == 16
    with pytest.raises(SystemExit, match="generated once"):
        main(["generate", "--count", "16", "--master-seed", "4", "--out", str(out)])


# --------------------------------------------------------------------------
# Keys agree with what the product stores.
#
# A key computed from a case's parameters is only a key if a correct session's
# end state satisfies it, so each template's correct operation is carried out
# through the product's own commands and the key is checked against the vault
# that results. A field the key names differently from the product would fail
# every correct session and read as a worse tool shape.
# --------------------------------------------------------------------------


def _correct_write(case: Case) -> list[SeedStep]:
    """The operation that does the task, as a user's tool call would, from the case parameters."""

    p = case.params
    if case.template_id == "records-log-entry":
        tracker, target = p["tracker"], p["target"]
        item = {"occurred_on": target["date"], tracker["measure"]: target["value"], "note": target["note"]}
        return [SeedStep(RECORD_COMMAND, "append", {"collection": records_path(tracker), "item": item})]
    if case.template_id == "records-update-row":
        tracker, row = p["tracker"], p["rows"][p["index"]]
        return [
            SeedStep(
                RECORD_COMMAND,
                "update",
                {"collection": records_path(tracker), "changes": {tracker["measure"]: p["new_value"]}},
                match={"occurred_on": row["date"]},
            )
        ]
    if case.template_id == "records-create-collection":
        spec = p["collection"]
        number = "integer" if spec["number_kind"] == "whole" else "number"
        text = _manifest_text(
            title=spec["title"],
            profile="records",
            source="Events",
            natural_key=[spec["date_field"]],
            fields={
                spec["date_field"]: {"type": "date", "required": True},
                spec["number_field"]: {"type": number},
                spec["text_field"]: {"type": "string"},
            },
        )
        path = f"Knowledge Base/Records/{spec['dir']}/_collection.md"
        return [SeedStep(RECORD_COMMAND, "create", {"manifest_path": path, "manifest_text": text})]
    if case.template_id == "plan-add-item":
        item = {"title": p["new_title"], "priority": p["new_priority"]}
        return [SeedStep(PLAN_COMMAND, "add", {"collection": PLAN_PATH, "item": item})]
    if case.template_id == "plan-triage-item":
        target = p["items"][p["target"]]
        transition = {"status": "active", "commitment": "committed", "horizon": p["horizon"]}
        return [
            SeedStep(
                PLAN_COMMAND, "triage", {"collection": PLAN_PATH, "transition": transition},
                match={"title": target["title"]},
            )
        ]
    raise AssertionError(f"{case.template_id} is not a write template")


def _answer_from_the_product(vault: Path, case: Case):
    """What a correct reader would conclude from the product's own output."""

    from exomem.plan_memory import plan_memory
    from exomem.record_memory import record_memory

    p = case.params
    if case.template_id == "records-range-total":
        tracker = p["tracker"]
        rows = record_memory(vault, "query", collection=records_path(tracker))["rows"]
        return sum(
            row[tracker["measure"]] for row in rows if p["from_date"] <= row["occurred_on"] <= p["to_date"]
        )
    if case.template_id == "plan-committed-week":
        rows = plan_memory(vault, "query", collection=PLAN_PATH)["rows"]
        return [r["title"] for r in rows if r["commitment"] == "committed" and r["horizon"] == "week"]
    if case.template_id == "plan-item-history":
        rows = plan_memory(vault, "query", collection=PLAN_PATH)["rows"]
        row = next(r for r in rows if r["title"] == p["target"]["title"])
        revisions = plan_memory(vault, "history", collection=PLAN_PATH, plan_id=row["plan_id"])["revisions"]
        return revisions[-1]["prior"]["values"][p["asked_field"]]
    raise AssertionError(f"{case.template_id} is not a read template")


WRITE_TEMPLATES = sorted(t for t, template in TEMPLATES.items() if template.kind == "write")
READ_TEMPLATES = sorted(t for t, template in TEMPLATES.items() if template.kind == "read")


def _dev_case(template_id: str) -> Case:
    return next(case for case in dev_cases() if case.template_id == template_id)


@pytest.mark.parametrize("template_id", WRITE_TEMPLATES)
def test_a_write_templates_key_accepts_the_product_after_the_correct_operation_and_not_before(
    template_id: str, tmp_path: Path
) -> None:
    case = _dev_case(template_id)
    vault = tmp_path / "vault"
    seed_vault(vault, case.seed_steps())
    before = read_end_state(vault)

    apply_steps(vault, _correct_write(case))
    after = read_end_state(vault)

    assert score.check_end_state(case.key, before, after) == []
    assert score.check_end_state(case.key, before, before) != []


@pytest.mark.parametrize("template_id", READ_TEMPLATES)
def test_a_read_templates_answer_is_what_the_product_returns_for_the_seeded_facts(
    template_id: str, tmp_path: Path
) -> None:
    case = _dev_case(template_id)
    vault = tmp_path / "vault"
    seed_vault(vault, case.seed_steps())

    answer = _answer_from_the_product(vault, case)

    assert answer_matches(answer, case.key)


def test_seeding_a_vault_leaves_the_rows_the_case_names(tmp_path: Path) -> None:
    case = _dev_case("plan-add-item")
    vault = tmp_path / "vault"

    seed_vault(vault, case.seed_steps())

    rows = read_end_state(vault)["collections"][PLAN_PATH]["rows"]
    assert sorted(row["title"] for row in rows) == sorted(item["title"] for item in case.params["items"])


def test_the_seeding_child_process_builds_a_vault_the_end_state_reader_can_read(tmp_path: Path) -> None:
    # The run seeds each case in a child with every product location pinned in
    # scratch; this is the boundary a live run depends on.
    case = _dev_case("plan-add-item")
    spec = ex.arm_spec("grouped", ("*",))
    run = tmp_path / "run"
    run.mkdir()

    seeded = ex.seed_case(run, case, parent_env=dict(os.environ), spec=spec)

    assert seeded["case_id"] == case.case_id
    titles = {row["title"] for row in read_end_state(run / "seeded" / case.case_id / "vault")["collections"][PLAN_PATH]["rows"]}
    assert titles == {item["title"] for item in case.params["items"]}
    assert ex.seed_case(run, case, parent_env=dict(os.environ), spec=spec) == seeded


# --------------------------------------------------------------------------
# Scoring.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        ('{"answer": 8}', 8),
        ('The total is 8.\n\n{"answer": 8}', None),
        ('```json\n{"answer": ["a", "b"]}\n```', ["a", "b"]),
        ('{"answer": 3} then, correcting myself, {"answer": 4}', None),
        ('The delta is not valid, so I am not returning `{"answer":"valid"}`. I saved nothing.', None),
        ('> ```json\n> {"answer": 8}\n> ```', None),
        ('[{"answer": 8}]', None),
        ('```text\n{"answer": 8}\n```', None),
        ('{"result": 8}', None),
        ("no object here", None),
        (None, None),
    ],
)
def test_answer_decoding_requires_whole_json_or_one_whole_json_fence(reply, answer) -> None:
    assert parse_answer(reply) == answer


def test_answers_compare_by_value_in_the_mode_the_key_names() -> None:
    number = _dev_case("records-range-total").key
    titles = _dev_case("plan-committed-week").key
    text = _dev_case("plan-item-history").key

    assert answer_matches(number.answer, number)
    assert answer_matches(float(number.answer), number)
    assert not answer_matches(number.answer + 1, number)
    assert not answer_matches(True, replace(number, answer=1))
    assert not answer_matches(str(number.answer), number)
    assert answer_matches(list(reversed(titles.answer)), titles)
    assert not answer_matches([*titles.answer, "an extra title"], titles)
    assert not answer_matches(titles.answer[:-1], titles)
    assert answer_matches(f"  {text.answer.upper()} ", text)
    assert not answer_matches("another value", text)


def _rows_of(case: Case) -> list[dict]:
    tracker = case.params["tracker"]
    return [
        {"occurred_on": row["date"], tracker["measure"]: row["value"], "note": row["note"]}
        for row in case.params["rows"]
    ]


def _records_state(case: Case, rows: list[dict], *, extra: dict | None = None) -> dict:
    path = records_path(case.params["tracker"])
    collection = {"profile": "records", "title": "t", "natural_key": ["occurred_on"], "fields": {}, "rows": rows}
    return {"collections": {path: collection, **(extra or {})}}


def _session(
    case: Case, *, answer: str | None = None, before: dict, after: dict, calls: list[dict] | None = None,
    outcome: str = "ok", arm: str = "grouped", repeat: int = 1,
) -> tuple[dict, list[dict]]:
    session = {
        "arm": arm,
        "repeat": repeat,
        "outcome": outcome,
        "answer": answer,
        "turns": [{"input_tokens": 1000, "output_tokens": 50, "wall_seconds": 4.0}],
        "facts": {
            "surface": {"schema_bytes": 90_000},
            "end_state_before": before,
            "end_state_after": after,
        },
    }
    return session, calls or []


def _call(ordinal: int, name: str, arguments: dict, *, codes: tuple[str, ...] = (), errored: bool = False) -> dict:
    return {
        "ordinal": ordinal, "name": name, "arguments": arguments, "outcome": "served",
        "errored": errored, "codes": list(codes),
    }


def _score(case: Case, session_and_calls: tuple[dict, list[dict]]) -> CaseResult:
    session, calls = session_and_calls
    return score_session(case, session, calls, grouped_resolver())


def test_a_write_task_succeeds_only_when_the_vault_ends_as_the_key_says() -> None:
    case = build_case("records-log-entry", 101)
    tracker, target = case.params["tracker"], case.params["target"]
    entry = {"occurred_on": target["date"], tracker["measure"]: target["value"], "note": target["note"]}
    other_path = "Knowledge Base/Records/Other/_collection.md"
    other = {"profile": "records", "title": "o", "natural_key": ["occurred_on"], "fields": {}}
    untouched = {other_path: {**other, "rows": [{"occurred_on": "2026-01-01", "note": "keep"}]}}
    start = _records_state(case, _rows_of(case), extra=untouched)

    def ended_with(rows: list[dict], other_rows=untouched[other_path]["rows"]) -> dict:
        return _records_state(case, rows, extra={other_path: {**other, "rows": other_rows}})

    def run(after: dict) -> CaseResult:
        return _score(case, _session(case, before=start, after=after))

    assert run(ended_with([*_rows_of(case), entry])).success
    assert not run(start).success
    assert not run(ended_with([*_rows_of(case), {**entry, tracker["measure"]: target["value"] + 1}])).success
    assert not run(ended_with([*_rows_of(case), entry, entry])).success
    # The right row is there, but a collection the task never named also changed.
    assert not run(ended_with([*_rows_of(case), entry], other_rows=[])).success


def test_read_answer_credit_requires_the_declared_whole_reply_format() -> None:
    case = build_case("records-range-total", 101)
    state = _records_state(case, _rows_of(case))
    total = case.key.answer

    def run(reply: str) -> CaseResult:
        return _score(case, _session(case, answer=reply, before=state, after=state))

    assert run(f'{{"answer": {total}}}').success
    assert run(f'```json\n{{"answer": {total}}}\n```').success
    assert not run(f'It adds up.\n{{"answer": {total}}}').success
    assert not run(f'{{"answer": {total + 1}}}').success
    assert not run("It adds up to a few.").success


def test_a_read_task_that_changed_the_vault_is_a_wrong_write_even_with_the_right_answer() -> None:
    case = build_case("records-range-total", 101)
    before = _records_state(case, _rows_of(case))
    after = _records_state(case, [*_rows_of(case), {"occurred_on": "2099-01-01", "note": "stray"}])

    result = _score(case, _session(case, answer=f'{{"answer": {case.key.answer}}}', before=before, after=after))

    assert result.wrong_write
    assert not result.success


def test_a_write_call_the_product_refused_is_not_a_wrong_write() -> None:
    case = build_case("records-range-total", 101)
    state = _records_state(case, _rows_of(case))
    refused = _call(
        1, "record_memory", {"action": "append", "collection": "x", "item": {}},
        codes=("INVALID_RECORD_ARGUMENTS",),
    )
    served = _call(2, "record_memory", {"action": "append", "collection": "x", "item": {"a": 1}})

    reply = f'{{"answer": {case.key.answer}}}'
    only_refused = _score(case, _session(case, answer=reply, before=state, after=state, calls=[refused]))
    served_without_a_visible_change = _score(case, _session(case, answer=reply, before=state, after=state, calls=[served]))

    assert not only_refused.wrong_write
    assert only_refused.success
    # A served write-capable call counts even when the state check finds
    # nothing, as when an update rewrites a row with its own values.
    assert served_without_a_visible_change.wrong_write
    assert served_without_a_visible_change.write_calls_served == 1


def _grouped_spelling(tool: str, operation: dict, **top) -> tuple[str, dict]:
    kind = operation["kind"]
    fields = {k: v for k, v in operation.items() if k != "kind"}
    return "record_memory", {"action": kind, **fields, **top}


def _nested_spelling(tool: str, operation: dict, **top) -> tuple[str, dict]:
    return tool, {"operation": operation, **top}


@pytest.mark.parametrize("arm,spell", [("grouped", _grouped_spelling), ("nested", _nested_spelling)])
def test_every_arm_reads_a_call_by_its_operation_and_counts_refusals_from_result_codes(arm, spell) -> None:
    """A call that names the right operation with a wrong field counts the same in every arm."""
    case = build_case("records-log-entry", 101)
    tracker, target = case.params["tracker"], case.params["target"]
    path = records_path(tracker)
    entry = {"occurred_on": target["date"], tracker["measure"]: target["value"], "note": target["note"]}
    start = _records_state(case, _rows_of(case))
    done = _records_state(case, [*_rows_of(case), entry])
    calls = [
        _call(1, *spell("read_records", {"kind": "inspect", "collection": path})),
        # The server refuses this append for its missing `why`; it still names the target operation.
        _call(2, *spell("change_records", {"kind": "append", "collection": path, "item": {}}), codes=("INVALID_RECORD_ARGUMENTS",)),
        # `response_detail` is a write tool's own argument beside the operation, in every shape.
        _call(
            3,
            *spell(
                "change_records",
                {"kind": "append", "collection": path, "item": entry, "why": "log it"},
                response_detail="full",
            ),
        ),
        _call(4, "a_tool_no_arm_serves", {}),
    ]

    session, calls = _session(case, before=start, after=done, calls=calls, arm=arm)
    result = score_session(case, session, calls, product_resolver())

    assert result.success
    assert result.calls == 4
    assert result.argument_refusals == 1
    assert result.first_correct_ordinal == 2
    assert result.unresolved_calls == 1
    assert result.write_calls_served == 1
    assert (result.schema_bytes, result.input_tokens, result.output_tokens) == (90_000, 1000, 50)


def test_the_product_resolver_reads_a_nested_call_as_its_server_does() -> None:
    resolver = product_resolver()

    nested_query = resolve_call(
        resolver, "read_records", {"operation": {"kind": "query", "collection": "c"}}, "nested"
    )
    nested_append = resolve_call(
        resolver,
        "change_records",
        {"operation": {"kind": "append", "collection": "c", "item": {"a": 1}, "why": "log it"}},
        "nested",
    )
    write_sent_to_read_tool = resolve_call(
        resolver, "read_records", {"operation": {"kind": "append", "collection": "c"}}, "nested"
    )
    grouped_form_under_nested = resolve_call(
        resolver, "read_records", {"action": "query", "collection": "c"}, "nested"
    )

    assert (nested_query.command, nested_query.action, nested_query.read_only) == ("record_memory", "query", True)
    assert (nested_append.command, nested_append.action, nested_append.read_only) == ("record_memory", "append", False)
    # The execution boundary keeps the selected command even when business fields refuse.
    assert write_sent_to_read_tool.command == "record_memory"
    assert write_sent_to_read_tool.action == "append"
    assert not write_sent_to_read_tool.resolved
    assert grouped_form_under_nested.command is None
    assert not grouped_form_under_nested.resolved


def test_resolution_reports_the_command_the_action_and_whether_it_reads() -> None:
    resolver = grouped_resolver()

    query = resolve_call(resolver, "record_memory", {"action": "query", "collection": "c"})
    append = resolve_call(resolver, "record_memory", {"action": "append", "collection": "c", "item": {}})
    unknown = resolve_call(resolver, "no_such_tool", {})

    assert (query.command, query.action, query.read_only) == ("record_memory", "query", True)
    assert (append.command, append.action, append.read_only) == ("record_memory", "append", False)
    assert not unknown.resolved


def test_a_session_that_did_not_finish_is_listed_unscored_and_not_counted_as_a_failure() -> None:
    case = build_case("records-range-total", 101)
    state = _records_state(case, _rows_of(case))

    result = _score(case, _session(case, before=state, after=state, outcome="harness_fault"))

    assert not result.scored
    assert not result.success


# --------------------------------------------------------------------------
# The readout: each arm against grouped, flipped over template groups.
# --------------------------------------------------------------------------


def _result(case: Case, arm: str, success: bool, *, repeat: int = 1, wrong_write: bool = False, scored: bool = True) -> CaseResult:
    return CaseResult(
        case.case_id, case.template_id, case.kind, arm, repeat, "ok" if scored else "harness_fault",
        scored=scored, success=success, wrong_write=wrong_write, calls=3, schema_bytes=90_000,
        input_tokens=1000, output_tokens=50,
    )


def _dev_by_id() -> dict[str, Case]:
    return {case.case_id: case for case in dev_cases()}


def _results(arm_success, *, arm: str = "nested", wrong_write=frozenset()) -> list[CaseResult]:
    """Grouped passes every dev case; ``arm`` follows ``arm_success(case)``."""

    out = []
    for case in dev_cases():
        out.append(_result(case, "grouped", True))
        out.append(_result(case, arm, arm_success(case), wrong_write=case.case_id in wrong_write))
    return out


def test_an_arm_that_does_as_well_as_grouped_everywhere_reads_as_no_change() -> None:
    report = ro.readout(_results(lambda case: True), _dev_by_id())

    nested = report["arms"]["nested"]
    assert nested["paired_cases"] == 20
    assert nested["verdict"] == ro.PASS
    assert "no change" in nested["reading"]
    assert nested["passes"]


def test_an_arm_that_loses_the_cases_of_every_template_fails() -> None:
    report = ro.readout(_results(lambda case: False), _dev_by_id())

    nested = report["arms"]["nested"]
    assert nested["comparison"]["mean_difference"] == -1.0
    assert nested["verdict"] == ro.FAIL
    assert not nested["passes"]


def test_a_comparison_over_fewer_than_six_template_groups_is_inconclusive() -> None:
    few = {template for template in list(TEMPLATES)[: protocol.MIN_GROUPS - 1]}
    cases = {case_id: case for case_id, case in _dev_by_id().items() if case.template_id in few}
    results = [r for r in _results(lambda case: True) if r.case_id in cases]

    report = ro.readout(results, cases)

    nested = report["arms"]["nested"]
    assert nested["verdict"] == ro.INCONCLUSIVE
    assert not nested["passes"]
    # The reader is told it is the group count, not a failure of the arm.
    assert f"{protocol.MIN_GROUPS - 1} template groups" in nested["reading"]


def test_a_session_that_could_not_be_scored_leaves_its_case_out_instead_of_counting_a_loss() -> None:
    cases = _dev_by_id()
    lost = next(iter(cases))
    results = [
        replace(r, scored=False, success=False, outcome="harness_fault") if (r.arm == "nested" and r.case_id == lost) else r
        for r in _results(lambda case: True)
    ]

    nested = ro.readout(results, cases)["arms"]["nested"]

    assert nested["excluded_cases"] == [lost]
    assert nested["paired_cases"] == 19
    assert nested["comparison"]["mean_difference"] == 0.0


def test_a_repeat_is_averaged_within_its_case_before_the_cases_are_compared() -> None:
    cases = _dev_by_id()
    case = cases["records-log-entry-s101"]
    results = [
        *_results(lambda c: True),
        _result(case, "grouped", True, repeat=2),
        _result(case, "nested", False, repeat=2),
    ]

    nested = ro.readout(results, cases)["arms"]["nested"]

    assert nested["paired_cases"] == 20
    assert nested["comparison"]["mean_difference"] == pytest.approx(-0.5 / 20)


def test_the_wrong_write_rule_allows_one_more_case_than_grouped_and_not_two() -> None:
    reads = [case.case_id for case in dev_cases() if case.kind == "read"]

    one_more = ro.readout(_results(lambda case: True, wrong_write=set(reads[:1])), _dev_by_id())["arms"]["nested"]
    two_more = ro.readout(_results(lambda case: True, wrong_write=set(reads[:2])), _dev_by_id())["arms"]["nested"]

    assert (one_more["wrong_write_excess"], one_more["passes"]) == (1, True)
    assert (two_more["wrong_write_excess"], two_more["passes"]) == (2, False)
    assert two_more["verdict"] == ro.PASS


@pytest.mark.parametrize("delta", [protocol.DELTA_FLOOR - 0.01, protocol.DELTA_CAP + 0.01])
def test_a_margin_outside_the_protocols_range_is_refused(delta: float) -> None:
    with pytest.raises(ValueError, match="outside the protocol's range"):
        ro.readout(_results(lambda case: True), _dev_by_id(), delta=delta)


def test_a_readout_without_the_baseline_arm_is_refused() -> None:
    results = [r for r in _results(lambda case: True) if r.arm != "grouped"]

    with pytest.raises(ValueError, match="no sessions of the baseline"):
        ro.readout(results, _dev_by_id())


# --------------------------------------------------------------------------
# The arms and the run's inputs.
# --------------------------------------------------------------------------


def test_each_arm_selects_its_shape_on_the_server_and_every_arm_gets_the_same_client_text(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    files = {
        shape: ex.arm_files(layout, ex.arm_spec(shape, ("alpha", "beta")))
        for shape in ex.SHAPES
    }

    for shape, arm in files.items():
        entry = json.loads(arm.contents["mcp.json"])["mcpServers"]["exomem"]
        assert entry["env"][ex.SHAPE_ENV] == shape
        args = entry["args"]
        assert args[args.index("--allow") + 1] == "alpha,beta"
        assert args[args.index("--budget") + 1] == str(ex.CALL_BUDGET)
    assert len({arm.contents["instructions.md"] for arm in files.values()}) == 1


def test_the_tool_search_setting_is_part_of_the_session_inputs_so_a_resumed_run_cannot_change_it(tmp_path: Path) -> None:
    from epistemic.journeys.f27_replay import AgentEnvelope

    case = _dev_case("records-range-total")
    layout = Layout(tmp_path / "run")

    def digest(tool_search: str) -> str:
        return ex.session_inputs(
            layout=layout,
            planned=PlannedSession("s", case.case_id, "grouped", 1, 0),
            case=case,
            spec=ex.arm_spec("grouped", ("alpha",)),
            envelope=AgentEnvelope(executable=Path("/opt/client"), version="1.0"),
            model="model-x",
            parent_env={"PATH": "/usr/bin"},
            seed_digest="seed",
            tool_search=tool_search,
        ).digest()

    assert digest("false") == digest("false")
    assert digest("false") != digest("auto")


def test_a_tool_selection_session_executes_from_its_seed_and_records_end_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from membench.utility.switch_eval import session

    case = _dev_case("records-range-total")
    layout = Layout(tmp_path / "run")
    seeded = layout.run / "seeded" / case.case_id / "vault"
    spec = ex.arm_spec("grouped", ())
    ex.seed_case(layout.run, case, parent_env={"PATH": os.defpath}, spec=spec)
    before = json.loads((seeded.parent / "admitted-state.json").read_text())
    init = {"type": "system", "subtype": "init", "apiKeySource": "none",
            "model": "m", "claude_code_version": "v", "tools": [],
            "mcp_servers": [{"name": "exomem", "status": "connected"}]}
    client = '''
import json, sys
sys.stdin.readline()
print(sys.argv[1], flush=True)
print(json.dumps({"type": "result", "subtype": "success", "result": "received"}), flush=True)
'''
    inputs = SessionInputs(
        planned=PlannedSession("session-1", case.case_id, "grouped", 1, 0),
        tree=ex.SURFACE_TREE, argv=(sys.executable, "-c", client, json.dumps(init)),
        env=ex.seeding_env({"PATH": os.defpath}, Slot(layout.slot), spec),
        cwd=layout.slot / "cwd", turns=(case.prompt,), scored_turn=0,
        files={"instructions.md": ex.client_instructions()}, state_digests={},
    )
    monkeypatch.setattr(session, "_USER_STATE_ROOT", tmp_path / "user-state")
    executor = ex.ToolSelectionExecutor(layout, {"grouped": spec}, "m", "v", {},
                                       cases={case.case_id: case}, turn_timeout=5)

    result = executor(inputs, tmp_path / "attempt")

    assert result.outcome == "ok" and result.answer == "received"
    actual_before = result.facts["end_state_before"]
    assert actual_before == result.facts["end_state_after"]
    # Opening the copied graph cache can recover a SQLite journal; canonical state and seed files stay exact.
    assert {key: value for key, value in actual_before.items() if key != "evidence"} == {
        key: value for key, value in before.items() if key != "evidence"
    }
    assert actual_before["evidence"]["files"] == before["evidence"]["files"]
    assert not layout.slot.exists()


def _probe(**schemas: str) -> dict:
    return {"tools": sorted(schemas), "hashes": {"per_tool": {"initialize": schemas}}}


def test_a_build_that_serves_one_surface_for_every_shape_is_refused() -> None:
    same = {"grouped": _probe(a="1", b="2"), "nested": _probe(a="1", b="2")}
    renamed = {"grouped": _probe(a="1", b="2"), "nested": _probe(a="1", c="3", d="4")}
    # Two builds can list the same names; only their argument schemas differ.
    reshaped = {"grouped": _probe(a="1", c="3"), "nested": _probe(a="1", c="5")}

    with pytest.raises(StateError, match=f"does not apply {ex.SHAPE_ENV}"):
        ex.require_distinct_shapes(same)
    ex.require_distinct_shapes(renamed)
    ex.require_distinct_shapes(reshaped)


STAND_IN_SERVER = """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "tools/list":
        result = {"tools": [{"name": "alpha", "inputSchema": {"type": "object"}},
                            {"name": "beta", "inputSchema": {"type": "object"}}]}
    elif message["method"] == "tools/call":
        result = {"content": [{"type": "text", "text": "ok"}]}
    else:
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "stand-in", "version": "0"}, "instructions": "x"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


def test_a_wildcard_allow_passes_every_tool_the_server_lists_and_serves_calls(tmp_path: Path) -> None:
    # The probe lists the server's own tools before an arm's list exists, and
    # an empty list there would read as a shape with no tools.
    server, log, turn = tmp_path / "server.py", tmp_path / "tap.jsonl", tmp_path / "turn"
    server.write_text(STAND_IN_SERVER, encoding="utf-8")
    turn.write_text("0", encoding="utf-8")
    tap = subprocess.Popen(
        [sys.executable, TAP_SCRIPT, "--log", str(log), "--turn-file", str(turn), "--budget", "5",
         "--allow", "*", "--skip-ready-gate", "--server-stderr", str(tmp_path / "server.stderr"),
         "--", sys.executable, str(server)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip

    def ask(request_id: int, method: str, params: dict) -> dict:
        tap.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        tap.stdin.flush()
        return json.loads(tap.stdout.readline())

    try:
        ask(1, "initialize", {})
        listed = ask(2, "tools/list", {})["result"]["tools"]
        called = ask(3, "tools/call", {"name": "beta", "arguments": {}})
    finally:
        tap.stdin.close()
        tap.wait(timeout=10)

    assert [tool["name"] for tool in listed] == ["alpha", "beta"]
    assert called["result"]["content"][0]["text"] == "ok"
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert next(e for e in events if e.get("event") == "call")["outcome"] == "served"


# --------------------------------------------------------------------------
# The plan stage and its calibration.
# --------------------------------------------------------------------------


def _calibration_file(tmp_path: Path) -> Path:
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "source": "a recorded run", "sessions": 3, "input_tokens": 200_000.0,
                "output_tokens": 400.0, "calls": 4.0, "wall_seconds": 10.0, "schema_bytes": 90_000.0,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_plan_stage_prints_session_counts_and_token_estimates_for_each_round(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plan", "--calibration", str(_calibration_file(tmp_path)), "--heldout-count", "64"]) == 0

    out = capsys.readouterr().out
    assert "round dev: 20 cases x 2 arms x 2 repeats = 80 sessions" in out
    assert "round heldout-64: 64 cases x 2 arms x 2 repeats = 256 sessions" in out
    assert "round heldout-90" not in out
    assert f"{80 * 200_000:,} input tokens" in out
    assert f"{80 * 400:,} output tokens" in out


def test_the_plan_stage_without_a_calibration_still_counts_sessions_and_says_it_cannot_estimate_tokens(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("membench.utility.tool_selection.__main__.DEFAULT_CALIBRATION", tmp_path / "absent.json")

    assert main(["plan", "--arms", "grouped,nested"]) == 0

    out = capsys.readouterr().out
    assert "= 80 sessions" in out
    assert "estimates are unavailable" in out


def test_the_plan_stage_estimates_from_the_committed_calibration_when_none_is_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["plan"]) == 0

    out = capsys.readouterr().out
    assert "= 80 sessions" in out
    assert "input tokens" in out
    assert "estimates are unavailable" not in out


def test_an_arm_with_smaller_schemas_is_estimated_lower_by_its_bytes_times_its_requests(tmp_path: Path) -> None:
    calibration = planning.load_calibration(_calibration_file(tmp_path))

    estimate = planning.estimate_round(
        cases=10, arms=["grouped", "nested"], repeats=1, calibration=calibration,
        schema_bytes={"nested": 90_000 - 3_500},
    )

    saved_per_session = 3_500 / planning.BYTES_PER_TOKEN * (calibration.calls + 1)
    assert estimate["input_tokens"] == round(10 * 200_000 + 10 * (200_000 - saved_per_session))
    assert estimate["notes"] == []


def test_an_arm_of_unknown_schema_size_is_estimated_at_the_baseline_size_and_says_so(tmp_path: Path) -> None:
    calibration = planning.load_calibration(_calibration_file(tmp_path))

    estimate = planning.estimate_round(
        cases=10, arms=["grouped", "nested"], repeats=1, calibration=calibration, schema_bytes={}
    )

    assert estimate["input_tokens"] == 2 * 10 * 200_000
    assert any("nested" in note for note in estimate["notes"])


def test_calibration_takes_its_means_from_the_scored_baseline_sessions_only() -> None:
    cases = _dev_by_id()
    case = cases["records-log-entry-s101"]
    kept = replace(_result(case, "grouped", True), input_tokens=100, calls=2, wall_seconds=4.0)
    other_kept = replace(_result(case, "grouped", True, repeat=2), input_tokens=300, calls=4, wall_seconds=6.0)
    unscored = replace(_result(case, "grouped", False, repeat=3, scored=False), input_tokens=9_999)
    other_arm = replace(_result(case, "nested", True), input_tokens=9_999)

    calibration = planning.calibrate([kept, other_kept, unscored, other_arm], source="unit")

    assert (calibration.sessions, calibration.input_tokens, calibration.calls, calibration.wall_seconds) == (2, 200, 3, 5)


# --------------------------------------------------------------------------
# Recorded sessions: real transcripts through classify, scoring and the readout.
# --------------------------------------------------------------------------

RECORDED = ("plan-triage-item-s101", "records-range-total-s101")


def _recording(name: str) -> tuple[list[str], list[dict], dict]:
    def lines(part: str) -> list[str]:
        return (FIXTURES / f"{name}.{part}.jsonl").read_text(encoding="utf-8").splitlines()

    states = json.loads((FIXTURES / f"{name}.end-states.json").read_text(encoding="utf-8"))
    return lines("transcript"), [json.loads(line) for line in lines("tap")], states


def _replay(name: str, *, after: dict | None = None):
    """An executor that plays a recording as the live executor's own steps would after the client ends."""

    transcript, tap, states = _recording(name)
    events = [json.loads(line) for line in transcript]
    init = next(e for e in events if e.get("type") == "system" and e.get("subtype") == "init")
    tools = [str(t["name"]) for t in next(e for e in tap if e["event"] == "tools_list")["tools"]]

    def execute(inputs: SessionInputs, attempt_dir: Path):
        record = _turn_record(0, transcript, events, [0.0] * len(events), tap, 0.0)
        result = classify(
            spec=ex.arm_spec(inputs.planned.arm, tools),
            model=init["model"],
            client_version=init["claude_code_version"],
            events=events,
            turn_records=[record],
            tap_events=tap,
            scored_turn=0,
            timed_out=False,
            quota=None,
            surface=None,
        )
        ex.session_facts(
            result=result,
            tap_events=tap,
            before=states["end_state_before"],
            after=after or states["end_state_after"],
            tool_search="false",
            instructions=inputs.files["instructions.md"],
        )
        return result

    return execute


def _play(layout: Layout, name: str, arm: str, *, after: dict | None = None, repeat: int = 1) -> dict:
    case_id = _recorded_case(name).case_id
    inputs = SessionInputs(
        planned=PlannedSession(f"{case_id}--{arm}--r{repeat}", case_id, arm, repeat, 0),
        tree=ex.SURFACE_TREE,
        argv=("client",),
        env={},
        cwd=Path("/run/slot/cwd"),
        turns=("the case prompt",),
        scored_turn=0,
        files={"instructions.md": ex.client_instructions()},
        state_digests={"seeded_vault": "seed"},
    )
    return run_session(inputs, layout.session_dir(case_id, arm, repeat), _replay(name, after=after))


def _recorded_case(name: str) -> Case:
    recorded = json.loads((FIXTURES / f"{name}.end-states.json").read_text(encoding="utf-8"))["case"]
    return Case(recorded["case_id"], recorded["template"], recorded["seed"], recorded["params"])


@pytest.mark.parametrize("name", RECORDED)
def test_the_recordings_are_of_the_cases_the_templates_still_draw(name: str) -> None:
    # A changed template draw leaves the recording a record of another task.
    recorded = _recorded_case(name)

    assert build_case(recorded.template_id, recorded.seed).as_dict() == recorded.as_dict()


def test_a_recorded_write_session_scores_from_its_end_state_and_a_refusal_it_met(tmp_path: Path) -> None:
    name = "plan-triage-item-s101"
    layout = Layout(tmp_path / "run")
    session = _play(layout, name, "grouped")
    case = _recorded_case(name)

    result = score_session(case, session, session["facts"]["calls"], grouped_resolver())

    assert session["outcome"] == "ok"
    assert result.success, result.reasons
    assert result.calls == 4
    assert result.argument_refusals == 1
    assert result.first_correct_ordinal == 3
    assert (result.input_tokens or 0) > 0
    assert (result.schema_bytes or 0) > 50_000


def test_a_recorded_read_with_prose_wrapped_json_fails_r2s1_without_a_wrong_write(tmp_path: Path) -> None:
    name = "records-range-total-s101"
    layout = Layout(tmp_path / "run")
    session = _play(layout, name, "grouped")
    case = _recorded_case(name)

    result = score_session(case, session, session["facts"]["calls"], grouped_resolver())

    assert result.scored and not result.success
    assert result.reasons == ["answer does not match the seeded facts"]
    assert not result.wrong_write
    assert result.write_calls_served == 0
    assert result.first_correct_ordinal is not None


def test_each_recorded_session_keeps_the_facts_that_say_what_the_client_was_shown(tmp_path: Path) -> None:
    session = _play(Layout(tmp_path / "run"), "records-range-total-s101", "grouped")

    surface = session["facts"]["surface"]

    assert surface["claude_code_version"]
    assert surface["model"]
    assert surface["tool_search"] == "false"
    assert surface["skills_digest"] and surface["client_instructions_digest"] and surface["server_instructions_digest"]
    assert surface["schema_bytes"] > 0 and surface["tool_names"]


def test_an_arm_comparison_runs_from_recorded_sessions_and_reads_a_lost_case_as_a_loss(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    triage_before = _recording("plan-triage-item-s101")[2]["end_state_before"]
    for name in RECORDED:
        _play(layout, name, "grouped")
    _play(layout, "records-range-total-s101", "nested")
    # The same recorded transcript, but the vault the key reads never changed.
    _play(layout, "plan-triage-item-s101", "nested", after=triage_before)
    cases = {name: _recorded_case(name) for name in RECORDED}
    by_id = {case.case_id: case for case in cases.values()}

    results = ro.load_results(sorted(layout.sessions.glob("session-*")), by_id, grouped_resolver())
    report = ro.readout(results, by_id)

    nested = report["arms"]["nested"]
    assert nested["paired_cases"] == 2
    assert nested["comparison"]["mean_difference"] == pytest.approx(-0.5)
    assert nested["verdict"] == ro.INCONCLUSIVE
    assert report["arms"]["grouped"]["secondary"]["success_rate"] == 0.5


def test_the_readout_stage_reads_a_run_directory_it_did_not_write_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run"
    layout = Layout(run)
    for name in RECORDED:
        _play(layout, name, "grouped")
    case_module.write_cases([_recorded_case(name) for name in RECORDED], run / "cases.json")

    assert main(["readout", "--run", str(run), "--resolver", "grouped"]) == 0

    assert "grouped: 2/2 sessions scored, success 0.5" in capsys.readouterr().out
    assert json.loads((run / "readout.json").read_text(encoding="utf-8"))["baseline"] == "grouped"


def test_a_recording_stays_a_record_of_a_real_surface() -> None:
    # The fixtures were captured from live sessions; this guards against a
    # regenerated or hand-edited file standing in for them.
    for name in RECORDED:
        _transcript, tap, _states = _recording(name)
        listing = next(e for e in tap if e["event"] == "tools_list")
        handshake = next(e for e in tap if e["event"] == "handshake")
        assert len(listing["tools"]) > 20
        assert handshake["server_info"]["name"] == "exomem"
