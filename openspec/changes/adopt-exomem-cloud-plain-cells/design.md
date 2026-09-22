## Context

The hosted stack has three layers:

- Substrate, the Next.js control plane on Vercel;
- a Python provisioner of about 38k lines of source and 39k of tests, in a single-node K3s cluster on Hetzner;
- one Exomem pod per tenant.

Around those layers sat two trust chains: a contract chain (candidates, digests, cohorts, promotion) and an authorization chain (provisioner-owned custody Secrets, attestation windows, serving membership, activation tuples, governance migration Jobs). Every launch failure since early September lived in those chains:

- the empty-fleet admission deadlock;
- a stranded attestation window;
- binder races;
- catalog publication refusals;
- a read-only acknowledgement target.

The infrastructure-as-code (Terraform, Ansible, K3s, the encrypted storage class, SOPS) and the runtime were not the problem. As of 2026-09-23 there are zero cells and zero users, so nothing needs migrating and nothing needs to stay compatible.

Stage 0 verified three facts against the code:

- Standalone custody is rooted at a fixed, deliberately non-configurable path, `<passwd home>/.local/state/exomem/standalone-host-control-v1` (`authorization_custody.py`, `_standalone_host_control_root`).
- `build_server` in `server.py` selects `build_oauth(...)` for standalone and `HostedCellTokenVerifier` for hosted. A cloud verifier fits the same seam.
- `HOSTED_SURFACE_EXCLUSIONS` in `commands.py` already treats hosted as "the product surface, absence is the exception". Only `transfer_artifact`, `adopt_vault`, `process_media` and `read_media` carry a technical reason.

## Goals / Non-Goals

**Goals**

- One trust model: the standalone one.
- Provisioning, upgrade and deletion are level-triggered and straightforward.
- No third party terminates vault traffic.
- Isolation and privacy at least as strong as the hosted design.
- Owner acceptance on the real node before friends.
- The whole hot path runs on our own servers.

**Non-Goals**

- Per-cell zero-downtime upgrades. A blip of tens of seconds on a ReadWriteOnce volume is accepted.
- Multi-region.
- A marketplace listing or client plugin packaging.
- Direct browser transfers.
- Media workers.
- Self-serve export UI.
- Hosted owner-approval flows through Home (vocabulary floor 2, egress-policy administration). Cells run standalone governance defaults.
- Moving the Substrate website off Vercel.

## Decisions

### D1. A cell is standalone Exomem with a cloud-mode seam

`EXOMEM_CLOUD_CELL=1` selects cloud mode. `EXOMEM_HOSTED_CELL` and every hosted module stay unused. Cloud mode changes exactly these things:

1. **Authentication.** `auth` becomes `CloudCellTokenVerifier`, which compares the presented bearer against `EXOMEM_CLOUD_CELL_TOKEN` in constant time. There is no GitHub OAuth proxy and no OAuth storage.
2. **Log redaction.** Content-private logging is enabled, through the same gate `privacy_log` uses for hosted.
3. **Tool surface.** The members of `CLOUD_SURFACE_EXCLUSIONS` are removed from the MCP server after registration. Each entry uses the `HostedSurfaceExclusion` shape: `command`, `reason` stating what is technically broken, and `lifted_when`. The initial members are `transfer_artifact`, `adopt_vault`, `process_media` and `read_media`.
4. **Routes.** Only MCP (`/mcp`) and `/health` are registered. REST (`/api/*`), `/upload` and `/download` are not, because FastMCP custom routes do not inherit MCP authentication.
5. **Configuration.** No `.env` file is loaded; configuration comes only from the pod environment.

Everything else is the standalone path, including `LocalRuntimeActivation`, `AuthorizationSessionMiddleware`, in-process schema migrations and the local writer lease.

The tool list is UX, not a security boundary. The boundary is the container, its volume and its network policy, so a tool that reads the container filesystem can reach only that tenant's own material.

### D2. Custody lives on the cell's own volume

The `cloud` Dockerfile target creates UID/GID 10001 with home directory `/data/host`. The tenant PVC is mounted at `/data`, with pod `fsGroup: 10001`. The vault is `/data/vault` (`EXOMEM_VAULT_PATH`), and standalone custody resolves to `/data/host/.local/state/exomem/standalone-host-control-v1` with no code override.

Backups cover both `/data/vault` and `/data/host`. A restore therefore brings back the host-control key that the custody attachments were minted under.

### D3. cellctl: desired rows in, observed rows out, server-side apply in between

cellctl is one Deployment in namespace `exomem-cloud`. It holds no database, no fence generations, no leases and no checkpoints. Its loop runs every 5 seconds and also wakes on `LISTEN exomem_cloud_cells`.

