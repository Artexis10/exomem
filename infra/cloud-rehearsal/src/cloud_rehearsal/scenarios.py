"""The twelve P3 steps (design.md Migration Plan step 3, task 5.2), in order.

Each step drives the stack only through the surfaces a real actor uses: an
invitee's browser, a connector (the OAuth MCP client), Paddle's signed
webhook, the owner's admin release route, Substrate's scheduler, and the
operator's kubectl. Checks read the control database and the cluster as an
operator would. A step that fails records why and the run continues; a step
whose precondition failed is recorded as blocked, naming the step it
depends on.

Tenant A is a paid tenant (Paddle activation) and the owner, so its cell is
the canary. Tenant B is complimentary and joins at step 10.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import boto3
from botocore.config import Config
from cellctl.manifests import (
    CellManifestSpec,
    namespace_name,
    render_cell_manifests,
    render_restore_job,
)

from . import substrate as substrate_mod
from .build import BuiltImages
from .http import Resolver
from .infra import BACKUP_BUCKET, Stack
from .mcp_client import (
    PKCE_FULL_GRAMMAR,
    BrowserSession,
    HeadlessBrowser,
    TenantClient,
    pkce_grammar,
    raw_mcp_post,
)
from .platform import CLOUD_NAMESPACE
from .report import (
    BLOCKED,
    FAILED,
    PASSED,
    CrossLaneDefect,
    Report,
    StepFailure,
    StepRecord,
    failure_record,
    innermost,
)
from .shell import run
from .substrate import PUBLIC_BASE_URL, Substrate

CLOUD_EXCLUSIONS = {"transfer_artifact", "adopt_vault", "process_media", "read_media"}
WARM_ITERATIONS = 20
LATENCY_ITERATIONS = 10
CELLCTL_REPO = "Artexis10/exomem#1368"

# Runs inside a cell: its own readiness report, reduced to content-free fields.
HEALTH_READY_PROBE = """
import json, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8765/health/ready")
    print(json.dumps({"status": "ready"}))
except Exception as error:
    report = json.loads(error.read())
    print(json.dumps({
        "status": report.get("status"),
        "reasons": report.get("reasons"),
        "components": (report.get("cutover") or {}).get("components"),
    }))
"""

# Runs inside the scratch cell: its own bearer is in its own environment.
SCRATCH_PROBE = """
import asyncio, json, os, sys
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

NOTE = "Written in the scratch restore." + chr(10) * 2 + "## Observations" + chr(10) * 2 + "- [rehearsal] scratch write #rehearsal" + chr(10)

async def main():
    headers = {"Authorization": "Bearer " + os.environ["EXOMEM_CLOUD_CELL_TOKEN"]}
    async with httpx.AsyncClient(headers=headers, timeout=120) as http, streamable_http_client(
        "http://127.0.0.1:8765/mcp", http_client=http
    ) as streams:
        # Older SDKs yield (read, write, session_id), newer (read, write).
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            recall = await session.call_tool("ask_memory", {"query": sys.argv[1]})
            written = await session.call_tool("remember", {"title": "Scratch restore write", "content": NOTE, "status": "draft"})
            def structured(result):
                return getattr(result, "structuredContent", None) or getattr(result, "structured_content", None) or {}

            print(json.dumps({"recall": json.dumps(structured(recall)), "write_error": structured(written).get("error")}))

