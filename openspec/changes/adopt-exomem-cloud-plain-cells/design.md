## Context

The hosted stack has three layers:

- Substrate, the Next.js control plane on Vercel;
- a Python provisioner of about 38k lines of source and 39k of tests, in a single-node K3s cluster on Hetzner;
- one Exomem pod per tenant.

Around those layers sat two trust chains. The contract chain covered candidates, digests, cohorts and promotion. The authorization chain covered provisioner-owned custody Secrets, attestation windows, serving membership, activation tuples and governance migration Jobs.

Every launch failure since early September lived in those chains:

- the empty-fleet admission deadlock;
- a stranded attestation window;
- binder races;
- catalog publication refusals;
- a read-only acknowledgement target.

The infrastructure-as-code and the runtime were not the problem. That means Terraform, Ansible, K3s with secrets encryption, the encrypted storage class and SOPS. As of 2026-09-23 there are zero cells and zero users.

Facts verified against the code, including by the independent critic review of 2026-09-23:

- **Custody root.** Standalone custody is rooted at a fixed, deliberately non-configurable path, `<passwd home>/.local/state/exomem/standalone-host-control-v1` (`authorization_custody.py`, `_standalone_host_control_root`). It requires owner-only file modes (around line 1785). Writer-lease idempotency state does too (`writer_lease.py:2419-2432`).
- **Governance schema.** A fresh desktop vault serves governed writes and recall with no governance sidecar and no schema migration. Schema v4 with standalone custody attachment is the non-public foundation for vault consolidation. Its forward migration verifies external custody, and a fresh vault has none. Measured on 2026-09-23 against the cloud image: a vault taken to v4 either refuses every write (`GOVERNANCE_CATALOG_PUBLICATION_BLOCKED`) or, with the hosted custody variables set, refuses every recall (`governed projected retrieval is unavailable`). A vault that skips v4 serves writes, lexical recall and paraphrased recall in cloud mode.
- **Vault layout.** `resolve_vault` refuses a directory that is not a vault. State migrations run offline through `exomem maintain --migrate-state --offline` before a server starts (`scripts/upgrade.sh`). A migrated state refuses an older image (`state_migration.py:384-385`).
- **Principal.** `resolve_mcp_principal` needs a `sub` claim (`governance/principal.py:182-187`). Owner-only governance operations require the owner audience (`governance/tool.py:154-158`). A remote OAuth principal is a non-owner, as on the desktop (`session_oauth.py:172-178`).
- **Health.** `/health` is liveness and always answers. `/health/ready` is readiness (`server_assets.py:111-173`).
- **Logging.** Content-private logging keys on `EXOMEM_HOSTED_CELL` (`privacy_log.py:24-26`). The standalone call trace logs `query=` (`server.py:268`).
- **Session credentials** are optional for ordinary product commands. They are required only for `govern_memory` session status, rotate and close (`authorization_request.py`).
- **Tool surface.** `HOSTED_SURFACE_EXCLUSIONS` (`commands.py`) treats hosted as "the product surface, absence is the exception". Only `transfer_artifact`, `adopt_vault`, `process_media` and `read_media` carry a technical reason.
- **Platform facts.**
  - K3s runs with `secrets-encryption: true`, and etcd snapshots are retained on node and in S3.
  - servicelb is disabled.
  - Traefik is ClusterIP, with its `web` entrypoint carrying routes behind Cloudflare Access.
  - Terraform has one `hcloud_server.alpha` with `cluster-init` and an `hcloud_network.alpha`.

## Goals / Non-Goals

**Goals**

- One trust model: the standalone one.
- Provisioning, upgrade, backup and deletion are level-triggered and unattended.
- No third party terminates vault traffic.
- Isolation and privacy at least as strong as the hosted design.
- Owner acceptance on the real node before friends.
- The hot path runs on our own servers.

**Non-Goals**

- Per-cell zero-downtime upgrades and backups. Short stops are accepted.
- Multi-node and multi-region. The design keeps them possible.
- A marketplace listing or client plugin packaging.
- Direct browser transfers, media workers, and a self-serve export UI.
- Owner-only governance authoring from a Cloud client. The Cloud principal is a non-owner, as with the desktop's remote clients.
- Moving the Substrate website off Vercel.

## Decisions

### D1. A cell is standalone Exomem with a cloud-mode seam

`EXOMEM_CLOUD_CELL=1` selects cloud mode. `EXOMEM_HOSTED_CELL` and every hosted module stay unused. Cloud mode changes exactly the following, and everything else is the standalone path: `LocalRuntimeActivation`, `AuthorizationSessionMiddleware`, and the local writer lease.

