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
   - **Caller identifiers.** Under content-private logging, the client name from MCP `clientInfo` or `User-Agent` is logged only as its client family (`claude-ai`, `chatgpt`, `codex`, `claude-code` or `other`), a client version only when it is a plain dotted number, and the `mcp-session-id` header only as a short digest. A `cf-ray` header that is not Cloudflare's own ray shape is dropped. These are caller-chosen text, so they never reach a log verbatim.
   - **Log directory.** The image defaults `EXOMEM_LOG_DIR` to `/tmp/exomem-logs`, so no runtime log ever lands on the tenant volume that D8 backs up, even without the manifest's setting. In cloud mode an unset or empty `EXOMEM_LOG_DIR` falls back to `<tmp>/exomem-logs`, never to a home- or checkout-derived path on the volume.
3. **Tool surface.**
   - The members of `CLOUD_SURFACE_EXCLUSIONS` are removed from the MCP server after registration.
   - Each entry uses the `HostedSurfaceExclusion` shape: `command`; `reason`, stating what is technically broken; and `lifted_when`.
   - The initial members are `transfer_artifact`, `adopt_vault`, `process_media` and `read_media`.
   - Legacy MCP aliases (`EXOMEM_MCP_LEGACY_COMPAT`) are never registered in cloud mode. A cell has no legacy clients, and aliases would re-expose the leaves of excluded commands.
4. **Read-only mode.** With `EXOMEM_CLOUD_READ_ONLY=1` the cell refuses every mutating command with `CLOUD_CELL_READ_ONLY`, before vault access. Mutation is classified from the command registry, not from a copied list. Reads keep working.
5. **Routes.** Only MCP (`/mcp`), `/health` and `/health/ready` are registered. REST (`/api/*`), `/upload` and `/download` are not, because FastMCP custom routes do not inherit MCP authentication.
6. **Configuration.** No `.env` file is loaded; configuration comes only from the pod environment. That covers every loader, including the runtime-resource dotenv policy and the CLI's own (`auth sessions`, `doctor`), not only the server's.

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

A writable `emptyDir` is mounted at `/tmp`. The pod sets `TMPDIR=/tmp`, `EXOMEM_LOG_DIR=/tmp/exomem-logs` and `OMP_NUM_THREADS`, carried over from the hosted StatefulSet environment. The root filesystem stays read-only. The fsGroup makes that emptyDir setgid, so the private vault lock directory created under it inherits `S_ISGID`: when that directory is a real directory owned by the running user with mode exactly `0700` plus `S_ISGID`, the inherited bit is cleared back to `0700` and re-checked exactly, never tolerated, and any other owner, symlink or mode bit is still refused.

### D3. First boot and every upgrade run offline preparation in an init container

The StatefulSet has one init container, `cell-init`, using the same image as the runtime. Its command is `exomem cell-init --vault /data/vault --json`. It runs non-root with the volume mounted and the server not yet started. The volume is ReadWriteOnce and the replica count is 1, so this is genuinely offline. The container is idempotent:

1. It creates `/data/vault` and `/data/host` if absent, and sets both to mode `0700`. `/data/host` is the running account's passwd home, the same entry custody resolves, never `$HOME`. The mode is set through a descriptor opened without following a final symlink, so a symlink planted at either path fails the step and its target is never changed.
2. If `/data/vault` is not a vault, it initializes one **atomically**. It builds the vault in a staging directory on the same volume (`/data/.vault-init-*`), then renames the staging directory onto `/data/vault`, which must be absent or empty. Stale staging directories from an earlier crash are removed first. A non-empty `/data/vault` that is not a vault fails with `CELL_INIT_VAULT_UNRECOGNIZED` and is never overlaid: an interrupted init cannot produce it, so it means something else wrote there.
3. It runs `exomem maintain --vault /data/vault --migrate-state --offline --json`.

That is the desktop's own first-run path, so the cell keeps the one rule. There is no governance schema migration and no custody environment. A cell runs standalone governance defaults, like a fresh desktop install. Schema v4 arrives only with a later change that needs multi-audience authorization inside a cell.

**Re-entry.** `init` is skipped once the volume holds a vault, and state migration is idempotent. A run interrupted at any point is completed by the next run. Lane A tests a fresh volume, a second run that changes nothing, a run interrupted after `init`, and runs interrupted **during** `init` at several points of the scaffold copy. Each interrupted run must be followed by a successful run that leaves a complete vault.

**Output.** `cell-init` installs the redaction hook, as the server does. A failure prints one JSON line with the step and a stable error code, and exits non-zero; it never prints a traceback, which would carry absolute paths.

**Setgid volume root.** fsGroup leaves the volume root setgid (`2770`). `init` and every later directory creation must succeed under it, and owner-only custody modes must still hold. The container test mimics it by `chmod 2770` on the volume root.

**Failure.** If any step fails, the init container fails, the pod stays not-ready, and cellctl reports `provisioning` until the init deadline. After that it reports `failed` with the step's error code. During an upgrade attempt, D6 owns the deadline instead.

### D4. cellctl: desired rows in, observed rows out, server-side apply in between

cellctl is one Deployment in namespace `exomem-cloud`, with `replicas: 1` and `strategy: Recreate`. It holds no database, no fence generations, no leases and no checkpoints. Its loop runs every 5 seconds and on `LISTEN exomem_cloud_cells`. The LISTEN uses a direct Postgres session, not the transaction-mode pooler.

**The loop survives its dependencies.**

