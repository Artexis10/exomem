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
import time
from dataclasses import asdict
from pathlib import Path

from . import (
    build,
    drill,
    drill_cluster,
    host_speed,
    images,
    infra,
    k3s_link,
    large_import,
    large_import_fixture,
    platform,
    raw_import,
    substrate,
    tls,
)
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
    run_parser.add_argument("--cloud-import", action="store_true", help="also run the native CLI import journey after provisioning")
    run_parser.add_argument("--owner-export", action="store_true", help="run the owner export workflow after provisioning (partial proof)")
    run_parser.add_argument("--operator-restore-abort", action="store_true", help="interrupt the shipped in-place restore after provisioning and prove recovery (partial proof; never gates a node)")
    run_parser.add_argument("--keep", action="store_true", help="leave the stack running for inspection")
    run_parser.add_argument(
        "--large-import", action="store_true",
        help="bring a large folder and then a media-heavy one into a cell whose local-storage volume cannot hold "
        "two copies (design D7); runs after provisioning, in place of the other steps (partial proof; never gates a node)",
    )
    run_parser.add_argument("--large-import-files", type=int, default=large_import_fixture.DEFAULT_FILES,
                            help="with --large-import: the number of files in both folders; the byte total shrinks first")
    run_parser.add_argument("--large-import-gb", type=float, default=large_import_fixture.DEFAULT_TARGET_BYTES / 1e9,
                            help="with --large-import: the target total size of both folders in GB, scaled down to the free disk")
    run_parser.add_argument("--large-import-minutes", type=float, default=115,
                            help="with --large-import: stop filing and report what is done this long after the process started")
    run_parser.add_argument("--large-import-smoke", action="store_true",
                            help="with --large-import: accept data that cannot show the volume is below two copies; "
                            "the verdict is then smoke_only, never pass")
    run_parser.add_argument(
        "--raw-import", action="store_true",
        help="bring the generator's messy collection into a fresh cell and into one that already holds another collection, "
        "and compare every item with the same journey run locally; runs after provisioning, in place of the other steps "
        "(partial proof; never gates a node)",
    )
    run_parser.add_argument("--raw-import-python", type=Path, default=None,
                            help="with --raw-import: an interpreter with the server and its extractors installed "
                            "(the local arm and the scorer run on it)")
    run_parser.add_argument(
        "--local-storage-coexistence", action="store_true",
        help="install TopoLVM while keeping Hetzner-class cells for the full P3 lifecycle",
    )
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
    link_parser = sub.add_parser(
        "k3s-link",
        help="the flannel MTU (6.5) and agent-token rotation (5.7) rehearsal on a K3s server and one agent",
    )
    link_parser.add_argument("--report", type=Path, default=Path("k3s-link-report.json"))
    link_parser.add_argument("--workdir", type=Path, default=None, help="scratch directory (default: a new temporary one)")
    link_parser.add_argument("--keep", action="store_true", help="leave the cluster running for inspection")
    link_parser.add_argument("--label", default=None, help="a key=value label for every container, network and volume it creates")
    link_parser.add_argument("--image-label", default=None, help="a key=value label for the image it builds")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run" and args.large_import:
        others = [flag for flag, chosen in (
            ("--cloud-import", args.cloud_import), ("--owner-export", args.owner_export),
            ("--operator-restore-abort", args.operator_restore_abort),
            ("--local-storage-coexistence", args.local_storage_coexistence), ("--steps", args.steps)) if chosen]
        if others:
            parser.error(f"--large-import runs alone; remove {', '.join(others)}")
    if args.command == "run" and args.raw_import:
        others = [flag for flag, chosen in (
            ("--cloud-import", args.cloud_import), ("--owner-export", args.owner_export),
            ("--operator-restore-abort", args.operator_restore_abort), ("--large-import", args.large_import),
            ("--local-storage-coexistence", args.local_storage_coexistence), ("--steps", args.steps)) if chosen]
        if others:
            parser.error(f"--raw-import runs alone; remove {', '.join(others)}")
        if args.raw_import_python is None:
            parser.error("--raw-import needs --raw-import-python")
    if args.command == "local-storage-drill":
        return asyncio.run(drill.run_drill(args))
    if args.command == "k3s-link":
        return asyncio.run(k3s_link.run_link(args))
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    run_id = secrets.token_hex(4)
    workdir = args.workdir or Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"exomem-cloud-rehearsal-{run_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    report = Report(run_id=run_id)
    report.environment = infra.environment_facts()
    if args.large_import:
        # Before any image build or cluster work loads the CPU, so the number is the host's alone.
        report.environment.update(host_speed.calibrate())
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
        "local_storage_coexistence": args.local_storage_coexistence,
        "native_cloud_import": args.cloud_import,
        "owner_export": args.owner_export,
        "operator_restore_abort": args.operator_restore_abort,
        "large_import": args.large_import,
        "raw_import": args.raw_import,
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
    if args.large_import or args.raw_import:
        # The cell must exist and be paid for; every later step is a P3 lifecycle check this mode does not make.
        only = {1, 2}
    if only is not None:
        report.valid = False
        report.invalid_reasons.append(
            "the large-import mode runs steps 1 and 2, then its own scenario" if args.large_import
            else "the raw-import mode runs steps 1 and 2, then its own scenario" if args.raw_import
            else f"only steps {sorted(only)} were selected")
    # The CLI journey needs the disposable CA in the images and versioned object storage.
    native_cli = args.cloud_import or args.large_import or args.raw_import
    node_storage: infra.NodeStorage | None = None
    node: drill_cluster.Agent | None = None
    plan: large_import.LargeImportPlan | None = None
    raw_plan: raw_import.RawImportPlan | None = None

    stack: infra.Stack | None = None
    code = 1
    try:
        if args.large_import:
            with stage(report, "node_storage"):
                node_storage, node = drill_cluster.prepare_node_storage(run_id, workdir, large_import.MAX_POOL_GIB)
        with stage(report, "substrate_source"):
            source = substrate.fetch_source(args.cache, args.substrate_commit)
            substrate.build_source(source)
        with stage(report, "images"):
            v1, v2, broken = build.build_cell_images(run_id, workdir, prebuilt=args.cell_image)
            gateway_tag = build.build_gateway_image(run_id, workdir, source, mode=args.gateway_build)
            cellctl_tag = build.build_cellctl_image(run_id, workdir, mode=args.cellctl_build)
        with stage(report, "infrastructure"):
            stack = infra.create_stack(run_id, workdir, node_storage=node_storage,
                                       tls_storage=native_cli or args.operator_restore_abort or args.owner_export)
            report.adaptations.extend(stack.adaptations)
            if node is not None:
                node.container = stack.k3s.container
        pki = stack.pki or tls.make_pki(workdir)
        tls_stage = "import_tls" if native_cli else "owner_export_tls" if args.owner_export else "restore_tls"
        if native_cli or args.operator_restore_abort or args.owner_export:
            with stage(report, tls_stage):
                source_v1 = v1
                v1, v2, broken = (build.trust_rehearsal_ca(tag, pki.ca_path, workdir, run_id=run_id) for tag in (v1, v2, broken))
                report.overlays.append("Cloud images: the disposable CA is the sole added trust root")
                from .native_import import image_payload

                baseline_payload = image_payload(source_v1, run_id=run_id)
                trusted_payload = image_payload(v1, run_id=run_id)
                if baseline_payload != trusted_payload:
                    raise RuntimeError("the CA layer changed product or model bytes")
                report.stages[tls_stage]["product_and_model"] = trusted_payload
                if native_cli or args.owner_export:
                    from .native_import import enable_versioning

                    if native_cli:
                        report.overlays.append("Native CLI container: legacy resource profile")
                    enable_versioning(stack.object_store)
        if args.operator_restore_abort:
            from .operator_restore_abort import build_writer_image

            with stage(report, "restore_writer_image"):
                v1 = build_writer_image(stack, v1)
                report.overlays.append("Cloud v1 image: restic delegates normally except this run's active in-place writer fixture")
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
        if args.large_import:
            with stage(report, "large_import_sizing"):
                plan = large_import.make_plan(
                    run_id=run_id, workdir=workdir, cli_image=v1, files=args.large_import_files,
                    target_bytes=int(args.large_import_gb * 1e9), smoke=args.large_import_smoke,
                    deadline=started + args.large_import_minutes * 60)
                print(f"[rehearsal] large-import sizing: {plan.sizing.note}", flush=True)
                report.stages["large_import_sizing"].update({
                    **asdict(plan.sizing), "free_bytes": plan.free_bytes, "ceiling_bytes": plan.ceiling,
                    "volume_group": drill_cluster.create_volume_group(node, drill_cluster.pool_gib_for(plan.volume_gib)),
                })
                plan.node = node
        if args.raw_import:
            raw_plan = raw_import.RawImportPlan(run_id=run_id, workdir=workdir, cli_image=v1, python=args.raw_import_python)
        with stage(report, "control_database"):
            await substrate.bootstrap_database(stack)
            report.stages["control_database"]["migrate_tail"] = substrate.migrate(stack, source)[-800:]
        with stage(report, "substrate"):
            control = substrate.start(stack, source, pki, build.CELL_REPOSITORY,
                                      exports_enabled=args.owner_export)
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
                    local_storage_coexistence=args.local_storage_coexistence,
                    local_storage_cell_gib=plan.volume_gib if plan else None,
                    operator_restore_abort=args.operator_restore_abort,
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
            report.stages["platform"]["storage_coexistence"] = deployed.storage_evidence
            report.stages["platform"]["hetzner_class"] = deployed.hetzner_class
            report.stages["platform"]["cell_class"] = deployed.cell_class
            if plan is not None:
                report.stages["platform"]["storage_node"] = platform.label_storage_node(stack, deployed)
            platform.wait_rollout(stack, platform.CLOUD_NAMESPACE, "cellctl")
            platform.wait_rollout(stack, platform.CLOUD_NAMESPACE, "exomem-cloud-gateway")
            platform.wait_rollout(stack, platform.EDGE_NAMESPACE, "rehearsal-traefik")
            _wait_json(resolver, f"https://{tls.MCP_HOST}/.well-known/oauth-protected-resource{substrate.MCP_PATH}", "the gateway through ingress")
        ctx = Context(stack=stack, substrate=control, images=built, resolver=resolver, report=report,
                      cli_image=v1 if args.cloud_import else None,
                      raw_import=raw_plan,
                      owner_export_image=v1 if args.owner_export else None,
                      operator_restore_abort=args.operator_restore_abort, large_import=plan)
        with stage(report, "release"):
            # The owner's first release: cell_image through Substrate's own route.
            await ctx.admin_release({"cellImage": built.cell_v1})
            report.stages["release"]["published_cell_slots"] = await _published_cell_slots(ctx, timeout=120)
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
        elif args.large_import:
            # The mode's own verdict; the P3 outcome is not a gate here (steps 3 to 12 did not run).
            code = 0 if report.stages["large_import"]["verdict"] in ("pass", "smoke_only") else 1
        elif args.raw_import:
            code = 0 if report.stages["raw_import"]["verdict"] == raw_import.PASS else 1
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
        if not guarded(report, "teardown", lambda: infra.teardown(stack, keep=args.keep)):
            code = 2
        if node is not None and not args.keep:
            # After the node's container is gone: only then can the volume group be deactivated and the loop device freed.
            if not guarded(report, "node_storage_release", lambda: drill_cluster.release_node_storage(node, stack, node_storage.image)):
                code = 2
        if not args.keep:
            # This run's own image tags; base images stay cached.
            tags = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], check=False).stdout.split()
            mine = [tag for tag in tags if tag.endswith(f":{run_id}") or f":{run_id}-" in tag]
            if mine:
                run(["docker", "rmi", "--force", *mine], check=False)
        if not guarded(report, "report", lambda: report.write(args.report)):
            code = 2
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


async def _published_cell_slots(ctx: Context, *, timeout: float) -> int:
    """Substrate admits no one until cellctl publishes free slots, and
    cellctl's first pass can land after its rollout reports ready."""
    deadline = time.monotonic() + timeout
    while True:
        row = await ctx.fetchrow("SELECT sum(cell_slots) AS slots FROM exomem_cloud_capacity")
        if row and row["slots"]:
            return row["slots"]
        if time.monotonic() >= deadline:
            raise TimeoutError(f"cellctl published no free cell slots within {timeout:.0f}s")
        await asyncio.sleep(1)


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
    raw = report.stages.get("raw_import")
    if raw and raw.get("verdict"):
        print(f"[rehearsal] raw-import verdict: {raw['verdict']} ({len(raw['findings'])} findings)")
    large = report.stages.get("large_import")
    if large and large.get("verdict"):
        print(f"[rehearsal] large-import verdict: {large['verdict']} ({len(large['findings'])} findings)")
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