1. **Authentication.**
   - `auth` is `CloudCellTokenVerifier`. It accepts a bearer equal to `EXOMEM_CLOUD_CELL_TOKEN`, or, during a rotation, to `EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS`. The comparison is constant-time.
   - A configured token shorter than 32 characters, current or previous, fails startup with `CLOUD_CELL_CONFIG_INVALID`, without echoing the value. A C4 bearer is always 43 characters, so this fires only on a mis-rendered Secret.
   - There is no GitHub OAuth proxy and no OAuth storage.
   - The verifier emits fixed claims `{sub: <cell_id>, iss: "exomem-cloud-cell"}` and the cell-service scope. The claims are never derived from the bearer or from a key version.
   - The resulting principal is a resolved **non-owner**, exactly as a remote OAuth principal is on the desktop.
   - Ordinary product commands and governed writes work.
   - Owner-only governance operations, which include policy authoring, return `GOVERNANCE_OWNER_REQUIRED`. That loss is accepted and listed under Non-Goals.
2. **Logging.**
   - `privacy_log.content_private_logging_enabled` also returns true in cloud mode, and the redaction hook is installed.
   - The call-trace middleware runs in its content-free form (as `hosted=True` does), so no `query=` is logged.
   - **Journals.** The query, read and write journals (`query_log`) are off whenever content-private logging is enabled. The check lives in `query_log` itself, not in the manifest or in a test-only variable, so it fails closed. Cloud mode also sets `EXOMEM_DISABLE_USAGE_BOOST` and `EXOMEM_DISABLE_RELEVANCE_CHECK`, as the hosted runtime does, because those features read the journals.
   - **Call ledger.** Under content-private logging, a ledger row keeps no target paths, no hashes of argument values and no caller-chosen argument names. It keeps the command, the argument count, value lengths, duration and error code. A hash of a short value is an offline confirmation oracle for a guessed query.
   - **Log directory.** The image defaults `EXOMEM_LOG_DIR` to `/tmp/exomem-logs`, so no runtime log ever lands on the tenant volume that D8 backs up, even without the manifest's setting.
3. **Tool surface.**
   - The members of `CLOUD_SURFACE_EXCLUSIONS` are removed from the MCP server after registration.
   - Each entry uses the `HostedSurfaceExclusion` shape: `command`; `reason`, stating what is technically broken; and `lifted_when`.
   - The initial members are `transfer_artifact`, `adopt_vault`, `process_media` and `read_media`.
   - Legacy MCP aliases (`EXOMEM_MCP_LEGACY_COMPAT`) are never registered in cloud mode. A cell has no legacy clients, and aliases would re-expose the leaves of excluded commands.
4. **Read-only mode.** With `EXOMEM_CLOUD_READ_ONLY=1` the cell refuses every mutating command with `CLOUD_CELL_READ_ONLY`, before vault access. Mutation is classified from the command registry, not from a copied list. Reads keep working.
5. **Routes.** Only MCP (`/mcp`), `/health` and `/health/ready` are registered. REST (`/api/*`), `/upload` and `/download` are not, because FastMCP custom routes do not inherit MCP authentication.
6. **Configuration.** No `.env` file is loaded; configuration comes only from the pod environment. That covers every loader, including the runtime-resource dotenv policy, not only the server's.

The tool list is UX, not a security boundary. The boundary is the container, its volume and its network policy.

### D2. The volume holds the vault and custody, with owner-only modes preserved

The `cloud` Dockerfile target derives from the `hosted` runtime stage:

- It keeps the hosted stage's offline ONNX model environment and `EXOMEM_DISABLE_RANKING`.
- It creates UID/GID 10001 with home directory `/data/host`.
- It sets `FASTMCP_CHECK_FOR_UPDATES=off` and `FASTMCP_SHOW_SERVER_BANNER=false`. A cell has no egress, and the update check otherwise stalls every cold start on DNS (measured: `/health` up after 24 s against 4 s).

The tenant PVC is mounted at `/data`. The vault is `/data/vault` (`EXOMEM_VAULT_PATH`), and standalone custody resolves to `/data/host/.local/state/exomem/standalone-host-control-v1` with no code override.

- The pod sets `fsGroup: 10001` and `fsGroupChangePolicy: OnRootMismatch`, so kubelet changes ownership only on the first mount of an empty volume and never rewrites modes afterwards.
- The init container creates `/data/vault` and `/data/host` with explicit mode `0700`.
- Every Job that touches the volume (init, backup, restore) runs as UID/GID 10001 with the same pod-level `fsGroup` and `fsGroupChangePolicy`, and preserves modes. A restore into a fresh PVC is therefore writable.
- A K3s test asserts `0700`/`0600` on custody and writer-lease state after first start, after pod replacement and after a restore. Each case is followed by a governed write.
- If `OnRootMismatch` or setgid inheritance still defeats the custody mode checks on the real storage class, the implementer stops and reports rather than weakening the checks.

