"""The f38 epistemic suite plays a project in lockstep and reports what memory changed.

Failures a green run would not reveal: an arm that drifts ahead of another, memory that a probe
wrote into, a workspace that carries files from one session into the next, a case set that holds
its own answers, a harness fault that leaves a half-played project in the figures, and an interval
that ignores which project a probe belongs to. A scripted fake ``claude`` plays the actor and a
fake judge grades its answers, so the run costs no money.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from epistemic.journeys.f27_replay import AgentEnvelope
from membench.outcome import command
from membench.outcome.epistemic import cases, layout, report
from membench.outcome.epistemic import run as epistemic_run
from membench.outcome.faults import RETRIES
from membench.outcome.stats import paired_interval, stratified_bootstrap_ci

# The shipped arms read their instruction block out of docs/prominence.md.
pytestmark = pytest.mark.reads_repository_docs

ARMS = ["N", "F", "P", "E"]
#: The session each scripted call belongs to.
SESSION_OF = {
    "task s1": 1,
    "task s2": 2,
    "task s3": 3,
    "probe p-early": 2,
    "probe p-first": 3,
    "probe p-second": 3,
}

#: A stand-in for ``claude -p``. It reads one stream-json user turn, takes the tag in the prompt
#: (``[task s1]`` or ``[probe p-early]``) for the call, and behaves as a small note-taker: an arm
#: that has memory and an instruction keeps the first KEEP "FACT: word" lines of each session.
#: Every call writes a stray file into its workspace. A probe also writes a note into the arm's
#: memory. The reply shows what the call could see, so a test reads the isolation from the answers.
FAKE_CLAUDE = r"""#!__PYTHON__
import json, re, sys, time
from pathlib import Path

LEDGER = Path("__LEDGER__")
FAULT = LEDGER.with_name("fault.json")
KEEP = {"F": 1, "P": 99}
argv = sys.argv[1:]


def option(name):
    return argv[argv.index(name) + 1] if name in argv else None


model = option("--model")
config = option("--mcp-config")
servers = json.loads(Path(config).read_text())["mcpServers"] if config else {}
cwd = Path.cwd()
arm = cwd.parent.name
prompt = json.loads(sys.stdin.readline())["message"]["content"]
tags = re.findall(r"\[(task|probe) ([\w-]+)\]", prompt)
kind, tag = tags[-1]
call = f"{kind} {tag}"


def log(event):
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"t": time.time(), "arm": arm, "call": call, "event": event}) + "\n")


log("start")
if FAULT.exists() and json.loads(FAULT.read_text()) == [arm, call]:
    sys.exit(1)
mcp = [{"name": "exomem", "status": "connected"}] if "exomem" in servers else []
init = {"type": "system", "subtype": "init", "model": model, "apiKeySource": "none", "mcp_servers": mcp}
print(json.dumps(init), flush=True)

if "exomem" in servers:
    memory = Path(servers["exomem"]["env"]["EXOMEM_VAULT_PATH"]) / "fake-notes.txt"
elif (cwd / "memory").is_dir():
    memory = cwd / "memory" / "notes.txt"
else:
    memory = None
held = memory.read_text().split() if memory and memory.exists() else []
files = sorted(p.name for p in cwd.iterdir() if p.name not in ("memory", "CLAUDE.md", ".claude"))
time.sleep(0.5)
if len(tags) > 1:
    # A prompt that carries the whole history: the answer is read from the prompt itself.
    held = re.findall(r"FACT: (\w+)", prompt)
elif kind == "task":
    facts = re.findall(r"FACT: (\w+)", " ".join(p.read_text() for p in cwd.glob("*.txt")))
    keep = KEEP.get(arm, 0) if (cwd / "CLAUDE.md").exists() else 0
    if memory and keep:
        with memory.open("a") as fh:
            fh.write(" ".join(facts[:keep]) + " ")
elif memory:
    with memory.open("a") as fh:
        fh.write("PROBE-NOTE ")
(cwd / f"{kind}-{tag}.scratch").write_text("stray")
log("end")
result = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": f"{call} files={files} memory={held} seen={[f'{k} {t}' for k, t in tags]}",
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "total_cost_usd": 0.01,
}
print(json.dumps(result), flush=True)
for _ in sys.stdin:
    pass
"""

#: A stand-in for the judge CLI: correct when the key's value occurs in the answer.
FAKE_JUDGE = r"""#!__PYTHON__
import json, sys
from pathlib import Path

