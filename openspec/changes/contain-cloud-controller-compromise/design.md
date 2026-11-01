## Context

`cellctl` is the Cloud controller. It reconciles cell rows from the control database into namespaces, StatefulSets, Jobs, Secrets and NetworkPolicies. A chart-rendered ValidatingAdmissionPolicy confines its writes, and `cellctl` cannot change that policy.

What `cellctl` holds today (from `origin/main`):

| Item | Where | What it opens |
|---|---|---|
| Platform master keys | `CELLCTL_BACKUP_MASTER_KEYS`, pod env | Every cell's wrapped cell root key, restic password and object-store key |
| Fleet bearer key | `CELLCTL_CELL_TOKEN_KEY_*`, pod env; the gateway holds the same key | Every cell's request bearer: `HMAC(key, "exomem-cloud-cell-token-v1:" + cell_id)` |
| Parent object-store key | `CELLCTL_B2_KEY_MANAGEMENT_*` | Creates keys with any capability on any bucket in the provider account (D9) |
| Per-cell object-store keys | Unwrapped every pass | Read, write and delete in a cell's prefix |
| Write access to cell workloads | RBAC on StatefulSets and Jobs | The admission policy pins image, volumes, placement, Secret names and NetworkPolicies, but not command, args or environment |
| Export and import URLs | Presigned by `cellctl` | The cell checks only the `https://` prefix |

The cell already verifies gateway-signed Ed25519 proofs (`cloud_origin.py`), for MCP requests, uploads and downloads. Those proofs have audience separation, `cell_id` binding, bounded lifetime and canonical UUIDs. The verifying key reaches the cell as an environment value that `cellctl` renders.

`cellctl`'s RBAC allows only `get` and `list` on pods. It creates workloads only through StatefulSets and Jobs, so pinning those pod templates covers every workload that it can start.

## Goals / Non-Goals

**Goals**
- A compromised `cellctl` cannot read any tenant's content or backups, cannot send cell data to a destination that it chose, and cannot impersonate a request to a cell.
- Every check holds against an attacker who has read the source code.
- Each check sits in a component that `cellctl` cannot change: the cell image, the chart-rendered admission policy, or Substrate.
- Each attack signal reaches the operator through a path that does not pass through `cellctl`.

**Non-Goals**
- **Operator-blind cells.** The operator still holds the volume key and the escrow key. `make-cloud-cells-operator-resistant` owns operator access.
- **Availability against `cellctl`.** A compromised controller can still stop, restart or delete cells. Backup retention against deletion is `lock-cloud-backups-against-deletion`.
- **A compromised Substrate or gateway.** Each holds a signing key, and its compromise is stated as a limit.
- **A malicious release that passes the required checks.** It is public and permanent.

## Threat model

The adversary has code execution inside the `cellctl` pod, with its environment, its service account and its database role. It has read the whole repository. It does not hold:
- the gateway's request-signing key;
- Substrate's grant-signing key;
- the operator's offline escrow identity;
- write access to the chart's admission policy.

| Mechanism | Stops | Does not stop |
|---|---|---|
| Mandatory gateway proof (D1) | Impersonating any request to any cell; a bearer leak at the gateway or `cellctl` | A compromised gateway |
| Pinned trust anchors (D2) | Swapping a verifying key, endpoint or escrow recipient to forge proofs or grants, or to redirect data | A chart change, which is admin-tier under `make-cloud-cells-operator-resistant` D6 |
| Closed pod template (D3) | Running any command, hook or probe against a cell's volume or Secret; planting a CA or a DNS override | A cell subcommand that is itself exploitable |
| Signed grants, destination checks and recipients (D4) | Exporting, importing or restoring without a Substrate grant; sending data to an address that `cellctl` chose; reading an owner export or an import upload in transit | A compromised Substrate |
| Cell-held backup keys (D5) | Decrypting any backup with what `cellctl` holds | The operator opening the escrow copy with their presence |
| Key custodian and key watcher (D9) | Creating an object-store key with any capability; changing the backup bucket's lock settings unnoticed | A compromised custodian or node, within the cell-data account |
| Independent attack alerts (D7) | A compromise that stays unnoticed | Root on the node suppressing events before they leave it |

## Decisions

### D1. A cell accepts a request only with a valid gateway proof

Today the static per-cell bearer is the cell's gate, and the gateway's Ed25519 proof is an added layer. The change reverses this:
- The cell requires a valid proof for every request: `mcp`, `upload` and `download`.
- The fleet bearer key is retired, so neither `cellctl` nor the gateway holds it.
- The cell's health-only port (`cell-recall-lagging`) stays unauthenticated and content-free, as before.

**Migration.**
1. The cell accepts either the bearer or the proof for one release, and logs which one each request used.
2. Once the logs show no bearer-only requests for that release, the cell refuses the bearer.
3. `cellctl` stops rendering `cell-token`, and the chart drops the bearer key Secret.

Why not keep a per-cell random bearer? `cellctl` would still render it. A signed proof needs a private key that only the gateway holds, and a fixed public key that the admission policy pins (D2).