A writable `emptyDir` is mounted at `/tmp`. The pod sets `TMPDIR=/tmp`, `EXOMEM_LOG_DIR=/tmp/exomem-logs` and `OMP_NUM_THREADS`, carried over from the hosted StatefulSet environment. The root filesystem stays read-only.

### D3. First boot and every upgrade run offline preparation in an init container

The StatefulSet has one init container, `cell-init`, using the same image as the runtime. It runs non-root with the volume mounted and the server not yet started. The volume is ReadWriteOnce and the replica count is 1, so this is genuinely offline. The container is idempotent:

1. It creates `/data/vault` and `/data/host` if absent, and sets both to mode `0700`.
2. If `/data/vault` is not a vault, it initializes one **atomically**. It builds the vault in a staging directory on the same volume (`/data/.vault-init-*`), then renames the staging directory onto `/data/vault`, which must be absent or empty. Stale staging directories from an earlier crash are removed first. A non-empty `/data/vault` that is not a vault fails with `CELL_INIT_VAULT_UNRECOGNIZED` and is never overlaid: an interrupted init cannot produce it, so it means something else wrote there.
3. It runs `exomem maintain --vault /data/vault --migrate-state --offline --json`.

That is the desktop's own first-run path, so the cell keeps the one rule. There is no governance schema migration and no custody environment. A cell runs standalone governance defaults, like a fresh desktop install. Schema v4 arrives only with a later change that needs multi-audience authorization inside a cell.

**Re-entry.** `init` is skipped once the volume holds a vault, and state migration is idempotent. A run interrupted at any point is completed by the next run. Lane A tests a fresh volume, a second run that changes nothing, a run interrupted after `init`, and runs interrupted **during** `init` at several points of the scaffold copy. Each interrupted run must be followed by a successful run that leaves a complete vault.

**Output.** `cell-init` installs the redaction hook, as the server does. A failure prints one JSON line with the step and a stable error code, and exits non-zero; it never prints a traceback, which would carry absolute paths.

**Setgid volume root.** fsGroup leaves the volume root setgid (`2770`). `init` and every later directory creation must succeed under it, and owner-only custody modes must still hold. The container test mimics it by `chmod 2770` on the volume root.

**Failure.** If any step fails, the init container fails, the pod stays not-ready, and cellctl reports `provisioning` until the init deadline. After that it reports `failed` with the step's error code. During an upgrade attempt, D6 owns the deadline instead.

### D4. cellctl: desired rows in, observed rows out, server-side apply in between

cellctl is one Deployment in namespace `exomem-cloud`, with `replicas: 1` and `strategy: Recreate`. It holds no database, no fence generations, no leases and no checkpoints. Its loop runs every 5 seconds and on `LISTEN exomem_cloud_cells`. The LISTEN uses a direct Postgres session, not the transaction-mode pooler.

1. **Select** rows whose `generation <> observed_generation`, or whose observed state is not terminal. Terminal means `running` and ready, `read_only` and ready, `stopped`, `deleted` or `failed`.
2. **Act** according to `desired_state`, then observe:
   - `running` or `read_only`: render the D5 manifests for the row with the cell's **current image** (D6), and apply them with server-side apply under field manager `cellctl`. `read_only` sets `EXOMEM_CLOUD_READ_ONLY=1`. No ordinary pass changes a cell's image; only a D6 attempt does.
   - `stopped`: set `replicas: 0`. A row that has never run creates nothing.
   - `deleted`: run D10.
3. **Write back** `observed_state`, `observed_image` (only from a Ready pod), `ready`, `last_error_code`, `node`, `volume_id`, `hold_kind`, `hold_started_at` and `observed_at`. `observed_generation` is set only once the observation matches the applied generation.

**Holds.** A maintenance operation owns a cell's image and replica count through one StatefulSet annotation:

- `exomem.io/hold` is `upgrade`, `backup` or `restore`;
- `exomem.io/hold-started-at` records when it began.

While a hold is present, D4 leaves image and replicas as the holder set them and applies everything else. Starting a hold writes `hold_kind` and `hold_started_at` to the row, and ending one clears them. After a cellctl restart, every hold resumes from its annotations (D6, D8), so a crash mid-maintenance is visible on the row and is finished rather than abandoned.

**Readiness.** cellctl reads readiness from the pod's conditions through the Kubernetes API, which reflect the `/health/ready` probe. It never opens a network connection to a cell.

**Waiting is normal.** A pending PVC, a pulling image, a running init container or a pod still terminating is recorded as `provisioning`, `deleting` or `stopping`, then retried. Only two things fail a cell, setting `last_error_code` and stopping further mutation until the row's generation changes:

- an identity conflict: a namespace whose `exomem.io/cloud-cell` label differs from the row, or a PVC bound to a volume not labelled for the cell;
- the init deadline in D3, outside a hold.

Server-side apply is idempotent, so a duplicate pass after a crash is harmless.

