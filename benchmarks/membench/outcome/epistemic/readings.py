"""What the epistemic report says about a family, a lineage group and a background norm.

``report.py`` scores arms; this module reads the scores. A family (and the pooled change probes) reads one of
``readings.yaml``'s readings, and prints each arm's position only where its rule allows. A position is
(arm - N) / (G - N): the arm's place between the no-memory floor and the gold-evidence ceiling. Harm probes
have no floor, so they take no part in a position; they and the false-alarm probes are read apart.
The rules, and the numbers they read, are decision 6 of the change's design.md and ``readings.yaml``.

Unknown, partial and not measured are states of their own: a count that cannot be formed, an arm that did not
play and a family that no probe sampled are named, never 0.
"""

from __future__ import annotations

import functools
import statistics
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ...world.loader import Catalogue, load_catalogue
from ..stats import paired_interval
from .cases import GOLD_ARM, families, is_harm, is_stable, lineage_sources
from .judge import behaviours_asked

Unit = tuple[str, str]  # (project, probe id)

READINGS_FILE = Path(__file__).with_name("readings.yaml")

# The readings are a closed set that design decision 6 fixes and this module implements, one rule each; they are no
# vocabulary that a person edits. ``readings.yaml`` describes each, and ``rules()`` refuses a file that does not.
NOT_MEASURED = "not measured"
TOO_FEW = "too few probes"
NO_ROOM = "no room"
SATURATED = "saturated"
ROOM = "room"
ROOM_UNCLEAR = "room unclear"
READINGS = (NOT_MEASURED, TOO_FEW, NO_ROOM, SATURATED, ROOM, ROOM_UNCLEAR)
# The arms that a position is read between, and the arm whose prediction is filed. ``run.py`` holds the arm table.
FLOOR, CEILING, PRIMARY, FOLDER = "N", GOLD_ARM, "P", "F"
# The three outcomes that a projection may file as `predict`, a closed set that its header defines and `observed` reads.
PASS, PARTIAL, FAIL = "pass", "partial", "fail"
OUTCOMES = (PASS, PARTIAL, FAIL)


@functools.cache
def rules() -> dict[str, Any]:
    """``readings.yaml``: the thresholds and the description of each reading, refused when it lacks a reading."""
    data = yaml.safe_load(READINGS_FILE.read_text(encoding="utf-8"))
    if set(data["readings"]) != set(READINGS) or set(data["observed"]) != set(OUTCOMES):
        raise SystemExit(
            f"refused: {READINGS_FILE.name} must describe exactly {READINGS} and {OUTCOMES}"
        )
    return data


@functools.cache
def catalogue() -> Catalogue:
    return load_catalogue()


def probe_kind(probe: Mapping[str, Any]) -> str:
    """``harm`` when the key reads it so, ``stable`` for the recall control, else ``position``."""
    return "harm" if is_harm(probe) else "stable" if is_stable(probe) else "position"


def difference(
    a: str,
    b: str,
    units: Sequence[Unit],
    unit_scores: Mapping[str, Mapping[Unit, float]],
    seed: int,
) -> tuple[list[Unit], dict]:
    """``a`` minus ``b`` over the units that both scored, with the paired interval; unknown when an arm did not play."""
    missing = next((arm for arm in (a, b) if arm not in unit_scores), None)
    if missing is not None:
        verdict = f"unknown: arm {missing} did not play"
        return [], {"paired_probes": 0, "mean_difference": None, "ci95": None, "verdict": verdict}
    paired = sorted(u for u in units if u in unit_scores[a] and u in unit_scores[b])
    diffs = [unit_scores[a][u] - unit_scores[b][u] for u in paired]
    return paired, paired_interval(diffs, seed=seed, unit="probe", strata=[u[0] for u in paired])


def above(interval: dict, bound: float) -> bool | None:
    """Whether the interval's lower bound is above ``bound``; None (unknown) when there is no interval."""
    return None if interval["ci95"] is None else interval["ci95"][0] > bound