1. Read rows whose `generation <> observed_generation`, or whose observed state is not terminal and not ready.
2. For each row:
   - **Running.** Render the cell manifests (D4) for that row and apply them with server-side apply under field manager `cellctl`, then observe.
   - **Stopped.** Apply with `replicas: 0`.
   - **Deleted.** Run D8.
3. Write `observed_*`, `ready`, `last_error_code` and `observed_at`. `observed_generation` is set only once the observation matches the applied generation.

**Waiting is normal.** A pending PVC, a pulling image or a pod still terminating is recorded as `provisioning` or `deleting`, then retried. Only an identity conflict fails the cell, with `last_error_code` set and no further mutation until the row's generation changes. Identity conflicts are:

- an existing namespace whose `exomem.io/cloud-cell` label differs from the row;
- a PVC bound to a volume not labelled for the cell.

cellctl runs as a single replica with `strategy: Recreate`. Server-side apply is idempotent, so a duplicate pass after a crash is harmless.

**RBAC.** Cluster-scoped namespace create and delete, restricted by admission to names with the `exo-cell-` prefix by one built-in `ValidatingAdmissionPolicy` that checks name and label only. Namespaced rights over the listed kinds. No other cluster rights.

### D4. Cell manifests

Namespace `exo-cell-<cell_id>` holds:

- Pod Security labels `enforce/audit/warn=restricted`.
- A ResourceQuota.
- A default-deny NetworkPolicy:
  - ingress on 8765 only from pods labelled `app.kubernetes.io/name=exomem-cloud-gateway` in namespace `exomem-cloud`;
  - egress only to DNS, plus the backup Job's egress to the B2 endpoint;
  - no internet egress for the runtime pod.
- A Secret with the cell bearer and backup password.
- A PVC of 10 GiB by default, on StorageClass `exomem-cloud-encrypted`: the existing encrypted Hetzner CSI class parameters with `reclaimPolicy: Delete` and `WaitForFirstConsumer`.
- A StatefulSet with `replicas: 1`, using the image from the row or the platform setting. The pod has:
  - `automountServiceAccountToken: false`;
  - non-root, seccomp `RuntimeDefault`, `readOnlyRootFilesystem`, dropped capabilities;
  - a readiness probe on `GET /health`;
  - resource requests and limits from values.
- A ClusterIP Service `cell` on 8765.
- A backup CronJob (D7).

The model files are baked into the image under `HF_HOME` and loaded offline.

### D5. Releases are one setting

`exomem_cloud_settings.cell_image` holds `ghcr.io/…/exomem-cloud-cell@sha256:<digest>`. When it changes, cellctl rolls cells one at a time:

1. It moves cells with `rollout_priority = 0` first; the owner's cell is the canary.
2. Before each cell, it runs a pre-upgrade snapshot Job.
3. It applies the new image and waits for the new pod to be Ready and `/health` to answer.
4. Then it moves to the next cell.

If a cell is not ready within 10 minutes, cellctl returns that cell to its previous digest, sets `rollout_paused = true` with the error code, and stops. Rollback is setting the previous digest. A row's own `desired_image` overrides the setting for that cell.

Nothing else pins a release: no candidates, locks, fixtures or adoption PRs. The cell image is published by a CI job on release tags, and the tag-to-digest mapping is recorded in the release notes.

### D6. Secrets

- **Cell bearer.** `HMAC-SHA256(cell_token_key, "exomem-cloud-cell-token-v1:" + cell_id)`, base64url. The gateway and cellctl both hold `cell_token_key`; no bearer is stored in the database. Rotation bumps the key version, and cellctl re-applies Secrets before the gateway switches versions.
- **Backup data key.** Each cell has a random 32-byte key. cellctl generates it and envelope-encrypts it with `backup_master_key` (AES-GCM), storing the result in the row as `backup_key_wrapped` with a version. The cell's Secret holds the plaintext for the backup Job only. The runtime container does not mount it.
- **Master keys.** `cell_token_key`, `backup_master_key`, the B2 credentials, the Hetzner API token (read-only volume listing for capacity and deletion verification) and database DSNs use the existing SOPS workflow under `infra/secrets`.

### D7. Backups and restore

A nightly CronJob, plus the pre-upgrade Job, runs restic against the B2 bucket at `cells/<cell_id>`:

- repository password is the cell's data key;
- it covers `/data/vault` and `/data/host`;
- the PVC is mounted read-only, and the Job may run while the cell serves;
- retention is 7 daily and 4 weekly.

Restore is a runbook for the alpha: create a scratch namespace, restic restore, and point a row at it. It is exercised in the local rehearsal before the node is touched.

### D8. Deletion removes everything and destroys the key

A row with `desired_state = deleted` makes cellctl:

