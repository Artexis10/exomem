"""The release panel: a small fixed slice of every suite, run once per Exomem release.

The panel is evidence and never a gate. It runs from a clean checkout of the release's tag,
plays the Exomem arms on development cases only (CL-Bench ``multi_group``, LAB dev matters,
planted dev cases), writes ``docs/benchmarks/outcome/<version>.json`` and ``.md``, and compares
them, paired by case, with the newest earlier version's file. When the actor itself changed since
that file (the CLI version or the model it reports), a movement may come from the actor and not
from Exomem, so the control arms run again and the report says which of the two changed.

The stored files are public: they hold numbers, ids and arm codes, never a fault string or a
judge's reason (``projection``).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import gate, record
from .command import SUITES, refuse, report_suite, run_suite, runs_dir, summary
from .isolation import ACTOR_EFFORT, ACTOR_MODEL, REPO_ROOT
from .judging import JudgeSpec
from .stats import paired_interval

DOCS = REPO_ROOT / "docs" / "benchmarks" / "outcome"
#: The Exomem arms of the panel, and the controls that explain an actor change.
PANEL = {"clbench": ("PP", "P"), "lab": ("WP",), "planted": ("E",)}
CONTROLS = {"clbench": ("N", "F"), "lab": ("S",), "planted": ("P",)}
#: What each suite measures per case, read out of the per-case report.
MEASURES = {
    "clbench": {"reward": lambda v: v},
    "lab": {"pass_fraction": lambda v: v["pass_fraction"]},
    "planted": {
        "correct": lambda v: float(v["correct"]),
        "stale_trusted": lambda v: float(v["stale_trusted"]),
    },
}
SEED = 20261109
#: What a stored report keeps per suite: numbers, ids and arm codes. Fault strings, judge reasons
#: and the like stay in the run's own files. ``arms`` keeps these keys of each arm's summary.
PUBLIC_ARM_KEYS = {
    "clbench": (
        "units",
        "scored_questions",
        "mean_reward",
        "accuracy",
        "mean_queries",
        "spent",
        "timeouts",
        "schema_repairs",
        "retries",
    ),
    "lab": ("units", "tasks_judged", "mean_pass_fraction", "all_pass_tasks", "spent"),
    "planted": ("units", "cases_judged", "unanswered", "spent"),
}


def version_key(stem: str) -> tuple[int, ...] | None:
    """``1.4.0`` as ``(1, 4, 0)``; a file name that is not a dotted version gives None."""
    parts = stem.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def previous_panel(version: str) -> dict | None:
    """The stored panel of the newest version older than ``version``, when there is one."""
    here = version_key(version)
    older = [
        (key, path)
        for path in DOCS.glob("*.json")
        if (key := version_key(path.stem)) and key < here
    ]
    return json.loads(max(older)[1].read_text()) if older else None


def actor_changed(previous: dict | None, identity: dict) -> dict | None:
    """What differs in the actor from the previous panel: the CLI version or the model it reported."""
    if previous is None:
        return None
    changes = {
        key: {"previous": previous["actor"][key], "now": identity[key]}
        for key in ("cli", "model_reported")
        if previous["actor"][key] != identity[key]
    }
    return changes or None


def compare(suite: str, current: dict, previous: dict) -> dict:
    """Per arm and measure: the current panel minus the previous one, paired over shared cases."""
    out: dict = {}
    for arm in sorted(set(current["arms"]) & set(previous["arms"])):
        for name, read in MEASURES[suite].items():
            now = {c: read(v[arm]) for c, v in current["cases"].items() if arm in v}
            then = {c: read(v[arm]) for c, v in previous["cases"].items() if arm in v}
            diffs = [now[c] - then[c] for c in sorted(set(now) & set(then))]
            out[f"{arm} {name}"] = paired_interval(diffs, seed=SEED, unit="case")
    return out


def projection(suite: str, report: dict) -> dict:
    """The part of a suite's report that the stored panel keeps: numbers, ids and arm codes."""
    kept = PUBLIC_ARM_KEYS[suite]
    cases = {
        case: {
            arm: {name: read(v) for name, read in MEASURES[suite].items()}
            for arm, v in by_arm.items()
        }
        for case, by_arm in report["cases"].items()
    }
    return {
        "suite": suite,
        "run_id": report["run_id"],
        "arms": {
            arm: {k: body[k] for k in kept if k in body} for arm, body in report["arms"].items()
        },
        "pairs": report["pairs"],
        "cases": cases,
        "failed_judgements": len(report.get("failed_judgements") or []),
        "struck_units": report.get("struck_units") or [],
        "judge": report.get("judge"),
        "judge_family": report.get("judge_family"),
    }


