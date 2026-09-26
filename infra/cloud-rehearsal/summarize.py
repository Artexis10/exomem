"""Renders a rehearsal report as a Markdown summary (stdlib only)."""

from __future__ import annotations

import json
import sys


def main(path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)
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
        print(f"| {name} | {value['observed']} | {value['comparison']} {value['target']} | {value['met']} |")
    failed_stages = {name: stage for name, stage in report["stages"].items() if stage.get("status") == "failed"}
    for name, stage in failed_stages.items():
        print(f"\n**Stage {name} failed:** {stage['failure'].get('message', '')[:600]}")
    if report["cross_lane_defects"]:
        print("\n### Cross-lane defects\n")
        for defect in report["cross_lane_defects"]:
            print(f"- step {defect.get('step')}: {defect.get('owner')} -- {defect.get('summary') or defect.get('message')}")


if __name__ == "__main__":
    main(sys.argv[1])
