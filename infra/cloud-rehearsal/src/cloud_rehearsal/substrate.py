"""The Substrate control plane: source, database, web app and TLS edge.

Substrate is cloned read-only at a pinned commit. Its real migrations run
through its own runner (`scripts/migrate.ts`), which also applies its real
`scripts/exomem-cloud-grants.sql`, the single implementation of the C1
privilege table. The role bootstrap mirrors the control-database Ansible
role: four LOGIN roles, `substrate_owner` owning the schema.

The web app runs `next start` in the same pinned Node image the gateway
Dockerfile uses, on a Docker network created with `--internal`: it reaches
PostgreSQL and nothing else, so Paddle, Brevo and every other real service
are unreachable by construction, not by convention. A Traefik edge on both
networks terminates TLS for the Substrate hostname, standing in for Vercel.

Emails are injected, never sent: every emailed secret in Substrate is stored
only as an unsalted SHA-256 digest, so the rehearsal mints the secret, seeds
its digest where the email path would, and hands the secret to the client.
Paddle is injected the same way: signed webhooks with the run's own secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

from . import images, tls
from .infra import PG_DATABASE, PG_ROLES, Stack
from .shell import run, wait_for

SUBSTRATE_REPOSITORY = "https://github.com/substrate-systems/substrate.git"
# PR #181 (feat/cloud-deletion-finish): Cloud deletion finish, paid Cloud
# activation and the gateway's tenant-status grant. Bump deliberately.
SUBSTRATE_COMMIT = "75fedbf63c7a8936f4a1412b33148c96e79bd6bc"
NODE_IMAGE = "node:24.18.1-bookworm-slim@sha256:235600a8101ab264e117b1768e925532262668dc9b581ef1dd7d96ced463b8e7"
MCP_PATH = "/mcp"
MCP_URL = f"https://{tls.MCP_HOST}{MCP_PATH}"
PUBLIC_BASE_URL = f"https://{tls.SUBSTRATE_HOST}"
OAUTH_CLIENT_ID = "exomem-cloud-rehearsal"
PADDLE_ID_ALPHABET = string.ascii_lowercase + string.digits


def sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def paddle_id(prefix: str) -> str:
    return prefix + "_" + "".join(secrets.choice(PADDLE_ID_ALPHABET) for _ in range(26))


@dataclass
class SubstrateSecrets:
    control_plane_key: str = field(default_factory=lambda: b64url(secrets.token_bytes(32)))
    # One 32-byte key: Substrate and the gateway read it as 64 hex
    # characters, cellctl as base64. Same bytes, three encodings.
    cell_token_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    admin_token: str = field(default_factory=lambda: b64url(secrets.token_bytes(32)))
    scheduler_secret: str = field(default_factory=lambda: b64url(secrets.token_bytes(32)))
    paddle_webhook_secret: str = field(default_factory=lambda: "pdl_ntfset_" + secrets.token_hex(16))
    ingress_source_value: str = field(default_factory=lambda: b64url(secrets.token_bytes(24)))


@dataclass
class Substrate:
    source: Path
    commit: str
    sealed_network: str
    app_container: str
    edge_container: str
    edge_host_port: int
    secrets: SubstrateSecrets
    owner_dsn: str
    cell_image_repository: str
    redirect_uri: str


def fetch_source(cache_dir: Path, commit: str = SUBSTRATE_COMMIT) -> Path:
    source = cache_dir / f"substrate-{commit[:12]}"
    if not (source / ".git").exists():
        source.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet", str(source)])
        run(["git", "-C", str(source), "remote", "add", "origin", SUBSTRATE_REPOSITORY])
    run(["git", "-C", str(source), "fetch", "--quiet", "--depth", "1", "origin", commit], timeout=600)
    run(["git", "-C", str(source), "checkout", "--quiet", "--force", "FETCH_HEAD"])
    head = run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    if head != commit:
        raise RuntimeError(f"Substrate checkout is at {head}, expected {commit}")
    return source


def build_source(source: Path) -> None:
    """`npm ci` and `next build`, skipped when this commit already built."""

    marker = source / ".rehearsal-built"
    head = run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == head:
        return
    env = {**os.environ, "NEXT_TELEMETRY_DISABLED": "1"}
    run(["npm", "ci", "--no-audit", "--no-fund"], cwd=source, env=env, timeout=1800)
    run(["npm", "run", "build"], cwd=source, env=env, timeout=1800)
    run(["npm", "run", "gateway:build"], cwd=source, env=env, timeout=900)
    marker.write_text(head, encoding="utf-8")


async def bootstrap_database(stack: Stack) -> None:
    """The control-database role's role set, then the schema's ownership."""

    pg = stack.postgres
    admin = await asyncpg.connect(pg.dsn("postgres", from_host=True).replace(f"/{PG_DATABASE}", "/postgres"))
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", PG_DATABASE)
        for role in PG_ROLES:
            # Passwords are generated per run; format() quotes the literal.
            statement = await admin.fetchval(
                "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', $1::text, $2::text)", role, pg.passwords[role]
            )
            await admin.execute(statement)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{PG_DATABASE}" OWNER substrate_owner')
    finally:
        await admin.close()
    database = await asyncpg.connect(pg.dsn("postgres", from_host=True))
    try:
        await database.execute("ALTER SCHEMA public OWNER TO substrate_owner")
        await database.execute("CREATE EXTENSION IF NOT EXISTS citext")
        await database.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await database.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        for role in ("substrate_app", "exomem_gateway", "exomem_cellctl"):
            await database.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        await database.execute(f"REVOKE CONNECT ON DATABASE {PG_DATABASE} FROM PUBLIC")
        for role in PG_ROLES:
            await database.execute(f"GRANT CONNECT ON DATABASE {PG_DATABASE} TO {role}")
    finally:
        await database.close()


def migrate(stack: Stack, source: Path) -> str:
    """Substrate's own runner, as `substrate_owner`; it applies the grants."""

    env = {
        **os.environ,
        "DATABASE_URL": stack.postgres.dsn("substrate_owner", from_host=True),
        "CONFIRM_ENDSTATE_CLOUD_RELEASE_A": "yes",
    }
    result = run(["npx", "--no-install", "tsx", "scripts/migrate.ts"], cwd=source, env=env, timeout=900)
    return result.stdout[-4000:]


