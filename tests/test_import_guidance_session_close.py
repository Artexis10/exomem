"""The session-close suite's own logic: the refusal to score without a key, and the snapshot diff the scores rest on."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_guidance import checks, session_close, session_score
from import_guidance.session_close import Case

SEED = session_close.suite_dir() / "seeds" / "c01"
PERSON = "Entities/People/Tavi Brennock.md"
OTHER = "Entities/People/Corin Haldane.md"
ROUTINE = "Notes/Patterns/sunday-meal-prep-routine.md"
RECAP = "Sources/Episodes/2026-11-03-recap.md"
RECAP_ID = "11111111-2222-5333-8444-555555555555"

KEY = {
    "reference": "Tavi's meeting-free mornings belong on her person page.",
    "must_change": [PERSON],
    "must_not_change": [OTHER],
    "recap": "required",
    "new_pages": "none",
}


def case(path: Path = Path("c-test.json")) -> Case:
    return Case("c-test", "t", SEED, "balanced", ("hi",), path)


def page(text: str, mtime_ns: int) -> dict:
    return {"text": text, "mtime_ns": mtime_ns}


def test_scoring_refuses_missing_malformed_and_unseeded_scorer_keys(tmp_path: Path) -> None:
    keyed = case(tmp_path / "c-test.json")
    key_path = tmp_path / "answer-keys/c-test.json"
    key_path.parent.mkdir()
    with pytest.raises(SystemExit, match="no answer_key"):
        session_score.refuse_unkeyed([keyed])
    key_path.write_text("null")
    with pytest.raises(SystemExit, match="no answer_key"):
        session_score.refuse_unkeyed([keyed])
    key_path.write_text("{")
    with pytest.raises(SystemExit, match="cannot be read"):
        session_score.refuse_unkeyed([keyed])
    key_path.write_text(json.dumps({**KEY, "must_change": ["Entities/People/Nobody.md"]}))
    with pytest.raises(SystemExit, match="not in the seed"):
        session_score.refuse_unkeyed([keyed])
    key_path.write_text(json.dumps(KEY))
    assert session_score.refuse_unkeyed([keyed]) == {keyed.id: KEY}


def test_every_shipped_case_loads_and_the_variants_change_only_engagement() -> None:
    cases = {c.id: c for c in session_close.load_cases("all")}
    assert cases["c08-regression-maximal"].turns == cases["c07-regression"].turns
    assert cases["c08-regression-maximal"].seed == cases["c07-regression"].seed
    assert cases["c08-regression-maximal"].engagement == "maximal"
    assert cases["c06-policy-light"].turns == cases["c01-person"].turns
    assert cases["c06-policy-light"].seed == cases["c01-person"].seed
    assert cases["c06-policy-light"].engagement == "light"
    assert cases["c06-policy-light"].recall_turn is None
    assert cases["c01-person"].recall_turn
    assert all(not hasattr(c, "answer_key") for c in cases.values())
    assert all("answer_key" not in json.loads(c.path.read_text()) for c in cases.values())


def test_external_cases_use_adjacent_truth_without_exposing_it_to_actor_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from import_guidance.__main__ import main
    from import_guidance.recording import Arm

    [default] = session_close.load_cases("c01")
    case_dir = tmp_path / "cases"
    (case_dir / "answer-keys").mkdir(parents=True)
    (case_dir / default.path.name).write_bytes(default.path.read_bytes())
    shipped_key = default.path.parent / "answer-keys" / f"{default.id}.json"
    key_bytes = shipped_key.read_bytes()
    external_key = case_dir / "answer-keys" / shipped_key.name
    external_key.write_bytes(key_bytes)
    truth = json.loads(key_bytes)
    [external] = session_close.load_cases("c01", case_dir=case_dir)
    assert external.turns == default.turns and external.seed == default.seed
    assert external.path.parent == case_dir and not hasattr(external, "answer_key")
    read_text, scoring, key_reads = Path.read_text, False, set()

    def observe_reads(path: Path, *args, **kwargs):
        if path in (shipped_key, external_key):
            assert scoring and path == external_key, "generation must not read keys; scoring must use adjacent external truth"
            key_reads.add(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observe_reads)
    root = Path(__file__).resolve().parents[1]
    assert main(["run", "--suite", "session-close", "--case-dir", str(case_dir), "--scenario", "c01", "--model", "unused", "--product-root", f"test={root}", "--dry-run", "--out", str(tmp_path / "run")]) == 0
    work = tmp_path / "actor"
    work.mkdir()
    env = session_close.pinned_env(Arm("test", root), work, external)
    files = session_close.stage_agent_files(Arm("test", root), work, env)
    staged = "\n".join(path.read_text() for path in files.values())
    assert not key_reads and str(case_dir) not in staged and truth["reference"] not in staged
    scoring = True
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps({"scenario": external.id, "repeat": 0, "model": "test", "arm": "test", "error": None,
                                "observed": observed(pages={PERSON: {"compiled": False, "cites": []}}, recaps=False)}) + "\n")
    out = tmp_path / "scores"
    arguments = ["session-score", str(runs), "--case-dir", str(case_dir), "--baseline", "test", "--candidate", "test", "--out", str(out)]
    assert main(arguments) == 0
    assert truth["reference"] in json.loads((out / "judge-views.jsonl").read_text())["prompt"]
    assert main(arguments + ["--judgments", str(tmp_path / "missing-judgments.jsonl")]) == 0
    score = json.loads((out / "scores.jsonl").read_text())
    assert score["recap_rule"] == truth["recap"] and score["restraint_case"] == (not truth["must_change"])
    assert key_reads == {external_key} and external_key.read_bytes() == key_bytes
    with pytest.raises(SystemExit, match="--case-dir applies to the session-close suite only"):
        main(["run", "--case-dir", str(case_dir), "--model", "unused", "--dry-run", "--out", str(tmp_path / "other")])


def test_client_modes_keep_capture_and_fresh_configuration_separate_without_changing_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from import_guidance.recording import Arm

    invocations = []

    def local_protocol(argv, cwd, env, turns, stream_path, hook_turn=None):
        assert not list(cwd.iterdir())
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        invocations.append((argv, settings))
        stream = json.dumps({"type": "system", "subtype": "init", "session_id": str(len(invocations)),
                             "mcp_servers": [{"name": checks.SERVER, "status": "connected"}]}) + "\n"
        stream += (json.dumps({"type": "result", "subtype": "success", "result": "Local protocol receipt."}) + "\n") * len(turns)
        stream_path.write_text(stream)
        return SimpleNamespace(stdout=stream, stderr="", returncode=0, stdout_eof=True, stopped=None, finished=len(turns), hook_turn=None)

    monkeypatch.setattr(session_close, "drive_session", local_protocol)
    selected = session_close.load_cases("c01")[0]
    arm = Arm("test", Path(__file__).resolve().parents[1])
    rows = [session_close.run_session(selected, arm, repeat=0, model="unused", out=tmp_path, executable=Path("unused"), stamp={"product_shas": {"test": "test"}}, client_mode=mode) for mode in ("plugin", "tool-only")]
    assert len(invocations) == 4 and rows[0]["stream"] != rows[1]["stream"]
    for row in rows:
        assert Path(row["stream"]).exists()
        assert row["client_mode"] == row["fresh"]["client_mode"]
        for stage in (row, row["fresh"]):
            assert stage["error"] is None and stage["hook_invocations"] == 0
            argv, settings = stage["argv"], stage["settings"]
            assert "--strict-mcp-config" in argv and "--include-hook-events" in argv
            assert argv[argv.index("--setting-sources") + 1] == "project"
            assert json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"][checks.SERVER]["args"] == ["-m", "exomem", "--transport", "stdio"]
            if row["client_mode"] == "tool-only":
                assert settings == {"disableAllHooks": True} and "--append-system-prompt-file" not in argv
            else:
                assert settings["hooks"] and "--append-system-prompt-file" in argv


def test_tool_only_rejects_a_synthetic_hook_turn_before_staging(tmp_path: Path) -> None:
    from import_guidance.__main__ import main

    with pytest.raises(SystemExit, match="--hook-turn requires --client-mode plugin"):
        main(["run", "--suite", "session-close", "--client-mode", "tool-only", "--hook-turn", "--model", "unused", "--dry-run", "--out", str(tmp_path)])


def test_credits_launcher_keeps_fresh_on_the_same_payer_and_preserves_tail_or_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from import_guidance.recording import Arm

    def no_subscription():
        pytest.fail("an explicit credits route must never discover or fall back to subscription Claude")

    monkeypatch.setattr(session_close, "discover_agent_envelope", no_subscription)
    monkeypatch.setattr(session_close, "WARM_HOLD_SECONDS", 0)
    launcher = tmp_path / "fake-harness"
    launcher.write_text(f"#!{sys.executable}\n" + '''import argparse,json,os,sys
from pathlib import Path
assert sys.argv[1:3] == ["claude-credits", "run"]
boundary = sys.argv.index("--")
parser = argparse.ArgumentParser()
for flag in ("--account", "--model", "--budget", "--reason"):
 parser.add_argument(flag, required=True)
parser.add_argument("--stream-json", action="store_true", required=True)
args = parser.parse_args(sys.argv[3:boundary])
# These flags belong to the maintained launcher's closed duplex protocol.
assert not set(sys.argv[boundary+1:]) & {"-p", "--print", "--input-format", "--output-format", "--verbose", "--model", "--no-session-persistence", "--session-id", "--resume"}
receipt = Path.cwd().parent / "launcher-receipt.json"
messages = []
receipt.write_text(json.dumps({"argv":sys.argv[1:], "messages":messages}))
if args.account == "rejected-payer":
 print("Local payer rejection.", file=sys.stderr, flush=True)
 sys.exit(23)
print(json.dumps({"type":"system", "subtype":"init", "session_id":str(os.getpid()), "claude_code_version":"fixture-cli", "mcp_servers":[{"name":"exomem", "status":"connected"}]}), flush=True)
for line in sys.stdin:
 message = json.loads(line)
 assert message["type"] == "user" and message["message"]["role"] == "user"
 messages.append(message["message"]["content"])
 print(json.dumps({"type":"result", "subtype":"success", "result":"Local protocol receipt."}), flush=True)
receipt.write_text(json.dumps({"argv":sys.argv[1:], "messages":messages}))
settings = json.loads(Path(sys.argv[sys.argv.index("--settings")+1]).read_text())
if not settings.get("disableAllHooks"):
 print(json.dumps({"type":"system", "subtype":"hook_started", "hook_event":"SessionEnd"}), flush=True)
 print(json.dumps({"type":"system", "subtype":"hook_response", "hook_event":"SessionEnd", "output":""}), flush=True)
''')
    launcher.chmod(0o700)
    selected = session_close.load_cases("c01")[0]
    arm = Arm("test", Path(__file__).resolve().parents[1])
    for payer, mode in (("fixture-payer", "plugin"), ("rejected-payer", "tool-only")):
        credits = session_close.CreditsRun(payer, 0.25, "Local adapter proof", executable=launcher)
        row = session_close.run_session(selected, arm, model="local-model", repeat=0, out=tmp_path / payer, executable=launcher,
                                        stamp={"cli_version": None, "product_shas": {"test": "test"}}, credits=credits, client_mode=mode)
        assert row["cli_version"] is None
        for stage, turns in ((row, selected.turns), (row["fresh"], (selected.recall_turn,))):
            assert stage["transport"] == "claude-credits" and stage["credits_account"] == payer
            assert stage["credits_budget_usd"] == 0.25 and stage["client_mode"] == mode
            argv = stage["argv"]
            assert argv[0] == str(launcher) and "--strict-mcp-config" in argv and "--include-hook-events" in argv
            assert argv[argv.index("--budget")+1] == str(credits.budget_usd) and argv[argv.index("--reason")+1] == credits.reason
            assert argv[argv.index("--account")+1] == payer and argv[argv.index("--model")+1] == "local-model"
            assert argv[argv.index("--setting-sources")+1] == "project"
            assert argv[argv.index("--allowedTools")+1] == f"mcp__{checks.SERVER}" and argv[argv.index("--tools")+1] == ""
            receipt = json.loads((Path(stage["stream"]).parent / "launcher-receipt.json").read_text())
            assert receipt["argv"] == argv[1:]
            if payer == "rejected-payer":
                assert stage["error"] and stage["returncode"] == 23 and stage["session_id"] is None
                assert stage["settings"] == {"disableAllHooks": True} and "--append-system-prompt-file" not in argv
            else:
                assert stage["error"] is None and stage["stdout_eof"] and stage["actor_version"] == "fixture-cli"
                assert stage["hook_starts"] == stage["hook_invocations"] == 1
                assert receipt["messages"] == [[{"type": "text", "text": turn}] for turn in turns]
                assert json.loads(Path(stage["stream"]).read_text().splitlines()[-1])["subtype"] == "hook_response"
        if payer != "rejected-payer":
            assert row["session_id"] != row["fresh"]["session_id"]


def test_credits_discovery_keeps_the_selected_launcher_or_fails_without_subscription(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from import_guidance.recording import Arm

    def no_subscription():
        pytest.fail("a missing credits launcher must fail without subscription discovery")

    monkeypatch.setattr(session_close, "discover_agent_envelope", no_subscription)
    monkeypatch.setattr(session_close.shutil, "which", lambda _name: None)
    # This discovery-only check starts no child; the output guard has its own maintained tests.
    monkeypatch.setattr(session_close, "refuse_unsafe_out_dir", lambda out: out)
    with pytest.raises(SystemExit, match="credits launcher.*not found"):
        session_close.run_all([], model="unused", repeats=1, out=tmp_path / "missing", arms=[], credits=session_close.CreditsRun("fixture-payer", 0.25, "Local adapter proof"))
    launched = []

    def record_launch(_case, _arm, **kwargs):
        launched.append(kwargs)
        return {"error": None}

    monkeypatch.setattr(session_close, "run_session", record_launch)
    launcher = tmp_path / "selected-harness"
    monkeypatch.setattr(session_close.shutil, "which", lambda name: str(launcher) if name == "harness" else None)
    credits = session_close.CreditsRun("fixture-payer", 0.25, "Local adapter proof")
    session_close.run_all(session_close.load_cases("c01"), model="unused", repeats=1, out=tmp_path / "selected", arms=[Arm("test", Path(__file__).resolve().parents[1])], credits=credits)
    assert len(launched) == 1 and launched[0]["executable"] == launcher and launched[0]["credits"] == credits


def test_credits_cli_rejects_partial_or_invalid_payer_options_before_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from import_guidance.__main__ import main

    def no_discovery(*_args, **_kwargs):
        pytest.fail("invalid credits options must fail before case loading or executable discovery")

    with monkeypatch.context() as invalid:
        invalid.setattr(session_close, "load_cases", no_discovery)
        invalid.setattr(session_close, "discover_agent_envelope", no_discovery)
        base = ["run", "--suite", "session-close", "--model", "unused", "--out", str(tmp_path)]
        for options in (["--credits-account", "fixture-payer"],
                        ["--credits-account", " ", "--credits-budget-usd", "1", "--credits-reason", "Local proof"],
                        ["--credits-account", "fixture-payer", "--credits-budget-usd", "1", "--credits-reason", " "],
                        *(["--credits-account", "fixture-payer", "--credits-budget-usd", budget, "--credits-reason", "Local proof"] for budget in ("0", "nan", "inf"))):
            with pytest.raises(SystemExit, match="--credits-"):
                main(base + options)
        with pytest.raises(SystemExit, match="session-close suite only"):
            main(["run", "--model", "unused", "--out", str(tmp_path), "--credits-account", "fixture-payer", "--credits-budget-usd", "1", "--credits-reason", "Local proof"])
    configurations = []

    def record_configuration(_cases, **kwargs):
        configurations.append(kwargs["credits"])
        return []

    monkeypatch.setattr(session_close, "run_all", record_configuration)
    assert main(base + ["--scenario", "c01", "--product-root", f"test={Path(__file__).resolve().parents[1]}", "--credits-account", "fixture-payer", "--credits-budget-usd", "0.25", "--credits-reason", "Local proof"]) == 0
    assert configurations == [session_close.CreditsRun("fixture-payer", 0.25, "Local proof")]


def test_tool_only_hook_observations_reject_contamination_and_unavailable_transport() -> None:
    base = {"error": None, "hook_invocations": 0, "hook_starts": 0, "server_connected": {checks.SERVER: "connected"}}
    assert session_close.client_mode_error("tool-only", base) is None
    assert "observed" in session_close.client_mode_error("tool-only", base | {"hook_invocations": 1})
    assert "unavailable" in session_close.client_mode_error("tool-only", base | {"server_connected": {}})
    assert "unavailable" in session_close.client_mode_error("tool-only", base | {"hook_invocations": None})
    assert "observed" in session_close.client_mode_error("tool-only", base | {"hook_starts": 1})
    assert session_close.client_mode_error("plugin", base | {"hook_invocations": 1}) is None
    for subtype in ("hook_response", "hook_started"):
        stream = "\n".join(json.dumps(event) for event in (
            {"type": "system", "subtype": "init", "session_id": "local", "mcp_servers": [{"name": checks.SERVER, "status": "connected"}]},
            {"type": "system", "subtype": subtype, "hook_event": "UserPromptSubmit", "output": ""},
            {"type": "result", "subtype": "success", "result": "Local protocol receipt."},
        ))
        result = session_close.session_result(SimpleNamespace(stdout=stream, stderr="", returncode=0, stdout_eof=True, stopped=None, finished=1), 1, "tool-only")
        assert result["hook_invocations"] + result["hook_starts"] == 1 and "observed" in result["error"]
    row = base | {"client_mode": "tool-only", "recall_enabled": True,
                  "fresh": base | {"client_mode": "tool-only", "hook_invocations": 1, "answer": KEY["reference"], "session_id": "fresh"}}
    assert session_score.recall_evidence(row, KEY) == (checks.UNKNOWN, [])


def routine(sources: str) -> str:
    return f"---\ntype: pattern\nsources: {sources}\n---\n\n# Sunday meal-prep routine\n\nOrder groceries on Friday.\n"


def test_diff_reports_changed_pages_and_which_recaps_a_compiled_page_cites_and_the_write_order() -> None:
    before = {
        PERSON: page("---\ntype: entity\n---\n\n# Tavi Brennock\n\nPrefers short updates.\n", 1),
        OTHER: page("---\ntype: entity\n---\n\n# Corin Haldane\n", 1),
        ROUTINE: page(routine("[]"), 1),
    }
    recap = f"---\ntype: source\nexomem_id: {RECAP_ID}\n---\n\n## Decided\n- Order on Friday.\n"
    after = {
        PERSON: page(before[PERSON]["text"] + "No meetings before 10:00.\n", 5),
        OTHER: before[OTHER],
        ROUTINE: page(routine(f"[exomem://memory/{RECAP_ID}]"), 6),
        RECAP: page(recap, 9),
    }
    seen = session_close.diff_vault(before, after)
    assert list(seen["changed"]) == [PERSON, ROUTINE]
    assert "+No meetings before 10:00." in seen["changed"][PERSON]
    assert list(seen["recaps"]) == [RECAP]
    # An entity page is not of a compiled type, and a ref names the recap as a wikilink to its path does.
    assert seen["pages"] == {PERSON: {"compiled": False, "cites": []}, ROUTINE: {"compiled": True, "cites": [RECAP]}}
    after[ROUTINE] = page(routine("['[[Sources/Episodes/2026-11-03-recap]]']"), 6)
    assert session_close.diff_vault(before, after)["pages"][ROUTINE]["cites"] == [RECAP]
    assert seen["order"] == "before"
    # The same edit after the recap is reported as after, not as before.
    after[PERSON] = page(after[PERSON]["text"], 12)
    after[ROUTINE] = page(after[ROUTINE]["text"], 12)
    assert session_close.diff_vault(before, after)["order"] == "after"


def observed(*, pages: dict, recaps: bool) -> dict:
    return {
        "changed": {p: "diff" for p in pages},
        "removed": [],
        "created": {},
        "recaps": {RECAP: {"text": ""}} if recaps else {},
        "pages": pages,
        "order": "before",
    }


def verdicts_of(key: dict, seen: dict) -> dict[str, str]:
    return {o.check: o.verdict for o in session_score.check_row(key, {"observed": seen})}


def test_checks_fail_a_missing_update_and_a_write_the_key_forbids() -> None:
    nothing = {"changed": {}, "removed": [], "created": {}, "recaps": {}, "pages": {}, "order": "no_change"}
    verdicts = verdicts_of(KEY, nothing)
    assert verdicts["must_change"] == checks.FAIL
    assert verdicts["recap"] == checks.FAIL
    assert verdicts["must_not_change"] == checks.PASS
    wrote = observed(pages={PERSON: {"compiled": False, "cites": []}, OTHER: {"compiled": False, "cites": []}}, recaps=True)
    wrote["created"] = {"Notes/extra.md": "x"}
    assert verdicts_of(KEY, wrote) == {
        "must_change": checks.PASS,
        "must_not_change": checks.FAIL,
        "recap": checks.PASS,
        "recap_cited": checks.NOT_APPLICABLE,
        "new_pages": checks.FAIL,
    }


def test_recap_cited_passes_fails_and_is_not_applicable_by_what_the_run_changed() -> None:
    """A run that cited nothing must not read as a pass, and a run with nothing to cite must not read as one either."""
    cited = {ROUTINE: {"compiled": True, "cites": [RECAP]}}
    uncited = {ROUTINE: {"compiled": True, "cites": []}}
    entity = {PERSON: {"compiled": False, "cites": []}}
    assert verdicts_of(KEY, observed(pages=cited, recaps=True))["recap_cited"] == checks.PASS
    assert verdicts_of(KEY, observed(pages=cited | uncited | {OTHER: {"compiled": True, "cites": []}}, recaps=True))["recap_cited"] == checks.FAIL
    assert verdicts_of(KEY, observed(pages=uncited, recaps=True))["recap_cited"] == checks.FAIL
    assert verdicts_of(KEY, observed(pages=entity, recaps=True))["recap_cited"] == checks.NOT_APPLICABLE
    assert verdicts_of(KEY, observed(pages=uncited, recaps=False))["recap_cited"] == checks.NOT_APPLICABLE
    # Not applicable leaves a run's verdict to the other checks.
    outcomes = [checks.Outcome("recap_cited", checks.NOT_APPLICABLE, ""), checks.Outcome("recap", checks.PASS, "")]
    assert checks.verdict(None, outcomes, []) == checks.PASS
    assert checks.verdict(None, [checks.Outcome("recap_cited", checks.NOT_APPLICABLE, "")], []) == checks.UNKNOWN


def test_judge_view_shows_turns_reference_and_diffs_but_not_the_arm_or_the_recap() -> None:
    observed = {
        "changed": {PERSON: "+No meetings before 10:00.\n"},
        "removed": [],
        "created": {RECAP: "A recap by the candidate arm."},
        "recaps": {RECAP: {"text": "A recap by the candidate arm."}},
        "order": "before",
    }
    row = {"arm": "candidate", "model": "sonnet", "observed": observed, "compile_blocks": [{"outcome": "x"}]}
    view = session_score.judge_view(case(), KEY, row)
    assert "+No meetings before 10:00." in view
    assert KEY["reference"] in view
    assert "1. hi" in view
    assert not {"candidate", "sonnet", "recap", "compile"} & set(view.lower().replace(".", " ").split())


def test_an_interrupted_session_with_a_recap_cannot_pass_missing_canonical_work(tmp_path: Path) -> None:
    seen = observed(pages={}, recaps=True)
    outcomes = session_score.judge_row(case(), KEY, {"observed": seen}, model="unused", executable=Path("absent"), scratch=tmp_path)
    assert {o.check: o.verdict for o in outcomes} == {"J1": checks.PASS, "J2": checks.PASS, "J3": checks.FAIL}
    assert checks.verdict(None, session_score.check_row(KEY, {"observed": seen}), outcomes) == checks.FAIL
    assert "capture" not in session_score.native_views(case(), KEY, {"error": None, "observed": seen})


def test_a_cited_compiled_note_cannot_rescue_missing_entity_work(tmp_path: Path) -> None:
    mixed = session_close.load_cases("c11")[0]
    key = session_score.refuse_unkeyed([mixed])[mixed.id]
    compiled = next(path for path in key["must_change"] if path.startswith("Notes/"))
    seen = observed(pages={compiled: {"compiled": True, "cites": [RECAP]}}, recaps=True)
    result = session_score.score_row(mixed, key, {"observed": seen, "error": None}, judge_model="unused", executable=Path("absent"), scratch=tmp_path, answers={"capture": {c["id"]: True for c in session_score.CRITERIA}})
    assert next(o for o in result["deterministic"] if o["check"] == "recap_cited")["verdict"] == checks.PASS
    assert next(o for o in result["judged"] if o["check"] == "J3")["verdict"] == checks.FAIL
    assert result["verdict"] == checks.FAIL


def test_unavailable_recall_is_unknown_and_stays_in_its_own_denominator(tmp_path: Path) -> None:
    row = {"recall_enabled": True, "fresh": {
        "error": None, "session_id": "fresh", "answer": KEY["reference"],
        "server_connected": {checks.SERVER: "connected"},
        "calls": [{"name": f"mcp__{checks.SERVER}__search_memory", "result": None, "is_error": True}],
    }}
    result = session_score.score_recall(KEY, row, judge_model="unused", executable=Path("absent"), scratch=tmp_path)
    assert result == {"verdict": checks.UNKNOWN, "judged": []}
    scored = {
        "scenario": "c-test", "repeat": 0, "model": "test", "error": None, "verdict": checks.FAIL,
        "deterministic": [], "judged": [], "recap_rule": "required", "recap_produced": True, "recall": result, "restraint_case": False,
    }
    report = session_score.build_report([scored | {"arm": "baseline"}, scored | {"arm": "candidate"}], baseline="baseline", candidate="candidate")
    assert "| plugin | fresh recall | baseline | 0/0 | 1 | 0 |" in report
    assert "| plugin | recap production (required) | baseline | 1/1 | 0 | 0 |" in report
    assert "fresh recall: no complete paired case; 1 pair(s) unknown" in report


def test_a_complete_fresh_answer_without_canonical_content_fails_even_if_it_repeats_the_reference(tmp_path: Path) -> None:
    row = {"recall_enabled": True, "fresh": {"error": None, "session_id": "fresh", "server_connected": {checks.SERVER: "connected"}, "answer": KEY["reference"], "calls": []}}
    assert session_score.score_recall(KEY, row, judge_model="unused", executable=Path("absent"), scratch=tmp_path)["verdict"] == checks.FAIL


def test_canonical_excerpts_activation_and_exact_units_reach_judgment_without_full_reads(tmp_path: Path) -> None:
    from exomem.kbdir import kb_prefix

    selected = session_close.load_cases("c02")[0]
    key = session_score.refuse_unkeyed([selected])[selected.id]
    path, text = kb_prefix() + ROUTINE, selected.turns[1]
    calls = [
        ("search_memory", [{"path": path, "title": "Sunday meal-prep routine", "excerpt": text, "status": "active"}]),
        ("search_memory", {"hits": [{"result_type": "semantic_unit", "parent_path": path, "excerpt": text, "parent_status": "active"}]}),
        ("search_memory", {"hits": [], "pack": {
            "claims": {path: {"lede": text, "sections": [text]}},
            "semantic_units": {path: {"parent": {"path": path, "status": "active"}, "units": [{"excerpt": text}]}},
            "truncation": ["More units omitted by bounds"],
        }}),
        ("activate_context", {"units": [{"text": text, "lifecycle": "active", "provenance": {"path": path, "anchor": OTHER}}],
                              "current_state": [{"statement": text, "path": path, "as_of": "2026-11-03"}],
                              "map": {"items": [{"path": OTHER, "state": {"statement": text, "path": path}}], "stale": False}}),
        ("read_memory", {"status": "found", "parent": {"path": path, "status": "active"},
                         "unit": {"content": text}, "parent_context": {"markdown": text, "truncated_before": True, "truncated_after": False}}),
    ]
    for tool, payload in calls:
        encoded_payload = json.dumps(payload)
        row = {"recall_enabled": True, "fresh": {"error": None, "session_id": "fresh", "question": selected.recall_turn,
               "answer": text, "server_connected": {checks.SERVER: "connected"},
               "calls": [{"name": f"mcp__{checks.SERVER}__{tool}", "is_error": False, "result": encoded_payload}]}}
        status, evidence = session_score.recall_evidence(row, key)
        assert status == checks.PASS, tool
        assert evidence and all(item["path"] == path and text in item["text"] for item in evidence)
        if tool == "activate_context":
            assert len(evidence) == 3
            assert evidence[0]["lifecycle"]["lifecycle"] == "active"
        if isinstance(payload, dict) and "pack" in payload:
            assert len(evidence) == 2
            assert evidence[0]["bounds"]["truncation"] == payload["pack"]["truncation"]
        if tool == "read_memory":
            assert len(evidence) == 2
            assert evidence[-1]["bounds"]["truncated_before"] is True
        result = session_score.score_recall(key, row, judge_model="unused", executable=None, scratch=tmp_path, answer={"R1": True, "R2": True})
        assert result["verdict"] == checks.PASS and len(result["judged"]) == 2
    preserved = key["must_not_change"][0]
    preserved_reply = json.dumps([{"path": preserved, "excerpt": (selected.seed / preserved).read_text()}])
    row["fresh"]["calls"] = [{"name": f"mcp__{checks.SERVER}__search_memory", "is_error": False,
                              "result": preserved_reply}]
    status, evidence = session_score.recall_evidence(row, key)
    assert status == checks.PASS and evidence[0]["path"] == preserved


def test_recap_and_locator_only_recall_fail_but_unknown_structures_stay_unknown(tmp_path: Path) -> None:
    selected = session_close.load_cases("c02")[0]
    key = session_score.refuse_unkeyed([selected])[selected.id]
    for tool, payload, expected in [
        ("read_memory", {"path": RECAP, "body": selected.turns[1]}, checks.FAIL),
        ("recall_context", {"units": [], "pointers": [{"ref": ROUTINE, "title": "Sunday meal-prep routine"}]}, checks.FAIL),
        ("search_memory", [{"path": ROUTINE, "title": "Sunday meal-prep routine"}], checks.FAIL),
        ("search_memory", [], checks.FAIL),
        ("recall_context", {"units": [{"path": ROUTINE, "text": selected.turns[1]}]}, checks.UNKNOWN),
        ("search_memory", {"unexpected": {"path": ROUTINE, "text": selected.turns[1]}}, checks.UNKNOWN),
    ]:
        encoded_payload = json.dumps(payload)
        row = {"recall_enabled": True, "fresh": {"error": None, "session_id": "fresh", "answer": selected.turns[1],
               "server_connected": {checks.SERVER: "connected"},
               "calls": [{"name": f"mcp__{checks.SERVER}__{tool}", "is_error": False, "result": encoded_payload}]}}
        result = session_score.score_recall(key, row, judge_model="unused", executable=None, scratch=tmp_path)
        assert result == {"verdict": expected, "judged": []}


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
def test_actor_cleanup_keeps_the_exited_leader_owned_until_its_group_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    killpg = os.killpg
    checked = []

    def kill_owned_group(pid: int, signum: int) -> None:
        exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        assert exited is not None and exited.si_pid == pid
        checked.append(pid)
        killpg(pid, signum)

    monkeypatch.setattr(session_close.os, "killpg", kill_owned_group)
    monkeypatch.setattr(session_close, "WARM_HOLD_SECONDS", 0)
    done = session_close.drive_session(
        [sys.executable, "-c", "import json,sys; [print(json.dumps({'type':'result','result':'done'}),flush=True) for line in sys.stdin]"],
        tmp_path, os.environ.copy(), ("local protocol check",), tmp_path / "stream.jsonl",
    )
    assert done.returncode == 0 and done.finished == 1 and len(checked) == 1


def test_session_end_hooks_after_input_eof_are_saved_and_contaminate_tool_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_close, "WARM_HOLD_SECONDS", 0)
    child = """import json,sys