1. delete the namespace (the PVC goes with it, and the volume goes through `reclaimPolicy: Delete`);
2. confirm the namespace, the PV and the Hetzner volume carrying the cell's labels are all absent;
3. delete the restic repository prefix in B2 and confirm it is empty;
4. null `backup_key_wrapped`;
5. write `observed_state = deleted`.

Each step retries until its check holds. A failed observation never counts as absence.

### D9. Capacity is observed, not reserved

For each node, cellctl publishes `exomem_cloud_capacity`: node, volume attachments in use, the Hetzner per-server attachment limit, and headroom from values. Substrate admits a new cell only while non-deleted rows are below the published capacity. There is no reservation ledger to leak.

Adding a node is a Terraform `count` change plus an Ansible K3s join. Placement is recorded in the row's `node`.

### D10. Direct TLS ingress

- The platform Traefik terminates TLS for the MCP hostname with an ACME TLS-ALPN-01 resolver.
- The Cloudflare record is DNS-only, and the Hetzner firewall opens 443.
- Only the gateway's IngressRoute is public. Cells, cellctl and the Kubernetes API are not.
- Cluster administration uses SSH or WireGuard.
- `cloudflared` stays only for the old platform's hostnames until retirement.

### D11. Control database on its own server

- Terraform adds `hcloud_server.control` (a small x86 instance), an `hcloud_network` shared with the fleet node, and firewall rules. Ansible adds a `postgres` role: PostgreSQL 17, PgBouncer in transaction mode, and TLS with a public certificate for verify-full.
- pgBackRest backs up to B2 with WAL archiving (point-in-time recovery), a nightly full and weekly verification.
- Roles:
  - `substrate_app`: owner of the schema.
  - `exomem_gateway`: read-only on the token, tenant and cell routing columns.
  - `exomem_cellctl`: column-level `SELECT` on desired columns, `UPDATE` on observed columns and `backup_key_wrapped`, `INSERT`/`UPDATE` on capacity, `SELECT` on settings.
- In-cluster clients connect over the private network. Vercel connects through PgBouncer's public TLS port.
- A separate server keeps the fleet and its records in different failure domains. That answers the 2026-09-10 objection to colocation for about EUR 5 per month.

## Shared contracts with Substrate

These must match the companion Substrate change byte for byte. The Substrate migration is the schema of record. cellctl's tests apply `infra/cellctl/tests/fixtures/exomem_cloud_schema.sql`, a copy of that migration, and the local rehearsal runs the real one.

- **C1 `exomem_cloud_cells`**
  - Identity and placement: `cell_id text primary key` (lowercase base32, 16 chars), `tenant_id` (references the tenant), `node text`, `storage_gib int not null default 10`, `rollout_priority int not null default 1`.
  - Desired state, written by Substrate: `desired_state text check in ('running','stopped','deleted')`, `desired_image text null`, `generation bigint not null`, which increments on every desired change and wakes cellctl through the trigger `pg_notify('exomem_cloud_cells', cell_id)`.
  - Observed state, written by cellctl:
    - `observed_generation bigint`;
    - `observed_state text check in ('pending','provisioning','running','stopped','deleting','deleted','failed')`;
    - `observed_image`, `ready boolean`, `last_error_code text`, `observed_at timestamptz`;
    - `backup_key_wrapped bytea`, `backup_key_version int`.
  - Timestamps: `created_at`, `updated_at`.
- **C1b `exomem_cloud_settings`** (`key text primary key`, `value jsonb`): `cell_image`, `rollout_paused`, `rollout_error_code`.
- **C1c `exomem_cloud_capacity`** (`node text primary key`, `attachments_used`, `attachments_limit`, `headroom`, `observed_at`).
- **C2 Cell environment**: `EXOMEM_CLOUD_CELL=1`, `EXOMEM_CLOUD_CELL_ID`, `EXOMEM_CLOUD_CELL_TOKEN`, `EXOMEM_VAULT_PATH=/data/vault`. Command `exomem --transport http --host 0.0.0.0 --port 8765`. Readiness is `GET /health`. MCP is at `/mcp`.
- **C3 Gateway to cell**
  - The gateway resolves the cell only from the authenticated OAuth principal.
  - It sends `Authorization: Bearer <D6 bearer>` to `http://cell.exo-cell-<cell_id>.svc.cluster.local:8765/mcp`.
  - It forwards only `content-type`, `accept`, `mcp-session-id`, `mcp-protocol-version` and `last-event-id`, and adds `x-request-id`.
  - It streams request and response bodies without buffering.
  - It never forwards the client's `Authorization`, cookies or forwarding headers.

## Controls retired, and why

Each row states what the control prevented, what it cost when it fired wrongly, and who paid.

