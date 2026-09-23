## 1. Specification and supersession (S)

- [x] 1.1 Get an independent critic review of this change and the companion Substrate change, and resolve every blocking finding in design.md
- [x] 1.2 Add a one-line superseded banner to the proposal of every change listed as superseded in design.md; do not change their tasks or specs
- [x] 1.3 Close the superseded hosted PRs with a one-line reason, and keep their branches

## 2. Cloud cell mode and image (lane A)

- [ ] 2.1 Write red-first tests. With `EXOMEM_CLOUD_CELL=1`:
  - the server authenticates only the current or previous configured bearer, in constant time;
  - the authenticated principal is the fixed non-owner `{sub: cell_id, iss: "exomem-cloud-cell"}`;
  - `/api/*`, `/upload` and `/download` are absent;
  - `/health` and `/health/ready` answer without content;
  - no `.env` file is read.
- [ ] 2.2 Add `CloudCellTokenVerifier` to `server_auth.py`, and add the cloud branch of `build_server` in `server.py` on the standalone path (D1)
- [ ] 2.3 Add `CLOUD_SURFACE_EXCLUSIONS` (`transfer_artifact`, `adopt_vault`, `process_media`, `read_media`, each with a reason and a lifting condition) and remove its members from the served surface. Test the listed surface.
- [ ] 2.4 Content-free logging in cloud mode:
  - extend `content_private_logging_enabled` to cloud mode and install the redaction hook;
  - use the content-free call trace;
  - test that a distinctive query phrase never reaches runtime or access logs.
- [ ] 2.5 Read-only mode: `EXOMEM_CLOUD_READ_ONLY=1` refuses every mutating command with `CLOUD_CELL_READ_ONLY`, classified from the command registry, while reads keep working. Test one read and one write per command class.
- [ ] 2.6 Add a `cloud` Dockerfile target derived from the `hosted` runtime stage. It carries:
  - the offline ONNX model environment and `EXOMEM_DISABLE_RANKING`;
  - UID/GID 10001 with home `/data/host`;
  - `restic` 0.17 or later, for the backup and restore Jobs;
  - the command `exomem --transport http --host 0.0.0.0 --port 8765`.
  Prove standalone custody resolves under `/data/host` in the built image.
- [ ] 2.7 Add the `cell-init` entrypoint (D3). It runs, idempotently:
  1. vault init when absent;
  2. `maintain --migrate-state --offline`;
  3. the governance-schema v3-to-v4 plan, stage and commit sequence, using each step's JSON digest.
  Test a fresh volume, a second run with no changes, a crash between stage and commit (re-entry re-plans and completes), and a crash during commit (the step fails with the status code).
- [ ] 2.8 Container test on the built image with `--read-only`, an `/tmp` tmpfs and UID 10001:
  1. init;
  2. first start;
  3. a governed write;
  4. container replacement on the same volume;
  5. owner-only modes still intact;
  6. recall of the write;
  7. a further governed write;
  8. governance status at v4.
- [ ] 2.9 Add a CI job that publishes the `cloud` image to GHCR by digest on release tags, and records tag and digest in the release notes

## 3. cellctl, manifests and ingress (lane B)

- [ ] 3.1 Scaffold `infra/cellctl/` as a Python package with its own pyproject and tests, plus `tests/fixtures/exomem_cloud_schema.sql` copied from Substrate migration `0056` (every C1 column, including the write-once key columns and the hold columns) and the grants script
- [ ] 3.2 Write red-first unit tests for the rendered manifests (D5):
  - Pod Security labels, quota, and a default-deny policy with gateway-only ingress;
  - no runtime egress (not even DNS), and backup egress limited to 443 plus DNS;
  - no ServiceAccount token, `readOnlyRootFilesystem`, the `/tmp` emptyDir, and `fsGroup` 10001 with `fsGroupChangePolicy: OnRootMismatch`;
  - the `cell-init` init container;
  - readiness on `/health/ready`, liveness on `/health`;
  - the encrypted `reclaimPolicy: Delete` class.
- [ ] 3.3 Implement the reconcile loop (D4):
  - poll, plus `LISTEN` on a direct session;
  - server-side apply under field manager `cellctl`;
  - readiness from pod conditions;
  - rendering the current image on every ordinary pass;
  - the `exomem.io/hold` marker pinning image and replicas, recorded on the row and resumed after a restart;
  - observed writes and generation matching;
  - transient retry, identity-conflict failure and the init deadline.
  Test against a disposable Postgres with the fixture schema.
- [ ] 3.4 Implement the rollout (D6) and prove each property:
  - target and initial-image selection, including `last_good_image` set at first provisioning and `NO_GOOD_IMAGE` while paused with none;
  - one attempt at a time, by priority;
  - stop, then backup, then start, with annotations as attempt state;
  - a backup deadline that restarts the cell on its current image with `BACKUP_FAILED`;
  - on timeout, stop, restore the snapshot with `restic restore --delete`, return to the previous image, and pause;
  - a failed restore holding the cell stopped with `RESTORE_FAILED` and retrying;
  - resumption of each hold after a cellctl restart;
  - no flap while paused.
- [ ] 3.5 Implement secrets (D7):
  - the C4 bearer, with current and previous versions;
  - per-cell data keys, AES-GCM envelope, versioned, with a write-once `backup_key_wrapped`;
  - per-cell prefix-restricted B2 keys stored write-once as `b2_key_id` and `b2_key_wrapped`, deleting the losing key on a race;
  - Secrets rendered from the row on every pass, never read back.
  Test that plaintext keys never reach the database, and that one cell's key cannot read another cell's prefix.
