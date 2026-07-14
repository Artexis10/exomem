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

Production is split into capability-bounded processes. The API signs the fixed,
complete recovery-envelope set before it accepts provider work. The routine worker
can verify those identities and reconcile namespaced Kubernetes, Helm, Traefik,
private-runtime, maintenance, and capacity state, but it has no signing key,
HCloud credential, B2 credential, persistent-volume mutation, or pod-exec
authority. `exomem-volume-worker` is the narrow privileged continuation for
retained HCloud volume and static PV registration. Backup, export, and ordered
deletion run as separate durability workloads. The deletion provider consumes
only the authoritative durable destroy/discard claim: it never calls back into
Substrate for admission, billing, or lifecycle state. No production process has
a fake-provider selection path.

Credential-dependent HCloud, B2 Object Lock, Cloudflare, and clean-cluster
rebind drills remain release gates even when deterministic and exact-K3s suites
pass.

## Durability contract

The central vault-backup sweep enumerates cells every 30 minutes, derives one
stable operation ID per cell/slot, and relies on the database partial unique
constraint to serialize backup/export/restore work. It renews claims during long
snapshots and reports verified-object age, warning at 45 minutes and blocking new
alpha invitations at 60 minutes.

Vault backups stop and verify routes, quiesce the cell, stage and authenticate the
portable archive, reopen service, then encrypt and upload. Every archive uses a
unique AES-256-GCM data key; the wrapped key remains in the provisioner database.
Recovery objects use seven-day B2 governance retention and a 30-day lifecycle.
After the lock expires, deletion uses exact B2 version IDs without governance
bypass and proves that neither object versions nor delete markers remain.
User exports use a separate private bucket without Object Lock: their exact
caller-supplied expiry is authenticated in the durable checkpoint, object row,
and B2 metadata, while a 31-day provider lifecycle is only a cleanup backstop.

B2 access is capability-separated. The normal worker receives upload/list only;
restore/presign and deletion use distinct short-lived clients. Download URLs are
HTTPS and expire within 15 minutes. Complete PostgreSQL backups use `pg_dump`'s
serializable-deferrable custom format, prove an empty scratch restore and owner /
tenant / cell resolution before upload, then encrypt and remotely verify the
object. Provider rediscovery scans Kubernetes, HCloud, Traefik, and B2 completely
before raising tenant fences or adopting/quarantining newer side effects.

## Reproducible development

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check .
RUN_POSTGRESQL17_TEST=1 uv run --frozen pytest -q tests/test_postgresql17.py
```

The pinned production provider libraries are `kubernetes` 35.x for Kubernetes
1.35 and the official Hetzner `hcloud` 2.x client. The shared provisioner image
contains PostgreSQL 17.10 client tools (`pg_dump`, `pg_restore`, `psql`,
`dropdb`, and `createdb`) for the separately permissioned durability workloads.
`HelmCliAdapter` additionally requires the repository-pinned Helm 3.19.4 binary
and chart 0.1.0. Helm values carry only non-secret configuration; cell
credentials are materialized through the Kubernetes Secret boundary and
provider references remain encrypted in the operation store.

SQLite is an injected test-only database. It can upgrade a disposable database
with the same Alembic history, but both production entry points reject it:

```bash
EXOMEM_PROVISIONER_DATABASE_URL=sqlite:///test.sqlite \
EXOMEM_PROVISIONER_DATABASE_SCHEMA=exomem_provisioner \
EXOMEM_PROVISIONER_DATABASE_ROLE=exomem_provisioner_runtime \
uv run --frozen alembic upgrade head
```

Production uses `postgresql+asyncpg`, a dedicated role, and a dedicated schema.
The immutable image carries the exact `alembic.ini` and revision tree at
`/opt/exomem/provisioner-migrations`, root-owned and non-writable. Production
database lifecycle uses three zero-argument, environment-only commands:

- `exomem-provisioner-database-bootstrap` briefly requires
  `EXOMEM_PROVISIONER_DATABASE_ADMIN_URL`. It creates or exactly validates the
  least-privilege runtime role/schema, migrates through a runtime-authenticated
  connection, and proves final runtime access while one bounded advisory lock
  spans the whole operation.
- `exomem-provisioner-database-migrate` uses only the runtime URL, accepts an
  empty or known packaged revision, and migrates to the single packaged head.
- `exomem-provisioner-database-validate` uses only the runtime URL and succeeds
  only when the database already equals that head. The platform uses this
  validation-only command on upgrade so Helm never implies a rollback-safe
  revision advance.

Existing role attributes, memberships, database/schema ownership, unknown or
multiple revisions, and mismatched admin/runtime database identities fail
closed. Bootstrap never repairs privilege drift. The admin URL belongs only to
the ephemeral operator bootstrap Job; stable chart resources never reference
it. The opt-in PostgreSQL 17 suite runs the built image without a checkout mount
and proves fresh/concurrent/retry bootstrap, exact role authority, lock
serialization, package integrity, exact-head upgrade refusal, and credential
non-disclosure.

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

User-export `expiresAt` is admitted only when a new idempotency key is future
and no more than 30 days away. Once that exact key and canonical input have been
accepted, pending and completed replays continue after expiry; a first-time
expired request receives the content-free terminal code
`EXPORT_REQUEST_EXPIRED` before an operation or provider artifact exists.
Missing, malformed, or more-than-30-day expiry values remain
`PROVISIONER_REJECTED` validation failures.

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
- `EXOMEM_PROVISIONER_DATABASE_ADMIN_URL`: one-use operator credential accepted
  only by `exomem-provisioner-database-bootstrap`; never configure it on API,
  worker, recurring migration, or validation workloads
- `EXOMEM_PROVISIONER_DATABASE_LOCK_TIMEOUT_SECONDS`: bounded database-command
  advisory-lock wait in seconds (default 60, range 1-300)
- `EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS`: comma-separated private/loopback IPs
  or networks whose forwarded HTTPS metadata Uvicorn may trust; wildcards and
  public networks are rejected
- `EXOMEM_PROVISIONER_MAX_FAILURE_ATTEMPTS`: bounded retryable-driver failure
  ceiling (default six); provider-pending observations do not consume it
- `EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY`: URL-safe base64 Ed25519 seed confined
  to signer-bearing processes. The API uses its governed seed to pre-seal the
  bounded recovery-identity pool, and the volume worker uses that same governed
  trust root for retained-volume identities. The routine worker and short-lived
  deletion Jobs receive only the corresponding public verifier and must never receive the
  seed.

The routine worker additionally requires the release-manifest path, pinned
Helm/chart details, internal/control/transfer origins, a worker ID, and
`EXOMEM_PROVIDER_RECOVERY_PUBLIC_KEY`. The Helm chart supplies non-secret
fields, mounts the release ConfigMap, and consumes each governed Secret by its
exact key. Its RBAC is read-mostly cluster discovery plus admission-bounded
namespaced lifecycle mutation; it has no `pods/exec` or persistent-volume write
rule.

The privileged volume worker receives only the common operation-store settings,
an HCloud token, the volume-encryption Secret identity, location, worker ID, and
its governed `EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY`. Its queue filter accepts
only `volume-registration-required` continuations. B2 credentials are confined
to the corresponding durability workloads, and Cloudflare Access credentials
remain outside K3s.

Run `exomem-provisioner-api` only after the packaged runtime migration gate. Startup fails
closed when configuration is missing or invalid. Readiness verifies the exact
database role, schema owner, current schema, and singleton Alembic revision.
Access logs are disabled and the installed application/server formatter emits
only allowlisted content-free operational fields.
