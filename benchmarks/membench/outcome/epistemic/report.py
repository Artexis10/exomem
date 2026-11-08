"""Report an f38 run: per-probe scores, paired intervals, the decision and the description rows.

A probe's score for an arm is the mean of the judge's ``correct`` over the project's counted windows
(0, 0.5 or 1; see ``layout.counted_windows``). The windows of one project are not independent units.
A pair (P-F, P-N, P-E) is the difference of those scores over change probes, with a 95% percentile
bootstrap over probes that resamples inside each project. A decision reads a held-out run only: two
valid windows per project, at least ``MIN_WINDOW_GAP_HOURS`` apart. Every other figure describes.
Unknown, partial and failed values stay visible as such, never 0.

``membench outcome report epistemic`` drives this module.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from recall_parity.scoring import DEFAULT_RESAMPLES

from ..faults import books
from ..stats import NOT_WORSE_BOUND, paired_interval, usage_totals
from . import layout
from .cases import is_stable
from .judge import BEHAVIOURS, HISTORY_ARM, STORE_NAMES, behaviours_asked, scored

BOOTSTRAP_SEED = 20261108
#: Agreement of a judge with its own second judgement on ``correct``, below which a run is incomplete. Final for the run.
AGREEMENT_FLOOR = 0.90
#: Dev gates: arm N, given the whole history, solves at least this share of probes; without it, it fails at least this share.
SOLVABLE_FLOOR = 0.90
MEMORY_NEEDED_FLOOR = 0.80

#: What each comparison is for. Only the first two are read by the decision.
PAIR_ROLES = {
    "P-F": "read by the decision: beats folder, no worse than folder",
    "P-N": "read by the decision: helps",
    "P-E": "description only: what the plugin, skills and instruction add",
}
NO_CROSS_CHECK = "none ran: the harness has no parser cross-check of the judge"

Unit = tuple[str, str]  # (project, probe id)


def mean(values: list[float]) -> float | None:
    """The mean, rounded; None (unknown) when there is nothing to average."""
    return round(statistics.mean(values), 4) if values else None


def score_tables(outcomes: list[dict], arms: list[str]) -> dict[str, dict[Unit, dict[int, float]]]:
    """``arm -> unit -> window -> 0 or 1`` for the judged answers; an unknown answer has no entry."""
    tables: dict[str, dict[Unit, dict[int, float]]] = {arm: defaultdict(dict) for arm in arms}
    for o in outcomes:
        if o["arm"] in tables and o["verdict"] is not None:
            tables[o["arm"]][(o["project"], o["probe_id"])][o["window"]] = float(
                o["verdict"]["correct"]
            )
    return tables


def paired_diffs(
    a: str, b: str, scores: dict, meta: dict, *, stable: bool
) -> tuple[list[Unit], list[float]]:
    """The probes both arms scored (change probes, or stable ones) and ``a`` minus ``b`` on each."""
    units = sorted(u for u in scores[a] if u in scores[b] and is_stable(meta[u]) == stable)
    return units, [
        mean(list(scores[a][u].values())) - mean(list(scores[b][u].values())) for u in units
    ]


def pair_row(a: str, b: str, scores: dict, meta: dict) -> dict:
    """``a`` minus ``b`` over change probes, with its interval: the figure a decision reads."""
    units, diffs = paired_diffs(a, b, scores, meta, stable=False)
    return paired_interval(diffs, seed=BOOTSTRAP_SEED, unit="probe", strata=[u[0] for u in units])


def point_difference(a: str, b: str, scores: dict, meta: dict, *, stable: bool) -> dict:
    """``a`` minus ``b`` as a mean over paired probes, no interval: unknown only when no probe is paired."""
    _, diffs = paired_diffs(a, b, scores, meta, stable=stable)
    return {"paired_probes": len(diffs), "mean_difference": mean(diffs)}


def arm_means(scores: dict, meta: dict, arm: str, pick) -> dict:
    units = [u for u in scores[arm] if pick(meta[u])]
    return {
        "probes": len(units),
        "mean_score": mean([mean(list(scores[arm][u].values())) for u in units]),
    }


def incomplete_reasons(
    failed: list[str], again: dict, *, heldout: bool, states: list[dict], expected: list[str]
) -> list[str]:
    """Why no figure can be final yet. Empty when the run is complete.

    A held-out project needs two valid windows, at most one replay and at least
    ``MIN_WINDOW_GAP_HOURS`` between its two counted windows. A report that only describes a held-out
    run would still be stamped as decision evidence by the gate, so a short gap is incomplete too.
    """
    why = [f"the judgement of {item} failed" for item in failed]
    if again["rate"] is None:
        why.append("judge agreement unknown: no item was judged twice")
    elif again["rate"] < AGREEMENT_FLOOR:
        why.append(f"judge agreement {again['rate']:.2f}")
    if heldout:
        counted = layout.counted_windows(states)
        for project in expected:
            have = len(counted.get(project, []))
            if have < layout.WINDOWS_PER_PROJECT:
                why.append(f"{project} has {have} valid window(s) of {layout.WINDOWS_PER_PROJECT}")
        why += layout.window_problems(states)
        why += [
            f"{project}'s valid windows start {hours} hours apart, under {layout.MIN_WINDOW_GAP_HOURS}: "
            "only a new run fixes the gap"
            for project, hours in layout.window_gaps(counted).items()
            if hours < layout.MIN_WINDOW_GAP_HOURS
        ]
    return why


def rule(row: dict | None, text: str, bound: float) -> dict:
    if row is None:
        return {"rule": text, "result": "unknown: an arm of this pair did not run"}
    if row["ci95"] is None:
        return {"rule": text, "result": f"unknown: {row['verdict']}"}
    return {"rule": text, "result": row["ci95"][0] > bound}


def decide(
    why: list[str], *, heldout: bool, counted: dict, rows: dict[str, dict]
) -> tuple[dict, dict]:
    """``(decision, hours apart per project)``. Incomplete beats description; a dev run only describes."""
    gaps = layout.window_gaps(counted)
    if why:
        return {"status": "incomplete", "why": why}, gaps
    if not heldout:
        return {"status": "description only", "why": ["a dev run decides nothing"]}, gaps
    return {
        "status": "complete",
        "beats_folder": rule(rows.get("P-F"), "lower bound of P-F above 0", 0.0),
        "helps": rule(rows.get("P-N"), "lower bound of P-N above 0", 0.0),
        "no_worse_than_folder": rule(
            rows.get("P-F"), f"lower bound of P-F above {NOT_WORSE_BOUND}", NOT_WORSE_BOUND
        ),
    }, gaps


def behaviour_rates(outcomes: list[dict], meta: dict, arms: list[str]) -> dict:
    """Per arm and behaviour: the answers of the probes that ask for it, as yes, no and n/a with the rate of yes among yes and no."""
    rates: dict[str, dict] = {
        arm: {b: {"yes": 0, "no": 0, "n/a": 0} for b in BEHAVIOURS} for arm in arms
    }
    for o in outcomes:
        if o["arm"] not in rates or o["verdict"] is None or o["state"] == "unanswered":
            continue
        for behaviour in behaviours_asked(meta[(o["project"], o["probe_id"])]):
            rates[o["arm"]][behaviour][o["verdict"][behaviour]] += 1
    for arm in rates:
        for count in rates[arm].values():
            decided = count["yes"] + count["no"]
            count["rate"] = round(count["yes"] / decided, 4) if decided else None
    return rates


def harm(outcomes: list[dict]) -> dict:
    """Probe-windows that N answers correctly and P does not, out of those that N answers correctly and P answered.

    A probe-window where P has no verdict is unknown, not a loss: it leaves the denominator and is counted apart.
    """
    verdicts = {
        (o["arm"], o["project"], o["probe_id"], o["window"]): o["verdict"]["correct"]
        for o in outcomes
        if o["verdict"] is not None
    }
    solved = sorted(k[1:] for k, ok in verdicts.items() if k[0] == "N" and ok)
    known = [k for k in solved if ("P", *k) in verdicts]
    harmed = [k for k in known if verdicts[("P", *k)] is False]
    return {
        "scope": "probe-windows, change and stable probes, where N is correct and P has a verdict",
        "harmed": len(harmed),
        "solved_by_n": len(known),
        "dropped_p_unknown": len(solved) - len(known),
        "rate": round(len(harmed) / len(known), 4) if known else None,
        "probes": [f"{p}:{probe} window {w}" for p, probe, w in harmed],
    }


def store_mentions(outcomes: list[dict], arms: list[str]) -> dict:
    """Per arm, the raw answers that name a memory store (``STORE_NAMES``); the judge read each one masked."""
    rows = {}
    for arm in arms:
        texts = [o["answer"] for o in outcomes if o["arm"] == arm and o["state"] == "answered"]
        naming = sum(any(name in text.casefold() for name in STORE_NAMES) for text in texts)
        rows[arm] = {
            "answers": len(texts),
            "naming_a_store": naming,
            "share": round(naming / len(texts), 4) if texts else None,
        }
    return rows


def window_scores(outcomes: list[dict], counted: dict, meta: dict, arms: list[str]) -> dict:
    """Per arm and project, the change-probe score of each counted window and the difference between them."""
    table: dict[str, dict] = {arm: {} for arm in arms}
    for arm in arms:
        for project, windows in counted.items():
            scores = [
                mean(
                    [
                        float(o["verdict"]["correct"])
                        for o in outcomes
                        if (o["arm"], o["project"], o["window"]) == (arm, project, w["window"])
                        and o["verdict"] is not None
                        and not is_stable(meta[(project, o["probe_id"])])
                    ]
                )
                for w in windows
            ]
            known = len(scores) == layout.WINDOWS_PER_PROJECT and None not in scores
            table[arm][project] = {
                "windows": [w["window"] for w in windows],
                "scores": scores,
                "second_minus_first": round(scores[1] - scores[0], 4) if known else None,
            }
    return table


def call_logs(run_dir: Path) -> dict[str, list[dict]]:
    """Every call's log by arm, from the windows' ended projects and the solvable gate. Parked unfinished attempts are not read."""
    logs: dict[str, list[dict]] = defaultdict(list)
    for pattern in ("window-*/*/*/sessions/*.json", "window-*/*/*/probes/*.json"):
        for path in sorted(run_dir.glob(pattern)):
            logs[path.parents[1].name].append(json.loads(path.read_text())["log"])
    for path in sorted(run_dir.glob("window-*/history/*/*/probe.json")):
        logs[HISTORY_ARM].append(json.loads(path.read_text())["log"])
    return logs


def gate_row(
    rate: float | None, floor: float, failing: list[str], judged: int, unknown: int, *, below: bool
) -> dict:
    """One dev gate: its rate against the floor, the probes that fail it and the answers that could not be judged."""
    return {
        "rate": None if rate is None else round(rate, 4),
        "floor": floor,
        "meets": None if rate is None else rate >= floor,
        "answers_judged": judged,
        "answers_unknown": unknown,
        "failing_probes": failing,
        "failing_means": "N without the history solved the probe"
        if below
        else "N with the history did not solve the probe",
    }


def dev_gates(outcomes: list[dict]) -> dict:
    """Solvable (arm N with the whole history) and memory needed (arm N without it), each with the probes that fail it."""
    result = {}
    for name, arm in (("solvable", HISTORY_ARM), ("memory_needed", "N")):
        rows = [o for o in outcomes if o["arm"] == arm]
        if not rows:
            what = (
                "no --history full run in this run" if arm == HISTORY_ARM else "arm N did not play"
            )
            result[name] = {"status": f"not run: {what}"}
            continue
        judged = [o for o in rows if o["verdict"] is not None]
        correct = [o for o in judged if o["verdict"]["correct"]]
        unknown = len(rows) - len(judged)
        if name == "solvable":
            failing = sorted(
                {f"{o['project']}:{o['probe_id']}" for o in judged}
                - {f"{o['project']}:{o['probe_id']}" for o in correct}
            )
            rate = len(correct) / len(judged) if judged else None
            result[name] = gate_row(
                rate, SOLVABLE_FLOOR, failing, len(judged), unknown, below=False
            )
        else:
            failing = sorted({f"{o['project']}:{o['probe_id']}" for o in correct})
            rate = 1 - len(correct) / len(judged) if judged else None
            result[name] = gate_row(
                rate, MEMORY_NEEDED_FLOOR, failing, len(judged), unknown, below=True
            )
    return result


def unit_books(
    states: list[dict], counted: dict, projects: dict, outcomes: list[dict], arm: str
) -> dict:
    """One arm's probe-windows: valid (judged or unanswered), invalid (the window is invalid) or missing.

    A valid window beyond a project's first ``WINDOWS_PER_PROJECT`` is not scheduled: nothing scores it.
    """
    counted_keys = {(p, w["window"]) for p, windows in counted.items() for w in windows}
    scheduled, valid, invalid = set(), set(), set()
    judged = {
        (o["project"], o["window"], o["probe_id"])
        for o in outcomes
        if o["arm"] == arm and o["verdict"] is not None
    }
    for state in states:
        if arm not in state["arms"]:
            continue
        if (
            state["status"] == "complete"
            and (state["project"], state["window"]) not in counted_keys
        ):
            continue
        for probe in projects[state["project"]][0]["probes"]:
            unit = f"w{state['window']}:{state['project']}:{probe['probe_id']}"
            scheduled.add(unit)
            if state["status"] != "complete":
                invalid.add(unit)
            elif (state["project"], state["window"], probe["probe_id"]) in judged:
                valid.add(unit)
    return books(scheduled, valid, invalid)


def build(
    run_dir: Path,
    projects: dict[str, tuple[dict, dict]],
    judge_run: Path,
    pairs: list[tuple[str, str]],
    arms: list[str],
    *,
    heldout: bool,
) -> dict:
    """The unstamped report of an f38 run whose answers are judged under ``judge_run``."""
    outcomes, failed, again = scored(judge_run)
    states = layout.project_states(run_dir)
    counted = layout.counted_windows(states)
    meta = {
        (name, p["probe_id"]): p
        for name, (project, _) in projects.items()
        for p in project["probes"]
    }
    scores = score_tables(outcomes, arms)
    rows = {f"{a}-{b}": pair_row(a, b, scores, meta) for a, b in pairs}
    why = incomplete_reasons(failed, again, heldout=heldout, states=states, expected=list(projects))
    decision, gaps = decide(why, heldout=heldout, counted=counted, rows=rows)
    play = [o for o in outcomes if o["arm"] != HISTORY_ARM]
    stable_pairs = [(a, b) for a, b in pairs if a in arms and b in arms]
    p_gain = None
    if all(a in scores for a in ("P", "N")):
        change = point_difference("P", "N", scores, meta, stable=False)["mean_difference"]
        stable = point_difference("P", "N", scores, meta, stable=True)["mean_difference"]
        p_gain = {
            "change": change,
            "stable": stable,
            "change_minus_stable": None if None in (change, stable) else round(change - stable, 4),
        }
    logs = call_logs(run_dir)
    report = {
        "run_id": run_dir.name,
        "status": "complete" if not why else f"incomplete: {'; '.join(why)}",
        "judge": json.loads((judge_run / "judge" / "judge.json").read_text()),
        "failed_judgements": failed,
        "judge_self_agreement": {"on": "correct", **again},
        "parser_cross_check": NO_CROSS_CHECK,
        "bootstrap": {
            "interval": "95% percentile",
            "draws": DEFAULT_RESAMPLES,
            "seed": BOOTSTRAP_SEED,
            "resampled": "probes, within each project",
        },
        "pairs": {k: {**row, "role": PAIR_ROLES[k]} for k, row in rows.items()},
        "decision": decision,
        "arms": {
            arm: {
                "units": unit_books(states, counted, projects, outcomes, arm),
                "unanswered": sum(
                    1 for o in play if o["arm"] == arm and o["state"] == "unanswered"
                ),
                "spent": usage_totals(logs.get(arm, [])),
            }
            for arm in arms
        },
        "skipped_answers": [
            {k: o[k] for k in ("project", "window", "arm", "probe_id", "reason")}
            for o in outcomes
            if o["state"] == "probe_dropped"
        ],
        "windows": {
            "counted": {
                p: [{"window": w["window"], "started_at": w["started_at"]} for w in ws]
                for p, ws in counted.items()
            },
            "hours_apart": gaps,
            "invalid": [
                {"project": s["project"], "window": s["window"], "errors": s["errors"]}
                for s in states
                if s["status"] != "complete"
            ],
            "unfinished": layout.unfinished_windows(run_dir),
            "not_counted": [
                {"project": s["project"], "window": s["window"]}
                for s in states
                if s["status"] == "complete"
                and s["window"] not in [w["window"] for w in counted.get(s["project"], [])]
            ],
        },
        "description": {
            "stable_probes": {a: arm_means(scores, meta, a, is_stable) for a in arms},
            "p_gain_over_n": p_gain,
            "stable_differences": {
                f"{a}-{b}": point_difference(a, b, scores, meta, stable=True)
                for a, b in stable_pairs
            },
            "dimensions": {
                d: {a: arm_means(scores, meta, a, lambda p, d=d: p["dimension"] == d) for a in arms}
                for d in sorted({p["dimension"] for p in meta.values()})
            },
            "behaviours": behaviour_rates(play, meta, arms),
            "harm": harm(play) if {"N", "P"} <= set(arms) else None,
            "answers_naming_a_store": {
                "scope": f"share of the raw answers that contain any of {list(STORE_NAMES)}, case-folded; the judge read them masked",
                "arms": store_mentions(play, arms),
            },
            "window_scores": window_scores(play, counted, meta, arms),
            "usage_scope": "every call of the ended projects and the solvable gate; parked unfinished attempts are not counted",
        },
        "case_check": {name: check for name, (_, check) in projects.items()},
        "probes": [
            {
                "project": project,
                "probe_id": probe_id,
                "dimension": meta[(project, probe_id)]["dimension"],
                "scores": {
                    a: mean(list(scores[a][(project, probe_id)].values()))
                    if (project, probe_id) in scores[a]
                    else None
                    for a in arms
                },
            }
            for project, probe_id in sorted(meta)
        ],
    }
    if not heldout:
        report["dev_gates"] = dev_gates(outcomes)
        if logs.get(HISTORY_ARM):
            report["dev_gates"]["solvable"]["spent"] = usage_totals(logs[HISTORY_ARM])
    return report


def summary_lines(report: dict) -> list[str]:
    """The lines ``membench outcome report`` prints beyond the pairs and units: status, agreement, windows and gates."""
    agreement = report["judge_self_agreement"]
    lines = [
        f"status: {report['status']}",
        "judge agreement on correct: "
        + (
            "unknown"
            if agreement["rate"] is None
            else f"{agreement['agree']}/{agreement['items']} = {agreement['rate']:.2f}"
        ),
        f"parser cross-check: {report['parser_cross_check']}",
    ]
    for invalid in report["windows"]["invalid"]:
        lines.append(
            f"invalid window {invalid['window']} of {invalid['project']}: {invalid['errors']}"
        )
    for unfinished in report["windows"]["unfinished"]:
        lines.append(
            f"unfinished window {unfinished['window']} of {unfinished['project']}: run the command again"
        )
    for skipped in report["skipped_answers"]:
        lines.append(
            f"skipped {skipped['arm']} answer to {skipped['probe_id']} of {skipped['project']}: {skipped['reason']}"
        )
    for name, gate in report.get("dev_gates", {}).items():
        if "status" in gate:
            lines.append(f"gate {name}: {gate['status']}")
        else:
            lines.append(
                f"gate {name}: rate {gate['rate']} against {gate['floor']} "
                f"({'meets' if gate['meets'] else 'below'}), failing probes {gate['failing_probes']}"
            )
    return lines