prompt = sys.stdin.read()
with Path("__PROMPTS__").open("a") as fh:
    fh.write(json.dumps(prompt) + "\n")
key = json.loads(prompt.split("KEY:\n", 1)[1].split("\n\nANSWER:\n", 1)[0])
answer = prompt.split("\n\nANSWER:\n", 1)[1].split("\n\nFILES CHANGED BY", 1)[0]
correct = key["value"] in answer
verdict = {
    "correct": correct,
    "uses_current": "yes" if correct else "no",
    "flags_conflict": "n/a",
    "cites_source": "n/a",
    "keeps_attribution": "n/a",
    "abstains_when_unknown": "n/a",
    "trusts_stale": "n/a",
    "reason": "key value in answer" if correct else "key value missing",
}
print(json.dumps({"structured_output": verdict, "total_cost_usd": 0.0, "modelUsage": {"claude-opus-5-5": {}}}))
"""


def script(path: Path, text: str, **fill: Path) -> Path:
    text = text.replace("__PYTHON__", sys.executable)
    for name, value in fill.items():
        text = text.replace(f"__{name.upper()}__", str(value))
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def probe(probe_id, session, task, value, *, dimension, change_id=None, files=None):
    return {
        "probe_id": probe_id,
        "session": session,
        "task": task,
        "files": files or {},
        "dimension": dimension,
        "change_id": change_id,
        "key": {
            "value": value,
            "accept": [],
            "rubric": [f"names {value}"],
            "behaviours": ["uses_current"],
            "trap": None,
        },
    }


def invented_project() -> dict:
    """Three sessions of tidying notes. Session 2 holds two facts; a note-taker that keeps one loses the second."""
    return {
        "sessions": [
            {
                "session": 1,
                "task": "[task s1] Tidy the notes.",
                "files": {"notes.txt": "FACT: alpha"},
            },
            {
                "session": 2,
                "task": "[task s2] Tidy the notes.",
                "files": {"notes.txt": "FACT: beta\nFACT: gamma"},
            },
            {
                "session": 3,
                "task": "[task s3] Tidy the notes.",
                "files": {"notes.txt": "Nothing new."},
            },
        ],
        "probes": [
            probe(
                "p-early", 2, "[probe p-early] Which fact came first?", "alpha", dimension="stable"
            ),
            probe(
                "p-first",
                3,
                "[probe p-first] Which fact did session two open with?",
                "beta",
                dimension="supersession",
                change_id="ch1",
                files={"question.txt": "Answer in one line."},
            ),
            probe(
                "p-second",
                3,
                "[probe p-second] Which fact came last in session two?",
                "gamma",
                dimension="conflict",
                change_id="ch2",
            ),
        ],
    }


def write_case_set(root: Path, projects: dict[str, dict], *, heldout: tuple[str, ...] = ()) -> None:
    folder = root / "cases" / "epistemic"
    folder.mkdir(parents=True)
    digests = {}
    for name, project in projects.items():
        text = json.dumps(project, indent=2)
        (folder / f"{name}.json").write_text(text)
        digests[name] = hashlib.sha256(text.encode()).hexdigest()
    split = {
        "dev": [n for n in projects if n not in heldout],
        "heldout": list(heldout),
        "case_sha256": digests,
    }
    (folder / "SPLIT.json").write_text(json.dumps(split))


def ledger_rows(ledger: Path) -> list[dict]:
    return [json.loads(line) for line in ledger.read_text().splitlines()]


def call_record(run_dir: Path, arm: str, kind: str, name: str, window: int = 1) -> dict:
    return json.loads(
        (run_dir / f"window-{window}" / "proj-a" / arm / kind / f"{name}.json").read_text()
    )


def drain_pumps(before: set[threading.Thread]) -> None:
    """Wait for the output threads of the fake CLI sessions, so that none outlives its test."""
    for thread in set(threading.enumerate()) - before:
        if thread.name.endswith("(_pump)"):
            thread.join(timeout=30)


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A run root and state root of the test's own: nothing reaches the live state root or the real run root."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(command, "RUN_ROOT", root)
    monkeypatch.setenv("EXOMEM_OUTCOME_ROOT", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    before = set(threading.enumerate())
    yield SimpleNamespace(root=root, tmp=tmp_path)
    drain_pumps(before)


def use_fake_cli(monkeypatch, cli: Path) -> None:
    envelope = AgentEnvelope(executable=cli, version="0.0-test")
    monkeypatch.setattr(epistemic_run, "discover_agent_envelope", lambda: envelope)


def play(run_id: str, **options) -> Path:
    return command.run_suite("epistemic", run_id, model="m", effort="high", **options)


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    """One dev window of the invented project by all four arms, then the solvable gate by arm N."""
    base = tmp_path_factory.mktemp("f38")
    root, ledger = base / "root", base / "ledger.jsonl"
    root.mkdir()
    write_case_set(root, {"proj-a": invented_project()})
    cli = script(base / "claude", FAKE_CLAUDE, ledger=ledger)
    judge = script(base / "judge", FAKE_JUDGE, prompts=base / "judge-prompts.jsonl")
    before = set(threading.enumerate())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(command, "RUN_ROOT", root)
        mp.setenv("EXOMEM_OUTCOME_ROOT", str(root))
        mp.setenv("XDG_STATE_HOME", str(base / "xdg-state"))
        use_fake_cli(mp, cli)
        run_dir = play("r1", arms=ARMS)
        main_rows = ledger_rows(ledger)
        play("r1", arms=["N"], history="full")
        yield SimpleNamespace(
            root=root,
            run_dir=run_dir,
            rows=main_rows,
            judge=judge,
            prompts=base / "judge-prompts.jsonl",
        )
    drain_pumps(before)


# --------------------------------------------------------------------------
# A project in lockstep.
# --------------------------------------------------------------------------


def test_arms_start_a_session_together_and_the_next_only_after_every_arm_finished(played):
    """An arm that raced ahead would meet the provider at another hour than the arm it is compared with."""
    rows = [r for r in played.rows if r["arm"] in ARMS]
    for arm in ARMS:
        order = [r["call"] for r in rows if r["arm"] == arm and r["event"] == "start"]
        assert order == [
            "task s1",
            "task s2",
            "probe p-early",
            "task s3",
            "probe p-first",
            "probe p-second",
        ]

    def window(session, event, pick):
        return pick(
            r["t"] for r in rows if SESSION_OF[r["call"]] == session and r["event"] == event
        )

    for session in (1, 2, 3):
        mains = [r for r in rows if r["call"] == f"task s{session}"]
        starts = [r["t"] for r in mains if r["event"] == "start"]
        ends = [r["t"] for r in mains if r["event"] == "end"]
        assert max(starts) < min(ends), f"the arms did not play session {session} at the same time"
    for session in (1, 2):
        assert window(session, "end", max) <= window(session + 1, "start", min), (
            f"an arm started session {session + 1} before every arm finished session {session}"
        )


def test_the_workspace_resets_each_session_but_the_folder_arm_keeps_its_memory(played):
    """Files an arm left behind must not carry into the next session; only its memory may."""
    run_dir = played.run_dir
    for arm in ARMS:
        third = call_record(run_dir, arm, "sessions", "s03")["reply"]
        assert "files=['notes.txt']" in third, f"{arm} saw files of an earlier session: {third}"
    folder = call_record(run_dir, "F", "sessions", "s03")["reply"]
    # F's note-taker kept alpha and beta; the memory folder survived two workspace resets.
    assert "memory=['alpha', 'beta']" in folder
    assert "memory=[]" in call_record(run_dir, "N", "sessions", "s03")["reply"]
    assert (
        "memory=['alpha', 'beta', 'gamma']" in call_record(run_dir, "P", "sessions", "s03")["reply"]
    )
    assert "memory=[]" in call_record(run_dir, "E", "sessions", "s03")["reply"], (
        "E has no instruction, so it keeps nothing"
    )


def test_what_a_probe_writes_is_discarded_before_the_next_call(played):
    """A probe that wrote to memory would hand its own answer to the probe after it."""
    run_dir = played.run_dir
    for arm in ARMS:
        replies = [
            call_record(run_dir, arm, kind, name)["reply"]
            for kind, name in (
                ("sessions", "s03"),
                ("probes", "p-first"),
                ("probes", "p-second"),
            )
        ]
        assert not any("PROBE-NOTE" in reply for reply in replies), (
            f"{arm} read a probe's memory write"
        )
        assert "probe-p-first.scratch" not in replies[2], f"{arm} kept a probe's workspace file"
    notes = played.run_dir / "window-1" / "proj-a" / "F" / "work" / "memory" / "notes.txt"
    assert "PROBE-NOTE" not in notes.read_text()
    # What the probe changed is kept for the judge, though the next call never sees it.
    assert call_record(run_dir, "F", "probes", "p-first")["changed"]["files"] == [
        "probe-p-first.scratch"
    ]


# --------------------------------------------------------------------------
# The report.
# --------------------------------------------------------------------------


def report_args(run_id: str, judge: Path) -> SimpleNamespace:
    return SimpleNamespace(
        suite="epistemic",
        run_id=run_id,
        rejudge_sample=None,
        judge="claude",
        judge_model=None,
        judge_effort=None,
        judge_exe=str(judge),
        jobs=1,
    )


def test_the_report_pairs_the_arms_on_change_probes_and_judges_blind(played, capsys):
    """The pairs are what the run is for, and a judge that can see the arm would grade the arm."""
    command._cmd_report(report_args("r1", played.judge))
    printed = capsys.readouterr().out
    built = json.loads((played.run_dir / "report-claude.json").read_text())

    assert built["status"] == "complete"
    assert built["windows"]["unfinished"] == [] and built["windows"]["invalid"] == []
    # Change probes p-first and p-second: P knows both, F lost the second, N and E know neither.
    pf, pn, ep = (built["pairs"][k] for k in ("P-F", "P-N", "E-P"))
    assert (pf["mean_difference"], pf["ci95"]) == (0.5, [0.0, 1.0])
    assert (pn["mean_difference"], pn["ci95"]) == (1.0, [1.0, 1.0])
    assert ep["mean_difference"] == -1.0
    assert built["decision"]["status"] == "description only", "a dev run decides nothing"
    stable = built["description"]["stable_probes"]
    assert (stable["P"]["mean_score"], stable["F"]["mean_score"], stable["N"]["mean_score"]) == (
        1.0,
        1.0,
        0.0,
    )
    assert built["description"]["harm"]["harmed"] == 0
    assert built["parser_cross_check"].startswith("none ran")
    # N with the whole history solves every probe, and N without it solves none.
    gates = built["dev_gates"]
    assert (gates["solvable"]["rate"], gates["solvable"]["meets"]) == (1.0, True)
    assert (gates["memory_needed"]["rate"], gates["memory_needed"]["meets"]) == (1.0, True)
    assert "status: complete" in printed and "decision: description only" in printed

    # The solvable gate's prompt carries sessions 1 to the probe's, then the probe.
    for probe_id, seen in (
        ("p-early", "['task s1', 'task s2', 'probe p-early']"),
        ("p-first", "['task s1', 'task s2', 'task s3', 'probe p-first']"),
    ):
        path = played.run_dir / "window-1" / "history" / "proj-a" / probe_id / "probe.json"
        assert f"seen={seen}" in json.loads(path.read_text())["reply"]

    prompts = [json.loads(line) for line in played.prompts.read_text().splitlines()]
    assert len(prompts) >= 15, "every arm's and the history's answers were judged"
    for text in prompts:
        for leak in (str(played.root), "window-", "unblinding", "history"):
            assert leak not in text, f"a judge input contains {leak!r}"


# --------------------------------------------------------------------------
# Faults.
# --------------------------------------------------------------------------


def test_a_harness_fault_that_survives_its_retries_invalidates_the_window_for_every_arm(
    scratch, monkeypatch
):
    """One arm's broken session must not leave the other arms' half-played project in the figures."""
    write_case_set(scratch.root, {"proj-a": invented_project()})
    ledger = scratch.tmp / "ledger.jsonl"
    use_fake_cli(monkeypatch, script(scratch.tmp / "claude", FAKE_CLAUDE, ledger=ledger))
    (scratch.tmp / "fault.json").write_text(json.dumps(["F", "task s2"]))

    run_dir = play("r2", arms=["N", "F"])

    state = json.loads((run_dir / "window-1" / "proj-a" / "state.json").read_text())
    assert state["status"] == "invalid" and state["arms"] == ["N", "F"]
    assert [(e["arm"], e["session"]) for e in state["errors"]] == [("F", 2)]
    rows = ledger_rows(ledger)
    starts = [
        r for r in rows if r["arm"] == "F" and r["call"] == "task s2" and r["event"] == "start"
    ]
    assert len(starts) == 1 + RETRIES, "the fault is retried, then it stands"
    assert "task s3" not in {r["call"] for r in rows}, "no arm plays on after the window is invalid"
    assert layout.counted_windows(layout.project_states(run_dir)) == {}, (
        "an invalid window scores nothing"
    )

    # The same project replays alone in a new window, and only that window counts.
    (scratch.tmp / "fault.json").unlink()
    play("r2", arms=["N", "F"], window=2, project="proj-a")
    counted = layout.counted_windows(layout.project_states(run_dir))
    assert [w["window"] for w in counted["proj-a"]] == [2]


# --------------------------------------------------------------------------
# The case set.
# --------------------------------------------------------------------------


def test_a_case_set_that_holds_its_own_answer_or_changed_since_its_digest_refuses_every_run(
    scratch,
):
    """A probe that carries its own key would measure reading, not memory; a changed file is not the one that was frozen."""
    leaking = invented_project()
    leaking["probes"][0]["task"] = "[probe p-early] Was it alpha, in the end?"
    write_case_set(scratch.root, {"proj-a": leaking})

    with pytest.raises(
        SystemExit,
        match=r"holds its own answers[\s\S]*probe p-early: key.value appears in its task",
    ):
        play("leaked", arms=["N"])
    assert not (scratch.root / "runs").exists(), "no run is recorded before the case check passes"

    sound = scratch.root / "cases" / "epistemic" / "proj-a.json"
    sound.write_text(json.dumps(invented_project()))
    with pytest.raises(SystemExit, match="differs from SPLIT.json"):
        play("changed", arms=["N"])


def test_a_short_key_string_warns_as_a_whole_token_and_never_refuses():
    """A number such as 7 is in ordinary text, so only the token 7 warns, and the run record names the probe, not the string."""
    project = invented_project()
    project["probes"][0]["key"]["value"] = "7"
    project["sessions"][1]["files"]["notes.txt"] = (
        "FACT: beta\nFACT: gamma\nroom 17 and room 7 are free"
    )

    refusals, warnings, checked = cases.check_probes(project)

    assert refusals == []
    assert [(w["probe_id"], w["field"], w["where"]) for w in warnings] == [
        ("p-early", "key.value", "session 2 file notes.txt")
    ]
    assert checked == 3
    assert set(warnings[0]) == {"probe_id", "field", "where", "length"}, (
        "the warning never carries the string"
    )


def test_a_probe_whose_answer_is_not_one_value_loads_with_a_null_value():
    """Attribution, argument and open-question probes have no single value; their rubric carries the answer."""
    project = invented_project()
    project["probes"][0]["key"].update(
        value=None, accept=["unverified per vendor"], trap="settled fact"
    )

    assert cases.structure_problems(project) == []
    refusals, _, checked = cases.check_probes(project)
    assert refusals == []
    assert checked == 4, (
        "the null value is skipped; its accepted phrasing, its trap and the other probes' values are checked"
    )


def test_a_held_out_epistemic_run_refuses_while_f38_is_not_registered(scratch):
    """No gate can pass an unregistered family, so a decision run must not start."""
    write_case_set(scratch.root, {"proj-a": invented_project()}, heldout=("proj-a",))
    with pytest.raises(SystemExit, match="not registered in the preregistration"):
        command.run_suite("epistemic", "heldout", split="heldout", arms=["N", "F", "P"])
    assert not (scratch.root / "runs").exists()


def test_the_folder_arms_instruction_is_the_product_text_and_a_changed_product_text_fails_loudly(
    monkeypatch,
):
    """F must read the learning instruction users get, pointed at its folder, or the pair measures two instructions."""
    text = epistemic_run.folder_instruction()
    assert "Exomem" not in text and "memory/" in text

    monkeypatch.setattr(epistemic_run, "LEARNING_INSTRUCTION", "Keep notes somewhere.")
    with pytest.raises(RuntimeError, match="no longer holds"):
        epistemic_run.folder_instruction()


# --------------------------------------------------------------------------
# The statistics, on values worked out by hand.
# --------------------------------------------------------------------------


def test_the_bootstrap_resamples_inside_each_project_and_every_project_keeps_its_weight():
    """Pooling probes would let a draw lose a project's share: project A has [1, 0] and B has [1, 1]."""
    low, high = stratified_bootstrap_ci([1, 0, 1, 1], ["A", "A", "B", "B"], seed=7)
    # B always sums to 2; A sums to 0, 1 or 2 with chances 1/4, 1/2, 1/4: the mean is 0.5, 0.75 or 1.
    assert (low, high) == (0.5, 1.0)
    # A project that is all 1s and one that is all 0s cannot move: the interval is a point.
    assert stratified_bootstrap_ci([1, 1, 0, 0], ["A", "A", "B", "B"], seed=7) == (0.5, 0.5)

    strata = paired_interval([1, 0, 1, 1], seed=7, unit="probe", strata=["A", "A", "B", "B"])
    assert strata["ci95"] == [0.5, 1.0] and strata["mean_difference"] == 0.75