def positions(
    position: Sequence[Unit], arms: Sequence[str], unit_scores: Mapping, seed: int
) -> dict[str, dict]:
    """Per arm other than N and G: (arm - N) / (G - N) on the probes that N, G and the arm all scored."""
    rows = {}
    for arm in arms:
        if arm in (FLOOR, CEILING):
            continue
        scored = [
            u for u in position if all(u in unit_scores.get(a, {}) for a in (arm, FLOOR, CEILING))
        ]
        _, lift = difference(arm, FLOOR, scored, unit_scores, seed)
        _, lead = difference(CEILING, FLOOR, scored, unit_scores, seed)
        value, why = None, None
        if None in (lift["mean_difference"], lead["mean_difference"]):
            why = f"unknown: {lift['verdict'] if lift['mean_difference'] is None else lead['verdict']}"
        elif lead["mean_difference"] <= 0:
            why = "unknown: the ceiling does not lead the floor on these probes"
        else:
            value = round(lift["mean_difference"] / lead["mean_difference"], 4)
        rows[arm] = {
            "paired_probes": len(scored),
            "arm_minus_n": lift,
            "g_minus_n": lead,
            "position": value,
            **({"unknown": why} if why else {}),
        }
    return rows


def false_alarms(
    units: Sequence[Unit],
    meta: Mapping[Unit, dict],
    outcomes: Sequence[dict],
    arms: Sequence[str],
    names: Sequence[str],
) -> dict:
    """Per arm, the share of answers to the probes that ask a false-alarm behaviour on which the judge finds it.

    ``names`` are the behaviours that a mutation-world probe asks (the projection's ``mutation_behaviours``): a
    probe of a twin or of a thread planted without its change asks them. An answer that is unanswered or unjudged
    is counted apart, and a behaviour that the judge rated not applicable leaves the rate's denominator.
    """
    asking = {u for u in units if set(names) & set(behaviours_asked(meta[u]))}
    rows = {arm: {"yes": 0, "no": 0, "n/a": 0, "unknown": 0} for arm in arms}
    for o in outcomes:
        unit = (o["project"], o["probe_id"])
        if unit not in asking or o["arm"] not in rows:
            continue
        if o["verdict"] is None or o["state"] == "unanswered":
            rows[o["arm"]]["unknown"] += 1
            continue
        for name in names:
            if name in behaviours_asked(meta[unit]):
                rows[o["arm"]][o["verdict"][name]] += 1
    for count in rows.values():
        decided = count["yes"] + count["no"]
        count["rate"] = round(count["yes"] / decided, 4) if decided else None
    return {"probes": len(asking), "behaviours": list(names), "arms": rows}


def read(position: Sequence[Unit], lead: dict, saturated: bool) -> str:
    """The reading of a group with ``position`` probes: ``readings.yaml`` lists the rules in the order applied."""
    threshold = rules()["thresholds"]
    if len(position) < threshold["min_position_probes"]:
        return TOO_FEW
    low_high = lead["ci95"]
    if low_high is not None and low_high[1] < threshold["room"]:
        return NO_ROOM
    if saturated:
        return SATURATED
    return ROOM if low_high is not None and low_high[0] >= threshold["room"] else ROOM_UNCLEAR