print(json.dumps({"type":"system","subtype":"init","session_id":"local","mcp_servers":[{"name":"exomem","status":"connected"}]}),flush=True)
for line in sys.stdin:
 print(json.dumps({"type":"result","subtype":"success","result":"Local protocol receipt."}),flush=True)
print(json.dumps({"type":"system","subtype":"hook_started","hook_event":"SessionEnd"}),flush=True)
print(json.dumps({"type":"system","subtype":"hook_response","hook_event":"SessionEnd","output":""}),flush=True)
"""
    path = tmp_path / "stream.jsonl"
    done = session_close.drive_session([sys.executable, "-c", child], tmp_path, os.environ.copy(), ("Local protocol check.",), path)
    result = session_close.session_result(done, 1, "tool-only")
    assert done.returncode == 0 and path.read_text() == done.stdout
    assert result["hook_starts"] == result["hook_invocations"] == 1
    assert "observed" in result["error"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup of the owned stdout holder")
def test_an_exited_actor_with_stdout_still_open_cannot_prove_zero_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_close, "WARM_HOLD_SECONDS", 0)
    monkeypatch.setattr(session_close, "EXIT_GRACE_SECONDS", 0.2)
    child = """import json,subprocess,sys
print(json.dumps({"type":"system","subtype":"init","session_id":"local","mcp_servers":[{"name":"exomem","status":"connected"}]}),flush=True)
for line in sys.stdin:
 print(json.dumps({"type":"result","subtype":"success","result":"Local protocol receipt."}),flush=True)
subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])
"""
    done = session_close.drive_session([sys.executable, "-c", child], tmp_path, os.environ.copy(), ("Local protocol check.",), tmp_path / "stream.jsonl")
    result = session_close.session_result(done, 1, "tool-only")
    assert done.returncode == 0 and done.finished == 1
    assert not result["stdout_eof"] and result["hook_starts"] is result["hook_invocations"] is None
    assert result["error"] is not None and "stdout" in result["error"]


def test_case_intervals_do_not_round_away_fractional_repeat_rates() -> None:
    assert session_score.paired_interval([(0.0, 0.5), (0.0, 0.0)]) == (0.0, 0.5)


def test_native_handoff_blinds_both_stages_and_imports_judgments_without_starting_a_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from import_guidance.__main__ import main

    def no_model(*_args, **_kwargs):
        pytest.fail("native preparation and import must not discover or start a model")

    monkeypatch.setattr(session_score, "discover_agent_envelope", no_model)
    monkeypatch.setattr(session_score, "run_judge", no_model)
    selected = session_close.load_cases("c01")[0]
    page_reply = json.dumps({"path": PERSON, "body": selected.turns[1]})
    row = {
        "scenario": selected.id, "repeat": 0, "model": "actor-sensitive", "error": None,
        "hook_invocations": 0, "hook_starts": 0, "server_connected": {checks.SERVER: "connected"},
        "observed": observed(pages={PERSON: {"compiled": False, "cites": []}}, recaps=False),
        "capture_after": {PERSON: {"text": selected.turns[1]}}, "recall_enabled": True,
        "fresh": {"error": None, "session_id": "fresh", "question": selected.recall_turn, "hook_invocations": 0, "hook_starts": 0,
                  "answer": selected.turns[1], "server_connected": {checks.SERVER: "connected"},
                  "calls": [{"name": f"mcp__{checks.SERVER}__read_memory", "is_error": False,
                             "result": page_reply}]},
    }
    runs = tmp_path / "runs.jsonl"
    inputs = []
    for mode in ("plugin", "tool-only"):
        for arm in ("arm-a-sensitive", "arm-b-sensitive"):
            variant = row | {"arm": arm}
            if mode == "tool-only":
                variant |= {"client_mode": mode, "fresh": row["fresh"] | {"client_mode": mode}}
            inputs.append(variant)
    runs.write_text("".join(json.dumps(item) + "\n" for item in inputs))
    # Other suites retain the reader's original four-field replacement semantics.
    assert len(session_score.read_rows([runs])[0]) == 2
    out = tmp_path / "judging"
    arguments = ["session-score", str(runs), "--baseline", "arm-a-sensitive", "--candidate", "arm-b-sensitive", "--out", str(out)]
    assert main(arguments) == 0
    blind = (out / "judge-views.jsonl").read_text()
    assert not any(marker in blind for marker in ("arm-a-sensitive", "arm-b-sensitive", "actor-sensitive", "tool-only", "disableAllHooks"))
    items = [json.loads(line) for line in blind.splitlines()]
    mapping = json.loads((out / "private/judge-map.json").read_text())
    assert len(items) == len({item["id"] for item in items}) == 8
    assert set(mapping) == {item["id"] for item in items}
    reference = session_score.refuse_unkeyed([selected])[selected.id]["reference"]
    assert all(reference in item["prompt"] for item in items)
    judgments = tmp_path / "judgments.jsonl"
    judgments.write_text("".join(json.dumps({"id": item["id"], "structured_output": {
        name: ((mapping[item["id"]]["run"][3] == "arm-b-sensitive") == (mapping[item["id"]]["run"][4] == "plugin")) if name == "J1" else True if field["type"] == "boolean" else "Boundary fixture."
        for name, field in item["answer_schema"]["properties"].items()
    }}) + "\n" for item in items))
    assert main(arguments + ["--judgments", str(judgments)]) == 0
    scored = [json.loads(line) for line in (out / "scores.jsonl").read_text().splitlines()]
    assert len(scored) == 4 and {row["client_mode"] for row in scored} == {"plugin", "tool-only"}
    assert all(row["recall"]["verdict"] == checks.PASS for row in scored)
    report = (out / "report.md").read_text()
    assert "plugin canonical correctness: 1 paired case(s), difference +1.00" in report
    assert "tool-only canonical correctness: 1 paired case(s), difference -1.00" in report


def test_missing_malformed_and_stale_native_judgments_stay_unknown_without_model_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_model(*_args, **_kwargs):
        pytest.fail("unavailable native judgments must not start another judge")

    monkeypatch.setattr(session_score, "discover_agent_envelope", no_model)
    monkeypatch.setattr(session_score, "run_judge", no_model)
    selected = session_close.load_cases("c01")[0]
    row = {"scenario": selected.id, "repeat": 0, "model": "test", "arm": "test", "error": None,
           "observed": observed(pages={PERSON: {"compiled": False, "cites": []}}, recaps=False)}
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps(row) + "\n")
    out = tmp_path / "judging"
    assert session_score.prepare_native([runs], out=out) == 1
    item = json.loads((out / "judge-views.jsonl").read_text())
    judgments = tmp_path / "judgments.jsonl"
    assert session_score.score_all([runs], out=out, judgments=judgments)[0]["verdict"] == checks.UNKNOWN
    judgments.write_text(json.dumps({"id": item["id"], "structured_output": {"J1": True, "J2": True, "J3": True}}) + "\n")
    assert session_score.score_all([runs], out=out, judgments=judgments)[0]["verdict"] == checks.UNKNOWN
    judgments.write_text(json.dumps({"id": item["id"], "structured_output": {
        name: True if field["type"] == "boolean" else "Boundary fixture."
        for name, field in item["answer_schema"]["properties"].items()
    }}) + "\n")
    mapping_path = out / "private/judge-map.json"
    mapping = json.loads(mapping_path.read_text())
    for mapped in mapping.values():
        mapped["run"].pop()
    mapping_path.write_text(json.dumps(mapping))
    assert session_score.score_all([runs], out=out, judgments=judgments)[0]["verdict"] == checks.PASS
    row["observed"]["changed"][PERSON] = "Different evidence."
    runs.write_text(json.dumps(row) + "\n")
    assert session_score.score_all([runs], out=out, judgments=judgments)[0]["verdict"] == checks.UNKNOWN


@pytest.mark.timeout(120)
def test_new_stdio_server_recalls_the_canonical_update_after_capture_closes(tmp_path: Path) -> None:
    """A recap-only receipt must not hide a canonical edit lost across a real client/server restart."""
    from collection_ingest.mcp_session import LedgerSession, decode, isolated_env
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    from import_guidance.recording import Arm

    root = Path(__file__).resolve().parents[1]
    arm = Arm("test", root)
    capture = session_close.load_cases("c02")[0]
    env = session_close.pinned_env(arm, tmp_path, capture)
    session_close.seed_vault(capture, arm, tmp_path, env)
    env = isolated_env(state=tmp_path, vault=tmp_path / "vault", server_root=root, extra=env)

    class Calls(LedgerSession):
        def __init__(self, client: Client) -> None:
            super().__init__()
            self.client = client

        async def invoke(self, tool: str, args: dict) -> tuple:
            return decode(await self.client.call_tool(tool, args, raise_on_error=False))

    def transport(name: str) -> StdioTransport:
        return StdioTransport(
            command=sys.executable,
            args=["-m", "exomem", "--transport", "stdio"],
            env=env,
            cwd=str(tmp_path),
            keep_alive=False,
            log_file=tmp_path / f"{name}.log",
        )

    async def exercise() -> None:
        async with Client(transport("capture"), timeout=30, init_timeout=30) as client:
            calls = Calls(client)
            outcome, recap, record = await calls.call(
                "record_episode",
                {"operation": {"kind": "record", "episode": "ep-" + "b2" * 16,
                               "subject": "Meal-prep routine update",
                               "summary": "Moved the grocery order to Friday evening.",
                               "decided": ["Order the groceries on Friday evening instead of Saturday afternoon."]}},
                journey="session-close", step="record-recap",
            )
            assert outcome == "ok", record
            assert recap["source"]["path"]
            operation = {"kind": "batch_replace", "edits": [
                {"old_string": "Saturday afternoon", "new_string": "Friday evening"},
                {"old_string": "## Relations", "new_string": "## Observations\n\n- [rule] Place the grocery order on Friday evening. ^order-day\n\n## Relations"},
            ]}
            arguments = {"path": ROUTINE, "why": "Record the change settled in this session.", "operation": operation}
            outcome, preview, record = await calls.call("edit_note", arguments | {"validate_only": True}, journey="session-close", step="preview-edit")
            assert outcome == "ok", (record, preview)
            semantic = preview.get("semantic") or {}
            if semantic.get("transition_token"):
                operation.update(
                    transition_token=semantic["transition_token"],
                    relation_disposition="reviewed-none",
                    relation_review_hash=semantic["transition_hash"],
                    relation_review_reason="The scheduling change adds no relation.",
                )
            outcome, _edited, record = await calls.call("edit_note", arguments, journey="session-close", step="commit-edit")
            assert outcome == "ok", (record, _edited)
        captured = session_close.snapshot(tmp_path / "vault")
        assert "Friday evening" in captured[ROUTINE]["text"]
        async with Client(transport("fresh"), timeout=30, init_timeout=30) as fresh:
            calls = Calls(fresh)
            outcome, found, record = await calls.call(
                "search_memory", {"query": "Sunday meal-prep routine", "mode": "keyword", "rerank": False},
                journey="session-close", step="find-canonical-page",
            )
            assert outcome == "ok", record
            hits = found
            hit = next(hit for hit in hits if hit["path"].endswith(ROUTINE))
            outcome, page, record = await calls.call("read_memory", {"operation": {"kind": "page", "path": hit.get("ref") or hit["path"]}}, journey="session-close", step="read-canonical-page")
            assert outcome == "ok", record
            assert page["path"].endswith(ROUTINE)
            assert "Friday evening" in page["body"]
            assert "Saturday afternoon" not in page["body"]
        assert session_close.snapshot(tmp_path / "vault")[ROUTINE]["text"] == captured[ROUTINE]["text"]

    asyncio.run(exercise())
    captured = session_close.snapshot(tmp_path / "vault")
    unavailable = session_close.fresh_recall(capture, arm, tmp_path, model="unused", executable=tmp_path / "absent-actor", env=env)
    assert unavailable["error"].startswith("FileNotFoundError:")
    assert unavailable["before"] == captured == unavailable["after"]
    assert unavailable["cost_usd"] is None
    assert session_score.recall_evidence({"fresh": unavailable}) == (checks.UNKNOWN, [])
