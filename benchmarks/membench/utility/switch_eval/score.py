"""Scores, measures, the five rules and the chain checks.

Everything here recomputes from the raw records: the session records, the raw
verdicts and the arm key. ``score`` writes its result and ``score --check``
recomputes it and compares, which is the protocol's last chain check.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path

import numpy as np

from . import protocol, stats
from .arms import sha256_text
from .cases import KIND_MISS, KIND_ORDINARY, SYNTHETIC_POSITIVE, SYNTHETIC_TWIN, Case
from .judge import build_items, read_items
from .session import ACTOR_FAILURE, HARNESS_FAULT, OK, STRUCK, load_session
from .state import Layout, load_arm_state, tree_digest


class ChainError(RuntimeError):
    """A chain check failed; the stage that needed it does not run."""


# --------------------------------------------------------------------------
# Chain checks.
# --------------------------------------------------------------------------


def read_power(
    path: Path, *, design: Sequence[stats.DesignCase], build: Mapping
) -> tuple[dict[int, float], list[int], str]:
    """Rules 2 and 3's δ, the rules short at the cap and the file's digest,
    from the power step's ``power.json``. Refused unless the figures were
    computed for this run's design and measured on this run's build."""

    text = Path(path).read_text(encoding="utf-8")
    recorded = json.loads(text)
    if recorded.get("design_digest") != stats.design_digest(design):
        raise ChainError(
            f"{path} was computed for another design: its groups, sizes or strata "
            "differ from this run's"
        )
    if recorded.get("build") != build:
        raise ChainError(
            f"{path} was measured on another build: its harness or arm hashes differ "
            "from this run's"
        )
    rules = recorded["rules"]
    deltas = {rule: rules[str(rule)]["delta"] for rule in (2, 3)}
    short = [int(rule) for rule, values in sorted(rules.items()) if values["short"]]
    return deltas, short, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _page_text(vault: Path, ref: str) -> str | None:
    path = vault / ref
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def check_trees(
    layout: Layout, *, cases: Sequence[Case], trees: Mapping[str, Mapping], arm_ids: Sequence[str]
) -> list[str]:
    """The snapshot digest and the removals, for every tree and every arm built on it."""

    problems: list[str] = []
    for name, tree in trees.items():
        vault = Path(tree["path"])
        digest = tree_digest(vault)
        if tree.get("digest") and digest != tree["digest"]:
            problems.append(f"tree {name}: digest {digest} is not the recorded {tree['digest']}")
        for arm in arm_ids:
            record = load_arm_state(layout, arm, name)
            if record is not None and record["snapshot_digest"] != digest:
                problems.append(
                    f"tree {name}: arm {arm} was built from another copy ({record['snapshot_digest']})"
                )
        for case in (c for c in cases if c.tree == name):
            for ref in case.excluded_refs:
                if (vault / ref).exists():
                    problems.append(f"case {case.case_id}: excluded ref still present ({ref})")
            for ref, lines in case.removed_lines.items():
                text = _page_text(vault, ref)
                kept = [line for line in lines if text is not None and line in text.splitlines()]
                if kept:
                    problems.append(
                        f"case {case.case_id}: {len(kept)} removed lines still in {ref}"
                    )
    return problems


def check_sessions(
    layout: Layout, *, plan: Sequence[Mapping], arm_hashes: Mapping[str, Mapping]
) -> tuple[list[str], dict[str, dict]]:
    """Every planned session present with its outcome named, and its captured
    inputs against the arm's frozen hashes."""

    problems: list[str] = []
    sessions: dict[str, dict] = {}
    for planned in plan:
        record = load_session(
            layout.session_dir(planned["case_id"], planned["arm"], planned["repeat"])
        )
        if record is None:
            problems.append(f"session {planned['session_id']}: no final record")
            continue
        sessions[planned["session_id"]] = record
        planned_entry = {k: planned[k] for k in ("session_id", "case_id", "arm", "repeat")}
        if {k: record.get(k) for k in planned_entry} != planned_entry:
            problems.append(f"session {planned['session_id']}: its record names another session")
        if record["outcome"] != OK and not record["kind"]:
            problems.append(f"session {planned['session_id']}: a failure without its kind")
        if record["outcome"] == OK:
            failed = [name for name, value in record["checks"].items() if value is False]
            if failed:
                problems.append(
                    f"session {planned['session_id']}: surface checks failed: {', '.join(failed)}"
                )
        inputs = layout.arm_dir(record["arm"]) / "session-inputs" / "instructions.md"
        frozen = arm_hashes[record["arm"]]["components"]["client_instructions"]
        if sha256_text(inputs.read_text(encoding="utf-8")) != frozen:
            problems.append(
                f"session {planned['session_id']}: client instructions differ from the arm hash"
            )
    return problems, sessions