def group_row(
    units: Sequence[Unit],
    *,
    meta: Mapping[Unit, dict],
    outcomes: Sequence[dict],
    unit_scores: Mapping,
    arms: Sequence[str],
    seed: int,
    earlier: Sequence[bool | None],
    names: Sequence[str],
    harm_of: Callable[[Sequence[Unit]], dict | None],
) -> dict:
    """One group of played probes (a family, or every probe): its counts, its reading and what the reading allows."""
    threshold = rules()["thresholds"]
    kinds = {u: probe_kind(meta[u]) for u in units}
    position = [u for u in units if kinds[u] == "position"]
    row: dict[str, Any] = {
        "probes": {
            "played": len(units),
            "position": len(position),
            "harm": sum(k == "harm" for k in kinds.values()),
            "stable": sum(k == "stable" for k in kinds.values()),
        }
    }
    if not units:
        return {**row, "reading": NOT_MEASURED}
    row["false_alarm"] = false_alarms(units, meta, outcomes, arms, names)
    row["harm"] = harm_of(units)
    if len(position) < threshold["min_position_probes"]:
        return {**row, "reading": TOO_FEW}
    scored, lead = difference(CEILING, FLOOR, position, unit_scores, seed)
    _, gap = difference(PRIMARY, CEILING, position, unit_scores, seed)
    _, folder = difference(PRIMARY, FOLDER, position, unit_scores, seed)
    now = above(gap, threshold["saturation_margin"])
    needed = threshold["saturation_reports"] - 1
    saturated = (
        now is True and len(earlier[:needed]) == needed and all(e is True for e in earlier[:needed])
    )
    row.update(
        reading=read(position, lead, saturated),
        ceiling_lead={
            "scope": f"G minus N over {len(scored)} position probes that both scored",
            **lead,
        },
        saturation={
            "p_minus_g": gap,
            "above_margin": now,
            "earlier_reports_above_margin": list(earlier[:needed]),
            "reports_needed": threshold["saturation_reports"],
        },
        primary_minus_folder=folder,
    )
    if lead["ci95"] is not None and lead["ci95"][0] >= threshold["room"]:
        row["positions"] = positions(position, arms, unit_scores, seed)
    return row


def observed(row: dict) -> tuple[str | None, str | None]:
    """The outcome that one report's arms give a family, as ``(outcome, why unknown)``; ``readings.yaml`` words each."""
    if row["reading"] in (NOT_MEASURED, TOO_FEW):
        return None, f"the family reads {row['reading']}"
    at_ceiling = row["saturation"]["above_margin"]
    if at_ceiling is None:
        return None, row["saturation"]["p_minus_g"]["verdict"]
    if at_ceiling:
        return PASS, None
    folder = above(row["primary_minus_folder"], 0.0)
    if folder is None:
        return None, row["primary_minus_folder"]["verdict"]
    return (PARTIAL if folder else FAIL), None


def prediction_of(row: dict, filed: Mapping[str, Any] | None) -> dict:
    """The filed prediction beside what one report observed; ``contradicted`` is None while either side is unknown."""
    if filed is None:
        return {"predicted": None, "unknown": "the projection files no prediction for this family"}
    result, why = observed(row)
    return {
        "predicted": filed["predict"],
        "models": filed["models"],
        "observed": result,
        **({"unknown": why} if why else {}),
        "contradicted": None if result is None else result != filed["predict"],
    }


def earlier_flags(earlier: Sequence[dict], *path: str) -> list[bool | None]:
    """The saturation flag of one group in each earlier report, newest first; None where a report holds none."""
    flags = []
    for stored in earlier:
        node: Any = (stored.get("description") or {}).get("families") or {}
        for step in path:
            node = (node or {}).get(step)
        flags.append(((node or {}).get("saturation") or {}).get("above_margin"))
    return flags