- **Single writer.** cellctl takes `pg_try_advisory_lock` on its direct session before its first pass, and acts only while it holds the lock. `Recreate` alone does not stop two pods overlapping during an eviction.
- **Reconnects.** A lost database session is reopened with capped exponential backoff (1 s up to 30 s). The new session re-takes the lock and re-issues `LISTEN`. A reconnect is followed by a full pass, because notifications sent while the session was down are lost. The session sets TCP keepalives (idle 30 s, interval 10 s, 3 probes) and a per-statement timeout, so that after a partition the server reaps the old session and releases its advisory lock within about a minute, not after the server's default keepalive of two hours. A pending notification is cleared before a pass starts, never after it, so a notification that arrives during a pass triggers the next one.
- **Liveness.** Every loop iteration touches a heartbeat file, including iterations spent waiting to reconnect. The liveness probe fails only when the heartbeat is older than 120 s. A database or object-storage outage is an eventually consistent condition, so it must not restart cellctl; a wedged loop must.
- **One row cannot stop the pass.** Each row runs in its own exception boundary. A row that raises is logged with its `cell_id` and exception class, never with secret values, and the pass continues. Capacity (D9) is published at the end of every pass, whatever the rows did.
- **Row validation.** A row whose whole `cell_id` does not match `[a-z2-7]{16}` (a full match; a trailing newline is not accepted) is skipped and logged. This is checked when the row is read, before the id reaches a name, a URL, an object-storage prefix or a shell string. Migration 0056 also enforces the format with a CHECK; cellctl does not rely on it.

1. **Select** rows that meet any of these:
   - `generation <> observed_generation`;
   - an observed state that is not terminal. Terminal means `running` and ready, `read_only` and ready, `stopped`, `deleted`, or `failed` with an identity conflict. A cell `failed` on the init deadline stays observed (below);
   - a **render digest** that differs from the StatefulSet's `exomem.io/render-digest` annotation. The render digest is a SHA-256 over the non-secret render inputs: a renderer version constant that changes whenever cellctl's manifests change, chart-level cell settings, the set of `cell_token_key` versions in play, the row's `storage_gib` (which renders into the PVC and the quota but bumps no generation) and the key versions of the row. It never covers a secret value. It is how a change that is not a row change, such as a bearer rotation (D7) or a new cellctl release, reaches cells that have already converged. The digest is also a pod-template annotation, so a digest change always restarts the pod and the new environment reaches it.
2. **Act** according to `desired_state`, then observe:
   - `running` or `read_only`: render the D5 manifests for the row with the cell's **current image** (D6), and apply them with server-side apply under field manager `cellctl`. `read_only` sets `EXOMEM_CLOUD_READ_ONLY=1`. No ordinary pass changes a cell's image; only a D6 attempt does.
   - A row that is dirty **only** because its render digest changed is re-applied **one cell at a time**. The re-apply records `exomem.io/render-digest-applied-at`. The next cell waits only while a cell **in flight** exists: one that carries the current digest, is not Ready on its update revision, and was re-applied less than 10 minutes ago. A cell that is not Ready for any other reason never blocks the fleet, and a cell whose last error is `MANIFEST_IMMUTABLE` is not a digest candidate. So a fleet-wide change never restarts every cell in one pass, and one broken tenant never stops a bearer rotation.
   - `stopped`: set `replicas: 0`. A row that has never run creates nothing.
   - `deleted`: run D10.
3. **Write back** `observed_state`, `observed_image`, `ready`, `last_error_code`, `node`, `volume_id`, `hold_kind`, `hold_started_at` and `observed_at`.
   - `observed_at` is written on every observation.
   - `ready` and `observed_image` come from a pod whose `controller-revision-hash` equals the StatefulSet's `status.updateRevision`, and whose condition is Ready. `observed_image` is that pod's container image. A pod from the previous revision never counts.
   - `observed_generation` is set only by a routine pass whose observation matches the applied generation. A pass that changes the live StatefulSet never also records convergence; the next pass observes it. The StatefulSet carries `exomem.io/row-generation`. A pass changes it when that annotation or the render digest differs from the row, and the StatefulSet's `status.observedGeneration` must equal its `metadata.generation` before `status.updateRevision` is trusted.
   - **`ready` is an observation, never a memory.** Every pass re-reads the Ready condition of the pod on the StatefulSet's update revision for every row it reconciles, dirty or not. A served, converged row whose only change is its pod's readiness is observed, not re-applied: `ready` follows the pod both ways on the next pass, `observed_state` stays `running` or `read_only` (so the cell is still backed up), and nothing but `ready` and `observed_at` is written. A parked refused row is observed through the decision every pass, which applies nothing while it is parked. Every row write drops columns whose value is unchanged, and a row with nothing changed writes `observed_at` alone. D6's rollout gate and canary check require the readiness observed this pass as well as the row's.

**Holds.** A maintenance operation owns a cell's image and replica count through one StatefulSet annotation:

- `exomem.io/hold` is `upgrade`, `backup` or `restore`;
- `exomem.io/hold-started-at` records when it began.

While a hold is present, D4 leaves image and replicas as the holder set them and applies everything else. Starting a hold writes `hold_kind` and `hold_started_at` to the row, and ending one clears them. After a cellctl restart, every hold resumes from its annotations (D6, D8), so a crash mid-maintenance is visible on the row and is finished rather than abandoned.