**RBAC and admission.**
- **Rights.** cellctl's ServiceAccount holds:
  - namespaces: create, patch, delete;
  - persistentvolumes: get and list;
  - pods and events: get and list;
  - the rendered namespaced kinds;
  - Secrets: only create, patch and delete. cellctl never reads a Secret: it renders each one from the row (C1) and its master keys on every pass, so server-side apply never drops a field.
  Namespaced rights are bound cluster-wide, because the namespaces are created at runtime.
- **Admission.** One `ValidatingAdmissionPolicy`, bound to requests from cellctl's ServiceAccount, confines those rights. It denies:
  - any write outside a namespace named `exo-cell-<16 base32>`;
  - namespace create or update without the Pod Security `restricted` labels;
  - any container image not matching `<configured cell repository>@sha256:<64 hex>`.
- **Bearer key.** With that policy in place, one shared `cell_token_key` is proportionate at this fleet size.

### D5. Cell manifests

Namespace `exo-cell-<cell_id>` holds:

- **Namespace** with Pod Security `enforce`, `audit` and `warn` all set to `restricted`.
- **ResourceQuota.**
- **NetworkPolicy**, default deny:
  - the runtime pod accepts ingress on 8765 only from pods labelled `app.kubernetes.io/name=exomem-cloud-gateway` in namespace `exomem-cloud`, and has no egress at all, not even DNS;
  - backup and restore pods may egress only to TCP 443 (object storage), plus DNS.
- **Secret** holding the cell bearer (current and, during rotation, previous), the backup password, and the per-cell object-storage key.
- **PVC** of 10 GiB by default, on StorageClass `exomem-cloud-encrypted`: the existing encrypted Hetzner CSI class parameters with `reclaimPolicy: Delete` and `WaitForFirstConsumer`.
- **StatefulSet** with the image and replica count D4 and D6 decide (`replicas: 1` outside a hold or `stopped`):
  - the pod runs non-root with `automountServiceAccountToken: false`, seccomp `RuntimeDefault`, `readOnlyRootFilesystem` and all capabilities dropped;
  - it carries the D2 fsGroup settings, the `/tmp` emptyDir and the D3 `cell-init` init container;
  - the readiness probe is `GET /health/ready`, liveness is `GET /health`, and resources come from values.
- **Service** `cell`, ClusterIP, on 8765.

### D6. Releases: one setting, a stateless rollout, restore-based rollback

**The release setting.**

- `exomem_cloud_settings.cell_image`, written by Substrate, holds `<cell repository>@sha256:<digest>`.
- The single row of `exomem_cloud_rollout` holds `paused`, `error_code`, `held_cell_id` and `last_good_image`. cellctl and the owner route may write it.
- **`last_good_image`** is set to `cell_image` whenever any cell becomes Ready on `cell_image`, including at first provisioning.

**Which image a cell runs.**

- **Current image:** the image on the cell's StatefulSet, or `observed_image` when the StatefulSet is gone. D4 always renders it.
- **Initial image**, for a cell with no StatefulSet yet:
  - `desired_image` if the row sets one;
  - otherwise `cell_image` while the rollout is not paused;
  - otherwise `last_good_image`.
  - While paused with no `last_good_image`, a new cell waits in `provisioning` with `last_error_code = NO_GOOD_IMAGE`.
- **Target image**, for an upgrade: `desired_image` if set, otherwise `cell_image`.

**When an attempt runs.** Only when all of these hold:

- the target differs from the current image;
- the cell is Ready and not held;
- the rollout is not paused;
- no other cell carries an `upgrade` hold.

The cell with the lowest `rollout_priority` goes first; the owner's cell is the canary. The attempt's state lives in annotations: `exomem.io/hold=upgrade`, `exomem.io/hold-started-at`, `exomem.io/previous-image` and `exomem.io/pre-upgrade-snapshot`.

**The attempt:**

1. Set the hold, scale the cell to 0, and wait until no pod uses the volume.
2. Run the backup Job (D8) and record its snapshot id. If the backup does not finish within the backup deadline (15 minutes by default):
   - restart the cell on the current image;
   - set `last_error_code = BACKUP_FAILED` and remove the hold;
   - retry the attempt after a backoff.
   Object storage is an external, eventually consistent dependency, so its failure must not keep a tenant stopped.
3. Apply the target image with 1 replica. `cell-init` runs any offline migration.
4. Wait for Ready, up to 10 minutes. D6 owns this deadline, not the D3 init deadline.
   - **On success:** write `observed_image`, set `last_good_image`, and remove the hold.
   - **On timeout:**
     1. Scale to 0.
     2. Switch the hold to `restore`, and restore the pre-upgrade snapshot with `restic restore --delete`. The restore Job runs as UID 10001, and `--delete` removes files the new image created.
     3. Apply the previous image with 1 replica, and remove the hold once Ready.
     4. Set the rollout to `paused`, recording `error_code` and `held_cell_id`.
     5. A failed restore keeps the `restore` hold with `last_error_code = RESTORE_FAILED` and retries with backoff. It never starts either image on an unrestored volume.