def run_panel(
    run_id: str,
    version: str,
    spec: JudgeSpec,
    *,
    jobs: int = 1,
    model: str = ACTOR_MODEL,
    effort: str = ACTOR_EFFORT,
    controls: bool = False,
) -> list[Path]:
    """Run the panel for ``version`` and write its two files. Returns their paths."""
    if version_key(version) is None:
        raise refuse(f"--version is a dotted release number such as 1.4.0, not {version!r}")
    checkout = record.checkout_state()
    if checkout["product_dirty"] or f"v{version}" not in checkout["product_tags"]:
        raise refuse(
            f"the panel runs from a clean checkout of the tag v{version}: this checkout is at "
            f"{checkout['product_revision']} with tags {checkout['product_tags'] or 'none'}"
            f"{' and has local changes' if checkout['product_dirty'] else ''}"
        )
    target = DOCS / f"{version}.json"
    if target.exists():
        raise refuse(f"{target} exists; panels are stored once per version")
    previous = previous_panel(version)
    ids = {suite: f"{run_id}-{suite}" for suite in SUITES}
    play = {"model": model, "effort": effort, "jobs": jobs}
    again = (
        "when the actor differs from the previous panel"
        if previous
        else "only with --controls (no earlier panel)"
    )
    print(
        f"panel {version}: CL-Bench PP,P on multi_group; LAB dev WP; planted dev E. "
        f"Control arms run {again}.",
        flush=True,
    )
    for suite in SUITES:
        run_suite(suite, ids[suite], split="panel", arms=list(PANEL[suite]), **play)
    exomem_ids = [runs_dir() / i for i in ids.values()]
    identity = record.run_identity(*exomem_ids)
    changed = actor_changed(previous, identity)
    lab_runs = [ids["lab"]]
    if controls or changed:
        for suite in SUITES:
            if suite == "lab":
                lab_runs.append(f"{ids['lab']}-controls")
                run_suite("lab", lab_runs[-1], split="panel", arms=list(CONTROLS["lab"]), **play)
            else:
                run_suite(suite, ids[suite], split="panel", arms=list(CONTROLS[suite]), **play)
    suites = {}
    for suite in SUITES:
        every = [*PANEL[suite], *(CONTROLS[suite] if controls or changed else ())]
        run_ids = lab_runs if suite == "lab" else [ids[suite]]
        suites[suite] = projection(
            suite,
            report_suite(
                suite, run_ids, None if suite == "clbench" else spec, jobs=jobs, arms=every
            ),
        )
    document = gate.stamp(
        {
            "version": version,
            "checkout": {k: checkout[k] for k in ("product_revision", "product_tags")},
            "run_ids": {**ids, **({"lab_controls": lab_runs[-1]} if len(lab_runs) > 1 else {})},
            "actor": record.run_identity(*(runs_dir() / i for i in [*ids.values(), *lab_runs])),
            "controls": {
                "run": bool(controls or changed),
                "why": "actor changed"
                if changed
                else "requested with --controls"
                if controls
                else None,
            },
            "actor_changed": changed,
            "suites": suites,
            "vs_previous": None
            if previous is None
            else {
                "version": previous["version"],
                "suites": {
                    s: compare(s, suites[s], previous["suites"][s])
                    for s in suites
                    if s in previous["suites"]
                },
            },
        },
        {"split": "panel"},
    )
    DOCS.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2))
    markdown = target.with_suffix(".md")
    markdown.write_text(render(document))
    return [target, markdown]


def table(suite: str, report: dict) -> list[str]:
    """One row per case, one column per arm, with the suite's first measure."""
    name, read = next(iter(MEASURES[suite].items()))
    arms = list(report["arms"])
    rows = [f"| case | {' | '.join(arms)} |", f"|---|{'---|' * len(arms)}"]
    for case, by_arm in report["cases"].items():
        cells = [f"{read(by_arm[a]):.3g}" if a in by_arm else "-" for a in arms]
        rows.append(f"| {case} | {' | '.join(cells)} |")
    return [f"Per case, {name}:", "", *rows]


def render(document: dict) -> str:
    """The panel's readable record: what ran, what the actor was, and how it moved."""
    actor = document["actor"]
    lines = [
        f"# Outcome panel {document['version']}",
        "",
        f"{document['label'].capitalize()}. Instrument evidence from subscription actors, not a gate and not a suite result.",
        "",
        "## Actor and product",
        "",
        f"- CLI: {', '.join(actor['cli']) or 'unknown'}",
        f"- Model requested: {', '.join(actor['model_requested']) or 'unknown'}",
        f"- Model reported: {', '.join(actor['model_reported']) or 'unknown'}",
        f"- Exomem: {', '.join(actor['exomem_version']) or 'unknown'}",
        "",
    ]
    if document["actor_changed"]:
        lines += ["## The actor changed since the previous panel", ""]
        for key, change in document["actor_changed"].items():
            lines.append(
                f"- {key}: {', '.join(change['previous']) or 'unknown'} to {', '.join(change['now']) or 'unknown'}"
            )
        lines += [
            "",
            "A difference from the previous panel may come from the actor and not from Exomem. "
            "The control arms ran again, so compare the Exomem arms against the controls of this panel.",
            "",
        ]
    elif document["controls"]["run"]:
        lines += [f"The control arms ran ({document['controls']['why']}).", ""]
    else:
        lines += [
            "The control arms did not run: the actor is the same as in the previous panel.",
            "",
        ]
    for suite, report in document["suites"].items():
        lines += [f"## {suite}", ""]
        if report["failed_judgements"]:
            lines += [
                f"{report['failed_judgements']} judgement(s) failed and are missing from the figures below.",
                "",
            ]
        lines += table(suite, report) + [""]
        lines += [f"- {line}" for line in summary(report)] + [""]
    if document["vs_previous"]:
        lines += [f"## Against {document['vs_previous']['version']}", ""]
        for suite, arms in document["vs_previous"]["suites"].items():
            for label, entry in arms.items():
                if entry["mean_difference"] is None:
                    lines.append(
                        f"- {suite} {label}: {entry['verdict']} ({entry['paired_cases']} shared cases)"
                    )
                else:
                    lines.append(
                        f"- {suite} {label}: {entry['mean_difference']:+} "
                        f"(95% CI {entry['ci95'][0]} to {entry['ci95'][1]}, {entry['paired_cases']} shared cases) {entry['verdict']}"
                    )
        lines.append("")
    return "\n".join(lines)
