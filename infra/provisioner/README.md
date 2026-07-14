# Exomem hosted provisioner

This is the durable `exomem-cell-provisioner.v1` API and worker core. It owns
operation idempotency, tenant fences, claims, checkpoints, encrypted request and
result material, and opaque provider-resource metadata. It does not contain live
Kubernetes, HCloud, B2, Helm, routing, backup, or restore implementations; those
plug into `ProvisionerDriver` in a later deployment lane.

## Reproducible development

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
RUN_POSTGRESQL17_TEST=1 uv run --frozen pytest -q tests/test_postgresql17.py
```

SQLite is an injected test-only database. It can upgrade a disposable database
with the same Alembic history, but both production entry points reject it:

```bash
EXOMEM_PROVISIONER_DATABASE_URL=sqlite:///test.sqlite \
EXOMEM_PROVISIONER_DATABASE_SCHEMA=exomem_provisioner \
EXOMEM_PROVISIONER_DATABASE_ROLE=exomem_provisioner_runtime \
uv run --frozen alembic upgrade head
```

Production uses `postgresql+asyncpg`, a dedicated role, and a dedicated schema.
The role is created outside the application. Alembic creates the schema before
its version table in the same migration transaction and rejects an existing
schema owned by any other role. The opt-in PostgreSQL 17 suite proves fresh
bootstrap, ownership rejection, readiness, database-clock leases, and concurrent
fence/claim interleavings against a real server.

Worker claims lock the tenant fence before the operation row and recheck that
the claimed generation is still current. Every checkpoint and durable side
effect requires that current claim identity. PostgreSQL's clock, rather than the
API host clock, is authoritative for implicit lease acquisition and expiry.
Credential metadata supports only staged-inactive to active promotion, permits
one active version per cell, and rejects reversal or identity drift.

## Required startup configuration

- `EXOMEM_PROVISIONER_BEARER`: independently generated bearer, at least 32 bytes
- `EXOMEM_PROVISIONER_ENVELOPE_KEY`: separately generated envelope root secret,
  at least 32 bytes
- `EXOMEM_PROVISIONER_DATABASE_URL`: `postgresql+asyncpg://...` in production
- `EXOMEM_PROVISIONER_DATABASE_SCHEMA`: dedicated lower-case SQL identifier
- `EXOMEM_PROVISIONER_DATABASE_ROLE`: dedicated lower-case SQL identifier
- `EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS`: comma-separated private/loopback IPs
  or networks whose forwarded HTTPS metadata Uvicorn may trust; wildcards and
  public networks are rejected
- `EXOMEM_PROVISIONER_MAX_FAILURE_ATTEMPTS`: bounded retryable-driver failure
  ceiling (default six); provider-pending observations do not consume it

Run `exomem-provisioner-api` only after `alembic upgrade head`. Startup fails
closed when configuration is missing or invalid. Readiness verifies the exact
database role, schema owner, current schema, and singleton Alembic revision.
Access logs are disabled and the installed application/server formatter emits
only allowlisted content-free operational fields.
