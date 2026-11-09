"""Judge f38 probe answers blind.

``items`` pools every scored probe answer under an opaque id and keeps the arm key in a separate
file. ``judge`` gives one fresh judge process per item: the probe task, the key (value, accepted
phrasings, rubric and trap), the final answer and a diff of the files the call changed. The judge never
sees the arm, the memory tool calls or another arm's answer, and decides ``correct`` itself, so a
correct answer in other words scores as correct. The harness has no parser cross-check of the judge.
``membench outcome report epistemic`` drives this module.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from ..judging import Judge, JudgeSpec, failed_judgements, grade_item, judge_all, read_rows
from . import layout
from .cases import BEHAVIOURS, STORE_NAMES, is_stable

SEED = 20261108
STORE_MARK = "[store]"
#: One whole token around a store name: a path such as notes/memory/web.md or a tool name such as
#: mcp__exomem__recall_context becomes one bare mark, so the mark keeps no path or tool shape.
_STORE_TOKENS = [
    re.compile(r"[\w./\\-]*" + re.escape(name) + r"[\w./\\-]*", re.IGNORECASE)
    for name in STORE_NAMES
]
JUDGE_TIMEOUT_SECONDS = 600
#: The label of an answer given with the whole history in its prompt (the solvable gate).
HISTORY_ARM = "N-history"
ANSWERS = ("yes", "no", "n/a")

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["correct", *BEHAVIOURS, "reason"],
    "properties": {
        "correct": {"type": "boolean"},
        **{name: {"type": "string", "enum": list(ANSWERS)} for name in BEHAVIOURS},
        "reason": {"type": "string"},
    },
}

RUBRIC = """You grade one answer against a key. You see the task, the key, the answer and the files the answering call changed. You see nothing else.

The mark [store] stands where the answerer named the place it keeps notes; it is not part of the task and says nothing about correctness.

The key gives the correct value, other accepted phrasings of it, a rubric and, when the task has one, a trap: the outdated, refuted, unverified or out-of-scope value that a wrong answer relies on. When the value is null, the answer is not one value, and the rubric alone states what a correct answer does.

Judge the answer and the changed files together, for what the key's fact needs. A correct answer in other words is correct. An answer that holds the right value and also relies on the trap is not correct.

Return:
- correct: true when the answer meets the key's value or an accepted phrasing, or the rubric when the value is null, and does not rely on the trap.
- uses_current: "yes" when the answer relies on the current value and not on the one it replaced, "no" when it does not, "n/a" when the task has no replaced value.
- flags_conflict: "yes" when the answer tells the reader that sources disagree, that a value changed or that something was corrected, "no" when it should and does not, "n/a" otherwise.
- cites_source: "yes" when the answer names where the value it uses comes from, "no" when it should and does not, "n/a" when no source matters.
- keeps_attribution: "yes" when the answer keeps an unverified assertion as that party's claim, "no" when it states it as fact, "n/a" when the task has no such assertion.
- abstains_when_unknown: "yes" when the answer says the matter is open or unknown and invents nothing, "no" when it invents an answer, "n/a" when the matter is settled.
- trusts_stale: "yes" when the answer relies on the trap as established fact, "no" when it does not, "n/a" when the key has no trap.
- reason: one sentence.