**Resuming after a cellctl restart.**

- An `upgrade` hold without a snapshot restarts from step 1.
- An `upgrade` hold with a snapshot and the target applied continues waiting against the original `hold-started-at`.
- A `restore` hold re-runs the restore, which is idempotent with `--delete`.

**Why rollback restores a snapshot.** A never-Ready pod is not in the Service endpoints and served nothing, so the restore loses no writes. Rolling back by digest alone is never done, because a migrated state refuses an older image.

**While paused,** no cell changes image, and new cells start on `last_good_image` or wait with `NO_GOOD_IMAGE`, so nothing flaps. The owner resumes by setting a new `cell_image` and clearing `paused`.

Nothing else pins a release: no candidates, locks, fixtures or adoption PRs. A CI job on release tags publishes the image by digest and records it in the release notes.

### D7. Secrets

- **Cell bearer** (contract C4): `base64url_nopad(HMAC-SHA256(cell_token_key[v], "exomem-cloud-cell-token-v1:" + cell_id))`. The gateway and cellctl both hold `cell_token_key` versions. No bearer is stored in the database.
- **Bearer rotation:**
  1. cellctl applies Secrets carrying both versions.
  2. Cells restart through the same one-at-a-time mechanism.
  3. The gateway switches to the new version.
  4. The previous version is removed.
- **Backup data key.** Each cell has a random 32-byte key, envelope-encrypted with `backup_master_key` (AES-GCM, versioned).
  - cellctl writes `backup_key_wrapped` once, with `WHERE backup_key_wrapped IS NULL`, and only then applies the Secret. It re-reads on conflict, and renders the Secret from the unwrapped row on every pass.
  - The plaintext lives only in the cell's Secret. The runtime container does not mount it.
  - K3s encrypts Secrets at rest, but etcd snapshots can retain the encrypted Secret until snapshot retention expires. D10 states this residual.
- **Per-cell object-storage key.** cellctl creates a B2 application key restricted to the name prefix `cells/<cell_id>/`, using a key-management credential that only cellctl holds.
  - B2 returns the secret only at creation. cellctl therefore writes `b2_key_id`, `b2_key_wrapped` (the secret, envelope-encrypted with `backup_master_key`) and `b2_key_version` once, with `WHERE b2_key_id IS NULL`, before it renders the Secret.
  - If that write loses a race, cellctl deletes the key it just created and uses the stored one.
  - A backup Job can never touch another tenant's prefix.
- **Master credentials** are `cell_token_key`, `backup_master_key`, the B2 key-management credential, a read-only Hetzner token (volume listing) and the database DSNs. All use the existing SOPS workflow under `infra/secrets`.

### D8. Backups are taken while the cell is stopped

cellctl drives backups; there is no CronJob.

- **Nightly** within a configured window, when `now - last_backup_at > 24h`:
  1. set `exomem.io/hold=backup` and scale to 0;
  2. run the backup Job;
  3. scale back to the desired replicas and remove the hold;
  4. write `last_backup_at` and `last_backup_snapshot`.
- **Bounded.** A backup that misses the backup deadline restarts the cell, sets `last_error_code = BACKUP_FAILED`, removes the hold, and retries after a backoff within the window. A cellctl restart resumes a `backup` hold from step 2.
- **Before every image change,** as part of D6.
- **What the Job does:**
  - uses the cell image, which carries `restic` 0.17 or later, so the image-pin admission policy admits it;
  - runs as UID 10001 with the D2 pod-level `fsGroup`;
  - mounts the volume read-only, with a restic cache on an `emptyDir`;
  - backs up `/data/vault` and `/data/host` to `cells/<cell_id>/` using the per-cell key;
  - applies retention of 7 daily and 4 weekly snapshots.
- **Why stopped.** Stopping makes the copy crash-consistent across WAL-mode SQLite and the receipts journal, which a live file-by-file copy is not (`governance/store.py:716-746`).
- **Placement.** Once there is more than one node, backup and restore Jobs carry node affinity to the volume's node.

**Restore** runs a restore Job as UID 10001, with `restic restore --delete`, into a new or quiesced volume. The operator export runbook uses the same Job to restore into a scratch namespace and hand the tenant an archive of their vault. It is exercised in the local rehearsal: a scratch-namespace restore must answer recall and accept a governed write.

### D9. Capacity is observed, not reserved

For each node, cellctl publishes `exomem_cloud_capacity`:

- `cell_slots = attachments_limit − headroom − non_cell_attachments`;
- `attachments_used`;
- `observed_at`.

Substrate admits a new cell only while non-deleted cell rows are below the sum of `cell_slots`. Rows are counted rather than attachments, because stopped cells still own their volumes. There is no reservation ledger to leak.

Adding a node needs a K3s agent role and join configuration, which the current single `cluster-init` server lacks. It is a bounded IaC task, not a protocol change, and it is out of scope here.

### D10. Deletion removes everything and destroys the key

A row with `desired_state = deleted` makes cellctl:

1. **Namespace.** Delete the namespace (the PVC goes with it, and the volume goes through `reclaimPolicy: Delete`).
2. **Absence.** Confirm that the namespace, the PV and the Hetzner volume `volume_id` are all absent.
3. **Backups.** Delete every object version under `cells/<cell_id>/` and confirm that no version remains. B2 keeps hidden versions, so "empty" means no versions at all.
4. **Keys.** Delete the per-cell B2 key by `b2_key_id` and confirm it is absent, then null `b2_key_id`, `b2_key_wrapped` and `backup_key_wrapped`.
5. **Report.** Write `observed_state = deleted`.

Each step retries until its check holds. A failed observation never counts as absence.

**Residual:** encrypted etcd snapshots may retain the cell's Secret until their retention expires. The requirement states that bound.

### D11. Direct TLS ingress

- **Exposure.** The platform Traefik exposes only its `websecure` entrypoint on the node's port 443 through `hostPort`. servicelb stays disabled, and `web` is never exposed.
- **Certificates.** cert-manager obtains the MCP hostname's certificate with an ACME DNS-01 solver through a Cloudflare DNS-edit token, so certificates live in Secrets and need no volume. The Cloudflare record is DNS-only.
- **Firewall.** The Hetzner firewall opens 443 and admin SSH.
- **Public routes.** Only the gateway's IngressRoute is public. Cells, cellctl and the Kubernetes API are not.
- **Client address.** Traefik `websecure` sets no `trustedIPs`, so it overwrites `X-Real-Ip` with the peer address that `hostPort` preserves. A NetworkPolicy admits ingress to the gateway only from the Traefik pods, which makes that header the gateway's trustworthy client address.
- **Legacy.** `cloudflared` stays only for the old platform's hostnames until retirement.

### D12. Control database on its own server

- **Infrastructure.** Terraform adds `hcloud_server.control` (a small x86 instance) attached to the existing `hcloud_network.alpha`, with firewall rules. Ansible adds a `postgres` role with:
  - PostgreSQL 17;
  - PgBouncer in transaction mode, plus a session-mode database alias admitting only `substrate_owner`, because the migration runner holds a session advisory lock (`scripts/migrate.ts`);
  - a public TLS certificate for verify-full, issued and renewed on the server by an ACME DNS-01 client through the Cloudflare DNS-edit token, with a timer that reloads PgBouncer and Postgres. cert-manager runs in the cluster and cannot serve this server.
- **Backups.** pgBackRest to B2 with WAL archiving (point-in-time recovery), a nightly full and a weekly restore verification.
- **Roles:**
  - `substrate_owner`: owns the schema and runs migrations only.
  - `substrate_app`: runtime DML, not the schema owner.
  - `exomem_gateway` and `exomem_cellctl`: exactly the privileges in the C1 privilege table.
  - `pgbouncer_auth`: the role PgBouncer's `auth_query` logs in as. It is LOGIN with no password, admitted only on the unix socket through a `peer` map from the `postgres` OS user. It holds `USAGE` on schema `pgbouncer` and `EXECUTE` on `pgbouncer.get_auth`, and no table privileges.
  - The PgBouncer admin console admits only the `postgres` OS user, through `peer` on the unix socket, and nothing over TCP.
- **Network access.**
  - The public PgBouncer listener serves only `substrate_app` and `substrate_owner`, with SCRAM authentication. PgBouncer caps server connections per role (`max_user_connections` 50), total clients (`max_client_conn` 2000) and login time (`client_login_timeout` 10 s). nftables caps concurrent connections per source address.
  - `exomem_gateway` and `exomem_cellctl` connect directly to Postgres on port 5432 over the private network, never through PgBouncer. That includes cellctl's LISTEN connection.
  - nftables limits the public port's new-connection rate, with an explicit burst of 100 packets.
  - There is no fail2ban jail on the database ports. Vercel's egress addresses are shared and change, so a ban would fire on Substrate itself and take the website, OAuth and billing down for every user. SCRAM and the connection limits already bound what a guesser can do.
- **Failure domains.** A separate server keeps the fleet and its records apart.

## Shared contracts with Substrate

These must match the companion Substrate change byte for byte. The Substrate migration `0056_exomem_cloud_cells.sql` is the schema of record. cellctl's tests apply `infra/cellctl/tests/fixtures/exomem_cloud_schema.sql`, a copy of it, and the local rehearsal runs the real migration.

### C1 `exomem_cloud_cells`

**Identity and placement**
- `cell_id text primary key`: 16 characters of lowercase base32.
- `tenant_id` references the tenant `ON DELETE RESTRICT`, with a partial unique index where `desired_state <> 'deleted'`. Deleting a tenant can never silently drop a cell row that cellctl has not yet torn down.
- `storage_gib int not null default 10`.
- `rollout_priority int not null default 1`.

**Desired state, written by Substrate**
- `desired_state text not null check (desired_state in ('running','read_only','stopped','deleted'))`.
- `desired_image text null`.
- `generation bigint not null default 1`. A trigger increments it on any change to a desired column and calls `pg_notify('exomem_cloud_cells', cell_id)`. An insert also notifies, so a new cell is picked up at once. The trigger never fires on observed-column or bookkeeping updates.

**Observed state, written by cellctl**
- `observed_generation bigint`.
- `observed_state text check (observed_state in ('pending','provisioning','running','read_only','stopping','stopped','deleting','deleted','failed'))`.
- `observed_image text`, `ready boolean not null default false`, `last_error_code text`, `observed_at timestamptz`.
- `node text` and `volume_id text`, both written at first placement.
- `last_backup_at timestamptz` and `last_backup_snapshot text`.
- `backup_key_wrapped bytea` and `backup_key_version int`, written once.
- `b2_key_id text`, `b2_key_wrapped bytea` and `b2_key_version int`, written once.
- `hold_kind text check (hold_kind in ('upgrade','backup','restore'))` and `hold_started_at timestamptz`.

**Control-plane bookkeeping, written by Substrate** (Substrate migration `0057`)
- `cancellation_notice_sent_at timestamptz`: set once when the cancellation notice is sent. It is not a desired column, so it neither bumps `generation` nor notifies cellctl.

**Timestamps:** `created_at` and `updated_at`.

### C1 privileges

| Role | `SELECT` | `INSERT` / `UPDATE` |
|---|---|---|
| `substrate_app` | every C1–C1d column | insert C1 identity and desired columns; update C1 desired columns, `cancellation_notice_sent_at`, C1b, and C1d `paused`, `error_code`, `held_cell_id` |
| `exomem_cellctl` | every C1 column, C1b, C1c, C1d | update C1 observed columns only; insert and update C1c; update C1d `paused`, `error_code`, `held_cell_id`, `last_good_image`, `updated_at` |
| `exomem_gateway` | C1 `cell_id`, `tenant_id`, `desired_state` | none on C1–C1d; insert and update the rate-limit buckets |

The observed columns are the ones under "Observed state" above. Substrate's `scripts/exomem-cloud-grants.sql` is the single place that applies this table.

### C1b `exomem_cloud_settings`

`key text primary key`, `value jsonb`. It holds `cell_image`, written by Substrate.

### C1c `exomem_cloud_capacity`

`node text primary key`, `cell_slots int`, `attachments_used int`, `observed_at timestamptz`. Written by cellctl.

### C1d `exomem_cloud_rollout`

A single row: `id int primary key check (id = 1)`, `paused boolean not null default false`, `error_code text`, `held_cell_id text`, `last_good_image text`, `updated_at timestamptz`. Writable by cellctl and the owner route.

### C2 Cell environment

- `EXOMEM_CLOUD_CELL=1`, `EXOMEM_CLOUD_CELL_ID`.
- `EXOMEM_CLOUD_CELL_TOKEN`, and during rotation `EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS`.
- `EXOMEM_CLOUD_READ_ONLY` (`1`, or absent).
- `EXOMEM_VAULT_PATH=/data/vault`.
- `TMPDIR=/tmp`, `EXOMEM_LOG_DIR=/tmp/exomem-logs`, `OMP_NUM_THREADS`.
- The hosted-stage offline model environment.
- Command: `exomem --transport http --host 0.0.0.0 --port 8765`.
- Readiness `GET /health/ready`, liveness `GET /health`, MCP at `/mcp`.

### C3 Gateway to cell

**Routing and transport**
- The gateway resolves the cell only from the authenticated OAuth principal. It proxies while `desired_state` is `running` or `read_only`.
- It sends `Authorization: Bearer <C4>` to `http://cell.exo-cell-<cell_id>.svc.cluster.local:8765/mcp`.
- It streams request and response bodies without buffering.

**Headers**
- Forwarded: only `content-type`, `accept`, `mcp-session-id` and `mcp-protocol-version`. The gateway adds `x-request-id`.
- Never forwarded: the client's `Authorization`, cookies or forwarding headers.

**Error mapping**
- `GET` returns 405, as hosted does.
- An upstream connect failure or a non-ready cell returns 503 `CELL_NOT_READY`.
- A 401 from the cell returns 502 `CELL_AUTH_MISMATCH`, and is never passed to the client.

### C4 Cell bearer derivation

