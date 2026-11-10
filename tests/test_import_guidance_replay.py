"""The context-replay suite: recording on a case seed, case-directory scenarios, answer checks, the replay report and
the blind judge batch. Seeds, cases and keys here are tiny invented fixtures."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from import_guidance import (
    checks,
    judge_batch,
    recording,
    replay_cases,
    replay_report,
    runner,
    session_close,
    session_seed,
    session_table,
)
from import_guidance.recording import Arm
from import_guidance.scripted_replies import Scenario, load_scenario

ROOT = Path(__file__).resolve().parents[1]
FERRY_PAGE = """---
exomem_id: 6f0f7c52-3a1b-5c1e-9a55-0d2f4c7e8b10
type: research-note
project: harbour-ferry
status: active
created: 2026-01-05
updated: 2026-02-01
sources: []
tags: [ferry]
---

# Ferry timetable

## Claim
- id: first-sailing

The first sailing leaves the harbour at 07:15 on weekdays.
"""
TURN = "When does the first ferry sail on weekdays?"


def ferry_seed(root: Path) -> Path:
    page = root / "seed" / "Notes" / "Research" / "harbour-ferry" / "timetable.md"
    page.parent.mkdir(parents=True)
    page.write_text(FERRY_PAGE, encoding="utf-8")
    return root / "seed"


def replay_scenario(folder: Path, *, seed: str, steps: list[dict] | None = None, ready_graph: bool = False) -> Path:
    path = folder / "ferry.json"
    path.write_text(
        json.dumps(
            {
                "id": "ferry",
                "title": "t",
                "suite": "context-replay",
                "prompt": TURN,
                "served_tools": ["recall_context"],
                "replies": {"recall_context": {"$shape": "context"}},
                "default_reply": {"error": "unscripted"},
                "record": {
                    "seed": seed,
                    **({"ready_graph": True} if ready_graph else {}),
                    "steps": steps
                    or [
                        {
                            "as": "context",
                            "tool": "recall_context",
                            "arguments": {"turn": TURN},
                            "warm": {"seconds": 3, "ignore": ["served_context_id"]},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_recording_on_a_case_seed_serves_that_seeds_page_and_no_throwaway_page(tmp_path: Path) -> None:
    """Catches a recording made on the fixture vault or on a settled one: the served context would hold a fixture note
    or a throwaway page, and a cold first read would hold nothing at all."""
    ferry_seed(tmp_path)
    scenario = load_scenario(replay_scenario(tmp_path, seed="seed"))
    target = tmp_path / "shapes.json"
    assert recording.record(scenario, Arm("test", ROOT), target, tmp_path / "scratch") == []
    served = json.dumps(recording.load_recording(target)["shapes"]["context"])
    assert "Ferry timetable" in served
    assert "Settle probe" not in served and "feature-flag" not in served


def test_a_seeded_recording_refuses_a_step_that_writes_throwaway_pages(tmp_path: Path) -> None:
    """Catches a scenario that settles the graph on a seeded vault: its throwaway pages are ones `recall_context`
    could serve to the agent."""
    ferry_seed(tmp_path)
    scenario = load_scenario(replay_scenario(tmp_path, seed="seed", steps=[{"settle_graph": True}]))
    problems = recording.record(scenario, Arm("test", ROOT), tmp_path / "shapes.json", tmp_path / "scratch")
    assert problems and "settle_graph" in problems[0]


def test_a_seed_that_is_not_a_folder_fails_the_recording_by_name(tmp_path: Path) -> None:
    scenario = load_scenario(replay_scenario(tmp_path, seed="no-such-seed"))
    problems = recording.record(scenario, Arm("test", ROOT), tmp_path / "shapes.json", tmp_path / "scratch")
    assert problems and "no-such-seed" in problems[0]


# The relation graph of a seeded vault.

OLD_RULE = """---
exomem_id: 3b1d7a40-6c2e-5f10-8a47-2d9e5c1b7f01
type: research-note
project: tool-shelf
status: active
created: 2026-01-10
updated: 2026-01-10
sources: []
tags: [shelf]
---

# Old shelf rule

## Claim
- id: closes-17