asyncio.run(main())
"""
SUBSTRATE_REPO = "substrate-systems/substrate#181"


@dataclass
class Tenant:
    label: str
    email: str
    paid: bool
    user_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    cell_id: str | None = None
    session: BrowserSession | None = None
    client: TenantClient | None = None
    subscription_id: str | None = None
    transaction_id: str | None = None
    phrase: str = field(default_factory=lambda: f"ultramarine-{secrets.token_hex(4)}-heron")
    notes: list[str] = field(default_factory=list)


@dataclass
class Context:
    stack: Stack
    substrate: Substrate
    images: BuiltImages
    resolver: Resolver
    report: Report
    a: Tenant = field(default_factory=lambda: Tenant("A", f"owner-{secrets.token_hex(3)}@rehearsal.test", paid=True))
    b: Tenant = field(default_factory=lambda: Tenant("B", f"friend-{secrets.token_hex(3)}@rehearsal.test", paid=False))
    browser: HeadlessBrowser = field(init=False)
    log_snapshots: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.browser = HeadlessBrowser(self.resolver)

    # --- control database, read as the operator ---

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        connection = await asyncpg.connect(self.substrate.owner_dsn)
        try:
            row = await connection.fetchrow(sql, *args)
            return dict(row) if row else None
        finally:
            await connection.close()

    async def execute(self, sql: str, *args: Any) -> None:
        connection = await asyncpg.connect(self.substrate.owner_dsn)
        try:
            await connection.execute(sql, *args)
        finally:
            await connection.close()

    async def cell(self, cell_id: str) -> dict[str, Any]:
        row = await self.fetchrow("SELECT * FROM exomem_cloud_cells WHERE cell_id = $1", cell_id)
        if row is None:
            raise StepFailure(f"no exomem_cloud_cells row for {cell_id}")
        return row

    async def rollout(self) -> dict[str, Any]:
        return await self.fetchrow("SELECT * FROM exomem_cloud_rollout WHERE id = 1") or {}

    async def wait_cell(self, cell_id: str, predicate: Callable[[dict[str, Any]], bool], *, timeout: float, description: str, interval: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        row: dict[str, Any] = {}
        while time.monotonic() < deadline:
            row = await self.cell(cell_id)
            if predicate(row):
                return row
            await asyncio.sleep(interval)
        raise StepFailure(f"timed out after {timeout:.0f}s waiting for {description}; last row: {cell_summary(row)}")

    # --- Substrate surfaces ---

    async def admin_release(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.resolver.async_client() as client:
            response = await client.put(
                f"{PUBLIC_BASE_URL}/api/exomem/admin/cloud-release",
                json=payload,
                headers={"authorization": f"Bearer {self.substrate.secrets.admin_token}"},
            )
        if response.status_code != 200:
            raise CrossLaneDefect(
                f"PUT /api/exomem/admin/cloud-release answered {response.status_code}",
                component="Substrate admin cloud-release route", owner=SUBSTRATE_REPO,
                evidence={"status": response.status_code, "body": _safe_body(response)},
            )
        return response.json()

    async def paddle(self, event: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(event, separators=(",", ":")).encode()
        async with self.resolver.async_client() as client:
            response = await client.post(
                f"{PUBLIC_BASE_URL}/api/webhooks/paddle",
                content=raw,
                headers={
                    "content-type": "application/json",
                    "paddle-signature": substrate_mod.paddle_signature(self.substrate.secrets.paddle_webhook_secret, raw),
                },
            )
        if response.status_code >= 300:
            raise CrossLaneDefect(
                f"the signed Paddle {event['event_type']} webhook answered {response.status_code}",
                component="Substrate Paddle webhook", owner=SUBSTRATE_REPO,
                evidence={"status": response.status_code, "body": _safe_body(response), "event_type": event["event_type"]},
            )
        return {"status": response.status_code}

    async def scheduler(self) -> int:
        async with self.resolver.async_client(timeout=120.0) as client:
            response = await client.get(
                f"{PUBLIC_BASE_URL}/api/cron/exomem-reconcile",
                headers={"authorization": f"Bearer {self.substrate.secrets.scheduler_secret}"},
            )
        return response.status_code

    async def identify(self, tenant: Tenant) -> None:
        row = await self.fetchrow(
            """SELECT i.consumed_by_user_id AS user_id, i.redeemed_tenant_id AS tenant_id, c.cell_id
                 FROM exomem_invites i
                 JOIN exomem_cloud_cells c ON c.tenant_id = i.redeemed_tenant_id AND c.desired_state <> 'deleted'
                WHERE i.email_normalized = $1 AND i.consumed_at IS NOT NULL""",
            tenant.email.lower(),
        )
        if row is None:
            raise StepFailure(f"tenant {tenant.label}: redemption left no user, tenant and cell rows")
        tenant.user_id, tenant.tenant_id, tenant.cell_id = row["user_id"], row["tenant_id"], row["cell_id"]

    # --- the cluster, read as the operator ---

    def kubectl(self, *args: str, check: bool = True, input_text: str | None = None):
        return self.stack.k3s.kubectl(*args, check=check, input_text=input_text)

    def kube_json(self, *args: str) -> dict[str, Any]:
        return json.loads(self.kubectl(*args, "--output=json").stdout)

    def runtime_pod(self, cell_id: str) -> dict[str, Any] | None:
        pods = self.kube_json("get", "pods", "--namespace", namespace_name(cell_id), "--selector=!exomem.io/cell-job")["items"]
        live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        return live[0] if live else None

    def exec_in(self, namespace: str, pod: str, *command: str, check: bool = True):
        return self.kubectl("exec", "--namespace", namespace, pod, "--container", "exomem", "--", *command, check=check)

    def health_ready(self, cell_id: str) -> dict[str, Any]:
        """The cell's own readiness report, content-free (reasons and component states)."""

        pod = self.runtime_pod(cell_id)
        if pod is None:
            return {"pod": None}
        probe = self.exec_in(
            namespace_name(cell_id), pod["metadata"]["name"], "python3", "-c", HEALTH_READY_PROBE,
            check=False,
        )
        return _json_or_text(probe.stdout)

    def pod_ready(self, cell_id: str) -> bool:
        pod = self.runtime_pod(cell_id)
        return bool(pod) and any(
            c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", [])  # type: ignore[index]
        )

    async def stays_ready(self, cell_id: str, *, seconds: float) -> dict[str, Any] | None:
        """None when the cell's pod stayed Ready throughout, else what it reported."""

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.pod_ready(cell_id):
                await asyncio.sleep(5)
                if not self.pod_ready(cell_id):
                    row = await self.cell(cell_id)
                    return {"health_ready": self.health_ready(cell_id), "row_ready": row.get("ready"), "row_observed_state": row.get("observed_state")}
            await asyncio.sleep(3)
        return None

    def s3(self):
        store = self.stack.object_store
        return boto3.client(
            "s3", endpoint_url=store.endpoint(from_host=True), aws_access_key_id=store.access_key,
            aws_secret_access_key=store.secret_key, region_name="us-east-1",
            # proxies={} keeps an ambient HTTP(S)_PROXY off the S3 double.
            config=Config(s3={"addressing_style": "path"}, proxies={}),
        )

    def s3_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for page in self.s3().get_paginator("list_objects_v2").paginate(Bucket=BACKUP_BUCKET, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def set_backup_window(self, window: str) -> None:
        """A chart value change: cellctl's Deployment env, rolled with Recreate."""

        self.report.overlays.append(
            f"cellctl Deployment: CELLCTL_BACKUP_WINDOW={window} at step 11 (the chart value cells.backupWindow, "
            "changed so a nightly backup falls inside the run)"
        )
        self.kubectl("set", "env", "deployment/cellctl", "--namespace", CLOUD_NAMESPACE, f"CELLCTL_BACKUP_WINDOW={window}")
        self.kubectl("rollout", "status", "deployment/cellctl", "--namespace", CLOUD_NAMESPACE, "--timeout=180s")


def cell_summary(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("cell_id", "desired_state", "desired_image", "generation", "observed_generation", "observed_state",
            "observed_image", "ready", "last_error_code", "hold_kind", "hold_started_at", "last_backup_at",
            "last_backup_snapshot", "b2_key_id")
    summary = {key: row.get(key) for key in keys if key in row}
    if summary.get("b2_key_id"):
        summary["b2_key_id"] = "<set>"
    return {key: (value.isoformat() if isinstance(value, dt.datetime) else value) for key, value in summary.items()}


def _safe_body(response: Any) -> Any:
    try:
        body = response.json()
    except ValueError:
        return f"<{len(response.content)} bytes, not JSON>"
    if isinstance(body, dict):
        return {key: body[key] for key in ("error", "code", "message", "requestId") if key in body}
    return "<non-object JSON>"


def _running_ready(image: str | None = None) -> Callable[[dict[str, Any]], bool]:
    def check(row: dict[str, Any]) -> bool:
        return (
            row.get("observed_state") in ("running", "read_only")
            and bool(row.get("ready"))
            and row.get("hold_kind") is None
            and (image is None or row.get("observed_image") == image)
        )

    return check


async def first_session(client: TenantClient, record: StepRecord, *, timeout: float = 120.0) -> list[str]:
    """The connector's first MCP session once its cell is observed ready.

    A pod that just turned Ready can still be unreachable through the
    gateway for a few seconds (its NetworkPolicy and Service endpoints
    converge after readiness), and the gateway answers 503 CELL_NOT_READY
    meanwhile, as C3 says. A connector retries; so does this, and the
    evidence records how long the gap was.
    """

    started = time.monotonic()
    refusals = 0
    while True:
        try:
            async with client.mcp() as session:
                tools, _ = await session.list_tools()
            record.evidence["gateway_503_before_serving"] = {"count": refusals, "seconds": round(time.monotonic() - started, 2)}
            return tools
        except Exception as error:  # noqa: BLE001 - only a 503 is retried
            if "503" not in _flatten(error) or time.monotonic() - started > timeout:
                raise
            refusals += 1
            await asyncio.sleep(1)


def _require(condition: object) -> None:
    """A step's precondition, left unmet by an earlier step's recorded failure."""

    if not condition:
        raise StepFailure("precondition not met: an earlier step did not leave the state this step needs")


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()[-300:]


def _flatten(error: BaseException) -> str:
    """The messages of an exception and, for a task group, every sub-exception."""

    if isinstance(error, BaseExceptionGroup):
        return "; ".join(_flatten(inner) for inner in error.exceptions)
    return f"{type(error).__name__}: {error}"


def _cited(result_text: str, needles: list[str]) -> bool:
    return any(needle and needle in result_text for needle in needles)


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


async def step_1_invite(ctx: Context, record: StepRecord) -> None:
    await substrate_mod.seed_oauth_client(ctx.substrate)
    ctx.a.notes.append(await substrate_mod.seed_invite(ctx.substrate, ctx.a.email, paid=True))
    invite = await ctx.fetchrow(
        "SELECT entitlement_source, consumed_at IS NULL AND revoked_at IS NULL AS unredeemed, expires_at > now() AS live FROM exomem_invites WHERE email_normalized = $1",
        ctx.a.email.lower(),
    )
    if not invite or not invite["unredeemed"] or not invite["live"]:
        raise StepFailure(f"the seeded invite is not a live, unredeemed invite: {invite}")
    record.evidence.update(
        {
            "invite": {"entitlement_source": invite["entitlement_source"], "unredeemed": True},
            "delivery": "injected: the invite secret is minted by the rehearsal and only its SHA-256 digest is stored, "
            "as POST /api/exomem/admin/invites stores it before emailing (no mail service is reachable)",
            "substrate_egress_sealed": substrate_mod.sealed_egress_refused(ctx.substrate),
        }
    )


async def step_2_provision(ctx: Context, record: StepRecord) -> None:
    token = ctx.a.notes.pop(0)
    ctx.a.session = await ctx.browser.redeem_invite(token)
    await ctx.identify(ctx.a)
    _require(ctx.a.cell_id and ctx.a.tenant_id and ctx.a.user_id)
    # The owner's cell is the canary (D6): the lowest rollout_priority.
    await ctx.execute("UPDATE exomem_cloud_cells SET rollout_priority = 0 WHERE cell_id = $1", ctx.a.cell_id)
    admitted = await ctx.cell(ctx.a.cell_id)
    record.evidence["admitted"] = cell_summary(admitted)
    if admitted["desired_state"] != "stopped":
        raise StepFailure(f"a paid invite must admit a stopped cell awaiting checkout, got {admitted['desired_state']}")

    ctx.a.transaction_id = await substrate_mod.bind_checkout(ctx.substrate, ctx.a.tenant_id)
    ctx.a.subscription_id = substrate_mod.paddle_id("sub")
    started = time.monotonic()
    await ctx.paddle(
        substrate_mod.paddle_event(
            event_type="subscription.activated", status="active", user_id=ctx.a.user_id, tenant_id=ctx.a.tenant_id,
            subscription_id=ctx.a.subscription_id, transaction_id=ctx.a.transaction_id,
        )
    )
    activated = await ctx.cell(ctx.a.cell_id)
    if activated["desired_state"] != "running":
        raise CrossLaneDefect(
            "a signed subscription.activated for a paid Cloud tenant did not set the cell running",
            component="Substrate Paddle hook (reconcileCloudCellDesiredState)", owner=SUBSTRATE_REPO,
            evidence={"cell": cell_summary(activated)},
        )
    row = await ctx.wait_cell(ctx.a.cell_id, _running_ready(), timeout=600, description="tenant A's cell to be running and ready")
    provision = time.monotonic() - started
    ctx.report.single["provision_seconds"] = provision
    pod = ctx.runtime_pod(ctx.a.cell_id)
    namespace = namespace_name(ctx.a.cell_id)
    pvc = ctx.kube_json("get", "pvc", "cell-data", "--namespace", namespace)
    record.evidence.update(
        {
            "provision_seconds": round(provision, 2),
            "measured_from": "the signed subscription.activated webhook's acceptance (the moment the tenant is entitled)",
            "measured_to": "cellctl's observed row: running and ready on the pod of the StatefulSet's update revision",
            "cell": cell_summary(row),
            "namespace": namespace,
            "pvc": {"phase": pvc["status"].get("phase"), "storageClassName": pvc["spec"].get("storageClassName")},
            "pod": pod["metadata"]["name"] if pod else None,
        }
    )
    if provision >= 180:
        raise StepFailure(f"provisioning took {provision:.1f}s, target < 180s")


async def step_3_oauth_mcp(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.session)
    # First, a verifier using the whole RFC 7636 grammar, as the MCP SDKs
    # generate them. If Substrate refuses it, that is recorded as a defect and
    # the connector continues with the base64url subset (equally valid), so
    # the later steps still run.
    pkce_defect: CrossLaneDefect | None = None
    ctx.a.client = TenantClient(ctx.resolver, ctx.browser, ctx.a.session, ctx.substrate.redirect_uri)
    try:
        with pkce_grammar(PKCE_FULL_GRAMMAR):
            tools = await first_session(ctx.a.client, record)
        record.evidence["pkce_full_rfc7636_grammar"] = "accepted"
    except Exception as error:  # noqa: BLE001 - classified below
        detail = _flatten(error)
        record.evidence["pkce_full_rfc7636_grammar"] = f"refused: {detail[:300]}"
        if "invalid_grant" not in detail:
            raise
        pkce_defect = CrossLaneDefect(
            "Substrate's token endpoint refuses an RFC 7636-valid PKCE verifier containing '.' or '~' "
            "(isPkceVerifier: /^[A-Za-z0-9_-]{43,128}$/); the MCP Python SDK draws verifiers from the full "
            "unreserved set, so its token exchange fails on ~98% of attempts",
            component="Substrate src/lib/exomem-hosted/oauth.ts PKCE_VALUE", owner=SUBSTRATE_REPO,
            evidence={"token_response": "400 invalid_grant", "substrate_log": "exomem_oauth_token_rejection stage=code_shape verifier_wellformed=false"},
        )
        await ctx.a.client.aclose()
        ctx.a.client = TenantClient(ctx.resolver, ctx.browser, ctx.a.session, ctx.substrate.redirect_uri)
        tools = await first_session(ctx.a.client, record)
    if ctx.a.client.authorizations != 1 or not ctx.a.client.access_token:
        raise StepFailure("the connector did not complete exactly one OAuth authorization")
    exposed = CLOUD_EXCLUSIONS & set(tools)
    record.evidence.update(
        {
            "oauth": {
                "discovery": "gateway 401 -> protected-resource metadata -> Substrate authorization-server metadata",
                "grant": "authorization_code + PKCE S256 with resource, pinned public client",
                "authorizations": ctx.a.client.authorizations,
            },
            "tool_count": len(tools),
            "tools": tools,
            "cloud_exclusions_absent": not exposed,
        }
    )
    if exposed:
        raise StepFailure(f"cloud-excluded tools are served: {sorted(exposed)}")
    # 5.3: warm initialize and tools/list, each on a fresh MCP session over
    # the connector's existing token and its one HTTP client, so a sample
    # measures the MCP exchange, not a new TCP or TLS handshake.
    for _ in range(WARM_ITERATIONS):
        async with ctx.a.client.mcp() as session:
            ctx.report.sample("initialize_warm", session.initialize_seconds)
            _, seconds = await session.list_tools()
            ctx.report.sample("tools_list_warm", seconds)
    if ctx.a.client.authorizations != 1:
        raise StepFailure("warm sessions re-ran the authorization instead of reusing the token")
    if pkce_defect is not None:
        raise pkce_defect


async def step_4_capture(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.client)
    body = (
        f"Field notebook, day three. The {ctx.a.phrase} was seen wading at the estuary at dawn, "
        "stalking small fish in the shallows before the tide turned."
    )
    async with ctx.a.client.mcp() as session:
        result = await session.call(
            "capture_source",
            {"title": f"Estuary sighting {ctx.a.phrase}", "content": body, "source_kind": "note",
             "why_captured": "rehearsal capture"},
        )
        if result.is_error or result.error_code:
            raise StepFailure(f"capture_source failed: {result.error_code or result.text()[:500]}")
        ctx.report.sample("capture", result.seconds)
        record.evidence["capture"] = {"seconds": round(result.seconds, 4), "result_keys": sorted(result.structured)[:20]}
        for index in range(LATENCY_ITERATIONS - 1):
            extra = await session.call(
                "capture_source",
                {"title": f"Rehearsal latency sample {index} {secrets.token_hex(3)}",
                 "content": f"Latency sample {index}: tide tables for the northern mudflats.", "source_kind": "note"},
            )
            if extra.is_error or extra.error_code:
                raise StepFailure(f"capture_source sample {index} failed: {extra.error_code}")
            ctx.report.sample("capture", extra.seconds)
    record.evidence["samples"] = len(ctx.report.samples.get("capture", []))


async def step_5_cited_recall(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.client)
    query = "what long-legged wading bird was watched hunting fish near the river mouth early in the morning?"
    async with ctx.a.client.mcp() as session:
        answers = []
        for _ in range(LATENCY_ITERATIONS):
            result = await session.call("ask_memory", {"query": query})
            if result.is_error or result.error_code:
                raise StepFailure(f"ask_memory failed: {result.error_code or result.text()[:300]}")
            answers.append(result)
            ctx.report.sample("cited_recall", result.seconds)
    text = answers[0].text()
    record.evidence.update(
        {
            "query_is_paraphrase": ctx.a.phrase not in query,
            "cites_the_capture": ctx.a.phrase in text,
            "cited_paths": [
                hit.get("path") for hit in (answers[0].structured.get("result") or {}).get("hits", [])[:5]
                if isinstance(hit, dict)
            ],
        }
    )
    if ctx.a.phrase not in text:
        raise StepFailure("paraphrased recall did not cite the captured source")


async def step_6_write_after_pod_kill(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.client and ctx.a.cell_id)
    namespace = namespace_name(ctx.a.cell_id)
    pod = ctx.runtime_pod(ctx.a.cell_id)
    if pod is None:
        raise StepFailure("tenant A has no runtime pod")
    old_uid = pod["metadata"]["uid"]
    started = time.monotonic()
    ctx.kubectl("delete", "pod", "--namespace", namespace, pod["metadata"]["name"], "--wait=false")

    def replaced() -> bool:
        current = ctx.runtime_pod(ctx.a.cell_id)  # type: ignore[arg-type]
        return bool(current and current["metadata"]["uid"] != old_uid and any(
            c["type"] == "Ready" and c["status"] == "True" for c in current["status"].get("conditions", [])
        ))

    deadline = time.monotonic() + 300
    while not replaced():
        if time.monotonic() > deadline:
            raise StepFailure("the killed pod was not replaced by a Ready pod within 300s")
        await asyncio.sleep(1)
    recovered = time.monotonic() - started
    note_title = f"Tide decision {ctx.a.phrase}"
    async with ctx.a.client.mcp() as session:
        write = await session.governed_write(
            {
                "title": note_title,
                "content": (
                    f"We decided to survey the estuary only on falling tides after the {ctx.a.phrase} sighting.\n\n"
                    "## Observations\n\n- [survey] Survey the estuary on falling tides only #fieldwork\n"
                ),
                "status": "draft",
            }
        )
        if write.is_error or write.error_code:
            raise StepFailure(f"the governed write after the pod kill failed: {write.error_code or write.text()[:400]}")
        readback = await session.call("ask_memory", {"query": f"{ctx.a.phrase} falling tides survey decision"})
    modes = ctx.exec_in(
        namespace, ctx.runtime_pod(ctx.a.cell_id)["metadata"]["name"],  # type: ignore[index]
        "python3", "-c",
        "import os,stat,json;r='/data/host/.local/state/exomem/standalone-host-control-v1';"
        "print(json.dumps({p:oct(stat.S_IMODE(os.stat(p).st_mode)) for p in ['/data/vault','/data/host',r] if os.path.exists(p)}))",
    ).stdout
    record.evidence.update(
        {
            "pod_replaced_ready_seconds": round(recovered, 2),
            "governed_write": "committed",
            "write_recalled": note_title in readback.text() or ctx.a.phrase in readback.text(),
            "owner_only_modes": json.loads(modes),
        }
    )
    observed_modes = json.loads(modes)
    # The vault and the account home always exist after cell-init. The
    # standalone custody root is created only when governance first needs
    # custody, which a fresh cell's ordinary governed writes do not (D1): it
    # is checked when present and its absence is recorded.
    record.evidence["custody_root_present"] = len(observed_modes) == 3
    if not {"/data/vault", "/data/host"} <= set(observed_modes):
        raise StepFailure(f"owner-only paths missing after pod replacement: found only {sorted(observed_modes)}")
    bad = {path: mode for path, mode in observed_modes.items() if mode != "0o700"}
    if bad:
        raise StepFailure(f"owner-only modes lost after pod replacement: {bad}")


async def step_7_upgrade(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.client and ctx.a.cell_id)
    before = await ctx.cell(ctx.a.cell_id)
    if before.get("observed_image") != ctx.images.cell_v1:
        raise StepFailure(f"tenant A is not on the release under test before the upgrade: {before.get('observed_image')}")
    started = time.monotonic()
    snapshot_logs(ctx)
    await ctx.admin_release({"cellImage": ctx.images.cell_v2})
    saw_hold = False

    def upgraded(row: dict[str, Any]) -> bool:
        nonlocal saw_hold
        saw_hold = saw_hold or row.get("hold_kind") == "upgrade"
        return _running_ready(ctx.images.cell_v2)(row)

    row = await ctx.wait_cell(ctx.a.cell_id, upgraded, timeout=1200, description="tenant A to run the upgrade image, Ready")
    upgrade = time.monotonic() - started
    ctx.report.single["upgrade_seconds_per_cell"] = upgrade
    rollout = await ctx.rollout()
    async with ctx.a.client.mcp() as session:
        recall = await session.call("ask_memory", {"query": "which wading bird did we see at the estuary at dawn?"})
    record.evidence.update(
        {
            "upgrade_seconds": round(upgrade, 2),
            "measured_from": "PUT /api/exomem/admin/cloud-release {cellImage}",
            "measured_to": "observed_image is the target on a Ready pod of the update revision, hold removed",
            "includes": "the D6 pre-upgrade backup with the cell stopped",
            "upgrade_hold_observed": saw_hold,
            "pre_upgrade_snapshot_recorded": bool(row.get("last_backup_snapshot")),
            "cell": cell_summary(row),
            "rollout": {"paused": rollout.get("paused"), "last_good_image": rollout.get("last_good_image")},
            "recall_after_upgrade": ctx.a.phrase in recall.text(),
        }
    )
    if ctx.a.phrase not in recall.text():
        raise StepFailure("recall after the upgrade lost the captured source")
    if rollout.get("last_good_image") != ctx.images.cell_v2:
        raise StepFailure(f"last_good_image did not move to the upgrade: {rollout.get('last_good_image')}")


async def step_8_canary_failure(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.client and ctx.a.cell_id)
    marker = f"Pre-canary marker {secrets.token_hex(4)}"
    async with ctx.a.client.mcp() as session:
        write = await session.governed_write(
            {"title": marker, "content": f"{marker}: written after the upgrade, before the canary.\n\n## Observations\n\n- [rehearsal] canary marker #rehearsal\n", "status": "draft"}
        )
        if write.error_code:
            raise StepFailure(f"pre-canary governed write failed: {write.error_code}")
    previous = (await ctx.cell(ctx.a.cell_id))["observed_image"]
    started = time.monotonic()
    snapshot_logs(ctx)
    await ctx.admin_release({"cellImage": ctx.images.cell_broken})
    seen: set[str] = set()

    def returned(row: dict[str, Any]) -> bool:
        if row.get("hold_kind"):
            seen.add(row["hold_kind"])
        return "restore" in seen and _running_ready(previous)(row)

    # D6: 15-minute backup deadline, 10-minute readiness deadline, restore.
    row = await ctx.wait_cell(ctx.a.cell_id, returned, timeout=2400, interval=2, description="the forced canary failure to restore tenant A onto its previous image")
    rollout = await ctx.rollout()
    async with ctx.a.client.mcp() as session:
        readback = await session.call("ask_memory", {"query": marker})
        after = await session.governed_write(
            {"title": f"After restore {secrets.token_hex(3)}", "content": "Written after the restore-based return.\n\n## Observations\n\n- [rehearsal] post-restore write #rehearsal\n", "status": "draft"}
        )
    record.evidence.update(
        {
            "seconds_to_return": round(time.monotonic() - started, 1),
            "holds_observed": sorted(seen),
            "cell": cell_summary(row),
            "rollout": {k: rollout.get(k) for k in ("paused", "error_code", "held_cell_id", "last_good_image")},
            "pre_canary_write_survived": marker in readback.text(),
            "write_after_restore": after.error_code or "committed",
        }
    )
    if not (rollout.get("paused") and rollout.get("error_code") == "UPGRADE_READINESS_TIMEOUT" and rollout.get("held_cell_id") == ctx.a.cell_id):
        raise StepFailure(f"the rollout is not paused on the canary with UPGRADE_READINESS_TIMEOUT: {record.evidence['rollout']}")
    if marker not in readback.text():
        raise StepFailure("the restore lost the governed write made before the canary attempt")
    if after.error_code:
        raise StepFailure(f"a governed write after the restore failed: {after.error_code}")
    # The owner resumes on the known-good release (D6 "While paused").
    await ctx.admin_release({"cellImage": previous, "clearRolloutPause": True})
    resumed = await ctx.rollout()
    record.evidence["resumed"] = {"paused": resumed.get("paused")}


async def step_9_read_only(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.client and ctx.a.cell_id and ctx.a.user_id and ctx.a.tenant_id)
    snapshot_logs(ctx)
    await ctx.paddle(
        substrate_mod.paddle_event(
            event_type="subscription.past_due", status="past_due", user_id=ctx.a.user_id, tenant_id=ctx.a.tenant_id,
            subscription_id=ctx.a.subscription_id or "", transaction_id=ctx.a.transaction_id or "",
        )
    )
    desired = (await ctx.cell(ctx.a.cell_id))["desired_state"]
    if desired != "read_only":
        raise CrossLaneDefect(
            f"subscription.past_due set desired_state {desired!r}, expected read_only",
            component="Substrate Paddle hook (reconcileCloudCellDesiredState)", owner=SUBSTRATE_REPO,
            evidence={"desired_state": desired},
        )
    row = await ctx.wait_cell(ctx.a.cell_id, lambda r: r.get("observed_state") == "read_only" and bool(r.get("ready")), timeout=600, description="tenant A to serve read-only")
    async with ctx.a.client.mcp() as session:
        refused = await session.call(
            "remember", {"title": f"Refused {secrets.token_hex(3)}", "content": "must be refused\n\n## Observations\n\n- [rehearsal] refused #rehearsal\n", "status": "draft"}
        )
        read = await session.call("ask_memory", {"query": "estuary wading bird"})
    record.evidence.update(
        {
            "cell": cell_summary(row),
            "write_refusal_code": refused.error_code,
            "read_kept_working": ctx.a.phrase in read.text(),
        }
    )
    if refused.error_code != "CLOUD_CELL_READ_ONLY":
        raise StepFailure(f"a write in read-only mode was not refused with CLOUD_CELL_READ_ONLY: {refused.error_code}")
    if ctx.a.phrase not in read.text():
        raise StepFailure("reads stopped working in read-only mode")
    snapshot_logs(ctx)
    # Payment recovers: back to running, writes work again.
    await ctx.paddle(
        substrate_mod.paddle_event(
            event_type="subscription.updated", status="active", user_id=ctx.a.user_id, tenant_id=ctx.a.tenant_id,
            subscription_id=ctx.a.subscription_id or "", transaction_id=ctx.a.transaction_id or "",
        )
    )
    await ctx.wait_cell(ctx.a.cell_id, lambda r: r.get("observed_state") == "running" and bool(r.get("ready")), timeout=600, description="tenant A to serve writes again")
    async with ctx.a.client.mcp() as session:
        write = await session.governed_write(
            {"title": f"Recovered {secrets.token_hex(3)}", "content": "writes work again\n\n## Observations\n\n- [rehearsal] recovered #rehearsal\n", "status": "draft"}
        )
    record.evidence["write_after_recovery"] = write.error_code or "committed"
    if write.error_code:
        raise StepFailure(f"writes did not recover after the subscription became active: {write.error_code}")
    # The cell must stay serving after the recovery, not only answer once.
    unready = await ctx.stays_ready(ctx.a.cell_id, seconds=60)
    record.evidence["stays_ready_after_recovery"] = unready or "ready for 60s"
    if unready:
        raise StepFailure(
            "after the read_only -> running recovery and one governed write, the cell's /health/ready "
            f"dropped to not-ready and stayed there: {unready}"
        )


async def step_10_second_tenant_denial(ctx: Context, record: StepRecord) -> None:
    try:
        await _step_10(ctx, record)
    except Exception:
        # Which session failed, and what the gateway says to each tenant now.
        for tenant in (ctx.a, ctx.b):
            if tenant.client and tenant.client.access_token:
                probe = await raw_mcp_post(ctx.resolver, token=tenant.client.access_token)
                record.evidence[f"gateway_now_{tenant.label}"] = {"status": probe.status_code, "body": _safe_body(probe)}
        raise


async def _step_10(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.client and ctx.a.cell_id)
    token = await substrate_mod.seed_invite(ctx.substrate, ctx.b.email, paid=False)
    started = time.monotonic()
    ctx.b.session = await ctx.browser.redeem_invite(token)
    await ctx.identify(ctx.b)
    _require(ctx.b.cell_id)
    await ctx.wait_cell(ctx.b.cell_id, _running_ready(), timeout=600, description="tenant B's cell to be running and ready")
    record.evidence["b_provision_seconds"] = round(time.monotonic() - started, 2)
    ctx.b.client = TenantClient(ctx.resolver, ctx.browser, ctx.b.session, ctx.substrate.redirect_uri)
    record.evidence["phase"] = "B first session"
    await first_session(ctx.b.client, record)
    record.evidence["phase"] = "B write and cross-recall"
    async with ctx.b.client.mcp() as session:
        write = await session.governed_write(
            {"title": f"Garden plan {ctx.b.phrase}", "content": f"Plant the {ctx.b.phrase} beans by the south fence.\n\n## Observations\n\n- [garden] beans by the south fence #garden\n", "status": "draft"}
        )
        if write.error_code:
            raise StepFailure(f"tenant B's governed write failed: {write.error_code}")
        b_sees_a = await session.call("ask_memory", {"query": ctx.a.phrase})
    record.evidence["phase"] = "A cross-recall"
    a_probe = await raw_mcp_post(ctx.resolver, token=ctx.a.client.access_token)
    if a_probe.status_code == 503:
        record.evidence["a_unavailable"] = {"status": 503, "body": _safe_body(a_probe), "health_ready": ctx.health_ready(ctx.a.cell_id)}
        a_sees_b = None
    else:
        async with ctx.a.client.mcp() as session:
            a_sees_b = await session.call("ask_memory", {"query": ctx.b.phrase})
    record.evidence["phase"] = "denial probes"
    selector = await raw_mcp_post(ctx.resolver, token=ctx.b.client.access_token, headers={"x-cell-id": ctx.a.cell_id})
    forged = await raw_mcp_post(ctx.resolver, token=secrets.token_urlsafe(32))
    anonymous = await raw_mcp_post(ctx.resolver, token=None)
    # Network: B's cell pod cannot open A's Service (NetworkPolicy, D5).
    a_service_ip = ctx.kube_json("get", "service", "cell", "--namespace", namespace_name(ctx.a.cell_id))["spec"]["clusterIP"]
    b_pod = ctx.runtime_pod(ctx.b.cell_id)
    probe = ctx.exec_in(
        namespace_name(ctx.b.cell_id), b_pod["metadata"]["name"],  # type: ignore[index]
        "python3", "-c",
        f"import socket,sys;s=socket.socket();s.settimeout(3);sys.exit(0 if s.connect_ex(('{a_service_ip}',8765))==0 else 1)",
        check=False,
    )
    record.evidence.update(
        {
            "b_recall_contains_a_phrase": ctx.a.phrase in b_sees_a.text(),
            "a_recall_contains_b_phrase": None if a_sees_b is None else ctx.b.phrase in a_sees_b.text(),
            "selector_header_status": selector.status_code,
            "selector_header_code": _safe_body(selector),
            "forged_token_status": forged.status_code,
            "anonymous_status": anonymous.status_code,
            "anonymous_www_authenticate": "resource_metadata" in anonymous.headers.get("www-authenticate", ""),
            "b_pod_to_a_service_connect": "refused" if probe.returncode != 0 else "connected",
        }
    )
    problems = []
    if ctx.a.phrase in b_sees_a.text():
        problems.append("tenant B recalled tenant A's note")
    if a_sees_b is None:
        problems.append("tenant A's cell answers 503 CELL_NOT_READY, so the A-side cross-recall is unverified (see step 9)")
    elif ctx.b.phrase in a_sees_b.text():
        problems.append("tenant A recalled tenant B's note")
    if selector.status_code != 400:
        problems.append(f"a cell selector header was not refused (status {selector.status_code})")
    if forged.status_code != 401 or anonymous.status_code != 401:
        problems.append(f"forged/anonymous bearer not 401 ({forged.status_code}/{anonymous.status_code})")
    if probe.returncode == 0:
        problems.append("tenant B's pod reached tenant A's Service")
    if problems:
        raise StepFailure("; ".join(problems))


async def step_11_backup_and_scratch_restore(ctx: Context, record: StepRecord) -> None:
    _require(ctx.b.cell_id and ctx.b.client)
    hour = dt.datetime.now(dt.UTC).hour
    window = f"{hour}-{(hour + 2) % 24}"
    snapshot_logs(ctx)
    ctx.set_backup_window(window)
    record.evidence["backup_window_opened"] = window
    saw_backup_hold = False

    def backed_up(row: dict[str, Any]) -> bool:
        nonlocal saw_backup_hold
        saw_backup_hold = saw_backup_hold or row.get("hold_kind") == "backup"
        return bool(row.get("last_backup_snapshot")) and _running_ready()(row)

    row = await ctx.wait_cell(ctx.b.cell_id, backed_up, timeout=1200, interval=2, description="tenant B's nightly backup to finish and the cell to restart")
    snapshot = row["last_backup_snapshot"]
    record.evidence["nightly"] = {"backup_hold_observed": saw_backup_hold, "snapshot_is_64_hex": len(snapshot) == 64, "cell": cell_summary(row)}
    objects = ctx.s3_keys(f"cells/{ctx.b.cell_id}/")
    record.evidence["objects_under_prefix"] = len(objects)

    # Operator export runbook (D8): restore into a scratch namespace with the
    # same Job, then serve it and check it. The per-cell credentials are
    # read from the source cell's Secret, as an operator with cluster-admin.
    secret = ctx.kube_json("get", "secret", "cell-credentials", "--namespace", namespace_name(ctx.b.cell_id))["data"]
    decoded = {key: base64.b64decode(value).decode() for key, value in secret.items()}
    source_pod = ctx.runtime_pod(ctx.b.cell_id)
    image = source_pod["spec"]["containers"][0]["image"]  # type: ignore[index]
    scratch_bearer = secrets.token_urlsafe(32)
    spec = CellManifestSpec(
        cell_id=ctx.b.cell_id, image=image, replicas=0, read_only=False,
        bearer_current=scratch_bearer, backup_password=decoded["backup-password"],
        b2_key_id=decoded["b2-key-id"], b2_key_secret=decoded["b2-key-secret"],
        hold_kind="restore", hold_started_at=dt.datetime.now(dt.UTC).isoformat(),
        job_egress_except=("10.42.0.0/16", "10.43.0.0/16"),
    )
    scratch = f"exo-scratch-{ctx.b.cell_id[:8]}"
    documents = render_cell_manifests(spec) + [
        render_restore_job(spec, bucket_name=BACKUP_BUCKET, endpoint=ctx.stack.object_store.endpoint(from_host=False), snapshot_id=snapshot)
    ]
    for document in documents:
        if document["kind"] == "Namespace":
            document["metadata"]["name"] = scratch
            document["metadata"].setdefault("labels", {})["exomem.io/scratch-of"] = ctx.b.cell_id
            document["metadata"]["labels"].pop("exomem.io/cloud-cell", None)
        else:
            document["metadata"]["namespace"] = scratch
    ctx.report.overlays.append(
        f"step 11 scratch restore: no operator export tool exists yet (task 3.6, deferred by ruling), so the "
        f"rehearsal renders the cell with cellctl's render_cell_manifests and render_restore_job, moves every "
        f"object into namespace {scratch} without the exomem.io/cloud-cell label, mints a scratch bearer, "
        "excepts only the cluster's pod and service ranges from Job egress, and applies it as cluster-admin"
    )
    payload = "\n---\n".join(json.dumps(doc) for doc in documents if doc["kind"] != "Job")
    ctx.kubectl("apply", "--server-side", "--field-manager=rehearsal-operator", "--filename=-", input_text=payload)
    job = next(doc for doc in documents if doc["kind"] == "Job")
    ctx.kubectl("apply", "--server-side", "--field-manager=rehearsal-operator", "--filename=-", input_text=json.dumps(job))
    ctx.kubectl("wait", "--namespace", scratch, "--for=condition=complete", f"job/{job['metadata']['name']}", "--timeout=600s")
    ctx.kubectl("scale", "statefulset/cell", "--namespace", scratch, "--replicas=1")
    ctx.kubectl("wait", "--namespace", scratch, "--for=condition=Ready", "pod/cell-0", "--timeout=600s")

    scratch_result = json.loads(
        ctx.exec_in(scratch, "cell-0", "python3", "-c", SCRATCH_PROBE, ctx.b.phrase).stdout.strip().splitlines()[-1]
    )
    # Compared whole: exit code and content-free JSON. A fresh standalone
    # vault reports a stable refusal code here, and the restore must report
    # exactly what its source does.
    status_cmd = ("exomem", "governance-schema", "status", "--vault", "/data/vault", "--json")
    source_run = ctx.exec_in(namespace_name(ctx.b.cell_id), source_pod["metadata"]["name"], *status_cmd, check=False)  # type: ignore[index]
    scratch_run = ctx.exec_in(scratch, "cell-0", *status_cmd, check=False)
    source_status = (source_run.returncode, _json_or_text(source_run.stdout))
    scratch_status = (scratch_run.returncode, _json_or_text(scratch_run.stdout))
    record.evidence["governance_schema_status"] = {"source": source_status, "scratch": scratch_status}
    record.evidence["scratch_restore"] = {
        "namespace": scratch,
        "snapshot": "pre-image nightly snapshot of tenant B",
        "recall_found_source_write": ctx.b.phrase in scratch_result["recall"],
        "governed_write": scratch_result["write_error"] or "committed",
        "governance_schema_status_matches_source": source_status == scratch_status,
    }
    ctx.kubectl("delete", "namespace", scratch, "--wait=false")
    if ctx.b.phrase not in scratch_result["recall"]:
        raise StepFailure("the scratch restore does not answer recall of tenant B's write")
    if scratch_result["write_error"]:
        raise StepFailure(f"the scratch restore refused a governed write: {scratch_result['write_error']}")
    if source_status != scratch_status:
        raise StepFailure("governance-schema status differs between the source cell and its scratch restore")


async def step_12_deletion(ctx: Context, record: StepRecord) -> None:
    _require(ctx.a.cell_id and ctx.a.session and ctx.a.user_id and ctx.a.tenant_id and ctx.a.client)
    cell_id = ctx.a.cell_id
    namespace = namespace_name(cell_id)
    pv_names = [
        pv["metadata"]["name"]
        for pv in ctx.kube_json("get", "pv")["items"]
        if (pv["spec"].get("claimRef") or {}).get("namespace") == namespace
    ]
    objects_before = len(ctx.s3_keys(f"cells/{cell_id}/"))
    record.evidence["before_deletion"] = {"persistent_volumes": len(pv_names), "backup_objects": objects_before}
    if not pv_names or objects_before == 0:
        raise StepFailure(
            "nothing to prove absent: the cell had no bound PersistentVolume or no backup objects before deletion "
            f"({record.evidence['before_deletion']})"
        )
    old_token = ctx.a.client.access_token
    # Cancel first, so the deletion finish needs no Paddle API call.
    await ctx.paddle(
        substrate_mod.paddle_event(
            event_type="subscription.canceled", status="canceled", user_id=ctx.a.user_id, tenant_id=ctx.a.tenant_id,
            subscription_id=ctx.a.subscription_id or "", transaction_id=ctx.a.transaction_id or "",
        )
    )
    deletion_token = await substrate_mod.seed_deletion_token(ctx.substrate, user_id=ctx.a.user_id, tenant_id=ctx.a.tenant_id)
    confirm = await ctx.browser.post_with_session(ctx.a.session, "/api/exomem/deletion/confirm", {"token": deletion_token})
    record.evidence["deletion_confirm_status"] = confirm.status_code
    if confirm.status_code >= 300:
        raise CrossLaneDefect(
            f"POST /api/exomem/deletion/confirm answered {confirm.status_code}",
            component="Substrate account deletion", owner=SUBSTRATE_REPO,
            evidence={"status": confirm.status_code, "body": _safe_body(confirm)},
        )
    started = time.monotonic()
    scheduler_statuses = []
    deadline = time.monotonic() + 900
    while True:
        scheduler_statuses.append(await ctx.scheduler())
        row = await ctx.cell(cell_id)
        if row.get("observed_state") == "deleted":
            break
        if time.monotonic() > deadline:
            raise StepFailure(f"deletion did not finish within 900s; last row {cell_summary(row)}; scheduler statuses {sorted(set(scheduler_statuses))}")
        await asyncio.sleep(5)
    namespace_gone = ctx.kubectl("get", "namespace", namespace, check=False)
    remaining_pvs = [
        pv["metadata"]["name"]
        for pv in ctx.kube_json("get", "pv")["items"]
        if (pv["spec"].get("claimRef") or {}).get("namespace") == namespace or pv["metadata"]["name"] in pv_names
    ]
    objects = ctx.s3_keys(f"cells/{cell_id}/")
    keys = await ctx.fetchrow(
        "SELECT b2_key_id IS NULL AS b2_key_nulled, b2_key_wrapped IS NULL AS b2_wrapped_nulled, backup_key_wrapped IS NULL AS backup_key_nulled, desired_state, observed_state FROM exomem_cloud_cells WHERE cell_id = $1",
        cell_id,
    )
    tenant = await ctx.fetchrow("SELECT status FROM exomem_tenants WHERE id = $1", ctx.a.tenant_id)
    after = await raw_mcp_post(ctx.resolver, token=old_token)
    b_row = await ctx.cell(ctx.b.cell_id) if ctx.b.cell_id else {}
    record.evidence.update(
        {
            "seconds_to_deleted": round(time.monotonic() - started, 1),
            "scheduler_statuses": sorted(set(scheduler_statuses)),
            "namespace_absent": namespace_gone.returncode != 0 and "NotFound" in namespace_gone.stderr,
            "persistent_volumes_absent": not remaining_pvs,
            "backup_objects_under_prefix": len(objects),
            "keys": keys,
            "tenant_status": (tenant or {}).get("status"),
            "old_access_token_status": after.status_code,
            "other_tenant_untouched": _running_ready()(b_row) if b_row else None,
        }
    )
    problems = []
    if not record.evidence["namespace_absent"]:
        problems.append("the cell namespace still exists")
    if remaining_pvs:
        problems.append(f"PersistentVolumes remain: {remaining_pvs}")
    if objects:
        problems.append(f"{len(objects)} backup objects remain under the prefix")
    if not keys or not (keys["b2_key_nulled"] and keys["b2_wrapped_nulled"] and keys["backup_key_nulled"]):
        problems.append(f"wrapped keys not destroyed: {keys}")
    if after.status_code != 401:
        problems.append(f"the deleted tenant's token still answers {after.status_code}")
    if b_row and not _running_ready()(b_row):
        problems.append("the other tenant's cell was disturbed")
    if problems:
        raise StepFailure("; ".join(problems))


STEPS: list[tuple[int, str, Callable[[Context, StepRecord], Awaitable[None]], tuple[int, ...]]] = [
    (1, "invite", step_1_invite, ()),
    (2, "provision", step_2_provision, (1,)),
    (3, "oauth_mcp_tools_list", step_3_oauth_mcp, (2,)),
    (4, "capture", step_4_capture, (3,)),
    (5, "paraphrased_cited_recall", step_5_cited_recall, (4,)),
    (6, "governed_write_after_pod_kill", step_6_write_after_pod_kill, (3,)),
    (7, "upgrade_and_recall", step_7_upgrade, (3,)),
    (8, "forced_canary_failure_restore", step_8_canary_failure, (3,)),
    (9, "read_only_mode", step_9_read_only, (3,)),
    (10, "second_tenant_denial", step_10_second_tenant_denial, (3,)),
    (11, "backup_and_scratch_restore", step_11_backup_and_scratch_restore, (10,)),
    (12, "deletion_with_absence_proofs", step_12_deletion, (3,)),
]


async def run_steps(ctx: Context, *, only: set[int] | None = None) -> None:
    outcome: dict[int, str] = {}
    for number, name, step, needs in STEPS:
        record = StepRecord(number=number, name=name)
        ctx.report.steps.append(record)
        if only is not None and number not in only:
            record.failure = {"type": "Skipped", "message": "not selected with --steps"}
            outcome[number] = BLOCKED
            continue
        unmet = [n for n in needs if outcome.get(n) != PASSED]
        # A failed step whose failure left its effect in place (e.g. slow
        # provisioning) does not block its dependants: only a failure that
        # left the precondition absent does, which the dependant finds itself.
        hard = [n for n in unmet if outcome.get(n) == BLOCKED or (n in (1, 2, 3, 10) and _precondition_absent(ctx, n))]
        if hard:
            record.failure = {"type": "Blocked", "message": f"depends on step(s) {hard}, which did not complete"}
            outcome[number] = BLOCKED
            print(f"[rehearsal] step {number} {name}: BLOCKED by {hard}", flush=True)
            continue
        record.started_at = dt.datetime.now(dt.UTC).isoformat()
        print(f"[rehearsal] step {number} {name}: start", flush=True)
        started = time.monotonic()
        try:
            await step(ctx, record)
            record.status = PASSED
        except Exception as raised:  # noqa: BLE001 - recorded, and the run continues
            error = innermost(raised)
            record.status = FAILED
            record.failure = failure_record(error)
            if isinstance(error, CrossLaneDefect):
                ctx.report.defects.append({"step": number, **record.failure})
        record.seconds = round(time.monotonic() - started, 2)
        outcome[number] = record.status
        snapshot_logs(ctx)
        detail = "" if record.status == PASSED else f" -- {record.failure.get('message', '')[:300]}"  # type: ignore[union-attr]
        print(f"[rehearsal] step {number} {name}: {record.status.upper()} in {record.seconds}s{detail}", flush=True)


async def close_clients(ctx: Context) -> None:
    for tenant in (ctx.a, ctx.b):
        if tenant.client is not None:
            await tenant.client.aclose()


def _precondition_absent(ctx: Context, step: int) -> bool:
    if step == 1:
        return not ctx.a.notes and ctx.a.session is None
    if step == 2:
        return ctx.a.session is None or ctx.a.cell_id is None
    if step == 3:
        return ctx.a.client is None or ctx.a.client.access_token is None
    if step == 10:
        return ctx.b.client is None or ctx.b.cell_id is None
    return False


def snapshot_logs(ctx: Context) -> None:
    """Keeps every workload's logs before a step can replace or delete it.

    Pod kills, upgrades, backup holds, the cellctl restart and the deletion
    each discard a container's logs, so the content-free check reads these
    snapshots, taken after every step, not only what survives to the end.
    """

    for namespace, selector in [
        (CLOUD_NAMESPACE, "app.kubernetes.io/name=exomem-cloud-gateway"),
        (CLOUD_NAMESPACE, "app.kubernetes.io/name=cellctl"),
        *[(namespace_name(t.cell_id), "app.kubernetes.io/name=exomem-cell") for t in (ctx.a, ctx.b) if t.cell_id],
    ]:
        for previous in ((), ("--previous",)):
            result = run(
                ["docker", "exec", ctx.stack.k3s.container, "kubectl", "logs", "--namespace", namespace,
                 f"--selector={selector}", "--all-containers", "--tail=-1", "--prefix", *previous],
                check=False,
            )
            if result.stdout:
                ctx.log_snapshots.append(result.stdout)


def post_checks(ctx: Context) -> dict[str, Any]:
    """Content-free logs: no distinctive phrase in any cell, gateway or cellctl log."""

    snapshot_logs(ctx)
    phrases = [ctx.a.phrase, ctx.b.phrase]
    haystack = "\n".join(ctx.log_snapshots)
    leaked = [phrase for phrase in phrases if phrase in haystack]
    return {"log_bytes_scanned": len(haystack), "phrases_checked": len(phrases), "phrases_leaked": len(leaked)}


async def ready_matches_pods(ctx: Context) -> list[dict[str, Any]]:
    """D4: a row's `ready` must be what its pod's Ready condition says."""

    mismatches = []
    for tenant in (ctx.a, ctx.b):
        if not tenant.cell_id:
            continue
        row = await ctx.fetchrow("SELECT ready, observed_state, observed_at FROM exomem_cloud_cells WHERE cell_id = $1", tenant.cell_id)
        if not row or row["observed_state"] in ("deleted", "deleting", "stopped"):
            continue
        pod_ready = ctx.pod_ready(tenant.cell_id)
        if bool(row["ready"]) != pod_ready:
            # Resample after several cellctl passes: a pod caught mid-restart
            # is not a finding; a mismatch that outlives the passes is.
            await asyncio.sleep(20)
            row = await ctx.fetchrow("SELECT ready, observed_state, observed_at FROM exomem_cloud_cells WHERE cell_id = $1", tenant.cell_id)
            pod_ready = ctx.pod_ready(tenant.cell_id)
        if row and bool(row["ready"]) != pod_ready:
            mismatches.append({"tenant": tenant.label, "row_ready": row["ready"], "pod_ready": pod_ready,
                               "row_observed_at": row["observed_at"].isoformat() if row["observed_at"] else None})
    return mismatches