def _app_environment(stack: Stack, substrate_secrets: SubstrateSecrets, cell_image_repository: str) -> dict[str, str]:
    return {
        "NODE_ENV": "production",
        "NEXT_TELEMETRY_DISABLED": "1",
        "DATABASE_URL": stack.postgres.dsn("substrate_app", from_host=False),
        "EXOMEM_PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        "EXOMEM_CONTROL_PLANE_KEY": substrate_secrets.control_plane_key,
        "EXOMEM_CLOUD_ENABLED": "1",
        "EXOMEM_CLOUD_MCP_URL": MCP_URL,
        "EXOMEM_CLOUD_MCP_PATH": MCP_PATH,
        "EXOMEM_CLOUD_CELL_TOKEN_KEY": substrate_secrets.cell_token_key.hex(),
        "EXOMEM_CLOUD_CELL_IMAGE_REPOSITORY": cell_image_repository,
        "EXOMEM_ADMIN_TOKEN": substrate_secrets.admin_token,
        "EXOMEM_HOSTED_SCHEDULER_SECRET": substrate_secrets.scheduler_secret,
        "PADDLE_ENVIRONMENT": "sandbox",
        "PADDLE_WEBHOOK_SECRET": substrate_secrets.paddle_webhook_secret,
    }


def start(stack: Stack, source: Path, pki: tls.RehearsalPki, cell_image_repository: str) -> Substrate:
    substrate_secrets = SubstrateSecrets()
    sealed = f"{stack.network}-sealed"
    run(["docker", "network", "create", "--internal", sealed])
    stack.networks.append(sealed)
    run(["docker", "network", "connect", sealed, stack.postgres.container])
    pg_sealed_ip = json.loads(run(["docker", "inspect", stack.postgres.container]).stdout)[0][
        "NetworkSettings"]["Networks"][sealed]["IPAddress"]

    env = _app_environment(stack, substrate_secrets, cell_image_repository)
    env["DATABASE_URL"] = env["DATABASE_URL"].replace(f"@{stack.postgres.ip}:", f"@{pg_sealed_ip}:")
    env_file = stack.workdir / "secrets" / "substrate.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
    env_file.chmod(0o600)

    app = f"exo-rehearsal-substrate-{stack.run_id}"
    run(
        [
            "docker", "run", "--detach", "--name", app, "--network", sealed,
            "--env-file", str(env_file),
            "--mount", f"type=bind,source={source},target=/app",
            "--workdir", "/app",
            NODE_IMAGE, "node", "node_modules/next/dist/bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000",
        ]
    )
    stack.containers.append(app)

    edge_dir = stack.workdir / "edge"
    edge_dir.mkdir(parents=True, exist_ok=True)
    leaf = pki.leaves[tls.SUBSTRATE_HOST]
    (edge_dir / "substrate.crt").write_text(leaf.cert_pem, encoding="utf-8")
    (edge_dir / "substrate.key").write_text(leaf.key_pem, encoding="utf-8")
    (edge_dir / "dynamic.yml").write_text(
        json.dumps(
            {
                "http": {
                    "routers": {
                        "substrate": {
                            "rule": f"Host(`{tls.SUBSTRATE_HOST}`)",
                            "entryPoints": ["websecure"],
                            "service": "substrate",
                            "tls": {},
                        }
                    },
                    "services": {"substrate": {"loadBalancer": {"servers": [{"url": f"http://{app}:3000"}]}}},
                },
                "tls": {"certificates": [{"certFile": "/edge/substrate.crt", "keyFile": "/edge/substrate.key"}]},
            }
        ),
        encoding="utf-8",
    )
    edge = f"exo-rehearsal-edge-{stack.run_id}"
    run(
        [
            "docker", "run", "--detach", "--name", edge, "--network", stack.network,
            "--publish", "127.0.0.1::443",
            "--mount", f"type=bind,source={edge_dir},target=/edge,readonly",
            images.TRAEFIK,
            "--entryPoints.websecure.address=:443",
            "--providers.file.filename=/edge/dynamic.yml",
            "--log.level=WARN",
            # No call home: nothing in the run may reach a real server.
            "--global.checkNewVersion=false",
            "--global.sendAnonymousUsage=false",
        ]
    )
    stack.containers.append(edge)
    run(["docker", "network", "connect", sealed, edge])
    edge_port = int(run(["docker", "port", edge, "443/tcp"]).stdout.strip().splitlines()[0].rsplit(":", 1)[1])
    return Substrate(
        source=source,
        commit=run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip(),
        sealed_network=sealed,
        app_container=app,
        edge_container=edge,
        edge_host_port=edge_port,
        secrets=substrate_secrets,
        owner_dsn=stack.postgres.dsn("substrate_owner", from_host=True),
        cell_image_repository=cell_image_repository,
        redirect_uri="http://localhost:53682/callback",
    )


