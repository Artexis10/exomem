"""The write-narration probe's own logic: reply choice by arguments, the leak check, the arm environment, the report."""

from __future__ import annotations

import json
from pathlib import Path

from import_guidance import checks, recording, report, runner
from import_guidance.scripted_replies import Scenario, pick_reply, tool_specs

VALIDATE = {"draft_id": "d"}
REFUSED = {"success": False}
COMMITTED = {"status": "committed"}
RULES = {
    "$rules": [
        {"when": {"validate_only": True}, "reply": VALIDATE},
        {"reply": [REFUSED, COMMITTED]},
    ]
}


def scenario(replies: dict) -> Scenario:
    return Scenario("s", "t", "p", ("create_note",), (), replies, {"default": True}, (), ())


def test_a_validate_call_between_two_commits_does_not_use_up_the_refusal() -> None:
    """Catches a plain reply sequence: an agent that validates twice would be handed the refusal for a
    validation and the success for its first commit."""
    s = scenario({"create_note": RULES})
    history: list[dict] = []
    seen = []
    for arguments in (
        {"validate_only": True},
        {"validate_only": True},
        {"validate_only": False},
        {"validate_only": True},
        {"validate_only": False},
    ):
        seen.append(pick_reply(s, "create_note", arguments, len(history), history))
        history.append(arguments)
    assert seen == [VALIDATE, VALIDATE, REFUSED, VALIDATE, COMMITTED]


SHAPES = {
    "committed": {
        "request_id": "8c1f0a52-3d7e-4b6a-9f10-2e5a7c4d9b31",
        "receipt_id": None,
        "graph_sync": "completed",
    },
    "refused": {"success": False, "error": {"code": "INVALID_RELATION_REVIEW"}},
}


def test_the_leak_check_finds_an_id_a_code_and_the_start_of_an_id_but_not_plain_words() -> None:
    spec = {"shapes": ["committed", "refused"], "literals": ["graph_sync"]}
    shapes = SHAPES

    assert checks.leaked_strings("I saved it.", shapes, spec) == []
    assert checks.leaked_strings("The id was 8C1F0A52-3D7E-4B6A-9F10-2E5A7C4D9B31.", shapes, spec)
    assert checks.leaked_strings("It began 8c1f0a52...", shapes, spec) == ["8c1f0a52"]
    assert checks.leaked_strings("Error INVALID_RELATION_REVIEW.", shapes, spec) == [
        "INVALID_RELATION_REVIEW"
    ]
    assert checks.leaked_strings("The graph_sync is done.", shapes, spec) == ["graph_sync"]


def test_a_leak_check_with_nothing_to_look_for_is_unknown_never_a_pass() -> None:
    """Catches a recording with no ids in it: every answer would pass the check."""
    evidence = checks.Evidence(
        (), (), {}, answer="anything", shapes={"committed": {"path": "a.md"}}
    )
    outcome = checks.answer_lacks_scripted_strings({"shapes": ["committed"]}, evidence)
    assert outcome.verdict == checks.UNKNOWN


def test_each_arm_is_shown_its_own_tool_text(tmp_path: Path) -> None:
    """Catches both arms reading one checkout's fixtures, which would make them identical by construction."""
    root = tmp_path / "arm"
    fixtures = root / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    for name in ("mcp_tool_schemas.json", "mcp_cloud_only_tool_schemas.json"):
        (fixtures / name).write_text(json.dumps({}), encoding="utf-8")
    (fixtures / "mcp_tool_schemas.json").write_text(
        json.dumps({"create_note": {"description": "arm text", "inputSchema": {}}}),
        encoding="utf-8",
    )
    assert tool_specs(root)["create_note"]["description"] == "arm text"
    assert tool_specs(root) != tool_specs()


def test_the_recording_worker_reaches_no_real_state(tmp_path: Path) -> None:
    """Catches a recording run that writes the operator's own state or vault through an inherited variable."""
    arm = recording.Arm("a", tmp_path / "checkout")
    env = recording.worker_env(arm, tmp_path / "scratch")
    assert env["PYTHONPATH"] == str(tmp_path / "checkout" / "src")
    assert not any(key.startswith("EXOMEM_") for key in env)
    for key in ("HOME", "TMPDIR", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        assert Path(env[key]).is_relative_to(tmp_path / "scratch")


def test_the_reference_gives_the_judge_the_request_id_the_arm_returned() -> None:
    filled = runner.fill_reference("The id is {committed.request_id}.", SHAPES)
    assert filled == "The id is 8c1f0a52-3d7e-4b6a-9f10-2e5a7c4d9b31."


def row(
    arm: str, model: str, r2: str, exact: str, error: str | None = None, repeat: int = 0
) -> dict:
    return {
        "arm": arm,
        "model": model,
        "scenario": "01",
        "repeat": repeat,
        "error": error,
        "verdict": "pass",
        "judge_model": "j",
        "criteria": [{"check": "R2", "verdict": r2, "detail": ""}],
        "checks": [{"check": "answer_lacks_scripted_strings", "verdict": exact, "detail": ""}],
    }


def test_a_leak_needs_a_failed_judgement_or_a_failed_exact_check() -> None:
    assert report.leak_of(row("a", "m", "fail", "pass")) is True
    assert report.leak_of(row("a", "m", "pass", "fail")) is True
    assert report.leak_of(row("a", "m", "pass", "pass")) is False
    assert report.leak_of(row("a", "m", "unknown", "pass")) is None
    assert report.leak_of(row("a", "m", "pass", "pass", error="timeout")) is None


def test_the_report_subtracts_the_baseline_from_the_candidate_and_skips_unknown_runs(
    tmp_path: Path,
) -> None:
    """Catches a sign error (a reduction read as a rise) and an unknown run counted as a clean one."""
    rows = [
        row("old", "m", "fail", "pass", repeat=0),
        row("old", "m", "fail", "pass", repeat=1),
        row("new", "m", "pass", "pass", repeat=0),
        row("new", "m", "pass", "pass", repeat=1),
        row("new", "m", "pass", "pass", error="timeout", repeat=2),
    ]
    runs = tmp_path / "runs.jsonl"
    runs.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    text = report.build([runs], baseline="old", candidate="new")

    assert "| 01 | m | 2/2 | 0/2 | -1.00 |" in text
    assert "**-1.000**" in text


def test_a_rerun_replaces_the_earlier_row_of_the_same_run_and_the_report_lists_it(
    tmp_path: Path,
) -> None:
    """Catches an errored run kept beside its rerun: it would count twice, or hide that it ever failed."""
    rows = [
        row("old", "m", "pass", "pass", error="timeout"),
        row("old", "m", "fail", "pass"),
        row("new", "m", "pass", "pass"),
    ]
    runs = tmp_path / "runs.jsonl"
    runs.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    text = report.build([runs], baseline="old", candidate="new")

    assert "| 01 | m | 1/1 | 0/1 | -1.00 |" in text
    assert "Rows replaced by a later run" in text
