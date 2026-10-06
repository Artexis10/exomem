"""Public asset and OAuth metadata routes for the FastMCP server."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mcp.types
from fastmcp import FastMCP
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

from . import runtime_readiness as runtime_readiness_module
from . import tool_surface as tool_surface_module
from .session_oauth import OAUTH_AUTHORIZATION_SCOPES, OAUTH_RESOURCE_SCOPES

log = logging.getLogger(__name__)

_STUDIO_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self'; manifest-src 'self'; object-src 'none'; "
        "script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _studio_dir() -> Path:
    return Path(__file__).parent / "studio"


def _studio_manifest() -> dict[str, str]:
    """Load the packaged allowlist as ``asset name -> media type``."""
    manifest_path = _studio_dir() / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = data.get("assets")
    if not isinstance(assets, dict) or "index.html" not in assets:
        raise ValueError("Studio asset manifest is invalid")
    clean: dict[str, str] = {}
    for name, media_type in assets.items():
        if (
            not isinstance(name, str)
            or not isinstance(media_type, str)
            or not name
            or Path(name).name != name
            or name.startswith(".")
        ):
            raise ValueError("Studio asset manifest contains an unsafe entry")
        clean[name] = media_type
    return clean


def _studio_error(message: str, *, status_code: int = 503) -> JSONResponse:
    return JSONResponse(
        {
            "error": "STUDIO_ASSETS_UNAVAILABLE",
            "message": message[:240],
            "remediation": "Reinstall Exomem and restart the service.",
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store", **_STUDIO_SECURITY_HEADERS},
    )


def server_icons() -> list[mcp.types.Icon]:
    """Load the packaged SVG icon as an MCP initialize icon."""
    icon_path = Path(__file__).parent / "icon.svg"
    if not icon_path.exists():
        return []
    svg_bytes = icon_path.read_bytes()
    b64 = base64.b64encode(svg_bytes).decode("ascii")
    return [
        mcp.types.Icon(
            src=f"data:image/svg+xml;base64,{b64}",
            mimeType="image/svg+xml",
            sizes=["any"],
        )
    ]


# How long a liveness snapshot is served before a worker thread re-reads it.
# Only state placement is re-read; install provenance is read once.
HEALTH_SNAPSHOT_TTL_SECONDS = 60.0
# A refresh in flight longer than this means a read is wedged (a hung
# filesystem): /health then answers 503 instead of serving a stale 200.
HEALTH_REFRESH_WEDGED_SECONDS = 120.0
# How long a ready proof answers `/health/ready` before a probe proves again.
# A not-ready proof is never reused. The kubelet probes every 5 s, and each
# proof costs a coordination thread, a catalogue open and a log-directory write.
READINESS_SUCCESS_TTL_SECONDS = 30.0

# Readiness measurements get their own workers. anyio's default limiter is
# shared with every synchronous tool call, so slow calls would queue readiness
# behind them and make a busy cell look NotReady.
_READINESS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="exomem-readiness")


def _read_install_provenance() -> dict[str, object] | None:
    """Where the running code was installed from, or `None` if that read failed."""
    try:
        from . import deploy_provenance

        return dict(deploy_provenance.provenance(include_local=False))
    except Exception:  # noqa: BLE001 — provenance must never fail the probe
        return None


def _read_state_placement() -> dict[str, object]:
    """Blocking read behind `/health`: content-free machine-local state placement."""
    facts: dict[str, object] = {}
    # Content-free machine-local state placement.  This route is public:
    # absolute roots belong only in the local doctor surface.
    try:
        from . import state_migration
        from . import vault as vault_module

        vault_root = vault_module.resolve_vault()
        facts["state"] = {
            "placement": "external-state",
            "migration": state_migration.migration_status(vault_root),
        }
    except Exception:  # noqa: BLE001 — placement must never fail the probe
        facts["state"] = {
            "placement": "external-state",
            "migration": "unavailable",
        }
    return facts


class _LivenessSnapshot:
    """Serve `/health` facts from memory; refresh them off the event loop.

    Read once at construction (route registration, before any load), then at
    most once per `HEALTH_SNAPSHOT_TTL_SECONDS` on an executor thread. A refresh
    that blocks leaves the previous snapshot in service instead of the probe.

    Install provenance is kept for the life of the process once it has been
    read: it describes the code that is running, and an in-place upgrade that
    rewrites the package metadata does not change that code.
    """

    def __init__(self) -> None:
        self._provenance = _read_install_provenance()
        self._facts = self._compose()
        self._read_at = time.monotonic()
        self._lock = threading.Lock()
        self._refreshing = False
        self._refresh_started = 0.0

    def current(self) -> tuple[dict[str, object], bool, bool]:
        """The served facts, whether to start a refresh, and whether one is wedged."""
        with self._lock:
            now = time.monotonic()
            stale = now - self._read_at >= HEALTH_SNAPSHOT_TTL_SECONDS
            start = stale and not self._refreshing
            if start:
                self._refreshing = True
                self._refresh_started = now
            wedged = (
                self._refreshing and now - self._refresh_started >= HEALTH_REFRESH_WEDGED_SECONDS
            )
            return dict(self._facts), start, wedged

    def _compose(self) -> dict[str, object]:
        facts: dict[str, object] = (
            dict(self._provenance) if self._provenance is not None else {"version": "unknown"}
        )
        facts.update(_read_state_placement())
        return facts

    def refresh(self) -> None:
        try:
            # Only a failed first read is retried, so a transient metadata
            # error cannot pin `version: unknown` for the life of the process.
            if self._provenance is None:
                self._provenance = _read_install_provenance()
            facts = self._compose()
        except BaseException:
            with self._lock:
                self._refreshing = False
            raise
        with self._lock:
            self._facts = facts
            self._read_at = time.monotonic()
            self._refreshing = False


def register_health_routes(
    mcp_app: FastMCP,
    *,
    traffic_monitor=None,
    on_liveness: Callable[[], None] | None = None,
) -> None:
    """Register the unauthenticated `/health` and `/health/ready` routes only.

    Shared by the standalone asset-route bundle below and by Exomem Cloud
    cells (design D1.5), which register nothing else from this module: an
    Exomem Cloud cell registers only MCP, `/health` and `/health/ready`.
    """
    if traffic_monitor is None:
        traffic_monitor = runtime_readiness_module.get_silent_traffic_monitor()

    # The proof in flight, by tool-surface digest. A probe that arrives while
    # one runs answers from it: waiters queued without bound behind the
    # readiness workers, and a client that gave up still left its proof queued, so a 30 s
    # stall replayed 30 proofs back to back. Each flight carries the cache token
    # taken before its proof started.
    readiness_flights: dict[object, tuple[asyncio.Future, tuple[int, int]]] = {}
    # The last ready proof, by digest: the token it was proved under, when it
    # was kept, and the payload. Only a ready proof is kept, and only while no
    # transition this process made could have outdated it.
    ready_proofs: dict[object, tuple[tuple[int, int], float, dict]] = {}

    def _reusable(snapshot: object) -> bool:
        if not isinstance(snapshot, dict) or snapshot.get("status") != "ready":
            return False
        # A standby is polled every 0.1 s for its `cutover` block, which moves
        # while its serving status stays ready.
        cutover = snapshot.get("cutover")
        return not (isinstance(cutover, dict) and cutover.get("standby") is True)

    def _cached_ready(digest: object) -> tuple[dict, float] | None:
        entry = ready_proofs.get(digest)
        if entry is None:
            return None
        token, proved_at, snapshot = entry
        age = time.monotonic() - proved_at
        if (
            age >= READINESS_SUCCESS_TTL_SECONDS
            or token != runtime_readiness_module.cached_readiness_token()
        ):
            ready_proofs.pop(digest, None)
            return None
        return snapshot, age

    def _keep_if_reusable(digest: object, token: tuple[int, int], snapshot: object) -> None:
        if not _reusable(snapshot):
            return
        if token != runtime_readiness_module.cached_readiness_token():
            return
        kept = ready_proofs.get(digest)
        if kept is None or kept[2] is not snapshot:
            ready_proofs[digest] = (token, time.monotonic(), snapshot)

    def _readiness_flight(digest: object, traffic: dict) -> tuple[asyncio.Future, tuple[int, int]]:
        current = readiness_flights.get(digest)
        if current is not None and not current[0].done():
            return current
        # Taken before the proof starts: a transition that lands while it runs
        # leaves its answer unservable to the next probe.
        token = runtime_readiness_module.cached_readiness_token()
        # The proof runs on the readiness workers, apart from anyio's default
        # limiter that every synchronous tool call shares.
        flight = asyncio.get_running_loop().run_in_executor(
            _READINESS_EXECUTOR,
            functools.partial(
                runtime_readiness_module.runtime_readiness,
                mcp_tool_surface_sha256=digest,
                traffic=traffic,
            ),
        )
        readiness_flights[digest] = (flight, token)

        def _forget(done: asyncio.Future) -> None:
            current = readiness_flights.get(digest)
            if current is not None and current[0] is done:
                del readiness_flights[digest]

        flight.add_done_callback(_forget)
        return flight, token

    def _record_health_probe() -> dict:
        try:
            return traffic_monitor.record_health_probe()
        except Exception:  # noqa: BLE001 - telemetry must never fail a probe
            log.debug("silent traffic health tracking failed", exc_info=True)
            return {}

    liveness = _LivenessSnapshot()

    @mcp_app.custom_route("/health", methods=["GET"])
    async def _health(request: Request) -> JSONResponse:  # noqa: ARG001
        """Unauthenticated liveness probe for tunnels/orchestrators. Reports that
        the process is up, its version, and where that code was installed from —
        no vault data, no auth required.

        Install provenance is included so an operator can tell a wheel-backed
        service from one running a local checkout without inspecting the service
        manager. Host-identifying detail (interpreter path, checkout location) is
        deliberately withheld here because this route is publicly reachable; use
        the local `provenance` command for that.

        The handler itself does no I/O: the provenance and state-placement reads
        come from a snapshot refreshed on a worker thread. A liveness probe that
        reads files on the event loop waits behind every busy thread in the
        process (the GIL is handed back per syscall), and a probe that times out
        gets the pod killed mid index build."""
        _record_health_probe()
        payload: dict[str, object] = {"status": "ok", "service": "exomem"}
        facts, refresh, wedged = liveness.current()
        payload.update(facts)
        if refresh:
            # Queue the read without waiting for it. Only the executor's first
            # use spawns a worker thread; the handler never joins one, and a
            # thread start would block the loop until it wins the GIL.
            asyncio.get_running_loop().run_in_executor(None, liveness.refresh)
        if wedged:
            payload["status"] = "degraded"
        return JSONResponse(
            payload,
            status_code=503 if wedged else 200,
            headers={"Cache-Control": "no-store"},
            background=(BackgroundTask(on_liveness) if on_liveness is not None else None),
        )

    @mcp_app.custom_route("/health/ready", methods=["GET"])
    async def _runtime_ready(request: Request) -> JSONResponse:  # noqa: ARG001
        """Content-free admission probe; liveness remains the separate /health route.

        A ready proof answers later probes for up to
        `READINESS_SUCCESS_TTL_SECONDS`, and `proof_age_seconds` says how old the
        answer is. A not-ready proof is never reused."""
        traffic = _record_health_probe()
        digest = getattr(mcp_app, "_exomem_tool_surface_sha256", None)
        if digest is None:
            try:
                live = await tool_surface_module.live_contract(mcp_app)
                digest = live["sha256"]
                mcp_app._exomem_tool_surface_sha256 = digest
            except Exception:  # noqa: BLE001 - readiness must stay structured
                digest = None
        cached = _cached_ready(digest)
        if cached is not None:
            snapshot, age = cached
        else:
            # Off the event loop, on the readiness workers: the retrieval proof
            # and coordination status take reserved-state locks, and one probe
            # held the loop 5.8 s on one at the 0.96.0 promotion, timing out the
            # liveness polls queued behind it. Shielded: one caller going away
            # must not cancel the proof the others are waiting on.
            flight, token = _readiness_flight(digest, traffic)
            snapshot = await asyncio.shield(flight)
            age = 0.0
            # Kept here rather than in a done callback: an executor future can
            # be done when it is created, and then its callbacks run only after
            # this handler has already answered.
            _keep_if_reusable(digest, token, snapshot)
        payload = dict(snapshot)
        # This probe's own counters, whichever proof answers it.
        if "traffic" in payload:
            payload["traffic"] = dict(traffic)
        payload["proof_age_seconds"] = round(age, 3)
        status_code = 200 if payload["status"] == "ready" else 503
        return JSONResponse(
            payload,
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )


def register_asset_routes(
    mcp_app: FastMCP,
    *,
    traffic_monitor=None,
    on_liveness: Callable[[], None] | None = None,
    vault_root: Path | None = None,
) -> None:
    """Serve inert public assets outside MCP auth; vault data stays behind REST."""
    asset_dir = Path(__file__).parent
    register_health_routes(mcp_app, traffic_monitor=traffic_monitor, on_liveness=on_liveness)

    if vault_root is not None:

        @mcp_app.custom_route("/control/promote", methods=["POST"])
        async def _promote(request: Request) -> JSONResponse:
            """Take state ownership once the previous worker has provably exited.

            Reachable only over the supervisor-owned private worker socket, which
            is the same boundary `/health` already has. A process that never
            entered standby refuses; a promoted one answers idempotently
            (`seamless-managed-worker-handoff` D8).
            """
            from . import service_standby

            migrated = False
            try:
                raw = await request.body()
                if len(raw) > 1024:
                    raise ValueError("oversized control request")
                if raw:
                    body = json.loads(raw)
                    migrated = bool(body.get("migrated")) if isinstance(body, dict) else False
            except (ValueError, UnicodeDecodeError):
                return JSONResponse(
                    {"ok": False, "error": "invalid control request"},
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )
            try:
                record = service_standby.promote(vault_root, migrated=migrated)
            except RuntimeError:
                return JSONResponse(
                    {"ok": False, "error": "this worker is not a standby"},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(record, headers={"Cache-Control": "no-store"})

    @mcp_app.custom_route("/metrics.json", methods=["GET"])
    async def _metrics_json(request: Request) -> JSONResponse:  # noqa: ARG001
        """Unauthenticated content-free counters/histograms snapshot, beside
        `/health/ready`. Disabled via `EXOMEM_DISABLE_METRICS`."""
        from . import metrics

        if metrics.metrics_disabled():
            return JSONResponse(
                {"error": "METRICS_DISABLED"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = metrics.render_json()
        except Exception:  # noqa: BLE001 - metrics must never fail the probe
            payload = {"counters": [], "histograms": [], "bucket_bounds_ms": []}
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @mcp_app.custom_route("/favicon.ico", methods=["GET"])
    async def _favicon_ico(request: Request):  # noqa: ARG001
        return FileResponse(
            asset_dir / "favicon.ico",
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @mcp_app.custom_route("/favicon.svg", methods=["GET"])
    async def _favicon_svg(request: Request):  # noqa: ARG001
        return FileResponse(
            asset_dir / "icon.svg",
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @mcp_app.custom_route("/studio", methods=["GET"])
    async def _studio_redirect(request: Request):  # noqa: ARG001
        return RedirectResponse("/studio/", status_code=307)

    @mcp_app.custom_route("/studio/", methods=["GET"])
    async def _studio_shell(request: Request):  # noqa: ARG001
        try:
            manifest = _studio_manifest()
            shell = _studio_dir() / "index.html"
            if not shell.is_file():
                raise FileNotFoundError("Studio shell is missing")
            return FileResponse(
                shell,
                media_type=manifest["index.html"],
                headers={"Cache-Control": "no-store", **_STUDIO_SECURITY_HEADERS},
            )
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            return _studio_error(str(exc))

    @mcp_app.custom_route("/studio/assets/{asset_path:path}", methods=["GET"])
    async def _studio_asset(request: Request):
        asset_name = request.path_params.get("asset_path", "")
        try:
            manifest = _studio_manifest()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return _studio_error(str(exc))
        if asset_name == "index.html" or asset_name not in manifest:
            return _studio_error("Studio asset is not in the packaged manifest", status_code=404)
        asset = _studio_dir() / asset_name
        if not asset.is_file():
            return _studio_error(f"Studio asset {asset_name!r} is missing")
        return FileResponse(
            asset,
            media_type=manifest[asset_name],
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                **_STUDIO_SECURITY_HEADERS,
            },
        )


def register_oauth_metadata_route(mcp_app: FastMCP, *, base_url: str, auth_enabled: bool) -> None:
    """Expose compatibility aliases for OAuth/OIDC discovery."""
    if not auth_enabled:
        return

    base_url = base_url.rstrip("/")
    resource_url = f"{base_url}/mcp"
    issuer_url = f"{base_url}/"

    @mcp_app.custom_route("/.well-known/openid-configuration", methods=["GET"])
    async def _openid_configuration(request: Request) -> JSONResponse:  # noqa: ARG001
        # Some MCP clients probe the OIDC alias after a successful OAuth token
        # exchange. Exomem uses OAuth (not ID tokens), so return the same RFC
        # 8414 authorization-server metadata shape as the canonical endpoint.
        return JSONResponse(
            {
                "issuer": issuer_url,
                "authorization_endpoint": f"{base_url}/authorize",
                "token_endpoint": f"{base_url}/token",
                "registration_endpoint": f"{base_url}/register",
                "scopes_supported": list(OAUTH_AUTHORIZATION_SCOPES),
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_post",
                    "client_secret_basic",
                    "private_key_jwt",
                    "none",
                ],
                "code_challenge_methods_supported": ["S256"],
                "client_id_metadata_document_supported": True,
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @mcp_app.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def _oauth_protected_resource_bare(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            {
                "resource": resource_url,
                "authorization_servers": [issuer_url],
                "scopes_supported": list(OAUTH_RESOURCE_SCOPES),
                "bearer_methods_supported": ["header"],
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )
