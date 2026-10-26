"""Paired MCP read latency gate: this checkout against the latest release.

`paired` times one shard of the generated public fixture on both arms and writes a
fragment. `aggregate` joins the fragments into per-group verdicts. `private` times
a prepared private vault the same way and also reports each store group against its
p95 target. Both arms run the real managed worker and every call is timed by
`recall_canary.measure_call`; nothing in this file times a call.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from importlib.resources import files
from pathlib import Path
from typing import Any

from release_arm import MeasurementRefused

from exomem.request_latency_contract import load_contract

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks/mcp-read-latency"
SCHEMA_VERSION = 1

CONTRACT = load_contract()
REFERENCE_ID = "mcp-read-paired"
REFERENCE = CONTRACT.references[REFERENCE_ID]
BOUNDARY = CONTRACT.bar("mcp-read-paired-pending").boundary.id
#: The chosen verdict for a group whose interval has no allowance to meet.
NO_ALLOWANCE = "allowance-not-set"


# --- Statistics and verdicts -------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * q / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def group_interval(medians: list[float], *, confidence: float, family: int) -> dict[str, Any]:
    """Student-t interval of the mean case difference, widened for the family of groups.

    Each case contributes one median of its (head - base) differences, so the cases are
    the independent units. `family` is the number of gating groups the contract declares.
    """
    from scipy.stats import t

    count = len(medians)
    mean = statistics.fmean(medians)
    half = float(t.ppf(1 - (1 - confidence) / (2 * family), count - 1)) * (
        statistics.stdev(medians) / math.sqrt(count)
    )
    return {"cases": count, "mean_ms": mean, "lower_ms": mean - half, "upper_ms": mean + half}


def judge(interval: dict[str, Any], allowance_ms: float | None) -> str:
    """Fail on the lower bound, demonstrate on the upper bound, otherwise stay inconclusive."""
    if allowance_ms is None:
        return NO_ALLOWANCE
    if interval["lower_ms"] > allowance_ms:
        return "failed"
    if interval["upper_ms"] <= allowance_ms:
        return "demonstrated"
    return "inconclusive"


def cpu_pressure(path: str = "/proc/pressure/cpu") -> float | None:
    """The kernel's 60-second share of time some task waited for a CPU; None when unreadable."""
    try:
        line = next(row for row in Path(path).read_text().splitlines() if row.startswith("some "))
        return float(dict(field.split("=") for field in line.split()[1:])["avg60"])
    except (OSError, StopIteration, KeyError, ValueError):
        return None


# --- Refusals ----------------------------------------------------------------


def state_keys(state_root: Path) -> frozenset[str]:
    """The per-vault state directories under an arm's state root."""
    if not state_root.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in state_root.iterdir() if entry.is_dir())


def require_warm_state(before: frozenset[str], after: frozenset[str]) -> None:
    """Refuse an arm whose worker created vault state; a cold start times index builds.

    State is keyed by the vault path, so a copied vault, or any state the worker made
    for itself, appears as a directory that was not there before the worker started.
    """
    if not before or after - before:
        raise MeasurementRefused("arm-state-created-during-run", phase="state-check")


def require_derived_ready(state_root: Path) -> None:
    """Refuse an arm with a derived collection still rebuilding.

    The store retries a rebuilding collection every 2 s under the writer lease, which
    puts a tail on unrelated calls. The derived file allows only 'ready' and
    'rebuilding', so "not ready" is the product's own `rebuilds_pending`; its argument
    is a store path and resolves to the derived file beside it, which is the file itself.
    """
    from exomem.collection_store import derived_rows

    for derived in sorted(state_root.glob(f"*/{derived_rows.FILENAME}")):
        if derived_rows.rebuilds_pending(derived):
            raise MeasurementRefused("derived-collection-not-ready", phase="state-check")


# --- Fragments and the family verdict -----------------------------------------

#: Fields every fragment of one run must agree on.
CONSISTENT = ("contract_revision", "contract_sha256", "profile_sha256", "cases_sha256", "boundary",
              "tag", "base_commit", "shards", "case_total", "reduced")


def contract_sha256() -> str:
    return hashlib.sha256(files("exomem").joinpath("request_latency_contract.json").read_bytes()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pair_samples(samples: list[dict], assigned: list[dict], *, repeats: int):
    """Pair each assigned case's base and head samples by repeat.

    Returns the per-group case medians of (head - base), the per-group arm times and
    the broken-instrument problems. A case with a failed or absent sample is a problem
    and contributes nothing, so a failure can never shift a group's interval.
    """
    by_key = {(sample["case"], sample["arm"], sample["repeat"]): sample for sample in samples}
    medians: dict[str, list[float]] = defaultdict(list)
    arm_ms: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"base": [], "head": []})
    problems = []
    for case in assigned:
        pairs = [(by_key.get((case["id"], "base", repeat)), by_key.get((case["id"], "head", repeat)))
                 for repeat in range(repeats)]
        if any(base is None or head is None for base, head in pairs):
            problems.append({"code": "pair-missing", "group": case["group"], "case": case["id"]})
        elif any(base["failure"] or head["failure"] for base, head in pairs):
            problems.append({"code": "case-failed", "group": case["group"], "case": case["id"]})
        else:
            medians[case["group"]].append(statistics.median(h["elapsed_ms"] - b["elapsed_ms"] for b, h in pairs))
            for base, head in pairs:
                arm_ms[case["group"]]["base"].append(base["elapsed_ms"])
                arm_ms[case["group"]]["head"].append(head["elapsed_ms"])
    return medians, arm_ms, problems


def fragment_problems(fragments: list[dict], *, head_contract_sha256: str,
                      head_profile_sha256: str | None) -> list[dict]:
    """Check that the fragments are one run: same contract, fixture, cases, release and shard set."""
    first = fragments[0]
    problems = [{"code": "fragment-differs", "field": key} for key in CONSISTENT
                if any(fragment[key] != first[key] for fragment in fragments)]
    if first["contract_sha256"] != head_contract_sha256:
        problems.append({"code": "fragment-differs", "field": "contract_sha256"})
    if head_profile_sha256 is not None and first["profile_sha256"] != head_profile_sha256:
        problems.append({"code": "fragment-differs", "field": "profile_sha256"})
    if sorted(fragment["shard"] for fragment in fragments) != list(range(first["shards"])):
        problems.append({"code": "shard-missing"})
    ids = [case["id"] for fragment in fragments for case in fragment["assigned"]]
    if len(ids) != len(set(ids)) or len(ids) != first["case_total"]:
        problems.append({"code": "case-assignment-inconsistent"})
    return problems


def group_row(group: str, setting: dict, *, gating: bool, medians: list[float], arm_ms: dict,
              reference: dict, reduced: bool, private: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"group": group, "gating": gating, "cases": len(medians)}
    if len(medians) >= 2:
        row.update(group_interval(medians, confidence=reference["confidence"],
                                  family=len(reference["gating_groups"])))
    for arm in ("base", "head"):
        row[f"{arm}_p50_ms"], row[f"{arm}_p95_ms"] = (percentile(arm_ms[arm], 50), percentile(arm_ms[arm], 95))
    if not gating:
        row["verdict"] = "reported"
    elif len(medians) < 2 or (len(medians) < reference["minimum_cases"] and not reduced):
        row["verdict"] = "insufficient-cases"
    else:
        row["allowance_ms"] = setting["allowance_ms"]
        row["verdict"] = judge(row, setting["allowance_ms"])
    if private and "p95_target_ms" in setting and row["head_p95_ms"] is not None:
        row["target"] = {"p95_target_ms": setting["p95_target_ms"],
                         "result": "meets target" if row["head_p95_ms"] <= setting["p95_target_ms"]
                         else "misses target"}
    return row


def summarize(fragments: list[dict], *, mode: str, reference: dict = REFERENCE,
              head_contract_sha256: str | None = None,
              head_profile_sha256: str | None = None) -> dict[str, Any]:
    """Join the fragments of one run into per-group verdicts and the run's state."""
    refused = [fragment for fragment in fragments if fragment["state"] != "measured"]
    if refused:
        return {"schema_version": SCHEMA_VERSION, "mode": mode, "state": "refused",
                "failures": [{"code": f["reason"], "phase": f["phase"]} for f in refused]}
    first = fragments[0]
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "mode": mode, "contract_revision": first["contract_revision"],
        "contract_sha256": first["contract_sha256"], "profile_sha256": first["profile_sha256"],
        "cases_sha256": first["cases_sha256"], "boundary": first["boundary"], "tag": first["tag"],
        "base_commit": first["base_commit"], "reduced": first["reduced"],
        "family": {"groups": len(reference["gating_groups"]), "confidence": reference["confidence"],
                   "multiplicity": reference["multiplicity"]},
        "environment": [{"shard": f["shard"], **f["environment"]} for f in fragments],
        "groups": [], "warnings": [], "failures": []}
    failures = fragment_problems(
        fragments, head_contract_sha256=head_contract_sha256 or contract_sha256(),
        head_profile_sha256=head_profile_sha256)
    samples = [sample for fragment in fragments for sample in fragment["samples"]]
    assigned = [case for fragment in fragments for case in fragment["assigned"]]
    medians, arm_ms, problems = pair_samples(samples, assigned, repeats=reference["repeats"])
    failures += problems
    settings = {**{name: (setting, True) for name, setting in reference["gating_groups"].items()},
                **{name: (setting, False) for name, setting in reference["non_gating_groups"].items()}}
    failures += [{"code": "group-undeclared", "group": g} for g in sorted({c["group"] for c in assigned} - settings.keys())]
    for name, (setting, gating) in settings.items():
        if not any(case["group"] == name for case in assigned):
            if gating:
                failures.append({"code": "group-missing", "group": name})
            continue
        summary["groups"].append(group_row(
            name, setting, gating=gating, medians=medians[name], arm_ms=arm_ms[name], reference=reference,
            reduced=first["reduced"], private=mode == "private"))
    for row in summary["groups"]:
        if row["verdict"] == "failed":
            failures.append({"code": "slower-than-allowance", "group": row["group"]})
        elif row["verdict"] == "insufficient-cases":
            failures.append({"code": "cases-below-floor", "group": row["group"]})
        elif row["verdict"] in ("inconclusive", NO_ALLOWANCE):
            summary["warnings"].append({"code": row["verdict"], "group": row["group"]})
    if first["reduced"]:
        summary["warnings"].append({"code": "reduced-run"})
    summary["failures"] = failures
    summary["state"] = "failed" if failures else "reduced" if first["reduced"] else "passed"
    return summary


def validate_document(document: dict) -> None:
    """Validate a stored plan, fragment or summary against the schema's definition for its mode."""
    from jsonschema import Draft202012Validator, FormatChecker

    shape = json.loads((BENCHMARK / "summary.schema.json").read_text())
    Draft202012Validator({"$schema": shape["$schema"], "$defs": shape["$defs"],
                          "$ref": f"#/$defs/{document['mode']}"}, format_checker=FormatChecker()).validate(document)
