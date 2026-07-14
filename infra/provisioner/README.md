# Exomem hosted provisioner

This is the durable `exomem-cell-provisioner.v1` API, worker core, and isolated
cell lifecycle reconciler. It owns operation idempotency, tenant fences, claims,
multi-step checkpoints, encrypted request/result/provider references, and
opaque provider metadata. The lifecycle slice includes official Kubernetes and
HCloud client adapters, a pinned Helm CLI adapter, exact Traefik route control,
high-fidelity provider fakes, retained-volume registration/rebind/destruction,
cell provision/health/lifecycle, and maintenance gating. The high-fidelity
provider also models discard and ordered tenant destruction for the dedicated
durability/deletion implementation lane.

The production worker composes only the official Kubernetes, HCloud, Helm,
Traefik, internal-runtime, and hosted-operator adapters; it has no fake-provider
selection path. B2 export/backup implementation remains a separate durability
lane. Credential-dependent HCloud and Cloudflare drills remain release gates
even when deterministic and exact-K3s suites pass.

## Reproducible development

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
RUN_POSTGRESQL17_TEST=1 uv run --frozen pytest -q tests/test_postgresql17.py
```

The pinned production provider libraries are `kubernetes` 35.x for Kubernetes
1.35 and the official Hetzner `hcloud` 2.x client. `HelmCliAdapter` additionally
requires the repository-pinned Helm 3.19.4 binary and chart 0.1.0. Helm values
carry only non-secret configuration; cell credentials are materialized through
the Kubernetes Secret boundary and provider references remain encrypted in the
operation store.

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

Provider checkpoints are preserved across claim expiry and worker restart.
Intermediate namespace/release/PVC/volume/route references are persisted before
the operation advances; the recoverable reference is encrypted while the
content-free kind and immutable operation/fence identity remain queryable.
Reconciliation adopts an exactly tagged partial attempt and rejects metadata,
location, release, protocol, policy, or admission drift.

One immutable hosted release manifest is the sole runtime deploy pin. The
platform chart places `exomem-hosted-release-v1.json` in an immutable ConfigMap,
mounts it read-only at `/etc/exomem/release`, and sets only
`EXOMEM_PROVISIONER_RELEASE_MANIFEST_PATH` for release selection. Worker startup
rejects an incomplete manifest, mutable image, publication-tag drift, unknown
fields, malformed 21-command registry, or legacy independent image/version/
contract overrides. The runtime image, release, protocol, gateway digest, and
operator digest are all derived from that parsed unit before provider work.
Production Helm invocations supply the reviewed file as one value:

```bash
helm upgrade --install exomem-platform infra/helm/platform \
  --set-file provisioner.releaseManifestJson=infra/contracts/exomem-hosted-release-v1.json
```

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

The worker additionally requires the release-manifest path, pinned Helm/chart
details, internal/control/transfer origins, the HCloud credential,
volume-encryption Secret identity, and a worker ID. The Helm chart
supplies non-secret fields, mounts the release ConfigMap, and consumes each
governed Secret by its exact key. The API receives only provisioner auth,
database, and wrapping-key material. The worker receives those plus HCloud and
capability-separated B2 credentials; Cloudflare Access credentials remain
Vercel-only and are not copied into K3s.

Run `exomem-provisioner-api` only after `alembic upgrade head`. Startup fails
closed when configuration is missing or invalid. Readiness verifies the exact
database role, schema owner, current schema, and singleton Alembic revision.
Access logs are disabled and the installed application/server formatter emits
only allowlisted content-free operational fields.