### D2. The admission policy pins every trust anchor that a cell reads

A cell trusts four values:
- the gateway's request-verifying key;
- Substrate's grant-verifying key;
- the object-store endpoint and bucket;
- the escrow recipient.

Today `cellctl` renders such values into the cell environment, so a compromised `cellctl` could swap a verifying key for its own and then sign proofs or grants itself.

The cell's own cell ID is a fifth anchor. The proof check and the destination check both compare against it. The policy therefore requires `EXOMEM_CLOUD_CELL_ID` to equal the cell ID in the namespace name.

The chart renders these values into the admission policy as literals. They are public values, not secrets. The policy accepts a cell StatefulSet or Job only when each anchor's environment value equals the pinned value, or the pinned next value during a rotation.

Why not a ConfigMap that the cell mounts? `cellctl` creates objects in the cell namespace, so it could create a decoy. A literal in the policy expression is out of `cellctl`'s reach.

### D3. Cell workloads run a closed pod template

Pinning the command alone is not enough. A pod template has other fields that run code, change what the process reads, or redirect its traffic, and Pod Security `restricted` limits none of them:
- `lifecycle.postStart` and `preStop` hooks;
- exec probes;
- `volumeMounts`, `subPath` and `workingDir`;
- `envFrom`, and environment values from any key of a Secret that `cellctl` writes;
- `dnsConfig` and `dnsPolicy`.

The admission policy therefore accepts a cell StatefulSet or Job only when its whole pod template is an allowlisted shape. Every container and init container must meet all of these:
- the command is the fixed process preamble, followed by `exomem` and one subcommand from the closed set;
- there is no `lifecycle`, and probes are `httpGet` on the cell's own fixed ports and paths;
- `volumeMounts` are exactly the known (name, mountPath) pairs for that subcommand, with no `subPath` and no `volumeDevices`;
- `workingDir` is unset;
- there is no `envFrom`. Every environment entry's name is on the allowlist for that subcommand. A `valueFrom` names only an allowlisted key of `cell-credentials`, or, for `cell-backup`, `cell-restore` and `cell-backup-migrate`, of `cell-object-store-key` (D9). A trust anchor is always a literal `value` (D2). The one exception is `cell-export-verify`: it may also read the URL key of the Secret named for its own export ID, which stays per export so that cleanup removes only that export's Secret.

Pod Security `restricted` does not cover every security field, so admission also pins these on every container:
- `readOnlyRootFilesystem: true` and the full container security context;
- `terminationMessagePolicy: File`, because the default copies the log tail into the message that `cellctl` reads.

At the pod level:
- the template has no ephemeral containers;
- `dnsPolicy` is `ClusterFirst`, and there is no `dnsConfig`;
- `enableServiceLinks` is false, so the kubelet injects no environment outside the allowlist;
- each volume name is bound to its expected source: the data volume to the cell's own claim, and Secret volumes to `cell-credentials`, or to `cell-object-store-key` for the three backup subcommands.

Admission already denies `hostAliases`.

The `cell-credentials` Secret's data keys must come from an allowlist too. Its values stay free, but no allowlisted environment name or mount reads one as configuration that changes trust.

These lists are protocol vocabulary that the cell image implements, not data, so fixed lists in the policy are sound. The environment rule is an allowlist, not a denylist. A denylist misses names such as `AWS_CA_BUNDLE`, `REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE`, which would let `cellctl` plant a certificate authority.

**Destination trust follows from this.** No allowlisted field can add a CA, change the cell's DNS resolution, or point the object-store host elsewhere. So the D4 host check rests on the image's CA bundle and the cluster DNS, and `cellctl` can change neither.

The subcommands:
- `serve`
- `cell-init`
- `cell-backup`
- `cell-restore`
- `cell-import`
- `cell-export`
- `cell-export-verify`
- `cell-backup-migrate`, which runs once per existing cell (D5)

**Backup and restore move off shell strings.**
- `exomem cell-backup` runs the vault check, `restic init` if needed, `restic backup`, the snapshot-ID capture, `forget`/`prune` and the termination message. Every step that the joined shell string runs today moves inside the subcommand.
- `exomem cell-restore` takes the snapshot ID and validates it.
- The restic binary stays in the image. The subcommands run it with argument vectors, never through a shell.

The policy also pins the remaining arguments where they carry trust. `cell-backup` takes no destination argument (D4).

Why pin the environment names? An unpinned environment could set `PYTHONPATH`, `LD_PRELOAD` or a restic hook variable and run other code inside an allowed subcommand. The command and the environment names are pinned together.

### D4. Data moves only to destinations that a signed grant names

**Signed grants.** Substrate signs a grant when it creates an export, import or restore request. The grant is an Ed25519 JWT with:
- a dedicated audience, `exomem-cell-operation`;
- `tenant_id`, `cell_id` and `operation` (one of `export`, `import` or `restore`);
- `operation_id`;
- the object key;
- for an import, `ciphertext_bytes` and `ciphertext_sha256`;
- for a restore, the snapshot ID;
- `iat`, `exp` and `jti`.