def outcome(arm, project, probe_id, window, correct):
    return {
        "arm": arm,
        "project": project,
        "probe_id": probe_id,
        "window": window,
        "verdict": None if correct is None else {"correct": correct},
    }


def test_a_probe_scores_the_mean_of_its_windows_and_an_unjudged_window_is_unknown_not_a_loss():
    """Two windows average to 0, 0.5 or 1; a window the judge failed on drops out of the mean instead of reading as 0."""
    meta = {
        (project, probe_id): {"dimension": "supersession", "probe_id": probe_id}
        for project, probe_id in (("a", "a1"), ("a", "a2"), ("b", "b1"), ("b", "b2"))
    }
    answers = {
        # P is right in both windows on a1, once on a2, never on b1, and always on b2. N is right on b2 only.
        "P": {"a1": (1, 1), "a2": (1, 0), "b1": (0, 0), "b2": (1, 1)},
        "N": {"a1": (0, 0), "a2": (0, None), "b1": (0, 0), "b2": (1, 1)},
    }
    outcomes = [
        outcome(arm, probe_id[0], probe_id, window, correct)
        for arm, probes in answers.items()
        for probe_id, per_window in probes.items()
        for window, correct in enumerate(per_window, start=1)
    ]
    scores = report.score_tables(outcomes, ["P", "N"])
    assert scores["P"][("a", "a2")] == {1: 1.0, 2: 0.0}
    assert scores["N"][("a", "a2")] == {1: 0.0}, "the unjudged window is absent, not 0"

    row = report.pair_row("P", "N", scores, meta)

    # a1: 1 - 0, a2: 0.5 - 0, b1: 0 - 0, b2: 1 - 1. Project A resamples [1, 0.5], project B [0, 0].
    assert row["paired_probes"] == 4 and row["mean_difference"] == 0.375
    assert row["ci95"] == [0.25, 0.5]


