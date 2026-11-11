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
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from epistemic.journeys.f27_replay import AgentEnvelope
from membench.outcome import command, record
from membench.outcome.epistemic import cases, layout, report
from membench.outcome.epistemic import judge as epistemic_judge
from membench.outcome.epistemic import run as epistemic_run
from membench.outcome.faults import RETRIES
from membench.outcome.stats import paired_interval, stratified_bootstrap_ci


@pytest.fixture(autouse=True)
def _subscription_run(monkeypatch):
    """A shell that carries an API key or a billing declaration must not decide what these tests run on."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXOMEM_OUTCOME_BILLING", raising=False)


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
content = json.loads(sys.stdin.readline())["message"]["content"]
# The actor sends one text block per user turn, the form the credits launcher also accepts.
prompt = content if isinstance(content, str) else "".join(block["text"] for block in content)
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
    # The run record reads the CLI version too; CI runners have no `claude` on PATH.
    monkeypatch.setattr(record, "discover_agent_envelope", lambda: envelope)


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
    pf, pn, pe = (built["pairs"][k] for k in ("P-F", "P-N", "P-E"))
    assert (pf["mean_difference"], pf["ci95"]) == (0.5, [0.0, 1.0])
    assert (pn["mean_difference"], pn["ci95"]) == (1.0, [1.0, 1.0])
    assert pe["mean_difference"] == 1.0, "P minus E reads positive when the plugin adds knowledge"
    assert built["decision"]["status"] == "description only", "a dev run decides nothing"
    stable = built["description"]["stable_probes"]
    assert (stable["P"]["mean_score"], stable["F"]["mean_score"], stable["N"]["mean_score"]) == (
        1.0,
        1.0,
        0.0,
    )
    assert built["description"]["harm"]["harmed"] == 0
    assert built["parser_cross_check"].startswith("none ran: the harness has no parser cross-check")
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


def test_the_judge_reads_store_names_masked_while_the_report_counts_them_raw(tmp_path, monkeypatch):
    """Every folder and Exomem answer in a smoke run named its store, which would show the judge the arm."""
    entries = [
        {
            "state": "answered",
            "arm": "F",
            "item": None,
            "task": "t",
            "key": {"value": "8443"},
            "behaviours": ["uses_current"],
            "answer": "Port 8443, from my note in memory/web.md.",
            "changed": "memory/web.md\n+port 8443",
        },
        {
            "state": "answered",
            "arm": "P",
            "item": None,
            "task": "t",
            "key": {"value": "8443"},
            "behaviours": ["uses_current"],
            "answer": "Port 8443, as Exomem recalled through mcp__exomem__recall_context.",
            "changed": "",
        },
    ]
    monkeypatch.setattr(epistemic_judge, "collect", lambda *_: [dict(e) for e in entries])
    judge_run = tmp_path / "judged"
    epistemic_judge.items(tmp_path, {}, judge_run, ["F", "P"])

    shown = (judge_run / "judge" / "items.jsonl").read_text()
    assert "memory/" not in shown and "exomem" not in shown.casefold()
    assert "[store]/" not in shown and "__recall" not in shown, (
        "the whole token goes, path and tool shape too"
    )
    assert shown.count("[store]") == 4

    rows = [json.loads(line) for line in shown.splitlines()]
    (judge_run / "judge" / "verdicts.jsonl").write_text(
        "".join(json.dumps({"id": r["id"], "verdict": {"correct": True}}) + "\n" for r in rows)
    )
    outcomes, _, _ = epistemic_judge.scored(judge_run)
    mentions = report.store_mentions(outcomes, ["F", "P"])
    assert mentions["F"]["naming_a_store"] == 1 and mentions["P"]["naming_a_store"] == 1


def test_case_text_that_holds_a_store_name_is_refused_because_the_judge_would_read_it_masked():
    """An ops key such as 'raise the memory/CPU limit' would reach the judge as an arm's note."""
    project = invented_project()
    project["probes"][0]["key"]["rubric"] = ["raises the memory/CPU limit to 2"]

    problems = cases.structure_problems(project)

    assert problems == [
        "probe p-early key.rubric[0] holds a store name the judge masks (memory/, exomem); reword it"
    ]


def test_the_mask_reads_a_long_unbroken_token_in_linear_time():
    """A minified line can fill the diff up to its cap; a backtracking mask took seconds per item."""
    diff = "a" * epistemic_run.MAX_DIFF_CHARS

    started = time.monotonic()
    shown = epistemic_judge.hide_store_names(diff)

    assert shown == diff
    assert time.monotonic() - started < 1.0