`cellctl` relays the grant from the request row to the Job, as an argument.

**The cell checks before it moves data.**
1. It verifies the grant against the pinned grant key (D2).
2. It checks that the grant's `cell_id` equals its own.
3. It checks that the operation and ID match the subcommand's.
4. It parses each presigned URL with a URL parser. The host must equal the pinned endpoint. The path must be the pinned bucket, then the grant's object key, under the cell's own prefix.

A mismatch refuses the operation and logs a cataloged, content-free event (D7).

**Recipients travel on a channel that excludes `cellctl`.** Two data paths otherwise leak plaintext to a passive `cellctl`, even with a valid grant:
- **Owner exports.** Today an export is a plaintext ZIP in a bucket that `cellctl` can read.
- **Imports.** Today the cell's import recipient reaches the browser through `cellctl`, which writes the `recipient` column. So `cellctl` can substitute its own key and decrypt the upload.

The fix in both cases:
- **Exports.** The tenant's browser generates an `age` key pair when it requests the export. Substrate takes the public recipient from the authenticated browser session, and stores it in a column that only Substrate's role writes, never `cellctl`'s. It also puts the recipient into the signed export grant. The cell encrypts the archive to that recipient before upload, and the browser decrypts it after download. The private key never leaves the browser.
- **Export integrity.** `age` does not prove who made a ciphertext, so `cellctl` could swap in a forged archive that the browser's key still decrypts. The export Job runs under a hold that stops the serving cell, so it cannot serve anything itself. It therefore records the ciphertext's SHA-256 and the export ID on the volume, outside the export root. The restarted serving cell reports that digest on a route that the gateway calls with a signed proof (D1), which excludes `cellctl`. Substrate takes the digest only from that route, never from the `archive_sha256` column, which `cellctl` writes. When the digest is missing, the client refuses the download. Substrate shows that digest to the browser, and the browser checks it before it accepts the download.
- **Export verification.** The verify Job cannot read an archive encrypted to the browser's key. So the export Job checks the plaintext archive with the canonical reader as it streams, before encryption. The verify Job then checks the uploaded ciphertext's size and SHA-256 against what the cell reported.
- **Imports.** The serving cell publishes its import recipient on a route that the gateway calls with a signed proof (D1). `cellctl` cannot reach that port, so the path excludes it. Substrate puts the recipient into the signed import grant, and the cell checks that the grant's recipient is its own before it decrypts.

**Backups need no grant.** The cell derives its repository from its own cell ID, its repository generation and the pinned endpoint and bucket: `cells/<cell_id>/repo-<generation>`. It keeps the generation on its volume and raises it only on a migration or on leaving the lock. `cell-backup` takes no destination.

**Restores need a grant.** A restore can roll a cell back. Substrate signs a restore grant only for an operator-initiated or tenant-initiated restore request.

**The grant key is separate.** Substrate's control plane holds a grant key apart from the gateway's request key. A gateway compromise then cannot authorize exports, and a grant-key compromise cannot impersonate requests.

### D5. The controller cannot decrypt backups

**Options considered**

| Option | What a compromised `cellctl` gets | Cost |
|---|---|---|
| O1: keep the master keys in `cellctl` | Every backup | None, and it does not meet the goal |
| O2: the cell holds its backup password, with an offline escrow copy | Ciphertext only | A restore after volume loss needs the operator's presence |
| O3: an external KMS unwraps for `cellctl` | Every backup, but slower and logged | A paid third party; the hosting provider has none; it does not meet the goal |

**Decision: O2, in two modes.** The operator chose these on 2026-11-01.
- **Standard mode.** The escrow copy is encrypted to the operator's presence-gated recipient. `cellctl` holds nothing that decrypts a backup. The operator can still open the escrow copy deliberately, and the privacy copy says so.
- **Tenant-locked mode (opt-in).** This is the tenant lock of `make-cloud-cells-operator-resistant` D3. The escrow copy is encrypted to the tenant's own key, from their recovery code or passkey, and no platform copy is written. The operator then cannot decrypt the cell's backups, or read it while it is stopped, locked or deleted, once earlier platform copies expire. A tenant who loses their recovery code loses their backups, and a restore after volume loss needs the tenant.
- **The mode statement.** Substrate signs a statement with the grant key (D2). It holds the mode, the escrow recipient, the cell ID, the tenant ID, a generation number and an expiry.
  - A standard-mode statement must name the pinned operator recipient.
  - A tenant-locked statement names the tenant's recipient, and only a tenant-authenticated request makes Substrate sign one.
  - The cell reads the statement on the tenant-lock key channel (`make-cloud-cells-operator-resistant` D3, task 5.1), which excludes `cellctl`.
  - The cell keeps the highest generation that it has seen on its volume and refuses an older statement. So `cellctl` can neither substitute a recipient nor downgrade a locked cell by replaying an old standard-mode statement.
  - The statement carries its own audience, `exomem-cell-mode`, which differs from the operation grants' audience. So no operation grant passes as a mode statement, and no mode statement passes as a grant.
  - A cell whose volume carries the tenant's inner layer never writes an operator escrow copy. Leaving the lock takes a newer, tenant-authenticated statement.
  - Entering the lock follows `make-cloud-cells-operator-resistant` D3.
  - **Leaving the lock starts an empty repository.** The cell initialises a repository at the next generation, with a new password, and copies nothing into it. It escrows only the new password to the operator.
    - Before the platform re-wraps the cell root key, the cell erases the locked-period password.
    - The cell then hides the locked-period repository, so its versions expire after the lock margin.
    - A tenant who wants those snapshots restores or exports them before leaving.
    - So the operator never gets a password that opens a snapshot taken while the cell was locked.