def check_judge_inputs(
    layout: Layout,
    *,
    cases: Sequence[Case],
    sessions: Sequence[Mapping],
    seed: int,
    inputs_digest: str,
    tool_names: Sequence[str],
) -> list[str]:
    """The written judge items against items rebuilt from the captured answers."""

    built = build_items(
        cases, sessions, seed=seed, inputs_digest=inputs_digest, tool_names=tool_names
    )
    rebuilt = {item["item_id"]: item for item in built.items}
    written = {item["item_id"]: item for item in read_items(layout)}
    if rebuilt != written:
        differing = sum(
            1 for key in set(rebuilt) | set(written) if rebuilt.get(key) != written.get(key)
        )
        return [f"{differing} judge items differ from the captured answers"]
    return []


# --------------------------------------------------------------------------
# Scores.
# --------------------------------------------------------------------------


def repeat_outcome(record: Mapping, verdict: Mapping | None) -> dict:
    """One repeat's primary score and contamination, from its record and verdict."""

    if record["outcome"] != OK:
        # An actor failure scores 0 and stays in the denominator; with no
        # answer there is nothing to contaminate.
        return {"score": 0.0, "contaminated": False, "failure": record["kind"]}
    if verdict is None:
        raise ChainError(f"session {record['session_id']} has an answer but no standing verdict")
    passes = all(value == "used" for value in verdict["required"].values()) and not any(
        value == "asserted_as_current" for value in verdict["forbidden"].values()
    )
    return {
        "score": 1.0 if passes else 0.0,
        "contaminated": bool(verdict.get("contaminated")),
        "failure": None,
    }


def _majority(flags: Sequence[bool], repeats: int) -> bool:
    return sum(flags) * 2 > repeats


def _delivered_refs(session_dir: Path, record: Mapping, vault_pages: Sequence[str]) -> set[str]:
    """The vault pages a session's pushes and served pulls named, up to the answer.

    A ref is a vault-relative page path, the key's own ref shape, matched by
    identity against the tree's page list.
    """

    texts: list[str] = [
        hook["push"] for turn in record["turns"] for hook in turn.get("hook_responses") or []
    ]
    attempt = session_dir / f"attempt-{record['attempts']}" / "runtime" / "tap.jsonl"
    if attempt.exists():
        for raw in attempt.read_text(encoding="utf-8").splitlines():
            event = json.loads(raw)
            if event.get("event") == "call" and event.get("outcome") == "served":
                texts.extend(_strings(event.get("result")))
    joined = "\n".join(texts)
    return {page for page in vault_pages if page in joined}


