"""Scores, measures, the five rules and the chain checks.

Everything here recomputes from the raw records: the session records, the raw
verdicts and the arm key. ``score`` writes its result and ``score --check``
recomputes it and compares, which is the protocol's last chain check.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

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
) -> list[str]:
    """The written judge items against items rebuilt from the captured answers."""

    rebuilt = {
        item["item_id"]: item
        for item in build_items(cases, sessions, seed=seed, inputs_digest=inputs_digest).items
    }
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
            if not records:
                continue
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
            row[arm] = {
                "repeats": [o["score"] for o in outcomes],
                "score": sum(o["score"] for o in outcomes) / len(outcomes),
                "passes_majority": _majority([o["score"] == 1.0 for o in outcomes], repeats),
                "fails_majority": _majority([o["score"] < 1.0 for o in outcomes], repeats),
                "contaminated": _majority([o["contaminated"] for o in outcomes], repeats)
                if case.no_memory
                else None,
                "failures": [o["failure"] for o in outcomes if o["failure"]],
                "calls": sum(t["calls"] for t in turns) / len(records),
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
    return {"evaluable": False, "holds": False, "reason": reason}


def rules(
    *,
    cases: Sequence[Case],
    table: Mapping,
    split: str,
    deltas: Mapping[int, float],
    seed: int,
    arm_ids: Sequence[str],
) -> dict:
    """The five rules (design, "Rule"). A rule without its arms or cases is not
    evaluable, and a rule that is not evaluable does not hold."""

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
        # Fewer calls is better: the difference is P's calls minus M's.
        calls = stats.compare(_differences(table, pooled, "P", "M", "calls"), groups, seed=seed)
        pays = score["superiority"]["passes"] or score["non_inferiority"]["passes"]
        out["3"] = {
            "evaluable": True,
            "holds": bool(pays and calls["superiority"]["passes"]),
            "cases": len(pooled),
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
        }

    out["switch"] = all(out[str(n)]["holds"] for n in range(1, 6))
    return out


def decide(decided: Mapping, *, void: bool, split: str) -> str:
    """What the rules decide. A switch needs all five, so one evaluable rule that
    fails keeps anchors; otherwise a rule that is not evaluable leaves the
    decision unknown and says why. The product default stays anchors either way."""

    numbered = [decided[str(n)] for n in range(1, 6)]
    if any(rule["evaluable"] and not rule["holds"] for rule in numbered):
        return "keep anchors"
    unknown = sorted({rule["reason"] for rule in numbered if not rule["evaluable"]})
    if unknown:
        return "not evaluable: " + "; ".join(unknown)
    return "switch to maps" if not void and split == protocol.SPLIT_HELDOUT else "keep anchors"


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
        failures: dict[str, int] = {}
        for _, row in rows:
            for kind in row["failures"]:
                failures[kind] = failures.get(kind, 0) + 1
        out[arm] = {"by_turn_completeness": per_completeness, "failures_by_kind": failures}
    return out


def _mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def usage(sessions: Mapping[str, Mapping], *, project_sessions: int | None) -> dict:
    """Measured usage per session, and the projection for a planned run size."""

    records = list(sessions.values())
    if not records:
        return {"sessions": 0}
    totals = {
        "input_tokens": sum(t["input_tokens"] for r in records for t in r["turns"]),
        "output_tokens": sum(t["output_tokens"] for r in records for t in r["turns"]),
        "wall_seconds": sum(t["wall_seconds"] for r in records for t in r["turns"]),
        "attempts": sum(r["attempts"] for r in records),
    }
    per_session = {key: value / len(records) for key, value in totals.items()}
    out = {"sessions": len(records), "totals": totals, "per_session": per_session}
    if project_sessions:
        out["projected"] = {
            "sessions": project_sessions,
            **{k: v * project_sessions for k, v in per_session.items()},
        }
    return out


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
) -> dict:
    for rule, delta in deltas.items():
        if not protocol.DELTA_FLOOR <= delta <= protocol.DELTA_CAP:
            raise ValueError(
                f"rule {rule}'s delta {delta} is outside the protocol's {protocol.DELTA_FLOOR}..{protocol.DELTA_CAP}"
            )
    built = case_table(
        layout, cases=cases, sessions=sessions, verdicts=verdicts, arm_ids=arm_ids, repeats=repeats
    )
    void = len(built["struck"]) > protocol.MAX_STRUCK_CASES
    decided = rules(
        cases=cases, table=built["cases"], split=split, deltas=deltas, seed=seed, arm_ids=arm_ids
    )
    return {
        "split": split,
        "diagnostic": split != protocol.SPLIT_HELDOUT,
        "void": void,
        "struck": built["struck"],
        "rules": decided,
        "decision": decide(decided, void=void, split=split),
        "secondary": secondary(cases=cases, table=built["cases"], arm_ids=arm_ids),
        "usage": usage(sessions, project_sessions=project_sessions),
        "cases": built["cases"],
        "actor_failures": sum(1 for r in sessions.values() if r["outcome"] == ACTOR_FAILURE),
        "deltas": {str(k): v for k, v in deltas.items()},
        "seed": seed,
    }