- **Tenant-locked cells keep their keys inside the inner layer.** The backup password sits only inside the tenant's inner layer, never on the outer volume, whose passphrase the platform keeps. A restore after volume loss receives the password on the tenant-lock key channel, never through a cluster Secret.
- **Neither mode stops one thing:** whoever controls the node can read a running, unlocked cell. Only a confidential cell (`make-cloud-cells-operator-resistant` D8) closes that.

The steps:
- **Generate.** On first start, `exomem cell-init` generates the restic password and writes it to the cell's encrypted volume, at a path outside both backup roots (`/data/vault` and `/data/host`). Otherwise the password would sit inside the repository that it protects. It also stays outside the export root.
- **Escrow.** It writes an escrow copy, encrypted with `age`, to the cell's own prefix in the locked backup bucket (`lock-cloud-backups-against-deletion`), so no running service can destroy it before retention ends. The copy's name carries the repository generation and a hash of the password, so a recreated namespace cannot overwrite it. The recipient comes only from a valid **mode statement** (see below), never from a value that `cellctl` renders.
- **Back up.** `cell-backup` reads the password from the volume. The backup Job already mounts the volume.
- **Restore with the volume.** A restore onto an existing volume uses that password.
- **Restore after volume loss.** The operator decrypts the escrow copy with their presence-gated identity (`make-cloud-cells-operator-resistant` D6) and supplies the password to the restore through a one-time Secret.
- **What `cellctl` keeps.** It no longer creates object-store keys (D9). Its remaining object-store keys cannot read a backup's plaintext.

**Existing cells move to a new repository.** Restic's `key add` and `key remove` change only key files, and every key file wraps the same master key. A re-key would therefore leave every snapshot readable to anyone who holds the old platform password and a copy of an old key file. Instead, each cell runs a new subcommand, `exomem cell-backup-migrate`:
1. `restic init` a new repository with the cell-held password, with `--copy-chunker-params` so deduplication continues.
2. `restic copy` every snapshot from the old repository. This re-encrypts every snapshot under the new master key.
3. Write the escrow copy.
4. Run `restic check --read-data` on the new repository. Check that every snapshot of the old repository appears in it, matched by the original snapshot ID that `restic copy` records.
5. Only when both pass, destroy the platform-wrapped password. Otherwise keep it and report the cell as not migrated.

This is the same move as `lock-cloud-backups-against-deletion` D5, which copies each repository into the locked bucket, so there is one migration, not two.

**Residual.** Snapshots in the old repository stay readable with the old platform password until the old repository is gone.

**Relation to the cell root key.** `make-cloud-cells-operator-resistant` D1 wraps the backup password under the cell root key, which the platform master key wraps. Under O2 the backup password leaves that hierarchy. The volume passphrase and the media key bundle stay under it, because the volume must open unattended when a pod is rescheduled.

**Costs.**
- Disaster recovery of a lost volume needs the operator. That needs a human by design: the purpose is that no running service can decrypt backups.
- A lost escrow identity permanently loses the backups of every cell whose volume is also gone. The escrow identity therefore follows the platform's existing age-custody recovery path.
- The escrow copy is written once and stays the current version. The locked bucket's lifecycle expires only noncurrent versions, so it never removes the escrow copy (`lock-cloud-backups-against-deletion` D3).

### D6. Job egress narrows to the object store (later phase)

With D3 and D4 in force, only cell code runs in a Job, and that code sends data only to verified URLs. Narrower egress is defence in depth against a code-execution bug inside a subcommand, for example an import parser.

**Options**
- **A platform egress proxy with a host allowlist.** Jobs may reach only the proxy. The proxy allows CONNECT only to the pinned object-store host. This is the recommendation.
- **An ipBlock list of the provider's address ranges.** It is brittle when the provider changes addresses.
- **The CNI's FQDN policy.** The current CNI does not offer it.

This phase starts after D3 and D4. The proxy is a new platform component with its own review. Until it lands, the closed pod template (D3) carries containment alone. A Job can still reach any public address on 443, so an exploitable subcommand would have an open path out.

### D7. Attack signals alert through a path that excludes the controller