The tool shelf closes at 17:00.
"""
NEW_RULE = """---
exomem_id: 9e4c2b18-0d3a-5b77-a1c6-7f8e2d4a6c02
type: research-note
project: tool-shelf
status: active
created: 2026-02-10
updated: 2026-02-10
sources: []
tags: [shelf]
---

# New shelf rule

## Claim
- id: closes-18
- relations: supersedes: [[Notes/Research/tool-shelf/old#closes-17]]

The tool shelf closes at 18:00.
"""


def shelf_seed(root: Path) -> Path:
    """An invented seed of two pages, one of which supersedes a claim of the other."""
    folder = root / "shelf-seed" / "Notes" / "Research" / "tool-shelf"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "old.md").write_text(OLD_RULE, encoding="utf-8")
    (folder / "new.md").write_text(NEW_RULE, encoding="utf-8")
    return root / "shelf-seed"


def seed_a_vault(root: Path, name: str, *, ready_graph: bool) -> tuple[Path, dict | None]:
    """The vault that a session-close run seeds, from the shelf seed, and what its seeding reported."""
    case = session_close.Case("shelf", "t", shelf_seed(root), "balanced", ("Which shelf rule holds now?",), root / "case.json", None)
    arm = Arm("test", ROOT)
    work = root / name
    for folder in ("hook-home", "logs", "scratch"):
        (work / folder).mkdir(parents=True)
    ready = session_close.seed_vault(case, arm, work, session_close.pinned_env(arm, work, case), ready_graph)
    return work / "vault", ready


def pages_and_seed_bytes(vault: Path, seed: Path) -> tuple[set[str], bool]:
    """Every markdown page of the vault, and whether each seed file is in the vault byte for byte."""
    from exomem.kbdir import kb_dirname

    pages = {path.relative_to(vault).as_posix() for path in vault.rglob("*.md")}
    same = all((vault / kb_dirname() / path.relative_to(seed)).read_bytes() == path.read_bytes() for path in seed.rglob("*") if path.is_file())
    return pages, same


def test_a_seeded_session_vault_is_readied_without_writing_a_page_or_touching_the_seed(tmp_path: Path) -> None:
    """Catches a graph build that writes a page (the actor could be served it) or rewrites a seed file, and a seeded
    vault that reaches the actor with its graph still warming, where the product marks nothing from relations."""
    seeded, nothing = seed_a_vault(tmp_path, "plain", ready_graph=False)
    readied, ready = seed_a_vault(tmp_path, "readied", ready_graph=True)
    seed = tmp_path / "shelf-seed"
    assert nothing is None and ready["graph"] == "available"
    seeded_pages, _ = pages_and_seed_bytes(seeded, seed)
    readied_pages, seed_unchanged = pages_and_seed_bytes(readied, seed)
    assert readied_pages == seeded_pages and seed_unchanged


def test_a_recording_that_asks_for_the_graph_reports_it_built_and_holds_no_page_beyond_the_seed(tmp_path: Path) -> None:
    """Catches a recorder that serves the graph warming for a seeded vault: both arms would then serve no relation
    mark, and the comparison would measure nothing."""
    seed = shelf_seed(tmp_path)
    baseline, _ = seed_a_vault(tmp_path, "baseline", ready_graph=False)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "ready_graph": True,
                "steps": [{"as": "context", "tool": "recall_context", "arguments": {"turn": "When does the tool shelf close?"}}],
            }
        ),
        encoding="utf-8",
    )
    scratch = tmp_path / "record"
    scratch.mkdir()
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(recording.WORKER), str(plan), str(ROOT), str(tmp_path / "out.json"), str(seed)],
        cwd=scratch, env=recording.worker_env(Arm("test", ROOT), scratch), capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stderr[-400:]
    recorded = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert recorded["ready"]["graph"] == "available"
    assert recorded["shapes"]["context"]["map"]["membership_graph"] == "available"
    vault = next(scratch.glob("ig-record-*/vault"))
    pages, seed_unchanged = pages_and_seed_bytes(vault, seed)
    baseline_pages, _ = pages_and_seed_bytes(baseline, seed)
    assert pages == baseline_pages and seed_unchanged


def test_waiting_for_the_graph_names_the_state_it_last_saw_when_the_bound_passes() -> None:
    """Catches a vault that is never ready being recorded anyway, or failing with a message that hides the state."""
    seen = []

    def stays_warming(tool: str, arguments: dict) -> dict:
        seen.append(tool)
        return {"error": {"code": "COLD"}} if len(seen) == 1 else {"map": {"membership_graph": "warming"}}

    with pytest.raises(SystemExit, match="warming"):
        session_seed.wait_for_graph(stays_warming, seconds=0.05, interval=0.02)
    steps = iter([{"map": {"membership_graph": "warming"}}, {"map": {"membership_graph": "available"}}])
    assert session_seed.wait_for_graph(lambda tool, arguments: next(steps), seconds=5, interval=0.01) == "available"


def test_a_recording_made_before_the_graph_was_built_is_not_reused_for_a_scenario_that_asks_for_it(tmp_path: Path) -> None:
    """Catches an old recording, with the graph warming, being served again after the scenario began to ask for the
    graph: its replies carry no relation mark, whatever the arm."""
    ferry_seed(tmp_path)
    old = tmp_path / "shapes.json"
    old.write_text(json.dumps({"shapes": {"context": {"map": {"membership_graph": "warming"}}}}), encoding="utf-8")
    asking = load_scenario(replay_scenario(tmp_path, seed="seed", ready_graph=True))
    problems = recording.record(asking, Arm("test", ROOT), old, tmp_path / "scratch")
    assert problems and "without building the graph" in problems[0]
    plain = load_scenario(replay_scenario(tmp_path, seed="seed"))
    assert recording.record(plain, Arm("test", ROOT), old, tmp_path / "scratch") == []


# Case folders, scenarios and the answer checks.

TURN_ONE = "When does the first ferry sail on weekdays?"


def case_folder(root: Path, cases: dict[str, dict]) -> Path:
    """An invented case folder: one seed, and for each case a case file and a marks key. `cases` maps an id to its
    `deterministic` block and set."""
    folder = root / "cases"
    (folder / "answer-keys").mkdir(parents=True)
    (folder / "seeds" / "ferry-seed" / "Notes").mkdir(parents=True)
    (folder / "seeds" / "ferry-seed" / "Notes" / "page.md").write_text("# Page\n", encoding="utf-8")
    for case_id, marks in cases.items():
        (folder / f"{case_id}.json").write_text(
            json.dumps({"id": case_id, "title": "t", "suite": "session-close", "seed": "ferry-seed", "engagement": "balanced", "turns": [TURN_ONE]}),
            encoding="utf-8",
        )
        (folder / "answer-keys" / f"{case_id}.marks.json").write_text(
            json.dumps({"reference": f"Reference of {case_id}.", "set": marks.get("set", "S"), "deterministic": marks.get("deterministic")}),
            encoding="utf-8",
        )
    return folder


def test_a_seed_in_the_case_folder_wins_over_a_shipped_seed_of_the_same_name(tmp_path: Path) -> None:
    """Catches a case that silently seeds from the shipped copy of a name: the actor and the key check would then read
    pages the evaluator never wrote."""
    shipped = next(p.name for p in (session_close.suite_dir() / "seeds").iterdir() if p.is_dir())
    folder = case_folder(tmp_path, {"c1": {}})
    own = folder / "seeds" / shipped
    own.mkdir()
    (folder / "c1.json").write_text(
        json.dumps({"id": "c1", "title": "t", "suite": "session-close", "seed": shipped, "engagement": "balanced", "turns": ["Which shelf rule holds now?"]}),
        encoding="utf-8",
    )
    assert session_close.load_case(folder / "c1.json").seed == own
    own.rmdir()
    assert session_close.load_case(folder / "c1.json").seed == session_close.suite_dir() / "seeds" / shipped


def test_the_scenario_writer_refuses_an_output_folder_inside_a_repository(tmp_path: Path) -> None:
    """Catches generated cases and keys landing in a checkout, where they could be committed or read by an agent."""
    folder = case_folder(tmp_path, {"c1": {}})
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit):
        replay_cases.write_scenarios(folder, repository / "out")
    assert not (repository / "out").exists()


def test_a_written_scenario_loads_by_folder_path_with_its_checks_set_and_judging_left_to_the_batch(tmp_path: Path) -> None:
    folder = case_folder(tmp_path, {"c1": {"set": "N", "deterministic": {"all_of": [["07:15"]], "in_order": [["first"], ["last"]]}}, "c2": {}})
    replay_cases.write_scenarios(folder, tmp_path / "out")
    scenarios = {s.id: s for s in runner.load_scenarios("all", str(tmp_path / "out" / "scenarios"))}
    first = scenarios["c1"]
    assert [spec["type"] for spec in first.checks] == ["only_scripted_tools", "answer_has_all_of", "answer_in_order"]
    assert [spec["type"] for spec in scenarios["c2"].checks] == ["only_scripted_tools"]
    assert first.prompt == TURN_ONE and first.reference == "Reference of c1." and first.judge == "batch"
    assert first.hooks == ("UserPromptSubmit",) and first.served_tools == ("bootstrap", "recall_context")
    assert first.record["ready_graph"] is True
    assert json.loads((tmp_path / "out" / "case-sets.json").read_text()) == {"c1": "N", "c2": "S"}


def test_a_marks_key_with_an_unknown_check_type_is_refused_by_name(tmp_path: Path) -> None:
    """Catches a typo in a key that would write a scenario with no check, so a wrong answer could pass."""
    folder = case_folder(tmp_path, {"c1": {"deterministic": {"allof": [["x"]]}}})
    with pytest.raises(SystemExit, match="allof|deterministic"):
        replay_cases.write_scenarios(folder, tmp_path / "out")


def answer(text: str) -> checks.Evidence:
    return checks.Evidence((), (), {}, answer=text)


def test_all_of_needs_one_spelling_from_each_group_whatever_the_case() -> None:
    spec = {"groups": [["15", "fifteen"], ["Deposit"]]}
    assert checks.answer_has_all_of(spec, answer("The DEPOSIT is fifteen pounds.")).verdict == checks.PASS
    assert checks.answer_has_all_of(spec, answer("The deposit is twelve pounds.")).verdict == checks.FAIL


def test_in_order_fails_a_missing_group_and_a_wrong_order_and_malformed_groups_are_unknown() -> None:
    spec = {"groups": [["opened"], ["insurance"], ["donated", "hand tools"]]}
    assert checks.answer_in_order(spec, answer("It opened, then insurance, then tools were donated.")).verdict == checks.PASS
    assert checks.answer_in_order(spec, answer("Insurance came first, then it opened, then tools were donated.")).verdict == checks.FAIL
    assert checks.answer_in_order(spec, answer("It opened and insurance followed.")).verdict == checks.FAIL
    assert checks.answer_in_order({"groups": []}, answer("anything")).verdict == checks.UNKNOWN


# The blind judge batch, the report and the table, on invented runs.


def invented_scenario(case_id: str) -> Scenario:
    return Scenario(
        id=case_id, title="t", prompt=TURN_ONE, served_tools=("recall_context",), builtin_tools=(), replies={}, default_reply={},
        checks=(), criteria=tuple(replay_cases.RUBRIC), reference=f"Reference of {case_id}.", judge="batch",
    )


def stage_b_row(case: str, arm: str, text: str | None, *, model: str = "model-a", repeat: int = 0, error: str | None = None, verdicts: str = checks.PASS) -> dict:
    return {
        "scenario": case, "arm": arm, "model": model, "repeat": repeat, "error": error, "final_text": text, "product_sha": "abc",
        "checks": [{"check": "answer_has_all_of", "verdict": verdicts, "detail": ""}], "hook_invocations": 1,
    }


def write_rows(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def judge_batch_as(out: Path, holds: Callable[[dict], bool]) -> Path:
    """Plays the native judge: reads the exported items, and answers each from the private map as `holds` says."""
    mapping = json.loads((out / "private" / "judge-map.json").read_text())["items"]
    lines = []
    for raw in (out / "judge-views.jsonl").read_text().splitlines():
        item_id = json.loads(raw)["id"]
        held = holds(mapping[item_id]["run"])
        lines.append(json.dumps({"id": item_id, "structured_output": {"M1": held, "M1_reason": "r", "M2": held, "M2_reason": "r"}}))
    path = out / "judgments.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def ten_stage_b_runs(tmp_path: Path) -> Path:
    rows = [stage_b_row(f"c{n}", arm, f"The first ferry sails at 07:15, case {n}.") for n in range(5) for arm in ("base", "cand")]
    return write_rows(tmp_path / "b.jsonl", rows)


def test_a_seeded_fifth_of_the_items_appears_twice_and_low_agreement_reads_incomplete(tmp_path: Path) -> None:
    """Catches a judge batch with no noise check: two judgments of the same answer that disagree would go unseen."""
    scenarios = [invented_scenario(f"c{n}") for n in range(5)]
    out = tmp_path / "batch"
    assert judge_batch.prepare([ten_stage_b_runs(tmp_path)], [], scenarios, out=out) == 12
    mapping = json.loads((out / "private" / "judge-map.json").read_text())["items"]
    twins = {i: e["twin_of"] for i, e in mapping.items() if e["twin_of"]}
    assert len(twins) == 2
    prompts = {json.loads(line)["id"]: json.loads(line)["prompt"] for line in (out / "judge-views.jsonl").read_text().splitlines()}
    assert all(prompts[twin] == prompts[first] for twin, first in twins.items())

    steady = judge_batch.import_judgments(judge_batch_as(out, lambda run: True), out=out)
    assert steady["agreement"] == 1.0 and not steady["incomplete"]

    flipped = next(iter(twins))
    judgments = judge_batch_as(out, lambda run: True)
    lines = [json.loads(line) for line in judgments.read_text().splitlines()]
    for line in lines:
        if line["id"] == flipped:
            line["structured_output"]["M1"] = False
    judgments.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    noisy = judge_batch.import_judgments(judgments, out=out)
    assert noisy["agreement"] == 0.5 and noisy["incomplete"]
    assert judge_batch.agreement_line(noisy).startswith("incomplete: judge agreement 0.50")


def test_a_missing_judgment_is_unknown_and_never_a_disagreement_or_a_pass(tmp_path: Path) -> None:
    scenarios = [invented_scenario(f"c{n}") for n in range(5)]
    out = tmp_path / "batch"
    judge_batch.prepare([ten_stage_b_runs(tmp_path)], [], scenarios, out=out)
    mapping = json.loads((out / "private" / "judge-map.json").read_text())["items"]
    first = next(entry["twin_of"] for entry in mapping.values() if entry["twin_of"])
    judgments = judge_batch_as(out, lambda run: True)
    kept = [line for line in judgments.read_text().splitlines() if json.loads(line)["id"] != first]
    judgments.write_text("\n".join(kept) + "\n", encoding="utf-8")
    summary = judge_batch.import_judgments(judgments, out=out)
    verdicts = [row["verdict"] for row in json.loads((out / "judged.json").read_text())["rows"]]
    assert sorted(verdicts) == [checks.PASS] * 9 + [checks.UNKNOWN]
    assert summary["usable_pairs"] == summary["doubled_items"] - 1 and summary["agreement"] == 1.0


def test_a_judge_item_shows_no_arm_model_or_stage_and_both_stages_share_one_shuffled_file(tmp_path: Path) -> None:
    """Catches an item that lets the judge tell the arm, the model or the stage, which would bias M1 and M2."""
    stage_b = write_rows(tmp_path / "b.jsonl", [stage_b_row("c0", "arm-zeta", "Sails at 07:15.", model="model-quokka")])
    stage_c = write_rows(
        tmp_path / "c.jsonl",
        [{"scenario": "c0", "arm": "arm-zeta", "model": "model-quokka", "repeat": 0, "client_mode": "plugin", "error": None, "replies": ["Sails at 07:15."]}],
    )
    out = tmp_path / "batch"
    assert judge_batch.prepare([stage_b], [stage_c], [invented_scenario("c0")], out=out) == 3
    raw = (out / "judge-views.jsonl").read_text()
    for label in ("arm-zeta", "model-quokka", "client_mode", "plugin", "stage"):
        assert label not in raw
    items = [json.loads(line) for line in raw.splitlines()]
    assert all(set(item) == {"id", "prompt", "answer_schema"} for item in items)
    runs = [entry["run"]["stage"] for entry in json.loads((out / "private" / "judge-map.json").read_text())["items"].values()]
    assert sorted(set(runs)) == ["B", "C"]


def test_a_case_with_no_known_pair_is_unknown_and_counted_never_a_failure(tmp_path: Path) -> None:
    """Catches an errored or missing arm counted as a failed pair: it would show the candidate as worse by accident."""
    rows = [
        stage_b_row("c0", "base", "Sails at 07:15, a long answer here."),
        stage_b_row("c0", "cand", "Sails at 07:15."),
        stage_b_row("c1", "base", "Sails at 07:15."),
        stage_b_row("c1", "cand", None, error="TimeoutExpired: the agent timed out"),
        stage_b_row("c2", "base", "Sails at 07:15."),
    ]
    runs = write_rows(tmp_path / "b.jsonl", rows)
    out = tmp_path / "batch"
    judge_batch.prepare([runs], [], [invented_scenario(f"c{n}") for n in range(3)], out=out)
    judge_batch.import_judgments(judge_batch_as(out, lambda run: True), out=out)
    sets = tmp_path / "case-sets.json"
    sets.write_text(json.dumps({"c0": "S", "c1": "S", "c2": "S"}), encoding="utf-8")
    text = replay_report.build([runs], judged=out / "judged.json", case_sets=sets, baseline="base", candidate="cand")
    line = next(line for line in text.splitlines() if line.startswith("- Set S, all models: difference"))
    assert "over 1 paired case(s) of 3" in line and "unknown cases (2): c1, c2" in line
    assert "TimeoutExpired" in text.split("## Runs without a usable result")[1]


def test_a_run_that_used_a_tool_outside_the_script_is_counted_apart_and_is_neither_a_pass_nor_a_fail(tmp_path: Path) -> None:
    """Catches an agent that reached a built-in tool when the script allowed none: its answer did not rest on the replayed
    context, so scoring it would credit or blame the arm for something the replay never showed."""
    folder = case_folder(tmp_path, {"c0": {"deterministic": {"all_of": [["07:15"]]}}, "c1": {"deterministic": {"all_of": [["07:15"]]}}})
    replay_cases.write_scenarios(folder, tmp_path / "out")
    scenario = {s.id: s for s in runner.load_scenarios("all", str(tmp_path / "out" / "scenarios"))}["c0"]
    answer_text = "The first ferry sails at 07:15."

    def row_of(case: str, arm: str, tools: tuple[str, ...]) -> dict:
        evidence = checks.Evidence((), tools, {}, answer=answer_text)
        outcomes = checks.run_checks(scenario.checks, evidence, scenario.served_tools, scenario.builtin_tools)
        return {**stage_b_row(case, arm, answer_text), "checks": [vars(o) for o in outcomes]}

    scripted = ("mcp__exomem__recall_context",)
    rows = [
        row_of("c0", "base", scripted),
        row_of("c0", "cand", (*scripted, "Bash")),
        row_of("c1", "base", scripted),
        row_of("c1", "cand", scripted),
    ]
    runs = write_rows(tmp_path / "b.jsonl", rows)
    out = tmp_path / "batch"
    judge_batch.prepare([runs], [], [invented_scenario("c0"), invented_scenario("c1")], out=out)
    judge_batch.import_judgments(judge_batch_as(out, lambda run: True), out=out)
    sets = tmp_path / "case-sets.json"
    sets.write_text(json.dumps({"c0": "S", "c1": "S"}), encoding="utf-8")
    text = replay_report.build([runs], judged=out / "judged.json", case_sets=sets, baseline="base", candidate="cand")
    assert "outside the script (unknown for every difference below): 1 of 4 (base 0, cand 1)" in text
    line = next(line for line in text.splitlines() if line.startswith("- Set S, all models: difference"))
    assert "over 1 paired case(s) of 2" in line and "unknown cases (1): c0" in line
    assert "- cand model-a c0#0: the agent used a tool outside the script" in text.split("## Runs without a usable result")[1]
    assert "c0" not in text.split("## Deterministic checks and the judge disagree")[1].split("## Runs without")[0]


def test_a_session_whose_run_failed_shows_unknown_cells_and_not_zero(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps({"type": "result", "duration_ms": 12500, "result": "done"}) + "\n", encoding="utf-8")
    calls = [{"name": "mcp__exomem__recall_context"}, {"name": "mcp__exomem__search_memory"}]
    ok = {"scenario": "c0", "arm": "base", "model": "m", "repeat": 0, "client_mode": "plugin", "error": None, "calls": calls,
          "observed": {"changed": {"a.md": ""}, "created": {}}, "replies": ["hello"], "stream": str(stream)}
    failed = {**ok, "arm": "cand", "error": "the agent returned no result"}
    runs = write_rows(tmp_path / "c.jsonl", [ok, failed])
    row = next(r for r in session_table.build([runs], baseline="base", candidate="cand").splitlines() if r.startswith("| c0"))
    assert "| 2 / unknown |" in row and "| 1 / unknown |" in row and "| 12.5 / unknown |" in row


def test_the_judged_verdict_of_a_stage_c_reply_reaches_the_session_table(tmp_path: Path) -> None:
    """Catches a stage-C reply that the judge scored but the table could not join to its session."""
    rows = [
        {"scenario": "c0", "arm": arm, "model": "m", "repeat": 0, "client_mode": "plugin", "error": None, "calls": [],
         "observed": {"changed": {}, "created": {}}, "replies": [text], "stream": None}
        for arm, text in (("base", "Sails at 07:15."), ("cand", "Sails at 06:00."))
    ]
    runs = write_rows(tmp_path / "c.jsonl", rows)
    out = tmp_path / "batch"
    judge_batch.prepare([], [runs], [invented_scenario("c0")], out=out)
    judgments = judge_batch_as(out, lambda run: run["arm"] == "base")
    judge_batch.import_judgments(judgments, out=out)
    row = next(r for r in session_table.build([runs], baseline="base", candidate="cand", judged=out / "judged.json").splitlines() if r.startswith("| c0"))
    assert row.rstrip().endswith("| pass / fail |")


class StreamClaude:
    """Stands in for `claude -p` on the replay: records how it was launched and prints a stream with one hook response."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.env: dict = {}
        self.judge_calls = 0

    def __call__(self, argv: list[str], cwd: Path, env: dict, timeout: float) -> subprocess.CompletedProcess:
        if "--json-schema" in argv:
            self.judge_calls += 1
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.argv, self.env = argv, dict(env)
        events = [
            {"type": "system", "subtype": "init", "mcp_servers": [{"name": "exomem", "status": "connected"}]},
            {"type": "system", "subtype": "hook_response", "hook_event": "UserPromptSubmit", "output": "{}"},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Sails at 07:15.", "num_turns": 2},
        ]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(e) for e in events) + "\n", "")


