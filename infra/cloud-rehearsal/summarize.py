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
    if "raw_import" in report["stages"]:
        raw_import(report)
        return
    if "catalogue_prewarm" in report["stages"]:
        catalogue_prewarm(report)
        return
    outcome = report["outcome"]
    print("## Evermatter Cloud P3 rehearsal\n")
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


def raw_import(report: dict) -> None:
    run = report["stages"]["raw_import"]
    print("## Raw-data import against the local journey (task 5.1)\n")
    print(f"- verdict: **{run.get('verdict', 'none: the run did not finish')}** ({run.get('verdict_meaning', '')})")
    print(f"- re-run duplicates: {run.get('rerun_duplicates', 'not recorded')}")
    local = run.get("local") or {}
    print(f"- local arm: {'scored' if local.get('scored') else 'not scored: ' + str(local.get('failure', 'no reason recorded'))[:300]}")
    for gap in local.get("gaps", []):
        print(f"  - gap: {gap}")
    for finding in run.get("findings", []):
        print(f"- **finding:** {finding.get('message', '')[:600]}")
    for name, target in run.get("targets", {}).items():
        print(f"\n### {name} vault: {target.get('verdict')}\n")
        for gap in target.get("gaps", []):
            print(f"- gap: {gap}")
        cli = target.get("cli") or {}
        print(f"- CLI coverage: {cli.get('declared_files')} files declared of {cli.get('collection_files')} in the collection"
              f" (left behind by the CLI: {cli.get('left_behind_by_the_cli', 'unknown')})")
        pages = target.get("existing_pages")
        if pages:
            print(f"- pre-existing source pages: {pages['sourced_before']}; Knowledge Base files {pages['files_before']} before, "
                  f"{pages['files_after']} after; earlier key notes landed {pages['earlier_key_notes_landed']}; problems {pages['problem_count']}")
            for problem in (pages.get("problems") or [])[:10]:
                print(f"  - {problem}")
        print("\n| group | items | kept by both | loss | cloud only | unknown |\n|---|---|---|---|---|---|")
        for group, row in (target.get("groups") or {}).items():
            print(f"| {group} | {row['items']} | {row['kept_by_both']} | {row['loss']['count']} | "
                  f"{row['cloud_extra']['count']} | {row['unknown']['count']} |")
        for group, row in (target.get("groups") or {}).items():
            for cause, found in row.get("loss_causes", {}).items():
                print(f"\n**losses in {group}, {cause}** ({found['count']}): " + "; ".join(found["listed"][:10]))
    for name, value in (("local arm", local.get("headline")), *((f"cloud arm, {k} vault", (v or {}).get("cloud")) for k, v in run.get("targets", {}).items())):
        print(f"\n**{name}**\n\n```json\n{json.dumps(value, indent=2, ensure_ascii=False)}\n```")


def _mib(value: int | None) -> str:
    return "unknown" if value is None else f"{value / 1048576:.0f}"


def catalogue_prewarm(report: dict) -> None:
    stage = report["stages"]["catalogue_prewarm"]
    run = stage.get("evidence") or {}
    print("## Catalogue pre-warm (warm-cell-catalogue task 3.1)\n")
    print(f"- verdict: **{run.get('state', 'none: the run did not finish')}** (a partial proof; steps 3 to 12 did not run)")
    if run.get("failure"):
        print(f"- failure: {str(run['failure'])[:600]}")
    if stage.get("status") == "failed" and not run:
        print(f"- **stage failed:** {(stage.get('failure') or {}).get('message', '')[:600]}")
    fixture = run.get("fixture") or {}
    print(f"- vault: {fixture.get('notes', 'not recorded')} synthetic notes, {fixture.get('bytes', 'not recorded')} bytes")
    environment = (run.get("configuration") or {}).get("cellctl_environment") or {}
    print(f"- cellctl settings read back: {json.dumps(environment)}")
    print("\n| scenario | state | build outcome | build start | build end | build s | build timeout s | hold started "
          "| new pod ready | hold to ready s | CPU peak (cores) | memory peak MiB |\n" + "|---" * 12 + "|")
    for name, scenario in (run.get("scenarios") or {}).items():
        build, stop, peaks = scenario.get("build") or {}, scenario.get("stop") or {}, scenario.get("peaks") or {}
        cells = [scenario.get("state"), build.get("outcome"), build.get("started_at"), build.get("ended_at"),
                 build.get("seconds"), build.get("timeout_seconds"), stop.get("hold_started_at"),
                 stop.get("new_pod_ready_at"), stop.get("hold_to_ready_seconds")]
        # A scenario whose build never ran records that state instead of peaks.
        figures = [peaks["state"]] * 2 if "state" in peaks else [peaks.get("cpu_cores_peak"), _mib(peaks.get("memory_peak_bytes"))]
        print(f"| {name} | " + " | ".join("unknown" if cell is None else str(cell) for cell in [*cells, *figures]) + " |")
    for name, scenario in (run.get("scenarios") or {}).items():
        failed = [check for check, passed in (scenario.get("checks") or {}).items() if not passed]
        if scenario.get("failure") or failed:
            print(f"\n- **{name}**: {scenario.get('failure') or ''} {'unmet checks: ' + ', '.join(failed) if failed else ''}".rstrip())
        for note, result in (scenario.get("observations") or {}).items():
            if not result["ok"]:
                print(f"- {name} observation {note}: {result['detail']}")
    for name, scenario in (run.get("scenarios") or {}).items():
        print(f"\n**{name}**\n\n```json\n{json.dumps(scenario, indent=2, default=str)}\n```")
    for deviation in run.get("deviations", []):
        print(f"- deviation: {deviation}")


if __name__ == "__main__":
    main(sys.argv[1])