A compromised `cellctl` would not report itself. The access record pipeline (`make-cloud-cells-operator-resistant` D4) ships cluster audit events and cell logs off the node. Its evaluator raises a platform alert through the existing receiver on any of:
- an admission denial for `cellctl`'s service account;
- a grant or destination refusal logged by a cell;
- a Secret read by an identity outside the expected set;
- a Job or StatefulSet that the policy denied as an unknown kind;
- an object-store key or a backup bucket setting outside the fixed shapes, which the key watcher reports (D9).

Each alert carries only an identity, a cell ID and a reason token. It never carries content.

A compromised `cellctl` cannot suppress these signals, because they come from the API server and the cells, not from `cellctl`. It can flood them, though. The evaluator therefore coalesces by (cell ID, reason token), so a flood becomes one open alert for each class and cannot bury a different one.

### D8. Old attested images

The admission policy accepts any digest from the cell image repository. The attested-image verifier (`make-cloud-cells-operator-resistant` task 4.1) limits digests to release-attested ones. A compromised `cellctl` could still choose an older attested image with a known flaw.

This change adds a minimum release that the chart pins, so that admission refuses a rollback below it. This is a required task, not an option: without it, `cellctl` can roll a cell back to a release that lacks these checks.

The cellctl admission policy matches only the digest pattern, and it cannot order releases. The attested-image verifier (`make-cloud-cells-operator-resistant` task 4.1) therefore enforces the floor. It reads the release version from the attestation and refuses one below the pinned minimum.

### D9. One key custodian creates object-store keys

**Provider facts.** A disposable test on the object store, on 2026-11-01, showed two things:
- A key that can create keys can create a key with any capability. A key without `writeBuckets` or `bypassGovernance` created keys with `writeBuckets`, `bypassGovernance`, `writeFileRetentions` and `readFiles`. The provider has no rule that limits a new key to its creator's capabilities.
- A key that can create keys cannot be limited to a bucket. The provider refuses `writeKeys` on a bucket-level key.

So `cellctl`'s parent key can today create a key that reads, deletes or reconfigures any bucket in the provider account. The account also holds:
- the Terraform state, which holds the Cloudflare tunnel token and the Access service-token secret as plain JSON. Server-side encryption does not hide them from a key that can read files. With the tunnel token, a rogue tunnel connector receives tenant requests, which bypasses D1 to D5;
- the recovery and database backup buckets, which use governance mode, so a created key with `bypassGovernance` deletes them;
- the buckets of another product.

In the locked bucket of `lock-cloud-backups-against-deletion`, such a key could also:
- remove the default retention, so that new backups are written unlocked and can be deleted outright (shown in the same test);
- extend locks without bound, which nobody can undo in compliance mode.

It cannot shorten or remove an existing compliance lock, and it cannot switch Object Lock off. The same test showed both refusals.

**Options considered**

| Option | Who can create keys | Cost |
|---|---|---|
| Keep minting in `cellctl` | `cellctl`, its whole attack surface | None, and it fails the goal |
| A key custodian in the same account | One small component | A compromised custodian or node still reaches the Terraform state, the platform backups and the other product |
| A separate account for cell data, with the custodian only there | The custodian, within cell data only | One more provider account for the operator to create |
| Keys created ahead of time, outside the cluster | A scheduled job outside the cluster | A pool of reserved cell IDs, and a key-creating key in CI secrets |

**Decision.** The operator chose on 2026-11-01 to isolate the account first. Key custody is phase 1 of this change, before every other phase:
1. The operator creates a member account in the provider's account group, for cell data only. The group bills both accounts together.
2. The custodian runs only in that account. New cells get their keys and repositories there.
3. `cellctl`'s parent key in the current account is deleted. No in-cluster component then holds a key that can create keys in the current account. The Terraform state, the platform backups and the other product's buckets are then out of the cluster's reach.
4. The operator rotates the tunnel token and the Access service-token secret, because both sat within reach of `cellctl`'s key.
5. Existing cells keep their prefix-limited keys in the current account until the backup-lock migration moves their data. The operator's infrastructure tooling deletes those keys when a cell migrates or is deleted.

**The custodian.**
- It runs as its own small Deployment, with its own service account and image, in its own namespace. That namespace is outside every namespace that admission rule 1 lets `cellctl` write. The same chart check as the lock extender's enforces this.
- It runs on a node that runs no cell. A default-deny NetworkPolicy lets it reach only the API server and the object store. It takes no inbound traffic.
- It receives its key through a Secret reference, never as a literal environment value, because `cellctl` can list pods cluster-wide.
- It writes one Secret of its own in each cell namespace, `cell-object-store-key`, and no other Secret. Admission limits its writes to that name, because RBAC `resourceNames` cannot limit `create`. It never reads `cell-credentials`.
- Admission rule 7 already keeps `cellctl` off every Secret name except its own, so `cellctl` cannot read a key through a patch response. The closed pod template (D3) mounts `cell-object-store-key` only in the subcommands that need it.
- It creates exactly one key shape:
  - the bucket pinned in the chart;
  - the name prefix `cells/<cell_id>/`, where the cell ID comes from the namespace name, never from a request;
  - exactly `listBuckets`, `listFiles`, `readFiles`, `writeFiles` and `deleteFiles`.