- [ ] 3.6 Implement backups (D8):
  - the nightly stop-backup-start window under a `backup` hold, with its deadline;
  - the pre-upgrade backup;
  - Jobs on the cell image as UID 10001 with the pod-level `fsGroup` and a cache emptyDir;
  - retention and `last_backup_*` writes;
  - a restore Job using `--delete`;
  - the operator export runbook: restore into a scratch namespace and produce the tenant's vault archive.
- [ ] 3.7 Implement verified deletion (D10) in this order:
  1. namespace;
  2. PV and Hetzner `volume_id`;
  3. every B2 object version under the prefix;
  4. the per-cell B2 key, by `b2_key_id`;
  5. the wrapped keys.
  A failed observation stays `deleting`. Document the etcd snapshot residual in the runbook.
- [ ] 3.8 Implement capacity publication (D9), with `cell_slots` = limit − headroom − non-cell attachments
- [ ] 3.9 Add platform chart entries:
  - cellctl, single replica, `Recreate`, with the RBAC in D4 and the `ValidatingAdmissionPolicy` on its ServiceAccount (cell namespaces only, restricted labels, digest-pinned images from the cell repository);
  - the gateway Deployment and Service consuming the Substrate gateway image digest;
  - the `exomem-cloud-encrypted` StorageClass;
  - Traefik `websecure` on hostPort 443 only, with no `trustedIPs`;
  - a NetworkPolicy admitting gateway ingress only from the Traefik pods;
  - cert-manager with a Cloudflare DNS-01 issuer and the gateway certificate and IngressRoute;
  - SOPS-sourced Secrets.
- [ ] 3.10 Integration test on disposable K3s:
  - create, then pod kill with a governed write after it;
  - owner-only modes asserted after first start, pod replacement and restore;
  - stop, resume and read-only;
  - an upgrade, and a forced canary failure with restore-based return;
  - a desired-state flip during a nightly backup hold, a cellctl kill mid-upgrade, and an object-storage outage during a pre-upgrade backup;
  - cross-cell network denial and runtime egress denial;
  - admission denial of an out-of-scope cellctl write;
  - deletion with absence proofs.

## 4. Control database server (lane D)

- [ ] 4.1 Terraform: `hcloud_server.control` attached to the existing `hcloud_network.alpha`, firewall rules for SSH and the PgBouncer TLS port, and DNS-only records
- [ ] 4.2 Ansible `postgres` role:
  - PostgreSQL 17 and PgBouncer in transaction mode, plus a session-mode alias for `substrate_owner` migrations;
  - the public certificate issued and renewed on the server through ACME DNS-01, with a reload timer;
  - a public TLS certificate for verify-full;
  - roles `substrate_owner`, `substrate_app`, `exomem_gateway` and `exomem_cellctl`;
  - the public listener admitting only the Substrate roles with SCRAM, and gateway and cellctl only on the private network;
  - nftables connection limits and fail2ban;
  - pgBackRest to B2 with WAL archiving, a nightly full and a weekly restore verification.
- [ ] 4.3 Disposable-VM or container test of the role:
  - a verify-full TLS connection for each role;
  - public refusal of the gateway and cellctl roles;
  - refused cross-role writes;
  - a pgBackRest backup and restore round trip.
- [ ] 4.4 The role creates the four roles only. Table privileges come from Substrate's `scripts/exomem-cloud-grants.sql`, the single implementation of the C1 privilege table (Substrate D7 and task 3.2). The role test applies that script to the migrated schema and asserts the table

## 5. Local rehearsal (P3)

- [ ] 5.1 Write one command that stands up disposable K3s, Postgres with the real Substrate migrations, Substrate, the gateway, cellctl and the real cell image
- [ ] 5.2 The command runs:
  1. invite;
  2. provision, in under 3 minutes;
  3. OAuth MCP `tools/list`;
  4. capture;
  5. paraphrased cited recall;
  6. governed write after a pod kill;
  7. upgrade and recall;
  8. forced canary failure, with restore-based return;
  9. read-only mode;
  10. second-tenant denial;
  11. backup and scratch restore, with a governed write;
  12. deletion with absence proofs.
  It writes a machine-readable report.
- [ ] 5.3 Measure warm p95 initialize and list, capture and cited recall, provisioning time and per-cell upgrade time, and record them in the report

## 6. Node deployment and owner acceptance (P4)

- [ ] 6.1 Apply the control server and run the database cutover under the Substrate runbook, which sets Neon read-only, lists every consumer, restores with `--no-owner --no-acl`, and runs the grants script. Then verify Endstate and Exomem.
- [ ] 6.2 Deploy cellctl and the gateway beside the old platform, set `cell_image`, and scale the old provisioner and workers to zero
- [ ] 6.3 Owner acceptance on the real node:
  1. invite and connect the claude.ai custom connector;
  2. capture;
  3. cited recall;
  4. pod restart and recall;
  5. image upgrade and recall;
  6. backup present in B2;
  7. token refresh after 15 minutes.

## 7. Retirement (R)

- [ ] 7.1 Delete the hosted-only runtime modules and their tests, provisioner v1, the old platform chart pieces, deployment locks, plugin candidates and `cloudflared`
- [ ] 7.2 Remove the superseded change directories, and remove the `hosted-*` canonical specs in the same delivery as the code they describe
- [ ] 7.3 After seven clean days, delete the Neon project, and retire merged worktrees and branches