def _strings(value: object) -> list[str]:
    """Every string leaf, with JSON text unpacked, so an escaped path still reads as itself."""

    if isinstance(value, str):
        try:
            inner = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return [value]
        return _strings(inner) if isinstance(inner, (dict, list)) else [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _vault_pages(layout: Layout, arm: str, tree: str) -> list[str]:
    vault = layout.arm_tree(arm, tree) / "vault"
    if not vault.exists():
        return []
    return sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*.md") if p.is_file())


def _attempts(layout: Layout, record: Mapping) -> list[dict] | None:
    """Every attempt the session logged, retried harness faults included, or
    ``None`` when the log is missing or does not hold the attempts its final
    record counts: then what those attempts used is unknown, not zero."""

    if record["outcome"] == STRUCK and record["attempts"] == 0:
        # The runner skips the sessions of a struck case: none ran, so no log exists.
        return []
    path = layout.session_dir(record["case_id"], record["arm"], record["repeat"]) / "attempts.jsonl"
    if not path.exists():
        return None
    logged = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return logged if len(logged) == record["attempts"] else None


def case_table(
    layout: Layout,
    *,
    cases: Sequence[Case],
    sessions: Mapping[str, Mapping],
    verdicts: Mapping[str, Mapping],
    arm_ids: Sequence[str],
    repeats: int,
) -> dict:
    """Per case and arm: repeat scores, the case score, contamination and measures."""

    struck = sorted(
        {
            record["case_id"]
            for record in sessions.values()
            if record["outcome"] in (HARNESS_FAULT, STRUCK)
        }
    )
    pages: dict[tuple[str, str], list[str]] = {}
    table: dict[str, dict] = {}
    for case in cases:
        if case.case_id in struck:
            continue
        expected = case.expected_refs()
        row: dict[str, dict] = {}
        for arm in arm_ids:
            records = sorted(
                (r for r in sessions.values() if r["case_id"] == case.case_id and r["arm"] == arm),
                key=lambda r: r["repeat"],
            )
            # A repeat without its final record may be a failure; scoring
            # without it would lift the arm's score (design, "Actor failures").
            if len(records) != repeats:
                raise ChainError(
                    f"case {case.case_id} arm {arm}: {len(records)} of {repeats} repeats have a final record"
                )
            key = (arm, case.tree)
            if key not in pages:
                pages[key] = _vault_pages(layout, arm, case.tree)
            outcomes = [repeat_outcome(r, verdicts.get(r["session_id"])) for r in records]
            reached, off_target = [], []
            for record in records:
                delivered = _delivered_refs(
                    layout.session_dir(case.case_id, arm, record["repeat"]), record, pages[key]
                )
                reached.append(len(delivered & expected) / len(expected) if expected else None)
                off_target.append(len(delivered - expected) / len(delivered) if delivered else None)

            def mean(values: Sequence[float | None]) -> float | None:
                present = [v for v in values if v is not None]
                return sum(present) / len(present) if present else None

            turns = [t for r in records for t in r["turns"]]
            logged = [_attempts(layout, record) for record in records]
            row[arm] = {
                "repeats": [o["score"] for o in outcomes],
                "score": sum(o["score"] for o in outcomes) / len(outcomes),
                "passes_majority": _majority([o["score"] == 1.0 for o in outcomes], repeats),
                "fails_majority": _majority([o["score"] < 1.0 for o in outcomes], repeats),
                "contaminated": _majority([o["contaminated"] for o in outcomes], repeats)
                if case.no_memory
                else None,
                "failures": [o["failure"] for o in outcomes if o["failure"]],
                "harness_faults": None
                if any(attempts is None for attempts in logged)
                else [
                    attempt["kind"]
                    for attempts in logged
                    for attempt in attempts
                    if attempt["outcome"] == HARNESS_FAULT
                ],
                "calls": sum(t["calls"] for t in turns) / len(records),
                "repeat_calls": [sum(t["calls"] for t in r["turns"]) for r in records],
                "user_turns": len(turns),
                "turns_with_activation": sum(1 for t in turns if t["activation_calls"]),
                "input_tokens": sum(t["input_tokens"] for t in turns) / len(records),
                "output_tokens": sum(t["output_tokens"] for t in turns) / len(records),
                "push_bytes": sum(t["push_bytes"] for t in turns) / len(records),
                "wall_seconds": sum(t["wall_seconds"] for t in turns) / len(records),
                "expected_reached": mean(reached),
                "off_target_share": mean(off_target),
            }
        table[case.case_id] = row
    return {"cases": table, "struck": struck}


# --------------------------------------------------------------------------
# The rules.
# --------------------------------------------------------------------------


def _differences(
    table: Mapping, case_ids: Sequence[str], first: str, second: str, field: str = "score"
) -> dict[str, float]:
    return {
        case_id: table[case_id][first][field] - table[case_id][second][field]
        for case_id in case_ids
        if case_id in table and first in table[case_id] and second in table[case_id]
    }


def _not_evaluable(reason: str) -> dict:
    return {"evaluable": False, "holds": False, "reason": reason, "inconclusive": []}


def _inconclusive(rule: dict, number: int, *, short: Collection[int]) -> dict:
    """A tested rule's reasons to read as inconclusive rather than as held or
    failed (design, "Groups" and "Power")."""

    reasons: list[str] = []
    if rule["evaluable"]:
        if rule["groups"] < protocol.MIN_GROUPS:
            reasons.append(
                f"rule {number}: {rule['groups']} groups, fewer than {protocol.MIN_GROUPS}"
            )
        if not rule["holds"] and number in short:
            reasons.append(
                f"rule {number}: failed, and the power step recorded it short of "
                f"{protocol.POWER_TARGET:.0%} at the cap"
            )
    return {**rule, "inconclusive": reasons}


def rules(
    *,
    cases: Sequence[Case],
    table: Mapping,
    split: str,
    deltas: Mapping[int, float],
    seed: int,
    arm_ids: Sequence[str],
    short: Collection[int] = (),
) -> dict:
    """The five rules (design, "Rule"). A rule without its arms or cases is not
    evaluable, and a rule that is not evaluable does not hold. A tested rule
    with too few groups, or a failed rule in ``short`` (the rules the power
    step recorded short at the cap), reads as inconclusive."""

    groups = {case.case_id: case.group for case in cases}
    in_split = [c for c in cases if c.split == split and c.case_id in table]
    miss = [c.case_id for c in in_split if c.kind == KIND_MISS]
    ordinary = [c.case_id for c in in_split if c.kind == KIND_ORDINARY]
    # Rule 4's set (design, "Cases and key"): the split's miss cases and
    # ordinary turns whose key expects no memory, plus the negative twins.
    no_memory = [
        c.case_id
        for c in cases
        if c.case_id in table
        and c.no_memory
        and (
            (c.split == split and c.kind in (KIND_MISS, KIND_ORDINARY)) or c.kind == SYNTHETIC_TWIN
        )
    ]
    # A positive with no required item has no gold to lose, so rule 5 cannot
    # read it; the output names it rather than counting it as kept.
    synthetic = [c for c in cases if c.case_id in table and c.kind == SYNTHETIC_POSITIVE]
    positives = [c.case_id for c in synthetic if c.required]
    no_gold = sorted(c.case_id for c in synthetic if not c.required)
    has = set(arm_ids).__contains__
    out: dict[str, dict] = {}

    # Rule 1 pools the cases that expect memory: the miss cases and the
    # ordinary turns, each in their groups (amendment 1).
    memory = {
        kind: [c.case_id for c in in_split if c.kind == kind and c.expects_memory]
        for kind in (KIND_MISS, KIND_ORDINARY)
    }
    expecting = memory[KIND_MISS] + memory[KIND_ORDINARY]
    if not (has("M") and has("A")):
        out["1"] = _not_evaluable("arms M and A are not both in the run")
    elif not expecting:
        out["1"] = _not_evaluable(f"no {split} cases that expect memory")
    else:
        differences = _differences(table, expecting, "M", "A")
        test = stats.compare(differences, groups, seed=seed)
        # The miss stratum was selected for A's failure, so it inflates M - A;
        # the report gives each stratum with its share of the pool.
        strata = {
            kind: {
                "cases": len(ids),
                "share": len(ids) / len(expecting),
                "mean_difference": _mean([differences.get(c) for c in ids]),
            }
            for kind, ids in memory.items()
        }
        out["1"] = {
            "evaluable": True,
            "holds": test["superiority"]["passes"],
            "cases": len(expecting),
            "groups": test["superiority"]["groups"],
            "strata": strata,
            "M_minus_A": test,
        }

    if not (has("M") and has("A")):
        out["2"] = _not_evaluable("arms M and A are not both in the run")
    elif not ordinary:
        out["2"] = _not_evaluable(f"no {split} ordinary turns")
    else:
        test = stats.compare(
            _differences(table, ordinary, "M", "A"), groups, seed=seed, delta=deltas[2]
        )
        out["2"] = {
            "evaluable": True,
            "holds": test["non_inferiority"]["passes"],
            "cases": len(ordinary),
            "groups": test["non_inferiority"]["groups"],
            "M_minus_A": test,
        }

    pooled = miss + ordinary
    if not (has("M") and has("P")):
        out["3"] = _not_evaluable("arms M and P are not both in the run")
    elif not pooled:
        out["3"] = _not_evaluable(f"no {split} miss cases or ordinary turns")
    else:
        score = stats.compare(
            _differences(table, pooled, "M", "P"), groups, seed=seed, delta=deltas[3]
        )
        # Fewer calls is better: the difference is P's calls minus M's. M's
        # calls include the activate_context call that fetches its map.
        calls = stats.compare(_differences(table, pooled, "P", "M", "calls"), groups, seed=seed)
        # M beats P, or M is non-inferior and makes fewer calls (amendment 3).
        beats = score["superiority"]["passes"]
        cheaper = score["non_inferiority"]["passes"] and calls["superiority"]["passes"]
        out["3"] = {
            "evaluable": True,
            "holds": bool(beats or cheaper),
            "path": "superiority"
            if beats
            else ("non-inferior with fewer calls" if cheaper else None),
            "cases": len(pooled),
            "groups": score["superiority"]["groups"],
            "M_minus_P": score,
            "P_calls_minus_M_calls": calls,
        }

    if not (has("M") and has("A")):
        out["4"] = _not_evaluable("arms M and A are not both in the run")
    elif not no_memory:
        out["4"] = _not_evaluable("no no-memory cases")
    else:
        counts = {
            arm: sum(bool(table[c][arm]["contaminated"]) for c in no_memory if arm in table[c])
            for arm in ("M", "A")
        }
        out["4"] = {
            "evaluable": True,
            "holds": counts["M"] <= counts["A"] + protocol.CONTAMINATION_TOLERANCE,
            "cases": len(no_memory),
            "contaminated": counts,
            "inconclusive": [],
        }

    if not (has("M") and has("A")):
        out["5"] = _not_evaluable("arms M and A are not both in the run")
    elif not positives:
        out["5"] = {
            **_not_evaluable("no synthetic positive cases with gold"),
            "excluded_no_gold": no_gold,
        }
    else:
        losses = [
            c
            for c in positives
            if table[c]["A"]["passes_majority"] and table[c]["M"]["fails_majority"]
        ]
        out["5"] = {
            "evaluable": True,
            "holds": not losses,
            "cases": len(positives),
            "losses": sorted(losses),
            "excluded_no_gold": no_gold,
            "inconclusive": [],
        }

    for number in (1, 2, 3):
        out[str(number)] = _inconclusive(out[str(number)], number, short=short)
    out["switch"] = _switch(out)
    return out


def _switch(decided: Mapping) -> bool:
    return all(
        decided[str(n)]["holds"] and not decided[str(n)]["inconclusive"] for n in range(1, 6)
    )


def decide(decided: Mapping, *, void: bool, split: str, audited: bool) -> str:
    """What the rules decide. A switch needs all five, so one rule that fails
    conclusively keeps anchors; otherwise a rule that is not evaluable or reads
    as inconclusive leaves the decision open and says why. The product default
    stays anchors either way."""

    numbered = [decided[str(n)] for n in range(1, 6)]
    if any(r["evaluable"] and not r["holds"] and not r["inconclusive"] for r in numbered):
        return "keep anchors"
    unknown = sorted({r["reason"] for r in numbered if not r["evaluable"]})
    unsure = [reason for r in numbered for reason in r["inconclusive"]]
    open_parts = [
        *(["not evaluable: " + "; ".join(unknown)] if unknown else []),
        *(["inconclusive: " + "; ".join(unsure)] if unsure else []),
    ]
    if open_parts:
        return "; ".join(open_parts)
    if void or split != protocol.SPLIT_HELDOUT:
        return "keep anchors"
    # Without the audit, no rule has been checked for a verdict that the
    # judge's arm leakage carried; the cost of waiting is one audit stage.
    return "switch to maps" if audited else "inconclusive: the arm-guess audit has not run"


# --------------------------------------------------------------------------
# Secondary measures and usage.
# --------------------------------------------------------------------------

_MEASURES = (
    "calls",
    "input_tokens",
    "output_tokens",
    "push_bytes",
    "wall_seconds",
    "expected_reached",
    "off_target_share",
)


def secondary(*, cases: Sequence[Case], table: Mapping, arm_ids: Sequence[str]) -> dict:
    """Per arm, means over cases, split by turn completeness, and failures by kind."""

    by_id = {case.case_id: case for case in cases}
    out: dict[str, dict] = {}
    for arm in arm_ids:
        rows = [(by_id[c], row[arm]) for c, row in table.items() if arm in row]
        per_completeness: dict[str, dict] = {}
        for completeness in sorted({case.turn_completeness for case, _ in rows}):
            subset = [r for case, r in rows if case.turn_completeness == completeness]
            per_completeness[completeness] = {
                "cases": len(subset),
                "score": sum(r["score"] for r in subset) / len(subset),
                **{m: _mean([r[m] for r in subset]) for m in _MEASURES},
            }
        user_turns = sum(row["user_turns"] for _, row in rows)
        activated = sum(row["turns_with_activation"] for _, row in rows)
        out[arm] = {
            "by_turn_completeness": per_completeness,
            # M's map reaches the actor only when it obeys the reminder.
            "activation_share": {
                "user_turns": user_turns,
                "with_activation": activated,
                "share": activated / user_turns if user_turns else None,
            },
            # Final outcomes that score 0, and every retried attempt's fault;
            # unknown while any session's attempt log is missing.
            "failures_by_kind": _counts(kind for _, row in rows for kind in row["failures"]),
            "harness_faults_by_kind": None
            if any(row["harness_faults"] is None for _, row in rows)
            else _counts(kind for _, row in rows for kind in row["harness_faults"]),
        }
    return out


def _counts(kinds: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def usage(layout: Layout, sessions: Mapping[str, Mapping], *, project_sessions: int | None) -> dict:
    """Measured usage per session over every attempt, and the projection for a
    planned run size. ``wall_seconds`` excludes the gate; ``ready_wait_seconds``
    reports that setup cost separately."""

    records = list(sessions.values())
    if not records:
        return {"sessions": 0}
    logged = [_attempts(layout, record) for record in records]
    unlogged = sum(1 for attempts in logged if attempts is None)
    if unlogged:
        return {
            "sessions": len(records),
            "state": f"unavailable: {unlogged} sessions lack the attempt log their record counts",
            "totals": None,
            "per_session": None,
        }
    attempts = [attempt for attempts in logged for attempt in attempts]
    turns = [turn for attempt in attempts for turn in attempt["turns"]]
    waits = [turn.get("ready_wait_seconds") for turn in turns]
    totals = {
        "input_tokens": sum(t["input_tokens"] for t in turns),
        "output_tokens": sum(t["output_tokens"] for t in turns),
        "wall_seconds": sum(t["wall_seconds"] for t in turns),
        "ready_wait_seconds": sum(waits) if all(w is not None for w in waits) else None,
        "attempts": len(attempts),
    }
    per_session = {
        key: value / len(records) if value is not None else None for key, value in totals.items()
    }
    out = {
        "sessions": len(records),
        "state": "complete",
        "totals": totals,
        "per_session": per_session,
    }
    if project_sessions:
        out["projected"] = {
            "sessions": project_sessions,
            **{k: v * project_sessions if v is not None else None for k, v in per_session.items()},
        }
    return out


#: The confounds the protocol tells the report to name (design, "Decision and
#: arms", "Pull budget" and "Secondary measures").
CONFOUNDS: tuple[str, ...] = (
    f"Pull budget: A and M have {protocol.PULL_BUDGET_PER_TURN - 1} Exomem calls left after "
    f"their {protocol.ACTIVATION_TOOL} call, and P has {protocol.PULL_BUDGET_PER_TURN}.",
    f"Call counts: M's count includes the {protocol.ACTIVATION_TOOL} call that fetches its map, "
    "and P has no such call.",
    f"P lacks {protocol.P_FILTERED_TOOL} through a client-side tool filter, while the shared "
    "server instructions still mention it.",
    "M - A changes both the packet and the client instructions.",
)


#: Harness knob: bootstrap draws for the audit's guess-accuracy interval.
BOOTSTRAP_DRAWS = 2000


def _verdict_core(verdict: Mapping) -> tuple:
    return (verdict["required"], verdict["forbidden"], verdict["contaminated"])


def arm_guess_audit(
    *,
    cases: Sequence[Case],
    sessions: Mapping[str, Mapping],
    verdicts: Mapping[str, Mapping],
    audit: Mapping,
    decided: Mapping,
    redone: Mapping,
    seed: int,
) -> dict:
    """Guess accuracy against chance with a group-clustered bootstrap interval,
    the confusion table, the share of verdicts that the re-judge changed on the
    flagged items and on the controls (the noise baseline), and the rules whose
    verdict flips when the flagged items' re-judged verdicts stand."""

    group_of = {case.case_id: case.group for case in cases}
    guesses = audit["guesses"]
    by_group: dict[str, list[bool]] = {}
    confusion = {arm: dict.fromkeys(protocol.ARM_IDS, 0) for arm in protocol.ARM_IDS}
    for session_id, guess in guesses.items():
        session = sessions[session_id]
        by_group.setdefault(group_of[session["case_id"]], []).append(
            guess["arm_guess"] == session["arm"]
        )
        confusion[session["arm"]][guess["arm_guess"]] += 1
    labels = sorted(by_group)
    hits = np.array([sum(by_group[g]) for g in labels], dtype=float)
    sizes = np.array([len(by_group[g]) for g in labels], dtype=float)
    interval = None
    if labels:
        draws = np.random.default_rng(seed).integers(
            0, len(labels), size=(BOOTSTRAP_DRAWS, len(labels))
        )
        rates = hits[draws].sum(axis=1) / sizes[draws].sum(axis=1)
        interval = [float(q) for q in np.quantile(rates, [0.025, 0.975])]

    def changed(rejudged: Mapping[str, Mapping]) -> dict:
        moved = sum(_verdict_core(v) != _verdict_core(verdicts[s]) for s, v in rejudged.items())
        return {"items": len(rejudged), "changed_verdicts": moved}

    flagged, controls = changed(audit["flagged"]), changed(audit["controls"])
    # The controls are the noise baseline that the flagged items' changes are read against.
    baseline = "complete" if controls["items"] >= flagged["items"] else "incomplete"
    return {
        "state": "complete"
        if baseline == "complete"
        else f"incomplete: {controls['items']} controls for {flagged['items']} flagged items",
        "items": len(guesses),
        "groups": len(labels),
        "accuracy": float(hits.sum() / sizes.sum()) if labels else None,
        "chance": 1 / len(protocol.ARM_IDS),
        "interval_95": interval,
        "bootstrap": {"draws": BOOTSTRAP_DRAWS, "seed": seed, "clustered_by": "group"},
        "confusion": confusion,
        "evidence_kinds": _counts(guess["evidence_kind"] for guess in guesses.values()),
        "flagged": flagged,
        "controls": {**controls, "baseline": baseline},
        "rules_rejudged": {n: redone[n]["holds"] for n in ("1", "2", "3", "4", "5")},
        "flips": [
            n for n in ("1", "2", "3", "4", "5") if decided[n]["holds"] != redone[n]["holds"]
        ],
    }


def score_run(
    layout: Layout,
    *,
    cases: Sequence[Case],
    sessions: Mapping[str, Mapping],
    verdicts: Mapping[str, Mapping],
    arm_ids: Sequence[str],
    repeats: int,
    split: str,
    deltas: Mapping[int, float],
    seed: int,
    project_sessions: int | None = None,
    short: Collection[int] = (),
    audit: Mapping | None = None,
) -> dict:
    """Score the run. ``audit`` is what ``audit.load_audit`` reads, or ``None``
    when the arm-guess audit has not run."""

    for rule, delta in deltas.items():
        if not protocol.DELTA_FLOOR <= delta <= protocol.DELTA_CAP:
            raise ValueError(
                f"rule {rule}'s delta {delta} is outside the protocol's {protocol.DELTA_FLOOR}..{protocol.DELTA_CAP}"
            )
    built = case_table(
        layout, cases=cases, sessions=sessions, verdicts=verdicts, arm_ids=arm_ids, repeats=repeats
    )
    void = len(built["struck"]) > protocol.MAX_STRUCK_CASES
    rule_inputs = {
        "cases": cases,
        "split": split,
        "deltas": deltas,
        "seed": seed,
        "arm_ids": arm_ids,
        "short": short,
    }
    decided = rules(table=built["cases"], **rule_inputs)
    audited: dict = {"state": "not run"}
    if audit is not None:
        replaced = {**verdicts, **audit["flagged"]}
        rejudged = case_table(
            layout,
            cases=cases,
            sessions=sessions,
            verdicts=replaced,
            arm_ids=arm_ids,
            repeats=repeats,
        )
        audited = arm_guess_audit(
            cases=cases,
            sessions=sessions,
            verdicts=verdicts,
            audit=audit,
            decided=decided,
            redone=rules(table=rejudged["cases"], **rule_inputs),
            seed=seed,
        )
        for number in audited["flips"]:
            decided[number]["inconclusive"].append(
                f"rule {number}: its verdict flips when the arm-guess audit's flagged items "
                "are re-judged without their spans"
            )
        decided["switch"] = _switch(decided)
    return {
        "split": split,
        "diagnostic": split != protocol.SPLIT_HELDOUT,
        "void": void,
        "struck": built["struck"],
        "rules": decided,
        "decision": decide(decided, void=void, split=split, audited=audit is not None),
        "arm_guess_audit": audited,
        "secondary": secondary(cases=cases, table=built["cases"], arm_ids=arm_ids),
        "usage": usage(layout, sessions, project_sessions=project_sessions),
        "cases": built["cases"],
        "actor_failures": sum(1 for r in sessions.values() if r["outcome"] == ACTOR_FAILURE),
        "deltas": {str(k): v for k, v in deltas.items()},
        "seed": seed,
        "confounds": CONFOUNDS,
    }