- **Annotations are the truth.** The row's hold columns only mirror the annotations, and every pass rewrites them from what it observes, in both directions. A row that shows a hold when the StatefulSet carries none has its hold columns cleared. A StatefulSet hold that the row does not show is written to the row. A mismatch never starts or resumes a hold. A pass during a hold also writes `ready` from what it observes.
- **Holds honour the desired state.** Every manifest a hold renders carries the row's current `desired_state`: `read_only` always sets `EXOMEM_CLOUD_READ_ONLY=1`, including on the upgrade target and on the restored previous image. When a hold ends, it scales to the desired replicas: 0 for `stopped`, 1 otherwise. The one exception is the upgrade target, which runs with 1 replica until it is Ready or the D6 deadline passes, whatever the desired state, so an attempt is never declared successful for an image that never ran. A pod that a hold starts for a row that is now `stopped` serves nothing, because the gateway refuses a cell that is not running (D1).
- **Hold exits observe; they do not declare.** Ending a hold removes the annotations and applies the desired replicas. It never writes `observed_generation`, `observed_state` or `observed_image` from what it intended. The next routine pass observes them.
- **Deletion supersedes every hold.** A `deleted` row goes straight to D10, whatever hold it carries. D10 clears the row's hold columns, and a deleted row never counts as holding an upgrade or restore for D6's one-at-a-time rule.

**Readiness.** cellctl reads readiness from the pod's conditions through the Kubernetes API, which reflect the `/health/ready` probe. It never opens a network connection to a cell.

**API budget.** A row observed as `deleted` is not observed again, and a row being deleted asks Hetzner at most once a minute. Hetzner's volume API is called only for the D10 absence check of rows being deleted, at most once a minute per row. It is not called on every pass. When a backup attempt ends in `BACKUP_FAILED`, its Job is deleted before the cell restarts, so the ResourceQuota does not hold the restart back.

**Waiting is normal.** A pending PVC, a pulling image, a running init container or a pod still terminating is recorded as `provisioning`, `deleting` or `stopping`, then retried. Only two things fail a cell. An identity conflict sets `last_error_code` and stops further mutation until the row's generation changes. The init deadline sets `last_error_code` and reports `failed`, but the cell stays observed:

- an **identity conflict**. Identity is fail-closed, because an unexpected owner here means one tenant's data could be served to another:
  - an existing `exo-cell-<id>` namespace whose `exomem.io/cloud-cell` label is missing or differs from the row. cellctl creates its namespaces with the label in the same apply, so an unlabelled namespace was never cellctl's;
  - a cell PVC bound to a PersistentVolume whose `spec.claimRef.uid` is not that PVC's uid, whose `storageClassName` is not `exomem-cloud-encrypted`, or, once the row records a `volume_id`, whose CSI `volumeHandle` differs from it. Nothing labels PersistentVolumes, so the check uses the binding identity, not a label;
- the **init deadline** in D3, outside a hold. It applies only while the current pod's `cell-init` has not completed, and is measured from that pod's creation, not from a running container, so a `cell-init` that crash-loops fails at the deadline as well. The one exception is a re-run: when the node restarted the pod's sandbox and `cell-init`'s previous run exited 0, the deadline runs from this sandbox's `cell-init` start, since the pod's creation time is days old. A re-run that then crash-loops falls back to the pod's creation time. A cell whose `cell-init` completed and whose server is not Ready is waiting, never failed on time alone: an OOM or liveness restart keeps the pod's creation time, and failing a healthy long-running cell on it would be failing closed on an eventually consistent condition. The recorded error code is `INIT_DEADLINE_EXCEEDED`. A cell failed this way stays observed without being re-applied; once its pod is Ready, the next pass reports it `running` or `read_only` and clears the code. `cell-init` writes no termination message, and its log tail is not copied into the pod status: the containers keep the default `terminationMessagePolicy: File`.

