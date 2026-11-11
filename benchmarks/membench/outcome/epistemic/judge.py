"""Judge f38 probe answers blind.

``items`` pools every scored probe answer under an opaque id and keeps the arm key in a separate
file. ``judge`` gives one fresh judge process per item: the probe task, the key (value, accepted
phrasings, rubric, traps and premise), the final answer and a diff of the files the call changed. The judge never
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
from .cases import TRUSTS_STALE, behaviours, holds_store_name, is_stable, key_traps

SEED = 20261108
STORE_MARK = "[store]"
#: One whole token: a path such as notes/memory/web.md or a tool name such as
#: mcp__exomem__recall_context. A token that holds a store name becomes one bare mark, so the mark
#: keeps no path or tool shape. One character class with no name inside it matches in linear time.
_TOKEN = re.compile(r"[\w./\\-]+")
JUDGE_TIMEOUT_SECONDS = 600
#: The label of an answer given with the whole history in its prompt (the solvable gate).
HISTORY_ARM = "N-history"
ANSWERS = ("yes", "no", "n/a")
#: The fields of an item that a judge reads. Every other field of an entry stays in the unblinding file.
SHOWN = ("task", "key", "behaviours", "answer", "changed")


def verdict_schema(asked: tuple[str, ...] | list[str]) -> dict:
    """The verdict schema of one probe: ``correct``, the behaviours that the probe asks and ``reason``, no other."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["correct", *asked, "reason"],
        "properties": {
            "correct": {"type": "boolean"},
            **{name: {"type": "string", "enum": list(ANSWERS)} for name in asked},
            "reason": {"type": "string"},
        },
    }


INTRO = """You grade one answer against a key. You see the task, the key, the answer and the files the answering call changed. You see nothing else.

The mark [store] stands where the answerer named the place it keeps notes; it is not part of the task and says nothing about correctness.

The key gives the correct value, other accepted phrasings of it, a rubric and, when the task has them, traps and a premise. A trap is an outdated, refuted, unverified or out-of-scope value that a wrong answer relies on. The premise is the stale or false state that the task itself presupposes; a correct answer does not rely on it. When the value is null, the answer is not one value, and the rubric alone states what a correct answer does.

Judge the answer and the changed files together, for what the key's fact needs. A correct answer in other words is correct. An answer that holds the right value and also relies on a trap or on the premise is not correct."""

OUTRO = "Ignore style and everything else in the task."


def rubric(asked: tuple[str, ...] | list[str]) -> str:
    """What a judge reads before one item: the fixed grading rules, then one line for each behaviour that the probe asks.

    A behaviour's line is its registry description, so a behaviour added to ``behaviours.yaml`` needs no code here.
    """
    registry = behaviours()
    lines = [f"- {name}: {registry[name]['description']}" for name in asked]
    return (
        f"{INTRO}\n\nReturn:\n"
        "- correct: true when the answer meets the key's value or an accepted phrasing, or the rubric when the value is null, and does not rely on any trap or on the premise.\n"
        'Then, for each behaviour below, "yes" when the answer shows it, "no" when the task gives an occasion for it and the answer does not show it, and "n/a" when the task gives no occasion for it:\n'
        + "\n".join(lines)
        + "\n- reason: one sentence.\n\n"
        + OUTRO
    )


#: What an answer with no reply scores without a judge call: a product failure, not correct.
UNANSWERED_VERDICT = {"correct": False, "reason": "unanswered"}


def shown_key(key: dict) -> dict:
    """The key as a judge reads it: the value, accepted phrasings, rubric, every trap as a list and the premise."""
    return {
        "value": key.get("value"),
        "accept": key.get("accept"),
        "rubric": key.get("rubric"),
        "traps": key_traps(key),
        "premise": key.get("premise"),
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
            "key": shown_key(probe["key"]),
            "behaviours": list(behaviours_asked(probe)),
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
        shown = {k: e[k] for k in SHOWN}
        shown["answer"] = hide_store_names(shown["answer"])
        shown["changed"] = hide_store_names(shown["changed"])
        lines.append(json.dumps({"id": e["item"], **shown}) + "\n")
    items_file = judge_run / "judge" / "items.jsonl"
    if items_file.exists():
        if items_file.read_text() != "".join(lines):
            raise SystemExit(
                f"{items_file} was pooled from different answers, by an older mask or in an older item format; "
                f"move {judge_run} aside to judge again"
            )
        return
    (judge_run / "judge").mkdir(parents=True)
    (judge_run / "unblinding").mkdir()
    items_file.write_text("".join(lines))
    # The arm key lives apart from the items; judge processes never receive it.
    unblinding = [
        {**{k: v for k, v in e.items() if k not in SHOWN}, "raw_answer": e.get("answer")}
        for e in entries
    ]
    (judge_run / "unblinding" / "items.json").write_text(json.dumps(unblinding, indent=2))
    print(f"{len(pool)} items", flush=True)


def hide_store_names(text: str) -> str:
    """``text`` with each whole token that holds a store name (``holds_store_name``) replaced by ``STORE_MARK``.

    A sentence's closing full stop stays outside the mark."""

    def mask(match: re.Match[str]) -> str:
        token = match.group()
        body = token.rstrip(".")
        if holds_store_name(body):
            return STORE_MARK + token[len(body) :]
        return token

    return _TOKEN.sub(mask, text)


def build_prompt(item: dict) -> str:
    """Exactly what a judge reads for one item."""
    return (
        f"{rubric(item['behaviours'])}\n\nTASK:\n{item['task']}\n\nKEY:\n{json.dumps(item['key'], indent=1)}\n\n"
        f"ANSWER:\n{item['answer']}\n\nFILES CHANGED BY THE ANSWERING CALL:\n{item['changed']}"
    )


def verdict_problem(verdict: dict, asked: tuple[str, ...] | list[str]) -> str | None:
    """Why a verdict cannot be scored, so that the judge is asked again; None when it can."""
    if not isinstance(verdict.get("correct"), bool) or not isinstance(verdict.get("reason"), str):
        return "the verdict needs a boolean `correct` and a string `reason`"
    bad = [name for name in asked if verdict.get(name) not in ANSWERS]
    return f"these behaviours are not yes, no or n/a: {bad}" if bad else None


def judge(judge_run: Path, spec: JudgeSpec, rejudge_fraction: float, jobs: int) -> None:
    judge_dir = judge_run / "judge"
    items_in = [json.loads(line) for line in (judge_dir / "items.jsonl").read_text().splitlines()]

    def grade(item: dict, judge: Judge, pass_name: str) -> dict:
        return grade_item(
            judge,
            item["id"],
            build_prompt(item),
            verdict_schema(item["behaviours"]),
            out_dir=judge_dir,
            pass_name=pass_name,
            timeout=JUDGE_TIMEOUT_SECONDS,
            problem=lambda verdict: verdict_problem(verdict, item["behaviours"]),
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
    """The behaviours a probe's verdict holds and its score reports: its key's own, and ``trusts_stale`` for every change probe."""
    return tuple(
        dict.fromkeys([*probe["key"]["behaviours"], *(() if is_stable(probe) else (TRUSTS_STALE,))])
    )
