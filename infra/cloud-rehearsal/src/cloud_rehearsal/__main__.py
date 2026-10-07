"""`exomem-cloud-rehearsal run`: P3, the Exomem Cloud local rehearsal.

One command stands up disposable K3s, PostgreSQL with Substrate's real
migrations and grants, Substrate, its gateway, cellctl and the real cell
image; runs the twelve P3 steps in order; writes a JSON report with the
5.3 measurements; and tears everything down.

Exit codes: 0 when the report gates the node (every step passed, every
gating target met, a valid rehearsal); 1 when it recorded product findings; 2 when
the rehearsal itself failed (a stage could not be stood up, or a step raised
something other than a recorded finding, or, with `--harness-check`, a
step failed that the known-findings baseline does not name). `--harness-check`
turns 1 into 0 for pull-request CI, where the question is whether the
harness works.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from . import build, drill, images, infra, platform, substrate, tls
from .http import Resolver
from .report import Report, StageFailed, failure_record, guarded, stage
from .scenarios import Context, close_clients, post_checks, ready_matches_pods, run_steps
from .shell import run, wait_for


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exomem-cloud-rehearsal", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run the whole rehearsal")
    run_parser.add_argument("--report", type=Path, default=Path("cloud-rehearsal-report.json"))
    run_parser.add_argument("--workdir", type=Path, default=None, help="scratch directory (default: a new temporary one)")
    run_parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/exomem-cloud-rehearsal")
    run_parser.add_argument("--substrate-commit", default=substrate.SUBSTRATE_COMMIT)
    run_parser.add_argument(
        "--cell-image", default=None,
        help="use this already-built `cloud` image instead of building it; the report records it",
    )
    run_parser.add_argument(
        "--gateway-build", choices=("dockerfile", "host"), default="dockerfile",
        help="build the gateway from Substrate's Dockerfile (default), or assemble its final stage from a host build",
    )
    run_parser.add_argument(
        "--cellctl-build", choices=("dockerfile", "host"), default="dockerfile",
        help="build cellctl from infra/cellctl/Dockerfile (default), or assemble its venv from a host resolution",
    )
    run_parser.add_argument("--steps", default=None, help="comma-separated step numbers to run (default: all)")
    run_parser.add_argument("--keep", action="store_true", help="leave the stack running for inspection")
    run_parser.add_argument(
        "--known-findings", type=Path, default=Path(__file__).resolve().parents[2] / "known-findings.json",
        help="with --harness-check: the product findings each step is expected to record",
    )
    run_parser.add_argument(
        "--harness-check", action="store_true",
        help="exit 0 whenever the rehearsal itself worked, even if it recorded product findings "
        "(pull-request CI); without it, only a report that gates the node exits 0",
    )
    drill_parser = sub.add_parser(
        "local-storage-drill",
        help="the node-loss drill of move-cloud-cells-to-local-storage on a three-node K3s with TopoLVM",
    )
    drill_parser.add_argument("--report", type=Path, default=Path("local-storage-drill-report.json"))
    drill_parser.add_argument("--workdir", type=Path, default=None, help="scratch directory (default: a new temporary one)")
    drill_parser.add_argument("--keep", action="store_true", help="leave the cluster running for inspection")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "local-storage-drill":
        return asyncio.run(drill.run_drill(args))
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    run_id = secrets.token_hex(4)
    workdir = args.workdir or Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"exomem-cloud-rehearsal-{run_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    report = Report(run_id=run_id)
    report.environment = infra.environment_facts()
    report.inputs = {
        "exomem_commit": run(["git", "-C", str(images.REPO_ROOT), "rev-parse", "HEAD"]).stdout.strip(),
        "exomem_worktree_clean": run(["git", "-C", str(images.REPO_ROOT), "status", "--porcelain"]).stdout.strip() == "",
        "substrate_repository": substrate.SUBSTRATE_REPOSITORY,
        "substrate_commit": args.substrate_commit,
        "k3s_image": images.K3S,
        "postgres_image": images.POSTGRES,
        "s3_double_image": images.S3_DOUBLE,
        "cell_image_source": "prebuilt:" + args.cell_image if args.cell_image else "Dockerfile --target cloud",
        "gateway_image_source": "Dockerfile.exomem-gateway" if args.gateway_build == "dockerfile" else "host-assembled final stage",
    }
    if args.cell_image:
        report.valid = False
        report.invalid_reasons.append(
            "the cell image was supplied with --cell-image instead of built from this checkout's Dockerfile"
        )
    report.inputs["cellctl_image_source"] = (
        "infra/cellctl/Dockerfile" if args.cellctl_build == "dockerfile" else "host-assembled venv on the pinned Python base"
    ) + ", plus a layer adding the rehearsal entry module and boto3"
    if not report.inputs["exomem_worktree_clean"]:
        report.notes.append("the exomem checkout had uncommitted changes")
    only = {int(part) for part in args.steps.split(",")} if args.steps else None
    if only is not None:
        report.valid = False
        report.invalid_reasons.append(f"only steps {sorted(only)} were selected")

    stack: infra.Stack | None = None
    code = 1
    try:
        with stage(report, "substrate_source"):
            source = substrate.fetch_source(args.cache, args.substrate_commit)
            substrate.build_source(source)
        with stage(report, "images"):
            v1, v2, broken = build.build_cell_images(run_id, workdir, prebuilt=args.cell_image)
            gateway_tag = build.build_gateway_image(run_id, workdir, source, mode=args.gateway_build)
            cellctl_tag = build.build_cellctl_image(run_id, workdir, mode=args.cellctl_build)
        with stage(report, "infrastructure"):
            stack = infra.create_stack(run_id, workdir)
            report.adaptations.extend(stack.adaptations)
        with stage(report, "images_into_k3s"):
            built = build.BuiltImages(
                cell_v1=build.load_into_k3s(stack.k3s.container, v1),
                cell_v2=build.load_into_k3s(stack.k3s.container, v2),
                cell_broken=build.load_into_k3s(stack.k3s.container, broken),
                gateway=build.load_into_k3s(stack.k3s.container, gateway_tag),
                cellctl=build.load_into_k3s(stack.k3s.container, cellctl_tag),
                gateway_build=args.gateway_build,
                build_seconds={},
            )
            run(["docker", "pull", "--quiet", images.TRAEFIK], timeout=900)
            run(["docker", "tag", images.TRAEFIK, f"rehearsal.local/ingress-traefik:{run_id}"])
            traefik = build.load_into_k3s(stack.k3s.container, f"rehearsal.local/ingress-traefik:{run_id}")
            report.inputs["images"] = {
                "cell_v1": built.cell_v1, "cell_v2": built.cell_v2, "cell_broken": built.cell_broken,
                "gateway": built.gateway, "cellctl": built.cellctl, "ingress_traefik": traefik,
            }
        with stage(report, "control_database"):
            await substrate.bootstrap_database(stack)
            report.stages["control_database"]["migrate_tail"] = substrate.migrate(stack, source)[-800:]
        pki = tls.make_pki(workdir)
        with stage(report, "substrate"):
            control = substrate.start(stack, source, pki, build.CELL_REPOSITORY)
            resolver = Resolver(pki.ca_path, {tls.SUBSTRATE_HOST: control.edge_host_port, tls.MCP_HOST: stack.k3s.ingress_host_port})
            _wait_json(resolver, f"{substrate.PUBLIC_BASE_URL}/.well-known/oauth-authorization-server/api/exomem/oauth", "Substrate")
            report.stages["substrate"]["egress_sealed"] = substrate.sealed_egress_refused(control)
            if not report.stages["substrate"]["egress_sealed"]:
                raise RuntimeError("Substrate's network is not sealed: a TEST-NET address did not fail with no route")
        with stage(report, "platform"):
            hour = dt.datetime.now(dt.UTC).hour
            # Closed until step 11 opens it, so no nightly backup lands mid-scenario.
            closed_window = f"{(hour + 6) % 24}-{(hour + 7) % 24}"
            deployed = platform.apply(
                stack,
                platform.PlatformConfig(
                    cellctl_image=built.cellctl, gateway_image=built.gateway,
                    cell_repository=build.CELL_REPOSITORY, backup_window=closed_window,
                    public_base_url=substrate.PUBLIC_BASE_URL, mcp_path=substrate.MCP_PATH,
                    trusted_ingress_source_value=control.secrets.ingress_source_value,
                ),
                cellctl_secrets=platform.cellctl_secrets(
                    stack, cell_token_key=control.secrets.cell_token_key,
                    control_plane_key=control.secrets.control_plane_key,
                ),
                pki=pki,
                ingress_source_value=control.secrets.ingress_source_value,
                ingress_image=traefik,
            )
            report.overlays.extend(deployed.overlays)
            platform.wait_rollout(stack, platform.CLOUD_NAMESPACE, "cellctl")
            platform.wait_rollout(stack, platform.CLOUD_NAMESPACE, "exomem-cloud-gateway")
            platform.wait_rollout(stack, platform.EDGE_NAMESPACE, "rehearsal-traefik")
            _wait_json(resolver, f"https://{tls.MCP_HOST}/.well-known/oauth-protected-resource{substrate.MCP_PATH}", "the gateway through ingress")
        ctx = Context(stack=stack, substrate=control, images=built, resolver=resolver, report=report)
        with stage(report, "release"):
            # The owner's first release: cell_image through Substrate's own route.
            await ctx.admin_release({"cellImage": built.cell_v1})
            capacity = await ctx.fetchrow("SELECT sum(cell_slots) AS slots FROM exomem_cloud_capacity")
            report.stages["release"]["published_cell_slots"] = capacity and capacity["slots"]
        try:
            await run_steps(ctx, only=only)
        finally:
            await close_clients(ctx)
        report.stages["post_checks"] = post_checks(ctx)
        mismatches = await ready_matches_pods(ctx)
        report.stages["post_checks"]["ready_matches_pod"] = not mismatches
        if mismatches:
            report.defects.append(
                {
                    "step": "post_checks", "cross_lane": True, "owner": "Artexis10/exomem#1368",
                    "component": "infra/cellctl reconcile._reconcile_row",
                    "message": "a converged row's `ready` is not re-observed: a row that is not dirty gets only "
                    "observed_at written, so a cell whose pod later turns NotReady keeps ready=true",
                    "evidence": mismatches,
                }
            )
        harness_errors = [step.number for step in report.steps if step.failure and "traceback" in step.failure]
        harness: dict[str, object] = {"steps_with_harness_errors": harness_errors}
        if args.harness_check:
            harness.update(_compare_with_known_findings(report, args.known_findings, only))
        failed = bool(harness_errors) or bool(harness.get("unexpected"))
        harness["status"] = "failed" if failed else "passed"
        report.stages["harness"] = harness
        outcome = report.to_json()["outcome"]
        if failed:
            code = 2
        elif outcome["gates_node"] or args.harness_check:
            code = 0
        else:
            code = 1
    except StageFailed:
        code = 2
    except Exception as error:  # noqa: BLE001 - recorded in the report
        report.stages.setdefault("harness", {})["failure"] = failure_record(error)
        code = 2
    finally:
        # Each cleanup stands alone, so one failing never strands the stack
        # or loses the report.
        if stack is not None and not args.keep:
            guarded(report, "diagnostics", lambda: _collect_diagnostics(stack, workdir))
        if not guarded(report, "report", lambda: report.write(args.report)):
            code = 2
        guarded(report, "teardown", lambda: infra.teardown(stack, keep=args.keep))
        if not args.keep:
            # This run's own image tags; base images stay cached.
            tags = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], check=False).stdout.split()
            mine = [tag for tag in tags if tag.endswith(f":{run_id}") or f":{run_id}-" in tag]
            if mine:
                run(["docker", "rmi", "--force", *mine], check=False)
        if not args.keep and args.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
    _print_summary(report, args.report)
    return code


def _compare_with_known_findings(report: Report, path: Path, only: set[int] | None) -> dict[str, object]:
    """Harness check: every failing or blocked step must be a known, owned finding.

    The baseline names the product findings the rehearsal is expected to
    record until their owners fix them. Anything else failing or blocked is
    treated as a regression in the rehearsal. A known finding that now
    passes is reported so the baseline gets pruned.
    """

    known = {int(number): entry for number, entry in json.loads(path.read_text(encoding="utf-8"))["steps"].items()}
    unexpected, resolved = [], []
    for step in report.steps:
        if only is not None and step.number not in only:
            continue
        message = (step.failure or {}).get("message", "")
        entry = known.get(step.number)
        # A listed step passes the check only when it fails for the listed
        # reason: the same step failing any other way is a regression. A step
        # that joins several problems with "; " is listed with each_problem,
        # so a new problem riding beside the known one is still caught.
        expected = entry is not None and step.status == "failed" and all(
            entry["match"] in part for part in (message.split("; ") if entry.get("each_problem") else [message])
        )
        if step.status != "passed" and not expected:
            unexpected.append({"step": step.number, "status": step.status, "message": message[:300]})
        if step.status == "passed" and entry is not None:
            resolved.append({"step": step.number, "baseline": entry["owner"]})
    return {"known_findings": {str(k): v for k, v in known.items()}, "unexpected": unexpected, "resolved_known_findings": resolved}


def _wait_json(resolver: Resolver, url: str, what: str) -> None:
    def probe() -> bool:
        with resolver.client() as client:
            response = client.get(url)
            return response.status_code == 200 and isinstance(response.json(), dict)

    wait_for(probe, timeout=300, interval=3, description=f"{what} to answer {url}")


def _collect_diagnostics(stack: infra.Stack, workdir: Path) -> None:
    out = workdir / "diagnostics"
    out.mkdir(exist_ok=True)
    for name, command in {
        "pods": ["get", "pods", "--all-namespaces", "-o", "wide"],
        "events": ["get", "events", "--all-namespaces", "--sort-by=.lastTimestamp"],
        "cellctl": ["logs", "--namespace", platform.CLOUD_NAMESPACE, "deployment/cellctl", "--tail=400"],
        "gateway": ["logs", "--namespace", platform.CLOUD_NAMESPACE, "deployment/exomem-cloud-gateway", "--tail=400"],
    }.items():
        result = stack.k3s.kubectl(*command, check=False)
        (out / f"{name}.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    artifacts = os.environ.get("REHEARSAL_DIAGNOSTICS_DIR")
    if artifacts:
        shutil.copytree(out, Path(artifacts), dirs_exist_ok=True)


def _print_summary(report: Report, path: Path) -> None:
    data = report.to_json()
    print(f"[rehearsal] report: {path}")
    for step in data["steps"]:
        print(f"[rehearsal]   {step['number']:>2} {step['name']:<34} {step['status']}")
    for name, measurement in data["measurements"].items():
        gating = "" if measurement.get("gating", True) else ", informational"
        print(f"[rehearsal]   {name:<34} {measurement['observed']} (target {measurement['comparison']} {measurement['target']}, met={measurement['met']}{gating})")
    print(f"[rehearsal] outcome: {json.dumps(data['outcome'])}")
    if data["cross_lane_defects"]:
        print(f"[rehearsal] cross-lane defects: {len(data['cross_lane_defects'])}")


if __name__ == "__main__":
    sys.exit(main())