def test_the_replay_stages_the_arms_prompt_hook_in_mcp_mode_under_an_isolated_home_and_leaves_judging_to_the_batch(tmp_path: Path) -> None:
    """Catches a replay that runs without the hook (the actor is never told to call recall_context) or with a hook
    that reaches the user's real state or the wrong injection mode."""
    folder = case_folder(tmp_path, {"c1": {}})
    replay_cases.write_scenarios(folder, tmp_path / "out")
    scenario = runner.load_scenarios("c1", str(tmp_path / "out" / "scenarios"))[0]
    fake = StreamClaude()
    row = runner.run_once(
        scenario, repeat=0, model="agent-m", judge_model="judge-m", out=tmp_path / "run", executable=Path("claude"), run=fake,
        arm=Arm("test", ROOT),
    )
    settings = json.loads(Path(fake.argv[fake.argv.index("--settings") + 1]).read_text())
    commands = json.dumps(settings["hooks"]["UserPromptSubmit"])
    assert "exomem-retrieve-nudge" in commands and "${CLAUDE_PLUGIN_ROOT}" not in commands and str(ROOT) in commands
    assert "--include-hook-events" in fake.argv
    assert fake.env["EXOMEM_RETRIEVE_INJECT"] == "mcp" and fake.env["EXOMEM_PROMINENCE"] == "balanced"
    assert Path(fake.env["EXOMEM_HOOK_HOME"]).is_relative_to(tmp_path / "run")
    assert fake.judge_calls == 0 and row["hook_invocations"] == 1 and row["final_text"] == "Sails at 07:15."