Server-side apply is idempotent, so a duplicate pass after a crash is harmless. **Refusals.** An apply answered with a 4xx other than 408, 409 or 429 is a refusal; those three are transient and retried at the normal cadence. Each object is applied on its own, in render order, and a refusal of one does not stop the others, so the StatefulSet (which carries replicas and hold state) always gets its attempt. Two exceptions keep a healthy cell on the template it already runs: outside an active hold, the StatefulSet is not applied in a pass whose Secret apply was refused (a rotation's new template references a key only the refused Secret carries), and a StatefulSet that does not exist yet is created only when every earlier object applied (so a new cell never starts without its NetworkPolicies). Inside a hold, including its exit, the StatefulSet is always applied. A refusal sets `last_error_code = MANIFEST_IMMUTABLE` and does not record `observed_generation`, since nothing converged. cellctl parks the row in memory, keyed on its generation, the applied render digest and the image to render, so any change to the row, settings or chart unparks it, and retries it on a doubling backoff from 2 minutes to 1 hour, so a transient cause clears by itself. A parked row is never excluded from backups or token rotation unless its own StatefulSet was refused, and a parked owner cell holds the rollout visibly with `error_code = CANARY_PARKED`. After a cellctl restart each refused row is retried once. It never retries at loop speed.

**RBAC and admission.**
- **Rights.** cellctl's ServiceAccount holds:
  - namespaces: get, list, watch, create, patch, delete. Reading namespaces is needed for the identity-conflict check, and exposes only cell names and labels that cellctl already holds;
  - persistentvolumes, csinodes and volumeattachments: get and list (D9 reads the last two);
  - pods and events: get and list;
  - validatingadmissionpolicies and validatingadmissionpolicybindings: get, for the self-check below;
  - the rendered namespaced kinds, without subresources. There is no `statefulsets/scale`; cellctl scales by applying `replicas`;
  - Secrets: only create, patch and delete. cellctl never reads a Secret: it renders each one from the row (C1) and its master keys on every pass, so server-side apply never drops a field.
  Namespaced rights are bound cluster-wide, because the namespaces are created at runtime.
- **Admission.** One `ValidatingAdmissionPolicy`, bound to requests from cellctl's ServiceAccount, confines those rights. Its `matchConstraints` cover every resource and subresource with `resources: ["*/*"]`. Kubernetes rejects `"*/*"` listed alongside anything else, and `"*"` alone would miss subresources. It denies:
  - any write outside a namespace named `exo-cell-<16 base32>`;
  - a StatefulSet or Job in a namespace whose `exomem.io/cloud-cell` label does not name its own cell, so an `exo-cell-*` namespace someone else made without it never takes a pod;
  - namespace create or update without the `exomem.io/cloud-cell` label naming its own cell, or without the Pod Security `restricted` labels for `enforce`, `audit` and `warn`, or with an `enforce-version` other than the platform's pinned Pod Security version (the one every platform namespace and cellctl's renderer use), so a cell namespace can never be relabelled to an older, weaker version;
  - any container image not matching `<configured cell repository>@sha256:<64 hex>`;
  - any pod template, in a StatefulSet or a Job, that does not set `automountServiceAccountToken: false`, that has a volume other than `persistentVolumeClaim`, `emptyDir`, `secret`, `configMap`, `downwardAPI` or `projected` (an allowlist, so `csi`, `ephemeral` and `image` volumes are denied), that has a projected volume source other than `configMap`, `secret` or `downwardAPI` (an allowlist, so `serviceAccountToken`, `podCertificate` and `clusterTrustBundle` are all denied), or that sets `nodeName`, `nodeSelector`, `affinity`, `tolerations`, `runtimeClassName`, `priorityClassName` or `hostAliases` (cellctl renders no scheduling constraints); and a StatefulSet with its own `volumeClaimTemplates`;
  - any Service create or update other than `type: ClusterIP` with no `externalIPs`;
  - any delete other than of a cell namespace (D10) or a Job (D8), so a NetworkPolicy goes only with its namespace;
  - any Secret other than the `Opaque` Secret `cell-credentials`, so a `kubernetes.io/service-account-token` Secret the token controller would fill in is denied;
  - any PVC other than `cell-data` on `exomem-cloud-encrypted`, or one with a `dataSource`, `dataSourceRef` or a `volumeName` it was not already bound to;
  - any ResourceQuota other than `cell-quota` with one PVC, two pods, no scopes and no limit kinds beyond the rendered ones;
  - any NetworkPolicy other than the three D5 renders, each pinned to its rendered shape: `default-deny` denies everything for every pod, `runtime-ingress` admits only the gateway's pods on TCP 8765 to this cell's pods and has no egress, and `job-egress` selects only Job pods and reaches only TCP 443 outside every `cells.jobEgressExcept` range, plus kube-dns.
- **Isolation.** A second policy, `exomem-cellctl-isolation`, admits cellctl's StatefulSet and Job writes only in a namespace whose `default-deny` NetworkPolicy exists and denies all ingress and egress. It takes that NetworkPolicy as its param: the binding's `paramRef` names `default-deny` with no namespace, so the lookup is in the request's own namespace, and `parameterNotFoundAction: Deny` refuses a fresh cell namespace that has none. The policy's `matchConstraints` select only namespaces labelled `exomem.io/cloud-cell`, because the API server resolves a binding's param before it evaluates `matchConditions`, so an unscoped policy would deny every other controller's Jobs in namespaces with no `default-deny`. The scope policy therefore requires every namespace cellctl creates or updates to carry `exomem.io/cloud-cell` equal to its own cell id, so a cell namespace can never fall outside the selector. Without the isolation policy, a pod in a namespace cellctl had just created would have open network access. cellctl already applies the NetworkPolicies before the StatefulSet and never runs a Job before the StatefulSet exists.
- **Self-check.** Before each pass, cellctl confirms, for both policies, that the policy and binding exist, that the policy fails closed (`failurePolicy: Fail`) and carries validations, that the binding names that policy, that its `validationActions` include `Deny`, and that it carries no `matchResources`, and for the isolation binding that its `paramRef` names `default-deny` in the request's namespace with `parameterNotFoundAction: Deny`. A binding downgraded to `Audit`, or narrowed by `matchResources`, confines nothing. Without them it does nothing that pass and logs why. What this prevents: Helm installs the policy after the Deployment, and without the policy the ClusterRole is close to cluster-admin. What it costs when it fires wrongly: a pass or two of idling at install. Nobody else pays.
- **Bearer key.** With that policy in place, one shared `cell_token_key` is proportionate at this fleet size.
- **Logging.** The `kubernetes.client.rest` logger is pinned to WARNING whatever the root level, because at DEBUG it logs response bodies, and the apply response for a cell Secret contains that cell's credentials.
- **Orphans.** A namespace labelled `exomem.io/cloud-cell` with no row is logged, never deleted. cellctl lists labelled namespaces at most every 10 minutes and logs each orphan's cell id.

### D5. Cell manifests

Namespace `exo-cell-<cell_id>` holds:

- **Namespace** with Pod Security `enforce`, `audit` and `warn` all set to `restricted`.
- **ResourceQuota.**
- **NetworkPolicy**, default deny:
  - the runtime pod accepts ingress on 8765 only from pods labelled `app.kubernetes.io/name=exomem-cloud-gateway` in namespace `exomem-cloud`, and has no egress at all, not even DNS;
  - backup and restore pods may egress only to TCP 443 (object storage), plus DNS. The 443 rule is an `ipBlock` of `0.0.0.0/0` that excepts the configured cluster, private, carrier-grade NAT and link-local ranges (chart value `cells.jobEgressExcept`). The local rehearsal overrides it so that it can reach its in-cluster S3 double.