def test_the_decision_waits_for_two_valid_windows_a_trusted_judge_and_six_hours():
    """A decision is read only from a complete held-out run; anything less describes, and says why."""
    first = {"window": 1, "started_at": "2026-11-08T08:00:00+00:00"}
    later = {"window": 2, "started_at": "2026-11-08T15:00:00+00:00"}
    soon = {"window": 2, "started_at": "2026-11-08T11:00:00+00:00"}
    lows = {
        "P-F": {"ci95": [0.1, 0.4], "verdict": "better"},
        "P-N": {"ci95": [0.2, 0.5], "verdict": "better"},
    }
    agreed = {"rate": 0.95}

    def read(counted, again=agreed, failed=(), heldout=True):
        why = report.incomplete_reasons(
            list(failed), again, heldout=heldout, counted=counted, expected=["proj-a", "proj-b"]
        )
        return report.decide(why, heldout=heldout, counted=counted, rows=lows)[0]

    both = {"proj-a": [first, later], "proj-b": [first, later]}
    decided = read(both)
    assert decided["status"] == "complete"
    assert [decided[k]["result"] for k in ("beats_folder", "helps", "no_worse_than_folder")] == [
        True
    ] * 3

    one_window = read({"proj-a": [first, later], "proj-b": [first]})
    assert (
        one_window["status"] == "incomplete" and "proj-b has 1 valid window" in one_window["why"][0]
    )

    unsure = read(both, again={"rate": 0.85})
    assert unsure["status"] == "incomplete" and "judge agreement 0.85" in unsure["why"]

    close = read({"proj-a": [first, soon], "proj-b": [first, soon]})
    assert close["status"] == "description only" and "3.0 hours apart" in close["why"][0]
    assert read(both, heldout=False)["status"] == "description only"