`base64url_nopad(HMAC-SHA256(key, "exomem-cloud-cell-token-v1:" + cell_id))`: ASCII input, 43 characters, no padding.

## Controls retired, and why

Each row states what the control prevented, what it cost when it fired wrongly, and who paid.

| Control | Prevented | Wrong-firing cost, and who paid |
|---|---|---|
| Provisioner-owned custody Secret, attestation window, serving membership, activation tuple and ack, provisioner-driven governance migration Jobs | A tenant process minting its own authority; stale replicas serving | Stranded cells and read-only acknowledgement failures, which blocked every user, paid by the owner for a month. The tenant process already is the vault's trust domain. Cross-tenant protection comes from isolation, which is kept |
| Contract candidates, digests, cohorts, rollout assignments, client artifacts, promotion, reviewer bootstrap | A served tool surface differing from a certified one | An empty fleet admitted no one, and every release cost a 27k-line adoption. The cell now publishes its own surface, and certification belongs to a future listing |
| Fenced checkpoint phases, leases, fence generations, provisioner database | Double effects from a non-idempotent workflow | Retries turned into terminal states. Server-side apply is idempotent |
| Deployment locks and bespoke CEL pod-shape pins | Release or pod-shape drift | Every change needed a lock ceremony. The remaining guardrails are Pod Security `restricted`, network policy and one policy on cellctl's own requests |
| A human copying the governance-migration digest | An unreviewed irreversible cutover of an existing vault | Nothing to review on a cell: the same pinned image plans and commits its own state |

## Controls kept

These stay fail-closed, because an unexpected value there means something is wrong:

- per-tenant namespace, volume, Secret, backup key and object-storage key;
- Pod Security `restricted`, no ServiceAccount token, read-only root filesystem, owner-only custody modes;
- default-deny network policy, with no runtime egress;
- tenant identity derived only from the OAuth principal;
- constant-time bearer comparison;
- content-free logs and telemetry;
- encrypted, crash-consistent backups;
- verified, version-aware deletion with key destruction;
- parsers, authentication, billing and schema validation.

The gateway gates on desired state, not on the observed `ready` column. An eventually consistent observation must not refuse a request that the cell itself can answer.

## Disposition of existing changes

**Superseded; not to be implemented further. Removed with their code in phase R:**
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

**Retained:** `hosted-human-capture-and-onboarding` (re-scoped onto Cloud after acceptance), and every non-hosted change.

## Risks / Trade-offs

- **Namespace deletion destroys data** under `reclaimPolicy: Delete`. The mitigation is nightly and pre-upgrade backups, and namespace deletion is reachable only through cellctl, under its admission policy, or through cluster admin.
- **Nightly and upgrade backups stop the cell** for about a minute. That is accepted for the alpha. An in-process consistent snapshot can replace the stop later.
- **One fleet node.** Losing it takes every cell down until it is rebuilt from IaC and restored from backups. The control database survives on its own server.
- **Public PgBouncer.** It is limited to the Substrate roles, with verify-full TLS, SCRAM, and PgBouncer and nftables connection limits.
- **fsGroup behaviour on the real CSI driver** is proven only by the D2 test. The implementer escalates rather than weakening custody checks.
- **Letting go of provisioner-held revocation.** Suspension is `stopped` (`replicas: 0`) plus gateway refusal, and it takes effect within one cellctl pass.

## Migration Plan

1. **S.** Specs, then critic review and recheck, then superseded-change banners and PR closures.
2. **Build lanes:**
   - **A:** cloud mode and image.
   - **B:** cellctl, manifests, platform chart, cert-manager and ingress.
   - **D:** control server and Postgres IaC.
   - In Substrate, **D2** (driver) lands before **C** (admission, gateway, schema).
3. **P3, local rehearsal as one command** on disposable K3s with the real image, Postgres, Substrate and gateway. It runs:
   1. invite;
   2. provision;
   3. OAuth MCP;
   4. capture;
   5. cited recall;
   6. governed write after pod kill;
   7. upgrade;
   8. forced canary failure, with restore-based return;
   9. read-only mode;
   10. second-tenant denial;
   11. backup and scratch restore, with a governed write;
   12. deletion with absence proofs.

   It gates the node.
4. **P4, node:**
   - Database cutover (see the Substrate design).
   - Deploy cellctl and the gateway beside the old platform, and scale the old provisioner to zero.
   - Owner acceptance.
5. **P5, friends**, after the owner confirms.
6. **R, retire.** Delete the hosted-only runtime modules, provisioner v1, the old chart pieces, locks and plugin candidates. Remove the superseded change directories and the `hosted-*` canonical specs with the code they describe. Delete Neon after seven clean days.

**Rollback before P4** is a no-op. **After P4**, restore the database from the retained Neon export.

## Open Questions

- The exact MCP hostname and brand domain for Exomem Cloud. This is configuration, set at P4.