- **Secret** holding the cell bearer (current and, during rotation, previous), the backup password, and the per-cell object-storage key.
- **PVC** of 10 GiB by default, on StorageClass `exomem-cloud-encrypted`: the existing encrypted Hetzner CSI class parameters with `reclaimPolicy: Delete` and `WaitForFirstConsumer`.
- **StatefulSet** with the image and replica count D4 and D6 decide (`replicas: 1` outside a hold or `stopped`):
  - the pod runs non-root with `automountServiceAccountToken: false`, seccomp `RuntimeDefault`, `readOnlyRootFilesystem` and all capabilities dropped;
  - it carries the D2 fsGroup settings, the `/tmp` emptyDir and the D3 `cell-init` init container;
  - the readiness probe is `GET /health/ready` and liveness is `GET /health`. A startup probe on `GET /health` allows 5 minutes (period 10 s, failure threshold 30) before liveness applies, so a slow cold start is not killed and misread as a failed canary. Resources come from values.
- **Service** `cell`, ClusterIP, on 8765.

Namespace `exomem-cloud` is default-deny too: one `podSelector: {}` policy denies all ingress and egress, and the cellctl and gateway policies each allow their own traffic in both directions. cellctl may egress only to the Kubernetes API, the control database on 5432 and object storage on 443. The gateway may egress only to cell pods on 8765 and the control database on 5432. Both get DNS. The gateway accepts ingress only from Traefik. The certificate Issuer uses DNS-01, so no ACME solver pod runs in the namespace.

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

**Candidates.** A candidate is a non-deleted row whose `desired_state` is `running` or `read_only` and whose target differs from its current image. Stopped cells are not candidates. One upgrades through an attempt once it runs again and is Ready.

**When an attempt runs.** Only when all of these hold:

- the cell is a candidate, Ready and not held;
- the rollout is not paused;
- no other non-deleted cell carries an `upgrade` or a `restore` hold. A `restore` hold older than its 45-minute bound (so it has recorded `RESTORE_FAILED`) stops counting once the owner resumes the rollout. The hold itself stays and keeps retrying, and a retry that runs after the resume does not block other cells either; one broken restore never stalls the fleet's releases. The bound is read from the hold's age, since a refusal inside the hold can replace the row's error code;
- every non-deleted cell was observed this pass. A cell whose observation failed may be the canary or may hold an upgrade or restore, so a pass that could not observe one starts no upgrade or digest re-apply anywhere, and logs it at error level. Backups go on, with such a row's own `backup` hold column counting as an occupied slot;
- the cell is not inside a backup backoff (`exomem.io/backup-retry-after` is absent or in the past);
- **canary first.** The canary is the owner's cell: the non-deleted row with the lowest `rollout_priority`. No other cell starts an attempt while the canary's image differs from the target. If the canary is not eligible, because it is stopped, not Ready or in backoff, the rollout waits for it. Canarying on the owner's own cell is the point of the rule, and when it stalls only the owner is affected, and the owner can fix it. After the canary, candidates go in `rollout_priority` then `cell_id` order, and a cell that is not eligible is skipped rather than waited for. Ordering among tenants protects nothing, and waiting on one broken tenant would hold the whole fleet back.

The attempt's state lives in annotations on the StatefulSet: `exomem.io/hold=upgrade`, `exomem.io/hold-started-at`, `exomem.io/previous-image`, `exomem.io/pre-upgrade-snapshot`, `exomem.io/target-applied-at` and, during a restore, `exomem.io/restored-snapshot`.

**The pre-upgrade snapshot is the attempt's own.** `exomem.io/pre-upgrade-snapshot` is set only from the backup Job that this attempt ran in step 2. `last_backup_snapshot` is never used in its place, for skipping the backup or for choosing what to restore: a nightly snapshot can be up to a day older than the tenant's last write, and restoring it with `--delete` would discard that day.

**The attempt:**

