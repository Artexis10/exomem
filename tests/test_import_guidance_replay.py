"""The context-replay suite: recording on a case seed, case-directory scenarios, answer checks, the replay report and
the blind judge batch. Seeds, cases and keys here are tiny invented fixtures."""

from __future__ import annotations

import itertools
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
updated: 2026-03-14
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
created: 2026-04-07
updated: 2026-04-07
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


@pytest.mark.parametrize("activation_packet", ["maps", "anchors"])
def test_a_recording_that_asks_for_the_graph_reports_it_built_and_holds_no_page_beyond_the_seed(tmp_path: Path, activation_packet: str) -> None:
    """Catches a recorder that serves the graph warming for a seeded vault: both arms would then serve no relation
    mark, and the comparison would measure nothing."""
    seed = shelf_seed(tmp_path)
    baseline, _ = seed_a_vault(tmp_path, "baseline", ready_graph=False)
    case = session_close.Case("shelf", "t", seed, "balanced", ("When does the tool shelf close?",), tmp_path / "case.json", None)
    generated = replay_cases.scenario_for(case, {"reference": "", "deterministic": {}}, [])
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(generated["record"]), encoding="utf-8")
    scratch = tmp_path / "record"
    scratch.mkdir()
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(recording.WORKER), str(plan), str(ROOT), str(tmp_path / "out.json"), str(seed)],
        cwd=scratch, env=recording.worker_env(Arm("test", ROOT), scratch, activation_packet=activation_packet), capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stderr[-400:]
    recorded = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert recorded["ready"]["graph"] == "available"
    if activation_packet == "maps":
        assert recorded["shapes"]["context"]["map"]["membership_graph"] == "available"
    else:
        assert "map" not in recorded["shapes"]["context"]
        assert "anchors" in recorded["shapes"]["context"]
        assert recorded["shapes"]["context"]["continuity"]
    vault = next(scratch.glob("ig-record-*/vault"))
    pages, seed_unchanged = pages_and_seed_bytes(vault, seed)
    baseline_pages, _ = pages_and_seed_bytes(baseline, seed)
    assert pages == baseline_pages and seed_unchanged


def test_waiting_for_the_graph_names_the_state_it_last_saw_when_the_bound_passes(tmp_path: Path) -> None:
    """Catches a vault that is never ready being recorded anyway, or failing with a message that hides the state."""
    seen = []

    def stays_unavailable() -> str:
        seen.append(True)
        return "unproven" if len(seen) == 1 else "unavailable"

    with pytest.raises(SystemExit, match="unavailable"):
        session_seed.wait_for_graph(tmp_path, availability=stays_unavailable, seconds=0.05, interval=0.02)
    steps = iter(["unproven", "unavailable", "available"])
    assert session_seed.wait_for_graph(tmp_path, availability=lambda: next(steps), seconds=5, interval=0.01) == "available"


def test_the_warm_read_records_once_two_successive_replies_agree_and_fails_by_name_when_none_do_within_the_bound() -> None:
    """Catches a warm step that records a reply the index pass is still changing, or that gives up on a vault whose
    index settles after the second read and so fails a recording that a short wait would have made."""
    from import_guidance.record_worker import warmed_reply

    case = session_close.Case("probe", "t", Path("seed"), "balanced", ("A request",), Path("case.json"), None)
    warm = replay_cases.scenario_for(case, {"reference": "", "deterministic": {}}, [])["record"]["steps"][-1]["warm"]
    settling = iter([{"slice": []}, {"slice": ["a"], "continuity": "first"}, {"slice": ["a", "b"], "continuity": "second"}, {"slice": ["a", "b"], "continuity": "third"}])

    def read(tool, arguments):
        assert arguments == {}, "warm reads must not feed earlier continuity into the next call"
        return next(settling)

    reply = warmed_reply(read, "recall_context", {}, {**warm, "seconds": 0.001})
    assert reply == {"slice": ["a", "b"], "continuity": "third"}

    counter = itertools.count()
    with pytest.raises(SystemExit, match=r"recall_context still changed between two reads 0.001s apart: the index is not warm \(polled for 0.05s\)"):
        warmed_reply(lambda tool, arguments: {"slice": [next(counter)]}, "recall_context", {}, {"seconds": 0.001, "within": 0.05})


