"""Score `session-close` runs against the answer key and a blind judge.

Refuses to score while any selected case has no key. The key is written by someone who did not build the suite and
did not run it, into each case file's `answer_key` slot (shape below); a run holds raw observations only.

    "answer_key": {
      "reference":        what the session settled and which page it belongs on, in plain words (the judge reads it),
      "must_change":      pages (relative to the knowledge base) that must differ after the session,
      "must_not_change":  seeded pages that must stay as they were,
      "recap":            "required", "forbidden" or "optional",
      "recap_must_link":  seeded pages that a recap must link,
      "new_pages":        "none" (no page besides the recap) or "allowed"
    }

The judge sees the person's turns, the reference and the page diffs. It never sees the arm, the model or a tool reply.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from epistemic.journeys.f27_replay import discover_agent_envelope, environment_floor

from . import checks, judge, runner
from .report import read_rows
from .session_close import Case, load_cases
from .store_report import paired_interval

KEY_FIELDS = {"reference", "must_change", "must_not_change", "recap", "recap_must_link", "new_pages"}
RECAP_RULES = ("required", "forbidden", "optional")
NEW_PAGE_RULES = ("none", "allowed")
#: Both are about the changes to existing pages; `expect` true means the criterion holds when the run is good.
CRITERIA = (
    {
        "id": "J1",
        "expect": True,
        "text": "Each change to an existing page records what the session settled, as the reference describes it, "
        "and states no fact the person did not give. True when no page changed.",
    },
    {
        "id": "J2",
        "expect": True,
        "text": "Each change to an existing page is appropriately scoped: it touches only what the session settled, "
        "keeps the page's other content, and leaves its structure and voice intact. True when no page changed.",
    },
)
JUDGE_TIMEOUT_SECONDS = 300.0


def refuse_unkeyed(cases: Sequence[Case]) -> None:
    """Stop before any page is read or any judge is called: a missing or malformed key means nothing can be scored."""
    missing = [c.id for c in cases if c.answer_key is None]
    if missing:
        raise SystemExit(
            f"refusing to score: {len(missing)} case(s) have no answer_key: {missing}. "
            "The key writer fills the `answer_key` slot of each case file."
        )
    for case in cases:
        problems = key_problems(case)
        if problems:
            raise SystemExit(f"refusing to score: {case.id} answer_key: " + "; ".join(problems))


def key_problems(case: Case) -> list[str]:
    key, problems = case.answer_key, []
    if not isinstance(key, dict) or set(key) != KEY_FIELDS:
        return [f"fields must be exactly {sorted(KEY_FIELDS)}"]
    if not isinstance(key["reference"], str) or not key["reference"].strip():
        problems.append("reference must be a non-empty string")
    if key["recap"] not in RECAP_RULES:
        problems.append(f"recap must be one of {RECAP_RULES}")
    if key["new_pages"] not in NEW_PAGE_RULES:
        problems.append(f"new_pages must be one of {NEW_PAGE_RULES}")
    seeded = {p.relative_to(case.seed).as_posix() for p in case.seed.rglob("*.md")}
    for name in ("must_change", "must_not_change", "recap_must_link"):
        listed = key[name]
        if not isinstance(listed, list) or not all(isinstance(p, str) for p in listed):
            problems.append(f"{name} must be a list of page paths")
        elif set(listed) - seeded:
            problems.append(f"{name} names pages that are not in the seed: {sorted(set(listed) - seeded)}")
    return problems


def check_row(key: dict, row: dict) -> list[checks.Outcome]:
    """Deterministic checks of one run's snapshot diff against the key."""
    seen = row["observed"]
    changed = set(seen["changed"])
    touched = changed | set(seen["removed"])
    recaps = seen["recaps"]
    linked = {p for recap in recaps.values() for p in recap["links"]["seeded"]}
    other_pages = [p for p in seen["created"] if p not in recaps]

    def outcome(name: str, held: bool, detail: str) -> checks.Outcome:
        return checks.Outcome(name, checks.PASS if held else checks.FAIL, detail)

    missing = sorted(set(key["must_change"]) - changed)
    wrong = sorted(set(key["must_not_change"]) & touched)
    absent = sorted(set(key["recap_must_link"]) - linked)
    recap_held = {"required": bool(recaps), "forbidden": not recaps, "optional": True}[key["recap"]]
    return [
        outcome("must_change", not missing, f"not changed: {missing}" if missing else "all changed"),
        outcome("must_not_change", not wrong, f"touched: {wrong}" if wrong else "none touched"),
        outcome("recap", recap_held, f"{len(recaps)} recap(s); the key says {key['recap']}"),
        outcome("recap_must_link", not absent, f"not linked: {absent}" if absent else "all linked"),
        outcome(
            "new_pages",
            key["new_pages"] == "allowed" or not other_pages,
            f"pages besides the recap: {other_pages}" if other_pages else "none besides the recap",
        ),
    ]


def judge_view(case: Case, key: dict, row: dict) -> str:
    """The same view for every arm: the person's turns, the reference, and what happened to the pages."""
    seen = row["observed"]
    turns = "\n".join(f"{n}. {t}" for n, t in enumerate(case.turns, start=1))
    changes = [f"### Changed: {p}\n{diff}" for p, diff in seen["changed"].items()]
    changes += [f"### Removed: {p}" for p in seen["removed"]]
    changes += [
        f"### Created: {p}\n{text}" for p, text in seen["created"].items() if p not in seen["recaps"]
    ]
    return (
        f"WHAT THE PERSON SAID, IN ORDER\n{turns}\n\n"
        f"REFERENCE (what the session settled; it is true)\n{key['reference']}\n\n"
        "PAGE CHANGES THE SESSION LEFT, AS DIFFS\n" + ("\n\n".join(changes) or "No page changed.")
    )