1. Set the hold, scale the cell to 0, and wait until no pod uses the volume.
2. Run the backup Job (D8) and record its snapshot id in `exomem.io/pre-upgrade-snapshot`. It is a real backup, so it also writes `last_backup_at` and `last_backup_snapshot`. If the backup fails or does not finish within the backup deadline (15 minutes by default, measured from `hold-started-at`, so it also bounds step 1's wait for the volume):
   - restart the cell on the current image, with the desired replicas;
   - set `last_error_code = BACKUP_FAILED` and remove the hold;
   - set `exomem.io/backup-retry-after` to now plus the backoff: 15 minutes, doubling per consecutive failure up to 4 hours, and reset by a successful backup. That annotation, not the row, carries the backoff, so it survives a cellctl restart.
   Object storage is an external, eventually consistent dependency, so its failure must not keep a tenant stopped.
3. Apply the target image with 1 replica, whatever the desired state, and record `exomem.io/target-applied-at` in the same apply. `cell-init` runs any offline migration.
   - If the API server **refuses** the target (a D4 refusal: a 4xx other than 408, 409 or 429, such as the image-pin admission policy), the target never ran and the volume holds the pre-attempt state. End the attempt without a restore: restore the current image and the desired replicas, remove the hold, and pause the rollout with `error_code = TARGET_REJECTED` and `held_cell_id`.
   - A transient failure (a 5xx, a timeout, 408, 409 or 429) is retried on the next pass.
4. Wait for Ready, up to 10 minutes from `exomem.io/target-applied-at`. The deadline never runs from `hold-started-at`, because the backup can take most of 15 minutes. D6 owns this deadline, not the D3 init deadline.
   - **On success:** set `last_good_image` and remove the hold. The next routine pass records `observed_image` from the Ready pod (D4).
   - **On timeout:**
     1. In one pass: set the rollout to `paused` with `error_code = UPGRADE_READINESS_TIMEOUT` and `held_cell_id`, then scale to 0 and switch the hold to `restore`. Pausing in the same pass as the switch means no other cell can start the same image in between, and writing the pause first means a crash between the two leaves the rollout paused.
     2. Wait until no pod uses the volume. Kubernetes lets pods on the same node share a ReadWriteOnce volume, so a restore that runs next to a terminating pod would corrupt it.
     3. Restore the pre-upgrade snapshot with `restic restore --delete`. The restore Job runs as UID 10001, and `--delete` removes files the new image created. The Job validates the snapshot id against `^[0-9a-f]{64}$` before it reaches a shell. The restore is scoped to `/data/vault` and `/data/host`, and leaves the volume root, including `lost+found`, untouched.
     4. On success, record `exomem.io/restored-snapshot` so that a restore Job removed by its TTL is not run again. Then apply the previous image with the desired replicas, and remove the hold once Ready.
     5. A failed restore keeps the `restore` hold with `last_error_code = RESTORE_FAILED` and retries with backoff. It never starts either image on an unrestored volume. A restore hold that has not finished within 45 minutes of starting (the restore Job's own deadline plus the previous image's readiness) records `RESTORE_FAILED` the same way and keeps waiting.
5. **Stall bound.** An attempt whose target was never applied 30 minutes after `hold-started-at` ends the same way as a refused target, with `error_code = UPGRADE_STALLED`.

**Resuming after a cellctl restart.**

- An `upgrade` hold without `exomem.io/pre-upgrade-snapshot` restarts from step 1, taking a new backup.
- An `upgrade` hold with a snapshot but no `target-applied-at` continues from step 3.
- An `upgrade` hold with `target-applied-at` continues waiting against that time.
- A `restore` hold with `exomem.io/restored-snapshot` continues from step 4.4. One without it re-runs the restore, which is idempotent with `--delete`.
- A `restore` hold without `exomem.io/pre-upgrade-snapshot` cannot happen through this procedure. If one is found, cellctl keeps the hold, sets `RESTORE_FAILED` and pauses the rollout once, when the row first records it, so an owner who resumes is not re-paused; it never falls back to another snapshot. This is fail-closed deliberately. The volume may hold a migrated state that no known image can safely start, so a wrong guess would lose data, while a wrong refusal leaves one cell stopped until the owner looks.

**Why rollback restores a snapshot.** A never-Ready pod is not in the Service endpoints and served nothing, so the restore loses no writes. Rolling back by digest alone is never done, because a migrated state refuses an older image.

**While paused,** no cell changes image, and new cells start on `last_good_image` or wait with `NO_GOOD_IMAGE`, so nothing flaps. The owner resumes by setting a new `cell_image` and clearing `paused`.

Nothing else pins a release: no candidates, locks, fixtures or adoption PRs. A CI job on release tags publishes the image by digest and records it in the release notes.

### D7. Secrets

- **Cell bearer** (contract C4): `base64url_nopad(HMAC-SHA256(cell_token_key[v], "exomem-cloud-cell-token-v1:" + cell_id))`. The gateway and cellctl both hold `cell_token_key`. No bearer is stored in the database.
- **Key encoding.** A `cell_token_key` is 32 bytes written as 64 hex characters. The Secret `exomem-cloud-cell-token-key` holds `current` (and during a rotation `previous`) in that form, and both readers take the same entry: the gateway as `EXOMEM_CLOUD_CELL_TOKEN_KEY`, cellctl as `CELLCTL_CELL_TOKEN_KEY_CURRENT`. cellctl refuses any other form at settings load, so the two can never derive bearers from different bytes.
- **Bearer rotation:**
  1. cellctl applies Secrets carrying both versions.
  2. Cells restart one at a time. The set of key versions is part of the D4 render digest, so the change reaches converged cells, and D4 re-applies digest-only changes one cell at a time.
  3. The gateway switches to the new version. Today the gateway holds one key and has no previous-version ring, so this step is a single cutover of the shared `current` entry; the gateway-side ring is a Substrate follow-up after launch (see Risks).
  4. The previous version is removed.
- **Wrapping.** Every envelope-encrypted value uses AES-GCM with associated data `"<cell_id>:<column>:<key version>"`, so a wrapped blob cannot be moved to another cell, column or version and still decrypt. No wrapped values exist yet, so there is no migration.
- **Write-once columns are written in one statement.** Each group is written together, in a single `UPDATE ... WHERE cell_id = $1 AND <first column> IS NULL`, and cellctl checks the affected row count:
  - `backup_key_wrapped` with `backup_key_version`;
  - `b2_key_id` with `b2_key_wrapped` and `b2_key_version`.
  A crash can therefore never leave half a group, which would wedge every later pass.
- **Backup data key.** Each cell has a random 32-byte key, envelope-encrypted with `backup_master_key` (AES-GCM, versioned).
  - cellctl writes `backup_key_wrapped` once, with `WHERE backup_key_wrapped IS NULL`, and only then applies the Secret. It re-reads on conflict, and renders the Secret from the unwrapped row on every pass.
  - The plaintext lives only in the cell's Secret. The runtime container does not mount it.
  - K3s encrypts Secrets at rest, but etcd snapshots can retain the encrypted Secret until snapshot retention expires. D10 states this residual.
- **Per-cell object-storage key.** cellctl creates a B2 application key restricted to the name prefix `cells/<cell_id>/`, using a key-management credential that only cellctl holds.
  - B2 returns the secret only at creation. cellctl therefore writes `b2_key_id`, `b2_key_wrapped` (the secret, envelope-encrypted with `backup_master_key`) and `b2_key_version` once, with `WHERE b2_key_id IS NULL`, before it renders the Secret.
  - If that write loses a race, cellctl deletes the key it just created and uses the stored one.
  - A backup Job can never touch another tenant's prefix.
  - **B2 authorization expires** after 24 hours. On a 401 (`expired_auth_token` or `bad_auth_token`), the client discards its cached authorization, re-authorizes, and retries the call once.
  - **Residual:** a crash between creating a B2 key and writing it leaves an orphan key scoped to that cell's prefix. Its secret was never stored anywhere, so nobody holds it.
- **Master credentials** are `cell_token_key`, `backup_master_key`, the B2 key-management credential, a read-only Hetzner token (volume listing) and the database DSNs. All use the existing SOPS workflow under `infra/secrets`.

### D8. Backups are taken while the cell is stopped

cellctl drives backups; there is no CronJob.

- **Nightly**, only inside the configured window (chart value `cells.backupWindow`, default `02:00-05:00` UTC; a window may cross midnight, such as `22:00-03:00`, and the schema rejects an empty one), for a cell that has never been backed up or whose `now - last_backup_at > 20h`:
  1. set `exomem.io/hold=backup` and scale to 0;
  2. run the backup Job;
  3. scale back to the desired replicas and remove the hold;
  4. write `last_backup_at` and `last_backup_snapshot`.
- **Staggered.** At most `cells.backupConcurrency` backup holds run at once (default 1). Due cells go oldest `last_backup_at` first, with never-backed-up cells first of all. 20 hours rather than 24 keeps a cell from drifting out of the window night after night.
- **Bounded.** The backup deadline runs from `hold-started-at`, so it also bounds the wait for the volume: a pod stuck terminating cannot hold the only backup slot forever. A backup that fails or misses the backup deadline deletes its Job, restarts the cell, sets `last_error_code = BACKUP_FAILED`, removes the hold, and sets `exomem.io/backup-retry-after` (the D6 backoff). It retries inside the window. A cellctl restart resumes a `backup` hold from step 2.
- **Failure is visible.** The Job exits non-zero when `restic backup` fails, or when it cannot extract a snapshot id. cellctl treats a Job that succeeded without a termination message matching `^[0-9a-f]{64}$` as `BACKUP_FAILED`, and never writes `last_backup_at` for it.
- **Before every image change,** as part of D6.
- **What the Job does:**
  - uses the cell image, which carries `restic` 0.17 or later, so the image-pin admission policy admits it;
  - talks to B2 through its S3-compatible endpoint, so a local S3 server is a faithful test double;
  - writes the snapshot id it produced to its termination message, which cellctl reads from the pod status. No ServiceAccount token is needed. `last_backup_snapshot` and the pre-upgrade annotation always hold a real snapshot id, never `latest`;
  - runs as UID 10001 with the D2 pod-level `fsGroup`;
  - mounts the volume read-only, with a restic cache on an `emptyDir`;
  - backs up `/data/vault` and `/data/host` to `cells/<cell_id>/` using the per-cell key;
  - applies retention of 7 daily and 4 weekly snapshots.
- **Why stopped.** Stopping makes the copy crash-consistent across WAL-mode SQLite and the receipts journal, which a live file-by-file copy is not (`governance/store.py:716-746`).
- **Placement.** Once there is more than one node, backup and restore Jobs carry node affinity to the volume's node.

**Restore** runs a restore Job as UID 10001, with `restic restore --delete`, into a new or quiesced volume. The operator export runbook uses the same Job to restore into a scratch namespace and hand the tenant an archive of their vault. It is exercised in the local rehearsal: a scratch-namespace restore must answer recall, report the same `governance-schema status` as its source cell, and accept a governed write. Like D6's restore, it waits until no pod uses the volume, validates the snapshot id, and leaves the volume root, including `lost+found`, untouched.

### D9. Capacity is observed, not reserved

For each node, cellctl publishes `exomem_cloud_capacity`:

- `cell_slots = attachments_limit − headroom − non_cell_attachments`;
- `attachments_used`;
- `observed_at`.

The numbers come from Kubernetes, keyed by the Kubernetes node name:

- `attachments_limit` is the Hetzner CSI driver's `allocatable.count` on the node's `CSINode` (16 on Hetzner today). Neither a chart constant nor a Hetzner server id is used.
- `attachments_used` counts that node's attached `VolumeAttachment`s for the Hetzner CSI driver.
- `non_cell_attachments` counts the ones whose PersistentVolume is not bound to a claim in an `exo-cell-*` namespace.

The driver name is chart value `capacity.csiDriver` (default `csi.hetzner.cloud`). If a node's `CSINode` has no allocatable count for it, cellctl uses `capacity.attachmentsLimitFallback` when that is set, which is how the local rehearsal runs without Hetzner CSI. Otherwise it publishes `cell_slots = 0` for that node and logs why. Nothing is admitted to a node whose limit is unknown.

A node that is no longer in the cluster has its capacity row's `cell_slots` set to 0. cellctl holds only insert and update on C1c, and admission sums `cell_slots`, so a zero row offers nothing and needs no delete privilege. A node that rejoins under the same name is republished with its real count on the next pass.

Substrate admits a new cell only while non-deleted cell rows are below the sum of `cell_slots`. Rows are counted rather than attachments, because stopped cells still own their volumes. There is no reservation ledger to leak.

Adding a node needs a K3s agent role and join configuration, which the current single `cluster-init` server lacks. It is a bounded IaC task, not a protocol change, and it is out of scope here.

### D10. Deletion removes everything and destroys the key

A row with `desired_state = deleted` makes cellctl:

0. **Holds.** Deletion supersedes any hold (D4). cellctl clears the row's hold columns, and the row stops counting toward D6's one-at-a-time rule.
1. **Namespace.** Delete the namespace (the PVC goes with it, and the volume goes through `reclaimPolicy: Delete`). A namespace that already has a `deletionTimestamp` is not deleted again. A 404 or a 409 from a repeated delete counts as progress, not as an error.
2. **Absence.** Confirm that the namespace is gone, that no PersistentVolume has a `claimRef` in the cell's namespace, and that the Hetzner volume `volume_id` is absent.
3. **Backups.** Delete every object version under `cells/<cell_id>/` and confirm that no version remains. B2 keeps hidden versions, so "empty" means no versions at all.
4. **Keys.** Delete the per-cell B2 key by `b2_key_id` and confirm it is absent, then null `b2_key_id`, `b2_key_wrapped` and `backup_key_wrapped`.
5. **Report.** Write `observed_state = deleted`.

Each step retries until its check holds. A failed observation never counts as absence.

**Residual:** encrypted etcd snapshots may retain the cell's Secret until their retention expires. The requirement states that bound.

### D11. Direct TLS ingress

- **Exposure.** The platform Traefik exposes only its `websecure` entrypoint on the node's port 443 through `hostPort`. servicelb stays disabled, and `web` is never exposed on a node port; it stays on the ClusterIP Service the Cloudflare tunnel targets, and `websecure` is not added to that Service.
- **Certificates.** cert-manager obtains the MCP hostname's certificate with an ACME DNS-01 solver through a Cloudflare DNS-edit token, so certificates live in Secrets and need no volume. The issuer is a namespaced `Issuer` in `exomem-cloud`, not a `ClusterIssuer`, and its solver is restricted with `selector.dnsNames` to the MCP hostname, so no other namespace can mint names in the zone with the DNS-edit token. The Cloudflare record is DNS-only.
- **Firewall.** The Hetzner firewall opens 443 and admin SSH.
- **Public routes.** Only the gateway's IngressRoute is public. Cells, cellctl and the Kubernetes API are not.
- **Client address.** Traefik `websecure` sets no `trustedIPs`, so it overwrites `X-Real-Ip` with the peer address that `hostPort` preserves. A NetworkPolicy admits ingress to the gateway only from the Traefik pods, which makes that header the gateway's trustworthy client address.
- **Trusted ingress source.** The gateway applies its per-IP bucket only to requests carrying `EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_HEADER` (`x-exomem-ingress-source`) with the configured value. The gateway's IngressRoute sets it through a Traefik `headers` middleware, which overwrites any copy a client sends, so only requests that came through this route carry it.
- **Legacy.** `cloudflared` stays only for the old platform's hostnames until retirement. The old provisioner is scaled to zero in the same step that deploys cellctl (task 6.2), so it never runs beside a Cloud cell namespace and its admission policy needs no `exo-cell-` exclusion. A rollback that scales the old provisioner back up scales cellctl to zero first.

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

**Environment.** The platform chart renders exactly the environment the Substrate gateway reads (`src/exomem-gateway/server.ts` `validateGatewayEnvironment` and `cloud-config.ts`): `EXOMEM_CONTROL_PLANE_KEY`, `EXOMEM_PUBLIC_BASE_URL`, `EXOMEM_CELL_PROTOCOL_VERSION`, `EXOMEM_GATEWAY_CONTROL_HOSTNAME`, `EXOMEM_GATEWAY_INTERNAL_ORIGIN`, `EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_HEADER` and `_VALUE`, `EXOMEM_CLOUD_MCP_URL`, `EXOMEM_CLOUD_MCP_PATH` and `EXOMEM_CLOUD_CELL_TOKEN_KEY`, plus `DATABASE_URL` for its own Postgres role and `EXOMEM_GATEWAY_PORT`. A chart test pins that set.

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

Shared test vector, asserted on both sides: key (64 hex) `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f`, `cell_id` `aaaaaaaaaaaaaaaa`, bearer `lAS-RM751FtdjYccMyK7IujLHk7OVYAZABBMG1aza6o`.

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
- **The gateway has no cell-token-key ring yet.** It reads one 64-hex `EXOMEM_CLOUD_CELL_TOKEN_KEY` from the same Secret entry cellctl reads, so during a D7 rotation it holds one version at a time, and any gateway restart after the entry changes (a rollout, a node drain, an OOM kill) switches it. cellctl moves cells one at a time, each digest-only re-apply waiting up to 10 minutes on the previous one, so a cell not yet re-applied answers `CELL_AUTH_MISMATCH` for up to about fleet size × one cell restart, not briefly. Accepted for launch; rotations are rare and operator-driven, and the runbook must change the entry only in a maintenance window and restart the gateway once every cell's render digest has converged. The Substrate follow-up adds the previous-version ring, after which step 3 needs no window.
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
