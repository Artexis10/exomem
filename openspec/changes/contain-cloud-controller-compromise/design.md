## Context

`cellctl` is the Cloud controller. It reconciles cell rows from the control database into namespaces, StatefulSets, Jobs, Secrets and NetworkPolicies. A chart-rendered ValidatingAdmissionPolicy confines its writes, and `cellctl` cannot change that policy.

What `cellctl` holds today (from `origin/main`):

| Item | Where | What it opens |
|---|---|---|
| Platform master keys | `CELLCTL_BACKUP_MASTER_KEYS`, pod env | Every cell's wrapped cell root key, restic password and object-store key |
| Fleet bearer key | `CELLCTL_CELL_TOKEN_KEY_*`, pod env; the gateway holds the same key | Every cell's request bearer: `HMAC(key, "exomem-cloud-cell-token-v1:" + cell_id)` |
| Parent object-store key | `CELLCTL_B2_KEY_MANAGEMENT_*` | Creates per-cell keys, including keys that read any prefix |
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
- there is no `envFrom`. Every environment entry's name is on the allowlist for that subcommand. A `valueFrom` names only an allowlisted key of `cell-credentials`, and a trust anchor is always a literal `value` (D2). The one exception is `cell-export-verify`: it may also read the URL key of the Secret named for its own export ID, which stays per export so that cleanup removes only that export's Secret.

Pod Security `restricted` does not cover every security field, so admission also pins these on every container:
- `readOnlyRootFilesystem: true` and the full container security context;
- `terminationMessagePolicy: File`, because the default copies the log tail into the message that `cellctl` reads.

At the pod level:
- the template has no ephemeral containers;
- `dnsPolicy` is `ClusterFirst`, and there is no `dnsConfig`;
- `enableServiceLinks` is false, so the kubelet injects no environment outside the allowlist;
- each volume name is bound to its expected source: the data volume to the cell's own claim, and Secret volumes to `cell-credentials`.

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
- **Export integrity.** `age` does not prove who made a ciphertext, so `cellctl` could swap in a forged archive that the browser's key still decrypts. The cell therefore reports the archive's SHA-256 on a route that the gateway calls with a signed proof (D1), which excludes `cellctl`. Substrate shows that digest to the browser, and the browser checks it before it accepts the download.
- **Export verification.** The verify Job cannot read an archive encrypted to the browser's key. So the export Job checks the plaintext archive with the canonical reader as it streams, before encryption. The verify Job then checks the uploaded ciphertext's size and SHA-256 against what the cell reported.
- **Imports.** The serving cell publishes its import recipient on a route that the gateway calls with a signed proof (D1). `cellctl` cannot reach that port, so the path excludes it. Substrate puts the recipient into the signed import grant, and the cell checks that the grant's recipient is its own before it decrypts.

**Backups need no grant.** The cell derives its repository from its own cell ID and the pinned endpoint and bucket. `cell-backup` takes no destination.

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
- **Neither mode stops one thing:** whoever controls the node can read a running, unlocked cell. Only a confidential cell (`make-cloud-cells-operator-resistant` D8) closes that.

The steps:
- **Generate.** On first start, `exomem cell-init` generates the restic password and writes it to the cell's encrypted volume, at a path outside both backup roots (`/data/vault` and `/data/host`). Otherwise the password would sit inside the repository that it protects. It also stays outside the export root.
- **Escrow.** It writes an escrow copy, encrypted with `age` to the pinned escrow recipient (D2), to the cell's own prefix in the locked backup bucket (`lock-cloud-backups-against-deletion`). So no running service can destroy it before retention ends.
- **Back up.** `cell-backup` reads the password from the volume. The backup Job already mounts the volume.
- **Restore with the volume.** A restore onto an existing volume uses that password.
- **Restore after volume loss.** The operator decrypts the escrow copy with their presence-gated identity (`make-cloud-cells-operator-resistant` D6) and supplies the password to the restore through a one-time Secret.
- **What `cellctl` keeps.** It still mints per-cell object-store keys, so it can read ciphertext but cannot decrypt it.

**Existing cells.** Each existing cell re-keys its restic repository in four steps:
1. `restic key add` with a cell-generated password.
2. Write the escrow copy.
3. `restic key remove` for the platform password.
4. Destroy the platform-wrapped password.

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
- a Job or StatefulSet that the policy denied as an unknown kind.

Each alert carries only an identity, a cell ID and a reason token. It never carries content.

A compromised `cellctl` cannot suppress these signals, because they come from the API server and the cells, not from `cellctl`. It can flood them, though. The evaluator therefore coalesces by (cell ID, reason token), so a flood becomes one open alert for each class and cannot bury a different one.

### D8. Old attested images

The admission policy accepts any digest from the cell image repository. The attested-image verifier (`make-cloud-cells-operator-resistant` task 4.1) limits digests to release-attested ones. A compromised `cellctl` could still choose an older attested image with a known flaw.

This change adds a minimum release that the chart pins, so that admission refuses a rollback below it. This is a required task, not an option: without it, `cellctl` can roll a cell back to a release that lacks these checks.

The cellctl admission policy matches only the digest pattern, and it cannot order releases. The attested-image verifier (`make-cloud-cells-operator-resistant` task 4.1) therefore enforces the floor. It reads the release version from the attestation and refuses one below the pinned minimum.

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

1. **Cell-side checks in report mode.** The cell verifies proofs, grants and destinations and logs each would-be refusal, but does not refuse. Substrate starts signing grants.
2. **Command pin.** Add `cell-backup` and `cell-restore`, and render every Job through them. The admission rules for command, environment names and anchors enforce once the K3s Live job admits every rendered kind.
3. **Enforcement.** The cell refuses a request without a proof, and an operation without a grant or with a foreign destination.
4. **Backup custody.** Cells re-key one at a time, owner first. `cellctl` stops holding backup passwords.
5. **Retirements.** Retire the bearer, then the master keys' backup wrapping.
6. **Egress proxy** (D6).

## Open Questions

- The browser side of encrypted owner exports (D4): `age` in the browser, and how a tenant who loses the export key asks for a new export.
- Whether a tenant-initiated restore needs a confirmation step in Substrate before it signs a restore grant.
- The age-custody recovery path for the escrow identity, shared with `make-cloud-cells-operator-resistant` task 4.5.
