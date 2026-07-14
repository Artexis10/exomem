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
```

Upgrade a disposable SQLite database with the same Alembic history used in
production:

```bash
EXOMEM_PROVISIONER_DATABASE_URL=sqlite:///test.sqlite \
EXOMEM_PROVISIONER_DATABASE_SCHEMA=exomem_provisioner \
EXOMEM_PROVISIONER_DATABASE_ROLE=exomem_provisioner_runtime \
uv run --frozen alembic upgrade head
```

Production uses `postgresql+asyncpg`, a dedicated role, and a dedicated schema.
The role is created outside the application; Alembic owns the schema and tables.

## Required startup configuration

- `EXOMEM_PROVISIONER_BEARER`: independently generated bearer, at least 32 bytes
- `EXOMEM_PROVISIONER_ENVELOPE_KEY`: separately generated envelope root secret,
  at least 32 bytes
- `EXOMEM_PROVISIONER_DATABASE_URL`: `postgresql+asyncpg://...` in production
- `EXOMEM_PROVISIONER_DATABASE_SCHEMA`: dedicated lower-case SQL identifier
- `EXOMEM_PROVISIONER_DATABASE_ROLE`: dedicated lower-case SQL identifier

Run `exomem-provisioner-api` only after `alembic upgrade head`. Startup fails
closed when configuration is missing or invalid. Access logs are disabled; the
application formatter emits only allowlisted content-free operational fields.