def section(
    *,
    meta: Mapping[Unit, dict],
    outcomes: Sequence[dict],
    unit_scores: Mapping,
    arms: Sequence[str],
    seed: int,
    earlier: Sequence[dict],
    projection: Mapping[str, Any],
    harm_of: Callable[[Sequence[Unit]], dict | None],
) -> dict:
    """The ``families`` block: a row for each catalogue family and one for every probe, read from this report's scores.

    A family is sampled when a probe of it has an outcome. A probe whose family the catalogue lacks, or that has none,
    is in no family row; the pooled row and the counts here name them.
    """
    played = sorted({(o["project"], o["probe_id"]) for o in outcomes} & set(meta))
    registry = families()
    common = dict(
        meta=meta,
        outcomes=outcomes,
        unit_scores=unit_scores,
        arms=arms,
        seed=seed,
        names=projection["f38"]["mutation_behaviours"],
        harm_of=harm_of,
    )
    rows = {}
    for family in registry:
        units = [u for u in played if meta[u].get("family") == family]
        row = group_row(units, earlier=earlier_flags(earlier, "rows", family), **common)
        row["probes"]["in_case_set"] = sum(p.get("family") == family for p in meta.values())
        row["prediction"] = (
            prediction_of(row, projection["families"].get(family)) if units else None
        )
        rows[family] = row
    contradicted = [
        {
            "family": family,
            "predicted": row["prediction"]["predicted"],
            "observed": row["prediction"]["observed"],
            "read_first": [f"{p}:{i}" for p, i in played if meta[(p, i)].get("family") == family],
        }
        for family, row in rows.items()
        if row["prediction"] and row["prediction"].get("contradicted")
    ]
    return {
        "scope": "probes with an outcome in this run, over the case set's projects; a probe counts once however many windows it played",
        "rules": rules(),
        "earlier_reports": len(earlier),
        "pooled": group_row(played, earlier=earlier_flags(earlier, "pooled"), **common),
        "rows": rows,
        "contradicted_predictions": contradicted,
        "probes_without_family": sum("family" not in meta[u] for u in played),
        "unregistered_families": sorted(
            {meta[u]["family"] for u in played if meta[u].get("family") not in (None, *registry)}
        ),
    }


def lineage_shares(meta: Mapping[Unit, dict]) -> dict:
    """Per lineage group, the probes whose phenomenon names it, and their share of the probes of a known phenomenon.

    A phenomenon's groups are its own and its family's. A probe belongs to each of its groups, so the shares need
    not add to 1. A probe with no known phenomenon is counted apart: its lineage is unknown, not none.
    """
    entries = catalogue().entries
    known = [u for u in meta if meta[u].get("phenomenon") in entries]
    groups = {key: 0 for key in lineage_sources()}
    none = 0
    for u in known:
        lineage = entries[meta[u]["phenomenon"]].lineage
        none += not lineage
        for key in lineage:
            groups[key] += 1

    def share(count: int) -> float | None:
        return round(count / len(known), 4) if known else None

    return {
        "scope": "probes of the case set whose phenomenon the catalogue knows; a probe belongs to every group of its phenomenon and its family",
        "probes": len(known),
        "unknown_phenomenon": len(meta) - len(known),
        "groups": {key: {"probes": n, "share": share(n)} for key, n in groups.items()},
        "no_lineage": {"probes": none, "share": share(none)},
    }


def background_norms(
    meta: Mapping[Unit, dict],
    history_scores: Mapping[Unit, float],
    *,
    declared_share: float,
    history_played: bool,
) -> dict:
    """Per background norm, the keys that rest on it and the share that the full-history arm reaches.

    A key rests on the norms of its phenomenon. A key's reach is its mean score over the windows that the arm
    answered. A norm whose keys the arm reaches less often than ``declared_share`` is named: careful readers may not
    share it, and its entries then move to the rubric track.
    """
    entries = catalogue().entries
    background = [n for n, basis in catalogue().norms.items() if basis == "background"]
    known = [u for u in meta if meta[u].get("phenomenon") in entries]
    table = {}
    for norm in background:
        keys = [u for u in known if norm in entries[meta[u]["phenomenon"]].norms]
        reached = [history_scores[u] for u in keys if u in history_scores]
        share = round(statistics.mean(reached), 4) if reached else None
        table[norm] = {
            "keys": len(keys),
            "answered_by_history": len(reached),
            "reached_share": share,
            "below_declared_share": None if share is None else share < declared_share,
        }
    return {
        "scope": "keys of the case set whose phenomenon names the norm; reach is the full-history arm's mean score on them",
        "declared_share": declared_share,
        "history": "answered" if history_played else "not run: no --history full run in this run",
        "unknown_phenomenon": len(meta) - len(known),
        "norms": table,
        "below_declared_share": [n for n, row in table.items() if row["below_declared_share"]],
    }