def test_a_files_value_that_is_not_an_object_is_named_not_a_crash():
    """An authoring lane that writes `files` as a list must get the shape problem back, not a traceback."""
    project = invented_project()
    project["probes"][0]["files"] = ["notes/a.md"]

    problems = cases.structure_problems(project)

    assert problems == ["probe p-early needs a `task` string and a `files` object"]


def test_a_key_list_field_that_is_not_a_list_is_named_not_a_crash():
    """A key with `accept: 5` must get the shape problem back, not a traceback from the store-name scan."""
    project = invented_project()
    project["probes"][0]["key"]["accept"] = 5

    problems = cases.structure_problems(project)

    assert problems == ["probe p-early: key.accept, key.rubric and key.behaviours must be lists"]


def test_a_solvable_gate_answer_whose_probe_the_case_file_dropped_is_skipped_with_a_reason(
    tmp_path,
):
    """The gate played the probe; a repaired case file no longer holds it, and the report must not crash."""
    run_dir = tmp_path / "run"
    gone = run_dir / "window-1" / "history" / "proj-a" / "p-gone"
    gone.mkdir(parents=True)
    (gone / "probe.json").write_text(json.dumps({"probe_id": "p-gone"}))

    entries = epistemic_judge.collect(run_dir, {"proj-a": (invented_project(), {})}, ["N"])

    assert [(e["probe_id"], e["state"]) for e in entries] == [("p-gone", "probe_dropped")]
    assert "no longer holds this probe" in entries[0]["reason"]


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


def test_an_actor_protects_the_paths_that_start_after_it_and_its_own_answer_records(scratch):
    """The protected list is read per call: a project that starts beside a running one must not be readable."""
    arm = scratch.root / "runs" / "r1" / "window-1" / "proj-a" / "N"
    arm.mkdir(parents=True)
    envelope = AgentEnvelope(executable=Path("claude"), version="0.0-test")
    actor = epistemic_run.EpistemicActor(
        epistemic_run.ARMS["N"], arm, "m", "high", root=scratch.root, envelope=envelope
    )
    (scratch.root / "runs" / "r1" / "window-1" / "proj-b").mkdir()

    protected = actor.protected()

    assert scratch.root / "runs" / "r1" / "window-1" / "proj-b" in protected
    assert arm / "sessions" in protected and arm / "probes" in protected
    assert arm / "work" not in protected


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


def test_a_key_with_an_unknown_behaviour_or_a_non_string_accept_item_refuses_with_a_named_problem():
    """A misspelt behaviour would never be rated, and a number in `accept` would crash the leak check."""
    project = invented_project()
    project["probes"][0]["key"]["behaviours"] = ["uses_current", "remembers_fondly"]
    project["probes"][1]["key"]["accept"] = ["beta", 7]
    project["probes"][2]["key"]["trap"] = 5

    problems = cases.structure_problems(project)

    assert any("p-early: key.behaviours names ['remembers_fondly']" in p for p in problems)
    assert "probe p-first: every key.accept item must be a string" in problems
    assert "probe p-second: key.trap must be a string or null" in problems

    listing_trusts_stale = invented_project()["probes"][1]
    listing_trusts_stale["key"]["behaviours"] = ["trusts_stale"]
    assert epistemic_judge.behaviours_asked(listing_trusts_stale) == ("trusts_stale",), (
        "a change probe asks trusts_stale once even when its key lists it"
    )


def test_a_key_naming_a_registered_behaviour_beyond_the_first_six_passes_the_case_check():
    """`keeps_scope` is in the registry and was never in the old code constant."""
    project = invented_project()
    project["probes"][1]["key"]["behaviours"] = ["uses_current", "keeps_scope"]

    assert cases.structure_problems(project) == []


def test_the_verdict_schema_of_a_probe_holds_its_keys_behaviours_and_trusts_stale_unless_it_is_stable():
    """A change probe's judge rates what its key names and trusts_stale; a stable probe's judge rates what its key names."""
    change = invented_project()["probes"][1]
    change["key"]["behaviours"] = ["keeps_scope", "resolves_identity"]
    stable = invented_project()["probes"][0]
    stable["key"]["behaviours"] = ["keeps_order"]

    change_schema = epistemic_judge.verdict_schema(epistemic_judge.behaviours_asked(change))
    stable_schema = epistemic_judge.verdict_schema(epistemic_judge.behaviours_asked(stable))

    assert set(change_schema["properties"]) == {
        "correct",
        "keeps_scope",
        "resolves_identity",
        "trusts_stale",
        "reason",
    }
    assert set(change_schema["required"]) == set(change_schema["properties"])
    assert set(stable_schema["properties"]) == {"correct", "keeps_order", "reason"}
    assert stable_schema["additionalProperties"] is False