def judge_prompt(view: str) -> str:
    listed = "\n".join(f"- {c['id']}: {c['text']}" for c in CRITERIA)
    return (
        "A person had a session with an AI agent that keeps a knowledge base of pages. Read what the person said, "
        "the reference for what the session settled, and the changes the agent left in the pages. For each "
        "criterion answer true if it holds and false if it does not, with a one-line reason. Judge only the page "
        "changes shown.\n\nCRITERIA\n" + listed + "\n\nCASE\n" + view
    )


def judge_row(case: Case, key: dict, row: dict, *, model: str, executable: Path, scratch: Path) -> list[checks.Outcome]:
    seen = row["observed"]
    if not (seen["changed"] or seen["removed"] or [p for p in seen["created"] if p not in seen["recaps"]]):
        # Nothing to judge: whether a change was owed is the deterministic checks' question, not the judge's.
        return [checks.Outcome(c["id"], checks.PASS, "no page changed; nothing to judge") for c in CRITERIA]
    empty_mcp = scratch / "empty-mcp.json"
    empty_mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
    env, _removed = environment_floor(os.environ)
    env = {k: v for k, v in env.items() if not k.startswith("EXOMEM_")}
    try:
        done = subprocess.run(  # noqa: S603
            runner.judge_argv(
                executable,
                judge_prompt(judge_view(case, key, row)),
                judge.answer_schema(CRITERIA),
                empty_mcp=empty_mcp,
                model=model,
            ),
            cwd=str(scratch),
            env=env,
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT_SECONDS,
        )
        answer = judge.parse_answer(done.stdout, CRITERIA)
    except (OSError, subprocess.TimeoutExpired):
        answer = None
    return checks.judged_outcomes(CRITERIA, answer)


def score_row(case: Case, row: dict, *, judge_model: str, executable: Path, scratch: Path) -> dict:
    key = case.answer_key
    if row["error"] is not None:
        return {"deterministic": [], "judged": [], "verdict": checks.UNKNOWN}
    deterministic = check_row(key, row)
    judged = judge_row(case, key, row, model=judge_model, executable=executable, scratch=scratch)
    return {
        "deterministic": [o.__dict__ for o in deterministic],
        "judged": [o.__dict__ for o in judged],
        "verdict": checks.verdict(None, deterministic, judged),
    }


def score_all(runs: Sequence[Path], *, out: Path, judge_model: str) -> list[dict]:
    cases = {c.id: c for c in load_cases("all")}
    rows, _replaced = read_rows(runs)
    refuse_unkeyed([cases[sid] for sid in dict.fromkeys(r["scenario"] for r in rows)])
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / "judge-cwd"
    scratch.mkdir(exist_ok=True)
    executable = discover_agent_envelope().executable
    scored = []
    with (out / "scores.jsonl").open("w", encoding="utf-8") as sink:
        for row in rows:
            case = cases[row["scenario"]]
            result = score_row(case, row, judge_model=judge_model, executable=executable, scratch=scratch)
            entry = {
                k: row[k]
                for k in ("scenario", "repeat", "model", "arm", "error", "cost_usd", "warming_replies")
                if k in row
            } | {"order": (row.get("observed") or {}).get("order"), "restraint": not case.answer_key["must_change"]} | result
            sink.write(json.dumps(entry) + "\n")
            sink.flush()
            scored.append(entry)
    shutil.rmtree(scratch, ignore_errors=True)
    return scored


def build_report(scored: list[dict], *, baseline: str, candidate: str) -> str:
    """Counts per case and arm, then the paired difference (candidate minus baseline) of runs that pass every check,
    for the cases that owe a page update and for the restraint cases. An unknown run is counted and left out."""
    lines = ["| model | case | arm | runs | pass | fail | unknown | page written before recap | judge pass |", "|" + "---|" * 9]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in scored:
        groups[(row["model"], row["scenario"], row["arm"])].append(row)
    for (model, case, arm), rows in sorted(groups.items()):
        verdicts = [r["verdict"] for r in rows]
        judged = [r for r in rows if r["judged"]]
        lines.append(
            f"| {model} | {case} | {arm} | {len(rows)} | {verdicts.count('pass')} | {verdicts.count('fail')} | "
            f"{verdicts.count('unknown')} | {sum(r['order'] == 'before' for r in rows)} | "
            f"{sum(all(o['verdict'] == 'pass' for o in r['judged']) for r in judged)}/{len(judged)} |"
        )
    for name, restraint in (("cases that owe a page update", False), ("restraint cases", True)):
        pairs, unknown = [], 0
        clusters: dict[tuple, dict] = defaultdict(dict)
        for row in scored:
            if row["restraint"] == restraint:
                clusters[(row["model"], row["scenario"], row["repeat"])][row["arm"]] = row["verdict"]
        for arms in clusters.values():
            both = (arms.get(baseline), arms.get(candidate))
            if checks.UNKNOWN in both or None in both:
                unknown += 1
            else:
                pairs.append((both[0] == checks.PASS, both[1] == checks.PASS))
        if pairs:
            low, high = paired_interval(pairs)
            mean = sum(c - b for b, c in pairs) / len(pairs)
            lines.append(
                f"\n{name}: {len(pairs)} paired runs, pass rate {candidate} minus {baseline} = {mean:+.2f} "
                f"(95% interval {low:+.2f} to {high:+.2f}); {unknown} pair(s) left out as unknown."
            )
        else:
            lines.append(f"\n{name}: no complete pair; {unknown} pair(s) unknown.")
    return "\n".join(lines) + "\n"