def sealed_egress_refused(substrate: Substrate) -> bool:
    """Evidence that the web app cannot reach a real service."""

    probe = run(
        [
            "docker", "exec", substrate.app_container, "node", "-e",
            "require('net').connect({host:'1.1.1.1',port:443,timeout:3000})"
            ".on('connect',()=>process.exit(0)).on('error',()=>process.exit(1)).on('timeout',()=>process.exit(1))",
        ],
        check=False,
    )
    return probe.returncode != 0


# --- injected email and billing: what the email and Paddle paths would do ---


async def seed_oauth_client(substrate: Substrate) -> None:
    """A pinned public client (Substrate has no dynamic registration)."""

    connection = await asyncpg.connect(substrate.owner_dsn)
    try:
        uris = json.dumps([substrate.redirect_uri])
        await connection.execute(
            """INSERT INTO exomem_oauth_clients
                 (client_id, admission_mode, enabled, redirect_uris, redirect_uris_digest)
               VALUES ($1, 'pinned', true, $2::jsonb,
                       digest(convert_to($2::jsonb::text, 'utf8'), 'sha256'))
               ON CONFLICT (client_id) DO NOTHING""",
            OAUTH_CLIENT_ID,
            uris,
        )
    finally:
        await connection.close()


async def seed_invite(substrate: Substrate, email: str, *, paid: bool) -> str:
    """What `POST /api/exomem/admin/invites` stores before it emails the link."""

    token = b64url(secrets.token_bytes(32))
    connection = await asyncpg.connect(substrate.owner_dsn)
    try:
        await connection.execute(
            """INSERT INTO exomem_invites (
                 token_digest, email_normalized, entitlement_source, entitlement_capabilities,
                 entitlement_limits, created_by_principal_digest, expires_at
               ) VALUES ($1, $2, $3, '["capture","recall"]'::jsonb, '{}'::jsonb, $4,
                         now() + interval '1 day')""",
            sha256(token),
            email.lower(),
            "paddle" if paid else "complimentary",
            secrets.token_bytes(32),
        )
    finally:
        await connection.close()
    return token


async def seed_deletion_token(substrate: Substrate, *, user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """What `POST /api/exomem/deletion/request` stores before it emails the token."""

    token = b64url(secrets.token_bytes(32))
    connection = await asyncpg.connect(substrate.owner_dsn)
    try:
        await connection.execute(
            """INSERT INTO exomem_access_tokens (purpose, token_digest, user_id, tenant_id, expires_at)
               VALUES ('deletion_confirmation', $1, $2, $3, now() + interval '1 hour')""",
            sha256(token),
            user_id,
            tenant_id,
        )
    finally:
        await connection.close()
    return token


async def bind_checkout(substrate: Substrate, tenant_id: uuid.UUID) -> str:
    """What a completed Paddle checkout leaves on the entitlement."""

    transaction_id = paddle_id("txn")
    connection = await asyncpg.connect(substrate.owner_dsn)
    try:
        await connection.execute(
            """UPDATE exomem_entitlements
                  SET provider_transaction_ref = $2, provider_environment = 'sandbox'
                WHERE tenant_id = $1""",
            tenant_id,
            transaction_id,
        )
    finally:
        await connection.close()
    return transaction_id


def paddle_event(
    *, event_type: str, status: str, user_id: uuid.UUID, tenant_id: uuid.UUID, subscription_id: str, transaction_id: str
) -> dict[str, object]:
    return {
        "event_id": paddle_id("evt"),
        "event_type": event_type,
        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "environment": "sandbox",
        "data": {
            "id": subscription_id,
            "transaction_id": transaction_id,
            "customer_id": paddle_id("ctm"),
            "status": status,
            "custom_data": {"product_key": "exomem-hosted", "user_id": str(user_id), "tenant_id": str(tenant_id)},
        },
    }


def paddle_signature(secret: str, raw_body: bytes) -> str:
    timestamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), timestamp.encode() + b":" + raw_body, hashlib.sha256).hexdigest()
    return f"ts={timestamp};h1={digest}"


def wait_ready(substrate: Substrate, probe) -> None:  # noqa: ANN001 - a zero-argument callable
    wait_for(probe, timeout=180, interval=2, description="Substrate to serve its authorization-server metadata")
