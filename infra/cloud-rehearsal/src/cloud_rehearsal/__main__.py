"""`exomem-cloud-rehearsal run`: P3, the Exomem Cloud local rehearsal.

One command stands up disposable K3s, PostgreSQL with Substrate's real
migrations and grants, Substrate, its gateway, cellctl and the real cell
image; runs the twelve P3 steps in order; writes a JSON report with the
5.3 measurements; and tears everything down.

Exit codes: 0 when the report gates the node (every step passed, every
target met, a valid rehearsal); 1 when it recorded product findings; 2 when
the rehearsal itself failed (a stage could not be stood up, or a step raised
something other than a recorded finding, or, with `--harness-check`, a
step failed that the known-findings baseline does not name). `--harness-check`
turns 1 into 0 for pull-request CI, where the question is whether the
harness works.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

from . import build, images, infra, platform, substrate, tls
from .http import Resolver
from .report import Report, failure_record
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
        with _stage(report, "substrate_source"):
            source = substrate.fetch_source(args.cache, args.substrate_commit)
            substrate.build_source(source)
        with _stage(report, "images"):
            v1, v2, broken, cell_overlays = build.build_cell_images(run_id, workdir, prebuilt=args.cell_image)
            report.overlays.extend(cell_overlays)
            report.product_overlays.extend(o for o in cell_overlays if o.startswith("APPLIED"))
            gateway_tag = build.build_gateway_image(run_id, workdir, source, mode=args.gateway_build)
            cellctl_tag = build.build_cellctl_image(run_id, workdir, mode=args.cellctl_build)
        with _stage(report, "infrastructure"):
            stack = infra.create_stack(run_id, workdir)
            report.adaptations.extend(stack.adaptations)
        with _stage(report, "images_into_k3s"):
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
        with _stage(report, "control_database"):
            await substrate.bootstrap_database(stack)
            report.stages["control_database"]["migrate_tail"] = substrate.migrate(stack, source)[-800:]
        pki = tls.make_pki(workdir)
        with _stage(report, "substrate"):
            control = substrate.start(stack, source, pki, build.CELL_REPOSITORY)
            resolver = Resolver(pki.ca_path, {tls.SUBSTRATE_HOST: control.edge_host_port, tls.MCP_HOST: stack.k3s.ingress_host_port})
            _wait_json(resolver, f"{substrate.PUBLIC_BASE_URL}/.well-known/oauth-authorization-server/api/exomem/oauth", "Substrate")
            report.stages["substrate"]["egress_sealed"] = substrate.sealed_egress_refused(control)
            if not report.stages["substrate"]["egress_sealed"]:
                raise RuntimeError("Substrate's network is not sealed: a TEST-NET address did not fail with no route")
        with _stage(report, "platform"):
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
                s3_access_key=stack.object_store.access_key,
                s3_secret_key=stack.object_store.secret_key,
                gateway_env=_gateway_env(),
                gateway_secret_env={
                    "EXOMEM_CONTROL_PLANE_KEY": control.secrets.control_plane_key,
                    "EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_VALUE": control.secrets.ingress_source_value,
                    "EXOMEM_CLOUD_CELL_TOKEN_KEY": control.secrets.cell_token_key.hex(),
                },
                cellctl_secrets=_cellctl_secrets(stack, control),
                pki=pki,
                ingress_source_value=control.secrets.ingress_source_value,
                ingress_image=traefik,
            )
            report.overlays.extend(deployed.overlays)
            for defect in deployed.chart_defects:
                report.defects.append({"step": "platform", "cross_lane": True, "owner": "Artexis10/exomem#1368", **defect})
                report.product_overlays.append(f"gateway environment overlay: {', '.join(defect['missing'])}")
            platform.wait_rollout(stack, platform.CLOUD_NAMESPACE, "cellctl")
            platform.wait_rollout(stack, platform.CLOUD_NAMESPACE, "exomem-cloud-gateway")
            platform.wait_rollout(stack, platform.PLATFORM_NAMESPACE, "rehearsal-traefik")
            _wait_json(resolver, f"https://{tls.MCP_HOST}/.well-known/oauth-protected-resource{substrate.MCP_PATH}", "the gateway through ingress")
        ctx = Context(stack=stack, substrate=control, images=built, resolver=resolver, report=report)
        with _stage(report, "release"):
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
    except _StageFailed:
        code = 2
    except Exception as error:  # noqa: BLE001 - recorded in the report
        report.stages.setdefault("harness", {})["failure"] = failure_record(error)
        code = 2
    finally:
        # Each cleanup stands alone, so one failing never strands the stack
        # or loses the report.
        if stack is not None and not args.keep:
            _guarded(report, "diagnostics", lambda: _collect_diagnostics(stack, workdir))
        _guarded(report, "report", lambda: report.write(args.report))
        _guarded(report, "teardown", lambda: infra.teardown(stack, keep=args.keep))
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


class _StageFailed(Exception):
    pass


def _guarded(report: Report, what: str, action) -> None:  # noqa: ANN001 - a zero-argument callable
    try:
        action()
    except Exception as error:  # noqa: BLE001 - cleanup must continue
        report.notes.append(f"cleanup step {what} failed: {type(error).__name__}: {str(error)[:300]}")
        print(f"[rehearsal] cleanup {what} failed: {error}", flush=True)


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
        # reason: the same step failing any other way is a regression.
        expected = entry is not None and step.status == "failed" and entry["match"] in message
        if step.status != "passed" and not expected:
            unexpected.append({"step": step.number, "status": step.status, "message": message[:300]})
        if step.status == "passed" and entry is not None:
            resolved.append({"step": step.number, "baseline": entry["owner"]})
    return {"known_findings": {str(k): v for k, v in known.items()}, "unexpected": unexpected, "resolved_known_findings": resolved}


class _stage:  # noqa: N801 - used as a context manager
    def __init__(self, report: Report, name: str) -> None:
        self.report, self.name = report, name

    def __enter__(self) -> None:
        print(f"[rehearsal] stage {self.name}: start", flush=True)
        self.started = time.monotonic()
        self.report.stages[self.name] = {"status": "running"}

    def __exit__(self, kind, error, _tb) -> bool:  # noqa: ANN001
        record = self.report.stages[self.name]
        record["seconds"] = round(time.monotonic() - self.started, 1)
        if error is None:
            record["status"] = "passed"
            print(f"[rehearsal] stage {self.name}: done in {record['seconds']}s", flush=True)
            return False
        record["status"] = "failed"
        record["failure"] = failure_record(error)
        print(f"[rehearsal] stage {self.name}: FAILED -- {str(error)[:1500]}", flush=True)
        raise _StageFailed from error


def _wait_json(resolver: Resolver, url: str, what: str) -> None:
    def probe() -> bool:
        with resolver.client() as client:
            response = client.get(url)
            return response.status_code == 200 and isinstance(response.json(), dict)

    wait_for(probe, timeout=300, interval=3, description=f"{what} to answer {url}")


def _gateway_env() -> dict[str, str]:
    """What the pinned Substrate gateway requires that is not a secret."""

    return {
        "EXOMEM_PUBLIC_BASE_URL": substrate.PUBLIC_BASE_URL,
        "EXOMEM_CLOUD_MCP_URL": substrate.MCP_URL,
        "EXOMEM_CLOUD_MCP_PATH": substrate.MCP_PATH,
        "EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_HEADER": platform.TRUSTED_INGRESS_HEADER,
        # Required by validateGatewayEnvironment for the hosted path the same
        # process still serves; values as the hosted runbook sets them.
        "EXOMEM_CELL_PROTOCOL_VERSION": "1",
        "EXOMEM_GATEWAY_CONTROL_HOSTNAME": tls.MCP_HOST,
        "EXOMEM_GATEWAY_INTERNAL_ORIGIN": f"http://exomem-cloud-gateway.{platform.CLOUD_NAMESPACE}.svc.cluster.local:8080",
    }


def _cellctl_secrets(stack: infra.Stack, control: substrate.Substrate) -> dict[str, dict[str, str]]:
    key = control.secrets.cell_token_key
    return {
        "exomem-cellctl-database-dsn": {"dsn": stack.postgres.dsn("exomem_cellctl", from_host=False)},
        "exomem-cloud-gateway-database": {"url": stack.postgres.dsn("exomem_gateway", from_host=False)},
        # D7: 64 hex characters, read by both cellctl and the gateway.
        "exomem-cloud-cell-token-key": {"current": key.hex(), "currentVersion": "1"},
        "exomem-cloud-gateway-control-plane-key": {"key": control.secrets.control_plane_key},
        "exomem-cloud-backup-master-key": {
            "keys": json.dumps({"1": base64.b64encode(secrets.token_bytes(32)).decode()}),
            "currentVersion": "1",
        },
        # Doubled in rehearsal_cellctl; present only because the chart requires them.
        "exomem-cloud-b2-key-management": {"keyId": "rehearsal-double", "applicationKey": "rehearsal-double"},
        "exomem-cloud-hetzner-read-token": {"token": "rehearsal-double"},
    }


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
        print(f"[rehearsal]   {name:<34} {measurement['observed']} (target {measurement['comparison']} {measurement['target']}, met={measurement['met']})")
    print(f"[rehearsal] outcome: {json.dumps(data['outcome'])}")
    if data["cross_lane_defects"]:
        print(f"[rehearsal] cross-lane defects: {len(data['cross_lane_defects'])}")


if __name__ == "__main__":
    sys.exit(main())