def test_the_judge_gives_each_item_the_schema_of_its_own_probe_and_asks_again_when_an_asked_behaviour_is_missing(
    tmp_path, monkeypatch
):
    """One pooled run holds probes that ask different behaviours: a fixed schema would make the judge rate the wrong ones."""
    pooled = {
        "task": "t",
        "key": {"value": "v", "accept": [], "rubric": [], "traps": [], "premise": None},
        "answer": "a",
        "changed": "(no file changed)",
    }
    items = [
        {"id": "it-0000", **pooled, "behaviours": ["keeps_scope", "trusts_stale"]},
        {"id": "it-0001", **pooled, "behaviours": ["keeps_order"]},
    ]
    (tmp_path / "judge").mkdir()
    (tmp_path / "judge" / "items.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
    asked = []

    class Judge:
        """Answers each item's first call without `keeps_scope`, as a judge that ignored its schema would."""

        def call(self, prompt, schema, *, out, timeout):
            asked.append((out.name, sorted(schema["required"])))
            rated = {"correct": True, "reason": "r", "trusts_stale": "no", "keeps_order": "yes"}
            if out.name.endswith(".0.codex-out.json"):
                return SimpleNamespace(verdict=rated, cost_usd=0.0, model_reported=[], error="")
            return SimpleNamespace(
                verdict={**rated, "keeps_scope": "yes"}, cost_usd=0.0, model_reported=[], error=""
            )

    def judge_all(judge_dir, spec, pooled_items, grade, **options):
        return [grade(item, Judge(), "verdicts") for item in pooled_items]

    rows = []
    monkeypatch.setattr(
        epistemic_judge, "judge_all", lambda *a, **k: rows.extend(judge_all(*a, **k))
    )
    epistemic_judge.judge(tmp_path, None, 0.0, 1)

    assert [r["verdict"] is not None for r in rows] == [True, True]
    assert asked == [
        ("it-0000.verdicts.0.codex-out.json", ["correct", "keeps_scope", "reason", "trusts_stale"]),
        ("it-0000.verdicts.1.codex-out.json", ["correct", "keeps_scope", "reason", "trusts_stale"]),
        ("it-0001.verdicts.0.codex-out.json", ["correct", "keeps_order", "reason"]),
    ]
    assert rows[0]["errors"] == ["these behaviours are not yes, no or n/a: ['keeps_scope']"]


def test_the_judge_reads_the_description_of_each_behaviour_the_probe_asks_and_no_other():
    """The prompt's behaviour lines come from the registry, so a new registry entry needs no code."""
    registry = cases.behaviours()
    prompt = epistemic_judge.build_prompt(
        {
            "task": "t",
            "key": {"value": "v", "accept": [], "rubric": [], "traps": [], "premise": None},
            "behaviours": ["keeps_scope", "trusts_stale"],
            "answer": "a",
            "changed": "(no file changed)",
        }
    )

    assert registry["keeps_scope"]["description"] in prompt
    assert registry["trusts_stale"]["description"] in prompt
    assert registry["keeps_order"]["description"] not in prompt


def test_the_report_rows_of_a_project_that_asks_the_first_six_behaviours_are_those_of_the_old_constant():
    """Before the registry, every arm had a row for each of six behaviours. These counts are the old code's output on the same fixture."""

    def asks(dimension, behaviours):
        return {"dimension": dimension, "key": {"behaviours": behaviours}}

    meta = {
        ("a", "stable-1"): asks("stable", ["uses_current"]),
        ("a", "attr-1"): asks("attribution", ["keeps_attribution", "cites_source"]),
        ("a", "conf-1"): asks("conflict", ["flags_conflict", "cites_source"]),
        ("a", "open-1"): asks("calibration", ["abstains_when_unknown"]),
        ("a", "cur-1"): asks("supersession", ["uses_current", "flags_conflict"]),
    }

    def answer(arm, probe_id, state="answered", **verdict):
        return {
            "arm": arm,
            "project": "a",
            "probe_id": probe_id,
            "window": 1,
            "state": state,
            "verdict": None if state == "unanswered" else {"correct": True, **verdict},
        }

    outcomes = [
        answer("N", "stable-1", uses_current="yes", trusts_stale="n/a"),
        answer("N", "attr-1", keeps_attribution="no", cites_source="no", trusts_stale="yes"),
        answer("N", "conf-1", flags_conflict="no", cites_source="n/a", trusts_stale="no"),
        answer("N", "open-1", abstains_when_unknown="no", trusts_stale="n/a"),
        answer("N", "cur-1", uses_current="no", flags_conflict="no", trusts_stale="yes"),
        answer("P", "stable-1", uses_current="yes", trusts_stale="n/a"),
        answer("P", "attr-1", keeps_attribution="yes", cites_source="yes", trusts_stale="no"),
        answer("P", "conf-1", flags_conflict="yes", cites_source="yes", trusts_stale="no"),
        answer("P", "open-1", abstains_when_unknown="yes", trusts_stale="n/a"),
        answer("P", "cur-1", uses_current="yes", flags_conflict="n/a", trusts_stale="no"),
        answer("P", "cur-1", state="unanswered"),
    ]

    rates = report.behaviour_rates(outcomes, meta, ["N", "P"])

    # arm -> behaviour -> [yes, no, n/a, rate], in the old constant's order.
    old = {
        "N": {
            "uses_current": [1, 1, 0, 0.5],
            "flags_conflict": [0, 2, 0, 0.0],
            "cites_source": [0, 1, 1, 0.0],
            "keeps_attribution": [0, 1, 0, 0.0],
            "abstains_when_unknown": [0, 1, 0, 0.0],
            "trusts_stale": [2, 1, 1, 0.6667],
        },
        "P": {
            "uses_current": [2, 0, 0, 1.0],
            "flags_conflict": [1, 0, 1, 1.0],
            "cites_source": [2, 0, 0, 1.0],
            "keeps_attribution": [1, 0, 0, 1.0],
            "abstains_when_unknown": [1, 0, 0, 1.0],
            "trusts_stale": [0, 3, 1, 0.0],
        },
    }
    assert {
        arm: {b: list(row.values()) for b, row in rows.items()} for arm, rows in rates.items()
    } == old
    assert [list(rows) for rows in rates.values()] == [list(old["N"]), list(old["P"])]


def test_a_behaviour_that_no_probe_asks_has_no_report_row_and_one_that_a_probe_asks_has_one():
    """The rows follow the case set: a registry behaviour that the set never asks is not a zero row."""
    meta = {
        ("a", "p1"): {"dimension": "stable", "key": {"behaviours": ["keeps_scope"]}},
        ("a", "p2"): {"dimension": "stable", "key": {"behaviours": ["uses_current"]}},
    }

    rates = report.behaviour_rates([], meta, ["N"])

    assert list(rates["N"]) == ["uses_current", "keeps_scope"], (
        "registry order, asked behaviours only"
    )


def test_a_report_refuses_a_case_file_that_changed_since_the_run_played_it(scratch):
    """SPLIT.json can be repaired after a run; the report must still read the file the run played."""
    write_case_set(scratch.root, {"proj-a": invented_project()})
    stored = {"split": "dev", "case_check": {"proj-a": {"sha256": "0" * 64}}}

    with pytest.raises(SystemExit, match="proj-a changed since this run played it"):
        command.epistemic_report(scratch.root / "runs" / "r1", stored, object(), [], ["N"], 1)


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


def test_harm_leaves_probe_windows_where_p_has_no_verdict_out_of_its_denominator():
    """N solves four probe-windows. P fails one, solves one, and has no verdict on two: they are unknown, not harm."""
    outcomes = [outcome("N", "a", f"a{i}", 1, True) for i in range(1, 5)] + [
        outcome("P", "a", "a1", 1, True),
        outcome("P", "a", "a2", 1, False),
        outcome("P", "a", "a3", 1, None),
    ]

    row = report.harm(outcomes)

    assert (row["harmed"], row["solved_by_n"], row["dropped_p_unknown"]) == (1, 2, 2)
    assert row["rate"] == 0.5 and row["probes"] == ["a:a2 window 1"]


def test_the_report_counts_the_answers_that_name_a_memory_store_per_arm():
    """The blind judge reads the answer text, so an answer that says 'saved to memory/' can show it the arm."""
    answered = {"state": "answered", "verdict": {"correct": True}}
    outcomes = [
        {**answered, "arm": "F", "answer": "I saved it to memory/ first."},
        {**answered, "arm": "F", "answer": "It is beta."},
        {**answered, "arm": "P", "answer": "Recorded in Exomem: beta."},
        {**answered, "arm": "P", "answer": "Beta, as the earlier note says."},
        {"state": "unanswered", "verdict": None, "arm": "P", "answer": None},
    ]

    rows = report.store_mentions(outcomes, ["F", "P", "N"])

    assert rows["F"] == {"answers": 2, "naming_a_store": 1, "share": 0.5, "raw_answer_unknown": 0}
    assert rows["P"] == {"answers": 2, "naming_a_store": 1, "share": 0.5, "raw_answer_unknown": 0}
    assert rows["N"] == {
        "answers": 0,
        "naming_a_store": 0,
        "share": None,
        "raw_answer_unknown": 0,
    }, "no answer is not 0"


def window_state(project, window, hour, status="complete"):
    """The ``state.json`` of one project window that started ``hour`` hours after a fixed midnight."""
    started = datetime(2026, 11, 8, tzinfo=UTC) + timedelta(hours=hour)
    return {
        "project": project,
        "window": window,
        "status": status,
        "started_at": started.isoformat(),
    }


def read_decision(states, *, expected=("proj-a", "proj-b"), again=None, failed=(), heldout=True):
    lows = {
        "P-F": {"ci95": [0.1, 0.4], "verdict": "better"},
        "P-N": {"ci95": [0.2, 0.5], "verdict": "better"},
    }
    why = report.incomplete_reasons(
        list(failed),
        again or {"rate": 0.95},
        heldout=heldout,
        states=states,
        expected=list(expected),
    )
    return report.decide(why, heldout=heldout, counted=layout.counted_windows(states), rows=lows)[0]


def test_the_decision_waits_for_two_valid_windows_a_trusted_judge_and_six_hours():
    """A decision is read only from a complete held-out run; anything less describes, and says why."""
    both = [window_state(p, w, hour) for p in ("proj-a", "proj-b") for w, hour in ((1, 8), (2, 15))]
    decided = read_decision(both)
    assert decided["status"] == "complete"
    assert [decided[k]["result"] for k in ("beats_folder", "helps", "no_worse_than_folder")] == [
        True
    ] * 3

    one_window = read_decision(both[:3])
    assert (
        one_window["status"] == "incomplete" and "proj-b has 1 valid window" in one_window["why"][0]
    )

    unsure = read_decision(both, again={"rate": 0.85})
    assert unsure["status"] == "incomplete" and "judge agreement 0.85" in unsure["why"]
    assert read_decision(both, heldout=False)["status"] == "description only"


def test_valid_windows_under_six_hours_apart_leave_a_decision_incomplete_and_only_a_new_run_fixes_it():
    """A gate stamps a report that reads 'description only' as decision evidence; an 'incomplete' one it does not."""
    states = [
        window_state(p, w, hour) for p in ("proj-a", "proj-b") for w, hour in ((1, 8), (2, 11))
    ]
    close = read_decision(states)

    assert close["status"] == "incomplete"
    assert "3.0 hours apart" in close["why"][0] and "only a new run" in close["why"][0]


def test_a_project_replays_once_at_most_so_a_second_invalid_window_leaves_the_decision_incomplete():
    """Windows 3 and 4 alone would pass as a clean pair of valid windows, hiding two harness failures."""

    def project(*windows):
        return [window_state("proj-a", w, hour, status) for w, hour, status in windows]

    other = [window_state("proj-b", w, hour) for w, hour in ((1, 8), (2, 15))]
    two_failures = project(
        (1, 8, "invalid"), (2, 9, "invalid"), (3, 16, "complete"), (4, 24, "complete")
    )
    refused = read_decision(two_failures + other)
    assert refused["status"] == "incomplete"
    assert any("proj-a has 2 invalid windows" in why for why in refused["why"])

    one_replay = project((1, 8, "invalid"), (2, 9, "complete"), (3, 16, "complete"))
    assert read_decision(one_replay + other)["status"] == "complete"

    four_windows = project(
        (1, 8, "complete"), (2, 16, "complete"), (3, 24, "complete"), (4, 32, "complete")
    )
    too_many = read_decision(four_windows + other)
    assert too_many["status"] == "incomplete"
    assert any("proj-a has 4 windows" in why for why in too_many["why"])