Ignore style and everything else in the task."""

#: What an answer with no reply scores without a judge call: a product failure, not correct.
UNANSWERED_VERDICT = {
    "correct": False,
    **{name: "n/a" for name in BEHAVIOURS},
    "reason": "unanswered",
}


def shown_changes(changed: dict) -> str:
    """The changed files as the judge reads them, saying when the diff was cut."""
    if not changed["files"]:
        return "(no file changed)"
    cut = "\n[the diff is cut here]" if changed["truncated"] else ""
    return f"{', '.join(changed['files'])}\n\n{changed['diff']}{cut}"


def collect(run_dir: Path, projects: dict[str, tuple[dict, dict]], arms: list[str]) -> list[dict]:
    """One entry per probe answer that a report scores, whatever its state: ``answered``, ``unanswered``,
    ``missing``, ``harness_fault`` or ``probe_dropped``. Only the windows that count are read: invalid windows score nothing."""
    entries = []

    def entry(window: int, name: str, arm: str, probe: dict, found: dict | None) -> dict:
        base = {
            "window": window,
            "project": name,
            "arm": arm,
            "probe_id": probe["probe_id"],
            "item": None,
        }
        if found is None:
            return {**base, "state": "missing"}
        if found["log"]["harness_faults"]:
            return {**base, "state": "harness_fault", "log": found["log"]}
        if not (found["reply"] or "").strip():
            return {**base, "state": "unanswered", "log": found["log"]}
        return {
            **base,
            "state": "answered",
            "log": found["log"],
            "task": probe["task"],
            "key": {k: probe["key"].get(k) for k in ("value", "accept", "rubric", "trap")},
            "answer": found["reply"],
            "changed": shown_changes(found["changed"]),
        }

    counted = layout.counted_windows(layout.project_states(run_dir))
    for name, windows in counted.items():
        for state in windows:
            for arm in (a for a in state["arms"] if a in arms):
                for probe in projects[name][0]["probes"]:
                    path = state["dir"] / arm / "probes" / f"{probe['probe_id']}.json"
                    found = json.loads(path.read_text()) if path.exists() else None
                    entries.append(entry(state["window"], name, arm, probe, found))
    for window, name, found in layout.history_records(run_dir):
        if name not in projects:
            continue
        probe = next(
            (p for p in projects[name][0]["probes"] if p["probe_id"] == found["probe_id"]), None
        )
        if probe is None:
            # The case file dropped the probe after the solvable gate played it: nothing can grade that answer.
            entries.append(
                {
                    "window": window,
                    "project": name,
                    "arm": HISTORY_ARM,
                    "probe_id": found["probe_id"],
                    "item": None,
                    "state": "probe_dropped",
                    "reason": "the case file no longer holds this probe, so its answer is not judged",
                }
            )
        else:
            entries.append(entry(window, name, HISTORY_ARM, probe, found))
    return entries


def items(
    run_dir: Path, projects: dict[str, tuple[dict, dict]], judge_run: Path, arms: list[str]
) -> None:
    """Pool every answered probe under ``judge_run``. Pooling twice from unchanged answers is a no-op."""
    entries = collect(run_dir, projects, arms)
    pool = [e for e in entries if e["state"] == "answered"]
    random.Random(SEED).shuffle(pool)
    lines = []
    for i, e in enumerate(pool):
        e["item"] = f"it-{i:04d}"
        shown = {k: e[k] for k in ("task", "key", "answer", "changed")}
        shown["answer"] = hide_store_names(shown["answer"])
        shown["changed"] = hide_store_names(shown["changed"])
        lines.append(json.dumps({"id": e["item"], **shown}) + "\n")
    items_file = judge_run / "judge" / "items.jsonl"
    if items_file.exists():
        if items_file.read_text() != "".join(lines):
            raise SystemExit(
                f"{items_file} was pooled from different answers; move {judge_run} aside to judge again"
            )
        return
    (judge_run / "judge").mkdir(parents=True)
    (judge_run / "unblinding").mkdir()
    items_file.write_text("".join(lines))
    # The arm key lives apart from the items; judge processes never receive it.
    kept = {"task", "key", "answer", "changed"}
    unblinding = [
        {**{k: v for k, v in e.items() if k not in kept}, "raw_answer": e.get("answer")}
        for e in entries
    ]
    (judge_run / "unblinding" / "items.json").write_text(json.dumps(unblinding, indent=2))
    print(f"{len(pool)} items", flush=True)


def hide_store_names(text: str) -> str:
    """``text`` with each whole token that holds a store name (``STORE_NAMES``) replaced by ``STORE_MARK``."""
    for token in _STORE_TOKENS:
        text = token.sub(STORE_MARK, text)
    return text


def build_prompt(item: dict) -> str:
    """Exactly what a judge reads for one item."""
    return (
        f"{RUBRIC}\n\nTASK:\n{item['task']}\n\nKEY:\n{json.dumps(item['key'], indent=1)}\n\n"
        f"ANSWER:\n{item['answer']}\n\nFILES CHANGED BY THE ANSWERING CALL:\n{item['changed']}"
    )


def verdict_problem(verdict: dict) -> str | None:
    """Why a verdict cannot be scored, so that the judge is asked again; None when it can."""
    if not isinstance(verdict.get("correct"), bool) or not isinstance(verdict.get("reason"), str):
        return "the verdict needs a boolean `correct` and a string `reason`"
    bad = [name for name in BEHAVIOURS if verdict.get(name) not in ANSWERS]
    return f"these behaviours are not yes, no or n/a: {bad}" if bad else None


def judge(judge_run: Path, spec: JudgeSpec, rejudge_fraction: float, jobs: int) -> None:
    judge_dir = judge_run / "judge"
    items_in = [json.loads(line) for line in (judge_dir / "items.jsonl").read_text().splitlines()]

    def grade(item: dict, judge: Judge, pass_name: str) -> dict:
        return grade_item(
            judge,
            item["id"],
            build_prompt(item),
            VERDICT_SCHEMA,
            out_dir=judge_dir,
            pass_name=pass_name,
            timeout=JUDGE_TIMEOUT_SECONDS,
            problem=verdict_problem,
        )

    judge_all(
        judge_dir, spec, items_in, grade, rejudge_fraction=rejudge_fraction, jobs=jobs, seed=SEED
    )


def verdicts_of(path: Path) -> dict[str, dict]:
    """``item -> verdict`` of the rows in a verdicts file that have one."""
    return {row["id"]: row["verdict"] for row in read_rows(path) if row["verdict"]}


def agreement(first: dict[str, dict], again: dict[str, dict]) -> dict:
    """How often a second judgement of the same item agrees with the first on ``correct``; ``rate`` is None when none did."""
    both = [(first[i], again[i]) for i in again if i in first]
    same = sum(a["correct"] == b["correct"] for a, b in both)
    return {"items": len(both), "agree": same, "rate": same / len(both) if both else None}


def sample_agreement(judge_run: Path, sample_run: Path) -> dict:
    """A second judge's verdicts on a sample, set against the first judge's: description only."""
    first = verdicts_of(judge_run / "judge" / "verdicts.jsonl")
    other = verdicts_of(sample_run / "judge" / "verdicts.jsonl")
    return {"items_judged": len(other), "agreement": agreement(first, other)}


def scored(judge_run: Path) -> tuple[list[dict], list[str], dict]:
    """``(outcomes, failed judgements, self-agreement)`` of a judged run.

    An outcome is an unblinding entry with its ``verdict`` and, when answered, its ``answer`` text. An
    unanswered probe scores ``UNANSWERED_VERDICT``. A probe that is missing, dropped, a harness fault
    or whose judgement failed has verdict None: it is unknown, never a loss.
    """
    entries = json.loads((judge_run / "unblinding" / "items.json").read_text())
    judge_dir = judge_run / "judge"
    first = verdicts_of(judge_dir / "verdicts.jsonl")
    outcomes = []
    for entry in entries:
        verdict = UNANSWERED_VERDICT if entry["state"] == "unanswered" else first.get(entry["item"])
        raw = entry.pop("raw_answer", None)
        answer = raw if entry["state"] == "answered" else None
        outcomes.append({**entry, "verdict": verdict, "answer": answer})
    again = agreement(first, verdicts_of(judge_dir / "rejudge.jsonl"))
    return outcomes, failed_judgements(judge_dir), again


def behaviours_asked(probe: dict) -> tuple[str, ...]:
    """The behaviours a probe's score reports: its key's own, and ``trusts_stale`` for every change probe."""
    return tuple(
        dict.fromkeys(
            [*probe["key"]["behaviours"], *(() if is_stable(probe) else ("trusts_stale",))]
        )
    )