| Control | Prevented | Wrong-firing cost, and who paid |
|---|---|---|
| Provisioner-owned custody Secret, attestation window, serving membership, activation tuple and ack, governance migration Jobs | A tenant process minting its own authority; stale replicas serving | Stranded cells and read-only acknowledgement failures, which blocked every user, paid by the owner for a month. The tenant process already is the vault's trust domain. Cross-tenant protection comes from isolation, which is kept |
| Contract candidates, digests, cohorts, rollout assignments, client artifacts, promotion, reviewer bootstrap | A served tool surface differing from a certified one | An empty fleet admitted no one, and every release cost a 27k-line adoption. The cell now publishes its own surface, and certification belongs to a future listing, not to admission |
| Fenced checkpoint phases, leases, fence generations, provisioner database | Double effects from a non-idempotent workflow | Retries turned into terminal states. Server-side apply is idempotent |
| Deployment locks, bespoke CEL pod-shape pins | Release or pod-shape drift | Every change needed a lock ceremony. Pod Security `restricted`, network policy and the one namespace-name policy remain |

## Controls kept

These stay fail-closed, because an unexpected value there means something is wrong:

- per-tenant namespace, volume, Secret and backup key;
- Pod Security `restricted`, no ServiceAccount token, read-only root filesystem;
- default-deny network policy;
- tenant identity derived only from the OAuth principal;
- constant-time bearer comparison;
- content-free logs and telemetry;
- encrypted backups;
- verified deletion with key destruction;
- parsers, authentication, billing and schema validation.

## Disposition of existing changes

- **Superseded; not to be implemented further. Removed with their code in phase R:**
  - `simplify-hosted-launch-boundaries`
  - `repair-hosted-activation-acknowledgement`
  - `unblock-drained-custody-migration`
  - `standardize-hosted-runtime-upgrades`
  - `bind-hosted-candidate-runtime-identity`
  - `fix-hosted-runtime-profile-selection`
  - `widen-hosted-epistemic-surface`, whose goal cloud mode reaches by construction
  - `make-hosted-admission-self-describing`
  - `idle-the-hosted-control-plane-database`
  - `add-hosted-client-plugins`
  - `add-hosted-private-alpha-infrastructure`: its remaining tasks move here, and its shipped Terraform and Ansible stay
  - `fix-hosted-alpha-runtime-fidelity`: lane A carries over any runtime setting it proves necessary
- **Retained:** `hosted-human-capture-and-onboarding` (a Home product surface, re-scoped onto cloud after acceptance), and every non-hosted change.

## Risks / Trade-offs

- **Namespace deletion destroys data** under `reclaimPolicy: Delete`. The mitigation is nightly and pre-upgrade backups, and namespace deletion is reachable only through cellctl or cluster admin. The trade is accepted because a user's deletion must actually delete.
- **One fleet node.** Losing it takes every cell down until it is rebuilt from IaC and restored from B2. The control database survives on its own server. This is acceptable for the alpha, and a second node is a `count` change.
- **Public PgBouncer port for Vercel.** Mitigated by verify-full TLS, strong per-role credentials, Hetzner firewall rate limits and fail2ban. Revisit if Vercel static egress becomes worthwhile.
- **Vercel-to-database latency** (fra1 to Hetzner Germany) is a few milliseconds per query, and it is off the MCP hot path.
- **The standalone governance kernel in a container** is proven on the desktop but not in this image. The local rehearsal gates the node.
- **Letting go of provisioner-held revocation.** Suspension is `replicas: 0` plus gateway refusal on a non-running row, and it takes effect within one cellctl pass.

## Migration Plan

1. **S.** Specs in both repositories, then an independent critic review, then superseded-change banners and PR closures.
2. **Build lanes:**
   - **A:** cloud mode and image.
   - **B:** cellctl, manifests, platform chart and Traefik ACME.
   - **D:** control server and Postgres IaC.
   - In Substrate, **D2** (driver) lands before **C** (admission, gateway, schema).
3. **P3, local rehearsal as one command** on disposable K3s with the real image, Postgres, Substrate and gateway. It runs invite, provision, OAuth MCP, capture, cited recall, pod kill, upgrade, second-tenant denial, backup and restore, and deletion. It gates the node.
4. **P4, node:**
   - Stand up the control server and restore the Substrate database from Neon in a maintenance window, then verify Endstate and Exomem.
   - Deploy cellctl and the gateway beside the old platform, and scale the old provisioner to zero.
   - Owner acceptance.
5. **P5, friends**, after the owner confirms.
6. **R, retire:**
   - Delete the hosted-only runtime modules, provisioner v1, the old chart pieces, locks and plugin candidates.
   - Remove the superseded change directories and the `hosted-*` canonical specs with the code they describe.
   - Delete Neon after seven clean days.

**Rollback before P4** is a no-op. **After P4**, the old platform is still installed but has no cells, and the database can be restored from the Neon export taken at cutover.

## Open Questions

- The exact MCP hostname and brand domain for Exomem Cloud. This is configuration, set at P4.
