"""Renders a rehearsal report as a Markdown summary (stdlib only)."""

from __future__ import annotations

import json
import sys


def main(path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)
    if report.get("schema") in DRILL_TITLES:
        drill(report)
        return
    if "large_import" in report["stages"]:
        large_import(report)
        return
    outcome = report["outcome"]
    print("## Exomem Cloud P3 rehearsal\n")
    print(f"- valid rehearsal: **{report['valid_rehearsal']}** {'; '.join(report['invalid_reasons'])}")
    print(f"- gates the node: **{outcome['gates_node']}** (steps {outcome['steps']})")
    for blocker in outcome.get("gate_blockers", []):
        print(f"  - blocked by: {blocker}")
    harness = report["stages"].get("harness") or {}
    for item in harness.get("unexpected", []):
        print(f"- **unexpected** step {item['step']} {item['status']}: {item['message']}")
    for item in harness.get("resolved_known_findings", []):
        print(f"- known finding now passes, prune the baseline: step {item['step']}")
    print()
    print("| # | step | status | seconds | failure |\n|---|---|---|---|---|")
    for step in report["steps"]:
        failure = (step.get("failure") or {}).get("message", "").replace("|", "\\|").replace("\n", " ")[:200]
        print(f"| {step['number']} | {step['name']} | {step['status']} | {step['seconds']} | {failure} |")
    print("\n| measurement | observed | target | met |\n|---|---|---|---|")
    for name, value in report["measurements"].items():
        met = value["met"] if value.get("gating", True) else f"{value['met']} (informational)"
        print(f"| {name} | {value['observed']} | {value['comparison']} {value['target']} | {met} |")
    failed_stages = {name: stage for name, stage in report["stages"].items() if stage.get("status") == "failed"}
    for name, stage in failed_stages.items():
        # The harness stage records failed steps rather than a stage exception.
        message = (stage.get("failure") or {}).get("message", "See step failures above.")
        print(f"\n**Stage {name} failed:** {message[:600]}")
    if report["cross_lane_defects"]:
        print("\n### Cross-lane defects\n")
        for defect in report["cross_lane_defects"]:
            print(f"- step {defect.get('step')}: {defect.get('owner')} -- {defect.get('summary') or defect.get('message')}")


# Reports written as drill reports (checks, stages, measurements), by schema.
DRILL_TITLES = {
    "exomem-local-storage-drill-report-v1": "Local-storage node-loss drill",
    "exomem-k3s-link-rehearsal-report-v1": "Flannel MTU, agent-token and client-CA rotation (k3s-link)",
}


def drill(report: dict) -> None:
    print(f"## {DRILL_TITLES[report['schema']]}\n")
    print(f"- all checks passed: **{report['outcome']['all_checks_passed']}** ({report['outcome']['checks']})")
    for name, stage in report["stages"].items():
        if isinstance(stage, dict) and "failure" in stage:
            print(f"- **stage {name} failed:** {stage['failure'].get('message', '')[:600]}")
    print("\n| # | check | status | seconds | failure |\n|---|---|---|---|---|")
    for check in report["checks"]:
        failure = (check.get("failure") or {}).get("message", "").replace("|", "\\|").replace("\n", " ")[:300]
        print(f"| {check['number']} | {check['name']} | {check['status']} | {check['seconds']} | {failure} |")
    for name, value in report["measurements"].items():
        print(f"\n**{name}**\n\n```json\n{json.dumps(value, indent=2)}\n```")


def large_import(report: dict) -> None:
    run = report["stages"]["large_import"]
    sizing = report["stages"].get("large_import_sizing") or {}
    print("## Large agent-led import (task 5.1)\n")
    print(f"- verdict: **{run.get('verdict', 'none: the run did not finish')}** ({run.get('mode', 'unknown')} mode)")
    if sizing.get("note"):
        print(f"- data and volume: {sizing['note']}")
    # A pass with the Cloud transfer tools refused (served: False, the Substrate route) did not exercise them.
    print(f"- Cloud transfer tools enabled in the chart: {run.get('transfer_tools_enabled', 'not recorded')}")
    for name, folder in run.get("folders", {}).items():
        served = (folder.get("observe_transfer") or {}).get("served", "not recorded")
        route = (folder.get("discard_call") or {}).get("route", "not recorded")
        print(f"- {name}: observe_transfer.served: {served}; discard_call.route: {route}")
    for stage, record in report["stages"].items():
        if isinstance(record, dict) and record.get("status") == "failed":
            print(f"- **stage {stage} failed:** {(record.get('failure') or {}).get('message', '')[:600]}")
    for finding in run.get("findings", []):
        print(f"- **finding** in {finding.get('phase')}: {finding.get('message', '')[:600]}")
    for problem in (run.get("volume") or {}).get("problems", []):
        print(f"- **volume:** {problem}")
    print("\n| folder | criterion | passed | problems |\n|---|---|---|---|")
    for name, folder in run.get("folders", {}).items():
        for criterion, result in (folder.get("criteria") or {}).items():
            problems = "; ".join(result["problems"]).replace("|", "\\|").replace("\n", " ")[:300]
            print(f"| {name} | {criterion} | {result['passed']} | {problems} |")
    print("\n| phase | seconds |\n|---|---|")
    for phase, seconds in run.get("phases", {}).items():
        print(f"| {phase} | {seconds} |")
    for name, value in run.get("measurements", {}).items():
        print(f"\n**{name}**\n\n```json\n{json.dumps(value, indent=2)}\n```")


if __name__ == "__main__":
    main(sys.argv[1])