- It takes the IDs of keys to delete only from the provider's key listing, matched on the prefix, never from a Secret.
- It hides a cell's prefix, and then deletes its key, only on a deletion record that Substrate signed with the grant key (audience `exomem-cell-delete`), and only after a 7-day grace period. It raises an alert on each hide. A deleted or recreated namespace alone hides nothing.

**What `cellctl` keeps.** It keeps two keys that cannot create keys:
- a key limited to the staging bucket, for presigning and removing export and import objects;
- a key that can only list files in the backup bucket, for version bookkeeping.

**The key watcher.** The provider cannot limit a key that creates keys, so detection catches a rogue key.
- It runs as a scheduled CI job outside the cluster, at least hourly. It alerts with its own receiver credential, which no in-cluster component holds.
- Its key in each account can only list keys and buckets and read bucket settings (`listKeys`, `listBuckets`, `readBucketRetentions`, `readBucketEncryption`, `readBucketReplications`). The provider withholds each of those settings from a key without the matching capability.
- It matches every single-purpose key by its key ID, from Terraform outputs. That covers the custodian, the lock extender, the watcher, the infrastructure key, the durability keys and every other key in the account. A clone with the same name and capabilities therefore does not pass.
- It matches each per-cell key on its exact bucket, prefix shape and capability set, with at most one key per prefix.
- It compares the backup bucket's Object Lock, default retention, lifecycle rules, bucket type, encryption, replication and CORS settings with pinned values.
- It raises an alert on any difference, on an authentication or listing failure, and on a missed run, because the receiver expects its heartbeat. Deleting the watcher's key therefore raises an alert.
- A version written after its default retention was removed can be deleted at once, so the run interval bounds that window. The 23-day margin protects every version written before the change.

**Residual.**
- Until phase 1 ships, `cellctl`'s key reaches everything listed above.
- After phase 1, a compromised custodian or node can still create any key in the cell-data account. Such a key can extend locks without bound or remove the default retention. The split does not shrink this, because there is no subset rule. The watcher reports such a key, but it cannot undo an extension.
- Keys created ahead of time outside the cluster would remove key creation from the cluster. That option stays open (Open Questions).

## Early check evidence

### Task 2.1: the rendered kinds

Method: `cellctl` rendered each kind through `render_cell_manifests`, `render_backup_job`, `render_restore_job`, `render_import_job` and `render_export_job` (in `infra/cellctl/src/cellctl/manifests.py`). The check listed each command vector and environment name. It ran on 2026-11-01 against `origin/main` at `17624b2dc`, plus the trust-anchor environment that this change adds.

Every container command is `sh -c` with a preamble. The preamble is `ulimit -c 0`, then `mkdir -p` of the scratch directories under `/data/scratch`, then `exec "$@"` of the process. The two exceptions are the backup and restore Jobs, which run one joined shell string, and the export verification Job, whose preamble has no `mkdir`.