def test_a_recording_made_before_the_graph_was_built_is_not_reused_for_a_scenario_that_asks_for_it(tmp_path: Path) -> None:
    """Catches an old recording, with the graph warming, being served again after the scenario began to ask for the
    graph: its replies carry no relation mark, whatever the arm."""
    ferry_seed(tmp_path)
    old = tmp_path / "shapes.json"
    old.write_text(
        json.dumps({"product_sha": recording.checkout_sha(ROOT), "shapes": {"context": {"map": {"membership_graph": "warming"}}}}), encoding="utf-8"
    )
    asking = load_scenario(replay_scenario(tmp_path, seed="seed", ready_graph=True))
    problems = recording.record(asking, Arm("test", ROOT), old, tmp_path / "scratch")
    assert problems and "without building the graph" in problems[0]
    plain = load_scenario(replay_scenario(tmp_path, seed="seed"))
    assert recording.record(plain, Arm("test", ROOT), old, tmp_path / "scratch") == []



def test_a_recording_from_another_commit_or_an_uncommitted_change_of_the_arm_is_refused_and_says_to_delete_it(tmp_path: Path) -> None:
    """Catches a recording reused after the arm's code moved: the agent would read the new skill and tool text beside
    the old code's replies, and the report would credit the arm's new state with them."""
    arm_root = tmp_path / "arm"
    arm_root.mkdir()
    git = ["git", "-C", str(arm_root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    (arm_root / "reply.py").write_text("REPLY = 'first'\n", encoding="utf-8")
    for command in (["init", "-q"], ["add", "reply.py"], ["commit", "-q", "--no-verify", "-m", "first"]):
        subprocess.run([*git, *command], check=True)
    ferry_seed(tmp_path)
    scenario = load_scenario(replay_scenario(tmp_path, seed="seed"))
    arm, target = Arm("cand", arm_root), tmp_path / "shapes.json"
    target.write_text(json.dumps({"product_sha": recording.checkout_sha(arm_root), "shapes": {}}), encoding="utf-8")
    assert recording.record(scenario, arm, target, tmp_path / "scratch") == []

    (arm_root / "reply.py").write_text("REPLY = 'second'\n", encoding="utf-8")
    uncommitted = recording.record(scenario, arm, target, tmp_path / "scratch")
    subprocess.run([*git, "commit", "-q", "--no-verify", "-am", "second"], check=True)
    committed = recording.record(scenario, arm, target, tmp_path / "scratch")
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert len(uncommitted) == 1 and "-dirty-" in uncommitted[0] and uncommitted[0].endswith("delete it to record again")
    assert len(committed) == 1 and f"the checkout is now at {head};" in committed[0]



def test_a_new_untracked_product_file_changes_the_arm_state_and_a_run_folder_does_not(tmp_path: Path) -> None:
    """Catches a recording reused after a dev-round repair added an uncommitted scaffold file: the seeded vault holds
    the new file, but the recorded replies came from the vault without it."""
    arm_root = tmp_path / "arm"
    (arm_root / "src").mkdir(parents=True)
    git = ["git", "-C", str(arm_root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    (arm_root / "src" / "reply.py").write_text("REPLY = 'first'\n", encoding="utf-8")
    for command in (["init", "-q"], ["add", "src/reply.py"], ["commit", "-q", "--no-verify", "-m", "first"]):
        subprocess.run([*git, *command], check=True)
    clean = recording.checkout_sha(arm_root)

    (arm_root / "runs").mkdir()
    (arm_root / "runs" / "runs.jsonl").write_text("{}\n", encoding="utf-8")
    assert recording.checkout_sha(arm_root) == clean

    (arm_root / "src" / "new-registry.yaml").write_text("kinds: {}\n", encoding="utf-8")
    added = recording.checkout_sha(arm_root)
    assert "-dirty-" in added
    (arm_root / "src" / "new-registry.yaml").write_text("kinds: {claim: {}}\n", encoding="utf-8")
    assert recording.checkout_sha(arm_root) not in (clean, added)


# Case folders, scenarios and the answer checks.

TURN_ONE = "When does the first ferry sail on weekdays?"
#: An invented rubric: the harness holds none of its own and copies a case folder's into every scenario.
RUBRIC = [
    {"id": "K1", "expect": True, "text": "The reply gives the sailing time that the reference gives."},
    {"id": "K2", "expect": True, "text": "The reply adds no sailing that the reference does not name."},
]


def case_folder(root: Path, cases: dict[str, dict]) -> Path:
    """An invented case folder: one seed, and for each case a case file and a marks key. `cases` maps an id to its
    `deterministic` block and set."""
    folder = root / "cases"
    (folder / "answer-keys").mkdir(parents=True)
    (folder / "rubric.json").write_text(json.dumps(RUBRIC), encoding="utf-8")
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
    assert {spec["type"] for spec in first.checks} == {"only_scripted_tools", "answer_has_all_of", "answer_in_order"}
    assert {spec["type"] for spec in scenarios["c2"].checks} == {"only_scripted_tools"}
    assert first.prompt == TURN_ONE and first.reference == "Reference of c1." and first.judge == "batch"
    assert first.hooks == ("UserPromptSubmit",) and first.served_tools == ("read_instructions", "recall_context")
    assert first.record["ready_graph"] is True and list(first.criteria) == RUBRIC
    assert json.loads((tmp_path / "out" / "case-sets.json").read_text()) == {"c1": "N", "c2": "S"}


def test_the_scenario_writer_refuses_a_case_folder_without_a_usable_rubric_and_names_the_file(tmp_path: Path) -> None:
    """Catches scenarios written with no judged criteria: every answer would then pass on its closed checks alone."""
    folder = case_folder(tmp_path, {"c1": {}})
    (folder / "rubric.json").unlink()
    with pytest.raises(SystemExit, match="rubric.json"):
        replay_cases.write_scenarios(folder, tmp_path / "out")
    (folder / "rubric.json").write_text(json.dumps([{"id": "K1", "text": "A criterion with no expected value."}]), encoding="utf-8")
    with pytest.raises(SystemExit, match="rubric.json.*boolean expect"):
        replay_cases.write_scenarios(folder, tmp_path / "out")
    assert not (tmp_path / "out" / "scenarios").exists()


def test_a_marks_key_with_an_unknown_check_type_or_malformed_groups_is_refused_by_name(tmp_path: Path) -> None:
    """Catches a typo in a key that would write a scenario with no check, so a wrong answer could pass, and groups that
    the run-time check would score as unknown on every paid run."""
    folder = case_folder(tmp_path, {"c1": {"deterministic": {"allof": [["x"]]}}})
    with pytest.raises(SystemExit, match="allof|deterministic"):
        replay_cases.write_scenarios(folder, tmp_path / "out")
    malformed = case_folder(tmp_path / "second", {"c1": {"deterministic": {"in_order": [["built"], []]}}})
    with pytest.raises(SystemExit, match=r"c1\.marks\.json: deterministic\.in_order: groups must be"):
        replay_cases.write_scenarios(malformed, tmp_path / "out")
    assert not (tmp_path / "out" / "scenarios").exists()


def answer(text: str) -> checks.Evidence:
    return checks.Evidence((), (), {}, answer=text)


def test_all_of_needs_one_spelling_from_each_group_whatever_the_case() -> None:
    spec = {"groups": [["42", "forty-two"], ["Telescope"]]}
    assert checks.answer_has_all_of(spec, answer("The TELESCOPE loan lasts forty-two nights.")).verdict == checks.PASS
    assert checks.answer_has_all_of(spec, answer("The telescope loan lasts thirty nights.")).verdict == checks.FAIL


def test_in_order_fails_a_missing_group_and_a_wrong_order_and_malformed_groups_are_unknown() -> None:
    spec = {"groups": [["built"], ["repainted"], ["relit", "new lens"]]}
    assert checks.answer_in_order(spec, answer("The lighthouse was built, then repainted, then relit.")).verdict == checks.PASS
    assert checks.answer_in_order(spec, answer("It was repainted first, then built, then given a new lens.")).verdict == checks.FAIL
    assert checks.answer_in_order(spec, answer("It was built and later repainted.")).verdict == checks.FAIL
    assert checks.answer_in_order({"groups": []}, answer("anything")).verdict == checks.UNKNOWN


# The blind judge batch, the report and the table, on invented runs.


def invented_scenario(case_id: str) -> Scenario:
    return Scenario(
        id=case_id, title="t", prompt=TURN_ONE, served_tools=("recall_context",), builtin_tools=(), replies={}, default_reply={},
        checks=(), criteria=tuple(RUBRIC), reference=f"Reference of {case_id}.", judge="batch",
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
        answer = {key: value for c in RUBRIC for key, value in ((c["id"], held), (f"{c['id']}_reason", "r"))}
        lines.append(json.dumps({"id": item_id, "structured_output": answer}))
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
    runs = ten_stage_b_runs(tmp_path)
    assert judge_batch.prepare([runs], [], scenarios, out=out) == 12
    mapping = json.loads((out / "private" / "judge-map.json").read_text())["items"]
    twins = {i: e["twin_of"] for i, e in mapping.items() if e["twin_of"]}
    assert len(twins) == 2
    prompts = {json.loads(line)["id"]: json.loads(line)["prompt"] for line in (out / "judge-views.jsonl").read_text().splitlines()}
    assert all(prompts[twin] == prompts[first] for twin, first in twins.items())

    steady = judge_batch.import_judgments(judge_batch_as(out, lambda run: True), out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
    assert steady["agreement"] == 1.0 and not steady["incomplete"]

    flipped = next(iter(twins))
    judgments = judge_batch_as(out, lambda run: True)
    lines = [json.loads(line) for line in judgments.read_text().splitlines()]
    for line in lines:
        if line["id"] == flipped:
            line["structured_output"]["K1"] = False
    judgments.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    noisy = judge_batch.import_judgments(judgments, out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
    assert noisy["agreement"] == 0.5 and noisy["incomplete"]
    assert judge_batch.agreement_line(noisy).startswith("incomplete: judge agreement 0.50")


def test_a_missing_judgment_is_unknown_and_never_a_disagreement_or_a_pass(tmp_path: Path) -> None:
    scenarios = [invented_scenario(f"c{n}") for n in range(5)]
    out = tmp_path / "batch"
    runs = ten_stage_b_runs(tmp_path)
    judge_batch.prepare([runs], [], scenarios, out=out)
    mapping = json.loads((out / "private" / "judge-map.json").read_text())["items"]
    first = next(entry["twin_of"] for entry in mapping.values() if entry["twin_of"])
    judgments = judge_batch_as(out, lambda run: True)
    kept = [line for line in judgments.read_text().splitlines() if json.loads(line)["id"] != first]
    judgments.write_text("\n".join(kept) + "\n", encoding="utf-8")
    summary = judge_batch.import_judgments(judgments, out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
    verdicts = [row["verdict"] for row in json.loads((out / "judged.json").read_text())["rows"]]
    assert sorted(verdicts) == [checks.PASS] * 9 + [checks.UNKNOWN]
    assert summary["usable_pairs"] == summary["doubled_items"] - 1 and summary["agreement"] == 1.0


def test_an_answer_rerun_after_judge_prepare_does_not_inherit_the_judgment_of_the_old_answer(tmp_path: Path) -> None:
    """Catches a judgment bound by run key only: a run rerun under the same key after judge-prepare would receive the
    verdict of an answer that the judge never read."""
    scenarios = [invented_scenario(f"c{n}") for n in range(5)]
    out = tmp_path / "batch"
    runs = ten_stage_b_runs(tmp_path)
    judge_batch.prepare([runs], [], scenarios, out=out)
    judgments = judge_batch_as(out, lambda run: True)
    with runs.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(stage_b_row("c2", "cand", "The first ferry sails at 09:40.")) + "\n")
    judge_batch.import_judgments(judgments, out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
    rows = {(row["scenario"], row["arm"]): row for row in json.loads((out / "judged.json").read_text())["rows"]}
    rerun = rows.pop(("c2", "cand"))
    assert rerun["verdict"] == checks.UNKNOWN and "changed after judge-prepare" in rerun["criteria"][0]["detail"]
    assert {row["verdict"] for row in rows.values()} == {checks.PASS}



def test_a_judge_import_given_none_of_the_prepared_runs_is_refused_and_names_the_run_files(tmp_path: Path) -> None:
    """Catches an import that forgot --stage-b: every judgment would read unknown with a reason that asks to judge
    again, and the report would show a batch that was judged in full as unjudged."""
    scenarios = [invented_scenario(f"c{n}") for n in range(5)]
    out = tmp_path / "batch"
    runs = ten_stage_b_runs(tmp_path)
    judge_batch.prepare([runs], [], scenarios, out=out)
    judgments = judge_batch_as(out, lambda run: True)
    with pytest.raises(SystemExit, match="--stage-b and --stage-c"):
        judge_batch.import_judgments(judgments, out=out, stage_b=[], stage_c=[], scenarios=scenarios)
    assert not (out / "judged.json").exists()


FAKE_JUDGE = """#!{python}
import json, sys
argv = sys.argv[1:]
prompt, schema = argv[argv.index("-p") + 1], json.loads(argv[argv.index("--json-schema") + 1])
with open({log!r}, "a", encoding="utf-8") as log:
    log.write(json.dumps({{"prompt": prompt, "schema": schema}}) + "\\n")
if "Reference of c3." in prompt:
    print("not an answer")
else:
    answer = {{key: True if spec["type"] == "boolean" else "fine" for key, spec in schema["properties"].items()}}
    print(json.dumps({{"type": "result", "structured_output": answer}}))
"""


def test_the_judge_runner_asks_each_item_in_a_process_of_its_own_and_its_lines_import_as_judgments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches a runner that judges the batch in one context (the second copy of a doubled item would see the first),
    that sends an item another item's schema, that keeps a malformed answer, or whose dry run starts a judge. The model
    is replaced at the process boundary by a script that answers from the schema it is given and fails case c3."""
    from import_guidance.__main__ import main

    scenarios = [invented_scenario(f"c{n}") for n in range(5)]
    out = tmp_path / "batch"
    runs = ten_stage_b_runs(tmp_path)
    judge_batch.prepare([runs], [], scenarios, out=out)
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_JUDGE.format(python=sys.executable, log=str(log)), encoding="utf-8")
    fake.chmod(0o755)

    assert main(["judge-run", "--out", str(out), "--judge-model", "judge-m", "--dry-run"]) == 0
    assert "Would judge 12 of 12 items" in capsys.readouterr().out
    assert not log.exists() and not (out / "judgments.jsonl").exists()

    total, judged, missing = judge_batch.judge_all(out, model="judge-m", executable=fake)
    items = {item["id"]: item for item in map(json.loads, (out / "judge-views.jsonl").read_text().splitlines())}
    failing = {item_id for item_id, item in items.items() if "Reference of c3." in item["prompt"]}
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == len(items) + len(failing)
    assert all(any((call["prompt"], call["schema"]) == (item["prompt"], item["answer_schema"]) for item in items.values()) for call in calls)
    assert (total, judged, set(missing)) == (12, 12 - len(failing), failing)
    lines = [json.loads(line)["id"] for line in (out / "judgments.jsonl").read_text().splitlines()]
    assert sorted(lines) == sorted(set(items) - failing)

    judge_batch.import_judgments(out / "judgments.jsonl", out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
    rows = json.loads((out / "judged.json").read_text())["rows"]
    assert {(row["scenario"], row["verdict"]) for row in rows} == {("c0", "pass"), ("c1", "pass"), ("c2", "pass"), ("c3", "unknown"), ("c4", "pass")}
    log.unlink()
    judge_batch.judge_all(out, model="judge-m", executable=fake)
    assert len(log.read_text().splitlines()) == 2 * len(failing)


def test_a_judge_item_shows_no_arm_model_or_stage_and_both_stages_share_one_shuffled_file(tmp_path: Path) -> None:
    """Catches an item that lets the judge tell the arm, the model or the stage, which would bias every judged criterion."""
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
    scenarios = [invented_scenario(f"c{n}") for n in range(3)]
    judge_batch.prepare([runs], [], scenarios, out=out)
    judge_batch.import_judgments(judge_batch_as(out, lambda run: True), out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
    sets = tmp_path / "case-sets.json"
    sets.write_text(json.dumps({"c0": "S", "c1": "S", "c2": "S"}), encoding="utf-8")
    text = replay_report.build([runs], judged=out / "judged.json", case_sets=sets, baseline="base", candidate="cand")
    line = next(line for line in text.splitlines() if line.startswith("- Set S, all models: difference"))
    assert "over 1 paired case(s) of 3" in line and "unknown cases (2): c1, c2" in line
    assert "TimeoutExpired" in text.split("## Runs without a usable result")[1]


def test_a_model_with_an_extra_repeat_weighs_no_more_in_its_case_than_a_model_with_one(tmp_path: Path) -> None:
    """Catches a case value that averages (model, repeat) pairs as one flat list: a model rerun on the case would then
    pull the difference and the length ratio toward its own result. Model A has two repeats with the baseline passing
    and the candidate failing, model B one repeat with the reverse; per model the case is even, so the difference is 0."""
    short, long = "s" * 100, "l" * 400
    rows = [
        stage_b_row("c0", "base", short, model="model-a", repeat=0, verdicts=checks.PASS),
        stage_b_row("c0", "cand", long, model="model-a", repeat=0, verdicts=checks.FAIL),
        stage_b_row("c0", "base", short, model="model-a", repeat=1, verdicts=checks.PASS),
        stage_b_row("c0", "cand", long, model="model-a", repeat=1, verdicts=checks.FAIL),
        stage_b_row("c0", "base", long, model="model-b", repeat=0, verdicts=checks.FAIL),
        stage_b_row("c0", "cand", short, model="model-b", repeat=0, verdicts=checks.PASS),
    ]
    runs = write_rows(tmp_path / "b.jsonl", rows)
    out = tmp_path / "batch"
    scenarios = [invented_scenario("c0")]
    judge_batch.prepare([runs], [], scenarios, out=out)
    judge_batch.import_judgments(judge_batch_as(out, lambda run: True), out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
    sets = tmp_path / "case-sets.json"
    sets.write_text(json.dumps({"c0": "S"}), encoding="utf-8")
    text = replay_report.build([runs], judged=out / "judged.json", case_sets=sets, baseline="base", candidate="cand")
    assert "- Set S, all models: difference +0.00 " in text
    assert "| c0 | S | 2/3 | 1/3 | +0.00 |" in text
    length = text.split("## Reply length")[1].split("\n- Set S, all models: ")[1]
    assert length.startswith("mean log ratio +0.000 (candidate over baseline x1.00;")


def test_the_report_names_every_product_state_of_an_arm_when_its_runs_came_from_more_than_one(tmp_path: Path) -> None:
    """Catches a report that names one state per arm when a re-recording into the same run folder mixed two: the
    difference would be read as one commit's."""
    later = stage_b_row("c1", "cand", "Sails at 07:15.") | {"product_sha": "def"}
    rows = [stage_b_row("c0", "base", "Sails at 07:15."), stage_b_row("c0", "cand", "Sails at 07:15."), stage_b_row("c1", "base", "Sails at 07:15."), later]
    sets = tmp_path / "case-sets.json"
    sets.write_text(json.dumps({"c0": "S", "c1": "S"}), encoding="utf-8")
    text = replay_report.build([write_rows(tmp_path / "b.jsonl", rows)], judged=None, case_sets=sets, baseline="base", candidate="cand")
    assert text.splitlines()[0] == (
        "Baseline `base` at `abc`. Candidate `cand` at `abc`, `def` (more than one product state: these runs did not all measure the same code)."
    )


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
    scenarios = [invented_scenario("c0"), invented_scenario("c1")]
    judge_batch.prepare([runs], [], scenarios, out=out)
    judge_batch.import_judgments(judge_batch_as(out, lambda run: True), out=out, stage_b=[runs], stage_c=[], scenarios=scenarios)
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
    scenarios = [invented_scenario("c0")]
    judge_batch.prepare([], [runs], scenarios, out=out)
    judgments = judge_batch_as(out, lambda run: run["arm"] == "base")
    judge_batch.import_judgments(judgments, out=out, stage_b=[], stage_c=[runs], scenarios=scenarios)
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


def test_a_reply_cannot_reveal_its_arm_through_its_runtime_directory(tmp_path: Path) -> None:
    """A quoted working directory must not unblind a stage-C answer or break its judgment join."""
    work = tmp_path / "model-quokka" / "arm-zeta" / "c0"
    answer = f"Sails at 07:15. My working directory is {work}/cwd."
    runs = write_rows(tmp_path / "runs.jsonl", [{
        "scenario": "c0", "arm": "arm-zeta", "model": "model-quokka", "repeat": 0,
        "client_mode": "plugin", "error": None, "replies": [answer],
        "stream": str(work / "stream.jsonl"),
    }])
    out = tmp_path / "batch"
    scenarios = [invented_scenario("c0")]
    judge_batch.prepare([], [runs], scenarios, out=out)

    views = (out / "judge-views.jsonl").read_text()
    assert "arm-zeta" not in views and "model-quokka" not in views
    assert "Sails at 07:15." in views and "<session-workdir>/cwd" in views
    assert answer in runs.read_text()
    summary = judge_batch.import_judgments(
        judge_batch_as(out, lambda run: True), out=out,
        stage_b=[], stage_c=[runs], scenarios=scenarios,
    )
    assert summary["agreement"] == 1.0
