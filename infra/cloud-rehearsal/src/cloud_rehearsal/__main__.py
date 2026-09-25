"""`exomem-cloud-rehearsal run`: P3, the Exomem Cloud local rehearsal.

One command stands up disposable K3s, PostgreSQL with Substrate's real
migrations and grants, Substrate, its gateway, cellctl and the real cell
image; runs the twelve P3 steps in order; writes a JSON report with the
5.3 measurements; and tears everything down. It exits 0 only when every
step passed, every target was met and the run is a valid rehearsal.
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
from .scenarios import Context, post_checks, run_steps
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
    run_parser.add_argument("--steps", default=None, help="comma-separated step numbers to run (default: all)")
    run_parser.add_argument("--keep", action="store_true", help="leave the stack running for inspection")
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
    if not report.inputs["exomem_worktree_clean"]:
        report.invalid_reasons.append("the exomem checkout had uncommitted changes (recorded, not disqualifying)")
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
            gateway_tag = build.build_gateway_image(run_id, workdir, source, mode=args.gateway_build)
            cellctl_tag = build.build_cellctl_image(run_id, workdir)
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
        with _stage(report, "platform"):
            hour = dt.datetime.now(dt.UTC).hour
            # Closed until step 11 opens it, so no nightly backup lands mid-scenario.
            closed_window = f"{(hour + 6) % 24}-{(hour + 7) % 24}"
            deployed = platform.apply(
                stack,
                platform.PlatformConfig(
                    cellctl_image=built.cellctl, gateway_image=built.gateway,
                    cell_repository=build.CELL_REPOSITORY, backup_window=closed_window,
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
        await run_steps(ctx, only=only)
        report.stages["post_checks"] = post_checks(ctx)
        outcome = report.to_json()["outcome"]
        code = 0 if outcome["gates_node"] else 1
    except _StageFailed:
        code = 2
    except Exception as error:  # noqa: BLE001 - recorded in the report
        report.stages.setdefault("harness", {})["failure"] = failure_record(error)
        code = 2
    finally:
        if stack is not None and not args.keep:
            _collect_diagnostics(stack, workdir)
        report.write(args.report)
        infra.teardown(stack, keep=args.keep)
        if not args.keep and args.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
    _print_summary(report, args.report)
    return code


class _StageFailed(Exception):
    pass


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
        "exomem-cloud-cell-token-key": {"current": base64.b64encode(key).decode(), "currentVersion": "1"},
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