| Kind | Process and arguments | Environment names |
|---|---|---|
| StatefulSet init container | `command`: `exomem cell-init`. `args`: `--vault /data/vault --json` | `EXOMEM_CLOUD_CELL`, `EXOMEM_CLOUD_CELL_ID`, `TMPDIR`; with a local volume, also `EXOMEM_CLOUD_CELL_BACKED_UP` (optional `secretKeyRef`) |
| StatefulSet serving container | `exomem --transport http --host 0.0.0.0 --port 8765` | Fixed: `EXOMEM_CLOUD_CELL`, `EXOMEM_CLOUD_CELL_ID`, `EXOMEM_CLOUD_CELL_TOKEN` (`secretKeyRef`), `EXOMEM_CLOUD_HEALTH_PORT`, `EXOMEM_VAULT_PATH`, `TMPDIR`, `EXOMEM_LOG_DIR`. Conditional: `EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS` (optional `secretKeyRef`), `EXOMEM_CLOUD_READ_ONLY`, `EXOMEM_CLOUD_ARTIFACT_BROKER_URL`, `EXOMEM_CLOUD_MEDIA_SLOT_URL`, `EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY`, `EXOMEM_MEDIA_STORE_CONFIG`, `EXOMEM_MEDIA_EDGE_URL`, `EXOMEM_MEDIA_EDGE_CREDENTIAL_FILE`, `EXOMEM_MEDIA_ENGINES`. Then every `model_env` entry |
| `cell-backup` Job | One shell string: vault check, `restic snapshots \|\| restic init`, `restic backup`, snapshot-ID capture, `restic forget --prune`, the termination message | `RESTIC_REPOSITORY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (`secretKeyRef`), `RESTIC_PASSWORD` (`secretKeyRef`), `RESTIC_CACHE_DIR`, `TMPDIR` |
| `cell-restore` Job | One shell string: `restic restore <64-hex snapshot ID> --target / --delete --include /data/vault --include /data/host` | The same six names as the backup Job |
| `cell-import` Job | `exomem cell-import <prepare\|import\|discard> --import-id <id>`; `import` adds `--request <json> --url <presigned URL>`; `discard` may add `--keep-adoption` | The init container's names, plus `EXOMEM_IMPORT_MAX_MEMBERS`, `EXOMEM_IMPORT_RESERVE_BYTES` |
| `cell-export` Job | `exomem cell-export --export-id <id> --part-bytes <n> --max-bytes <n> --part-url <presigned URL>` (repeated) | The init container's names, plus `EXOMEM_LOG_DIR` |
| `cell-export` verification Job | The same subcommand with `--verify --archive-bytes <n> --archive-sha256 <hex> --archive-version-id <id>` | `EXOMEM_CLOUD_CELL`, `EXOMEM_CLOUD_CELL_ID`, `TMPDIR`, `EXOMEM_EXPORT_VERIFY_URL` (`secretKeyRef` to the per-export Secret) |

The `cell-import` and `cell-export` Jobs also carry the trust-anchor names of the next section when `cellctl` has the values.

Two fields carry the vector. The init container splits it: `command` holds the preamble and `exomem cell-init`, and `args` holds the rest. Kubernetes appends `args` to `command`, and the preamble's `exec "$@"` passes them on to the process. Every Job and the serving container put the whole vector in `command` and set no `args`. So the admission rule of task 4.2 must pin `args` as well, exact or absent. A pin on `command` alone leaves `args` free to extend the process.

What the check shows against D3:
- The closed set for the subcommands is smaller today than D3 names. The cell has `cell-init`, `cell-import` and `cell-export`, and `serve` is the process with no subcommand. `cell-backup` and `cell-restore` are shell strings until task 4.1. `cell-backup-migrate` belongs to D5.
- D3 lists `cell-export-verify` as a subcommand, but the cell runs it as `cell-export --verify` with a different environment set and no scratch volume. Task 4.2 must either add the subcommand or let the policy tell the two apart by `--verify`.
- The environment allowlist is per rendered variant, not per subcommand: `cell-export` renders two sets, and the serving container's conditional names depend on the cell.
- `model_env` is still a denylist. `check_model_env` refuses the names in `MODEL_ENV_FORBIDDEN_KEYS`, `MODEL_ENV_FORBIDDEN_PREFIXES` and `MODEL_ENV_FORBIDDEN_SUFFIXES`, and accepts any other upper-case name. So the serving container's names are not a closed set yet, and task 4.2 needs an allowlist in its place. The denylist already refuses the `EXOMEM_CLOUD_` prefix, so it cannot set the trust anchors below.

### Tasks 3.1 and 3.2: names that the report-mode release fixes

The mode is a constant of the cell image, `cloud_containment.ENFORCE` in `src/exomem/cloud_containment.py`. No environment value, flag or rendered argument selects it, so nothing that `cellctl` writes can switch it. Each check also takes an `enforce` parameter that defaults to the constant, so tests cover the enforce branch. Task 5.1 changes the constant, and the minimum-release floor (D8) then stops a rollback.

Events. Both are in `EVENT_CATALOG`, carry no content and name no token, claim or URL. A serving cell writes them to its JSONL log. A Job writes them to its stderr in the `key=value` form.
- `cloud_request_credential`, once per `/mcp` request. Fields: `credential` (`both`, `bearer_only`, `proof_only` or `neither`), `proof` (`valid`, `invalid`, `absent` or `unconfigured`), `cell_id` and `verdict`. The `credential` follows from the bearer's validity and the proof's alone. A cell without the proof key reports a valid bearer as `bearer_only`, with `proof` `unconfigured`, so the D1 step-2 query for no bearer-only requests covers that cell too.
- `cloud_operation_check`, once per reason for each `cell-export` or `cell-import` run that moves data, or once with `reason=none` when the run passes. Fields: `operation`, `operation_id`, `cell_id`, `reason` and `verdict`. The cell logs `cell_id` only as sixteen base32 characters and `operation_id` only as a canonical UUID, and logs `invalid` otherwise, so no value can add a line to the log.
- `verdict` is `pass`, `would_refuse` (report mode) or `refused` (enforce mode).
- The reasons are `grant_missing`, `grant_unconfigured`, `grant_invalid`, `grant_operation`, `grant_request`, `destination_unconfigured`, `url_malformed`, `url_scheme`, `url_userinfo`, `url_host`, `url_path`, `url_foreign_key`, `url_grant_key`, `url_query`, `url_missing`, `check_failed` and `none`.

Trust anchors. `cellctl` renders them as literal values into the import, export and export-verification Jobs, from its own object-store endpoint and bucket settings:
- `EXOMEM_CLOUD_OBJECT_STORE_ENDPOINT` and `EXOMEM_CLOUD_OBJECT_STORE_BUCKET`;
- `EXOMEM_CLOUD_GRANT_PUBLIC_KEY`, an Ed25519 public key PEM that `cellctl` reads from its own `CELLCTL_GRANT_PUBLIC_KEY`. Nothing sets that variable yet. The controller wiring, and the choice between a chart value and a Secret, come with the Substrate grant batch (task 3.3). An empty anchor is not rendered.

Today `cellctl` renders these anchors, so a compromised `cellctl` can change them and the cell's checks report without yet protecting. Once task 4.2 pins them in the admission policy (D2), `cellctl` can change none of them.

Grant claims, as `cloud_grant.py` verifies them. The token is an `EdDSA` JWT with header `typ` `JWT`.
- Every grant has exactly `aud` (`exomem-cell-operation`), `iat`, `exp`, `jti`, `tenant_id`, `cell_id`, `operation`, `operation_id` and `object_key`.
- An import grant adds `ciphertext_bytes` and `ciphertext_sha256`. A restore grant adds `snapshot_id`.
- Task 4.3 adds a `recipient` claim to the export and import grants. That changes the exact claim set, so the cell's verifier and Substrate's signer (task 3.3) change in the same release.
- There is no `v` or `iss` claim. `exp - iat` is at most 72 hours. Substrate signs an import grant when it records the upload, because the ciphertext digest exists only then. A replayed grant still names one operation ID, one object key and one digest, and an import is idempotent per import ID, so the long bound adds no capability. Substrate's signer (task 3.3) must use these claim names and bound.
- The cell checks the claims with the same function as the gateway proof (`cloud_origin.signed_claims`), then the `operation` and `operation_id` against the subcommand's, then each presigned URL.

Destination rule. The cell parses each URL with `urllib.parse`. The scheme, host and port must equal the endpoint anchor's. The URL may carry no user information and no fragment. The path must be `/<bucket anchor>/<object key>` with every segment decoded once, no empty, `.` or `..` segment, and no encoded slash. The key must sit under `cells/<own cell ID>/`, and it must equal the grant's `object_key` when a grant is present. `cellctl` presigns path-style URLs of this shape (checked with botocore 1.43.111), and its S3 client pins path style so the check does not rest on a library default.

The query may hold only these names, compared without case: the six SigV4 presign names (`X-Amz-Algorithm`, `X-Amz-Credential`, `X-Amz-Date`, `X-Amz-Expires`, `X-Amz-SignedHeaders` and `X-Amz-Signature`), `partNumber` and `uploadId` for an upload part, and `versionId` and `response-content-disposition` for a download. The SigV4 and S3 protocols fix these names. They come from presigning with `cellctl`'s own export and import storage classes, and a `cellctl` test fails when a URL gains another name. A name such as `x-amz-acl` or `x-amz-copy-source` changes what the request does, so the cell reports `url_query`. An operation with no URL reports `url_missing`.

## Reuse

- The proof verifier, `cloud_origin.py`, and Substrate's `sign()` for the request proof and the grant.
- `age` for the escrow copy, which the platform already uses for SOPS custody and owner exports.
- The admission policy and its K3s forged-write tests, which already prove pins for images, volumes and NetworkPolicies.
- The access record pipeline and the platform alert receiver.

## Risks / Trade-offs

- **The operator holds the restore path for lost volumes.** Losing the escrow identity is a disaster for those cells. Mitigation: the escrow identity follows the platform's age-custody recovery path, and a rehearsal restores from escrow.
- **Rolling out the command pin can refuse a legitimate workload.** Mitigation: the K3s Live job renders every Job kind and checks that the policy admits it, before the pin enforces.
- **The bearer retirement can cut traffic.** Mitigation: a dual-accept release with request logging, and retirement only after a release with no bearer-only requests.
- **Pinned anchors make key rotation a chart release.** Mitigation: each anchor has a current and a next value.

## Migration Plan

1. **Key custody** (D9 phase 1). The cell-data account, the custodian and the key watcher start. Then `cellctl`'s parent key is deleted, and the tunnel token and the Access secret are rotated.
2. **Cell-side checks in report mode.** The cell verifies proofs, grants and destinations and logs each would-be refusal, but does not refuse. Substrate starts signing grants.
3. **Command pin.** Add `cell-backup` and `cell-restore`, and render every Job through them. The admission rules for command, environment names and anchors enforce once the K3s Live job admits every rendered kind.
4. **Enforcement.** The cell refuses a request without a proof, and an operation without a grant or with a foreign destination.
5. **Backup custody.** Cells move to new repositories one at a time, owner first. `cellctl` stops holding backup passwords.
6. **Retirements.** Retire the bearer, then the master keys' backup wrapping.
7. **Egress proxy** (D6).

## Open Questions

- The browser side of encrypted owner exports (D4): `age` in the browser, and how a tenant who loses the export key asks for a new export.
- Whether a tenant-initiated restore needs a confirmation step in Substrate before it signs a restore grant.
- The age-custody recovery path for the escrow identity, shared with `make-cloud-cells-operator-resistant` task 4.5.
- Whether to create cell keys ahead of time outside the cluster, which removes key creation from the cluster at the cost of a pool of reserved cell IDs (D9).
