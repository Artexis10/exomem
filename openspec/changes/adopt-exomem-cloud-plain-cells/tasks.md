## 1. Specification and supersession (S)

- [x] 1.1 Get an independent critic review of this change and the companion Substrate change, and resolve every blocking finding in design.md
- [x] 1.2 Add a one-line superseded banner to the proposal of every change listed as superseded in design.md; do not change their tasks or specs
- [x] 1.3 Close the superseded hosted PRs with a one-line reason, and keep their branches

## 2. Cloud cell mode and image (lane A)

- [x] 2.1 Write red-first tests. With `EXOMEM_CLOUD_CELL=1`:
  - the server authenticates only the current or previous configured bearer, in constant time;
  - the authenticated principal is the fixed non-owner `{sub: cell_id, iss: "exomem-cloud-cell"}`;
  - `/api/*`, `/upload` and `/download` are absent;
  - `/health` and `/health/ready` answer without content;
  - no `.env` file is read.
- [x] 2.2 Add `CloudCellTokenVerifier` to `server_auth.py`, and add the cloud branch of `build_server` in `server.py` on the standalone path (D1)
- [x] 2.3 Add `CLOUD_SURFACE_EXCLUSIONS` (`transfer_artifact`, `adopt_vault`, `process_media`, `read_media`, each with a reason and a lifting condition) and remove its members from the served surface. Test the listed surface.
- [x] 2.4 Content-free logging in cloud mode:
  - extend `content_private_logging_enabled` to cloud mode and install the redaction hook;
  - use the content-free call trace;
  - turn the query, read and write journals off under content-private logging, in `query_log` itself, and disable usage boost and the relevance check (D1);
  - keep call-ledger rows content-free under content-private logging (D1);
  - test, with `EXOMEM_DISABLE_EMBEDDINGS` removed from the environment, that neither a distinctive query phrase nor a distinctive note-path phrase reaches runtime logs, access logs or any journal file.
- [x] 2.5 Read-only mode: `EXOMEM_CLOUD_READ_ONLY=1` refuses every mutating command with `CLOUD_CELL_READ_ONLY`, classified from the command registry, while reads keep working. Test one read and one write per command class.
- [x] 2.6 Add a `cloud` Dockerfile target derived from the `hosted` runtime stage. It carries:
  - the offline ONNX model environment and `EXOMEM_DISABLE_RANKING`;
  - UID/GID 10001 with home `/data/host`;
  - `restic` 0.17 or later, for the backup and restore Jobs;
  - the command `exomem --transport http --host 0.0.0.0 --port 8765`.
  Prove standalone custody resolves under `/data/host` in the built image.
- [x] 2.7 Add the `cell-init` entrypoint (D3). It runs, idempotently:
  1. `/data/vault` and `/data/host` at mode `0700`;
  2. atomic vault init, through a staging directory and a rename, when absent;
  3. `maintain --migrate-state --offline`.
  It runs no governance schema migration and sets no custody environment. It installs the redaction hook and reports failure as one JSON line with a stable error code. Test a fresh volume, a fresh volume under a setgid root, a second run with no changes, a run interrupted after init, and runs interrupted during init.
- [x] 2.8 A committed container test, gated on Docker and an opt-in variable, on the built image with `--read-only`, an `/tmp` tmpfs, UID 10001 and the volume root at `2770` as fsGroup leaves it:
  1. init;
  2. first start with only the D1 environment;
  3. a governed write;
  4. container replacement on the same volume;
  5. owner-only modes still intact;
  6. exact and paraphrased recall of the write;
  7. a further governed write;
  8. `/health/ready` not ready until retrieval is admitted;
  9. no container log line, and no log or journal file (the log directory, or any `*.log` or `*.jsonl` outside `/data/vault`), contains the distinctive query or note-path phrase.
- [x] 2.9 Add a CI job that publishes the `cloud` image to GHCR by digest on release tags, and records tag and digest in the release notes

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
  - the gateway Deployment and Service consuming the Substrate gateway image digest, rendering exactly the gateway's environment contract (C3), with a chart test pinning that set;
  - the cellctl image, built from `infra/cellctl/Dockerfile` (digest-pinned base, non-root, read-only root filesystem) and published to GHCR by digest on the same release trigger as the `cloud` image, which the chart consumes by digest;
  - the `exomem-cloud-encrypted` StorageClass;
  - Traefik `websecure` on hostPort 443 only, with no `trustedIPs`;
  - a NetworkPolicy admitting gateway ingress only from the Traefik pods;
  - cert-manager with a Cloudflare DNS-01 issuer and the gateway certificate and IngressRoute, whose middleware sets the gateway's trusted-ingress source header;
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

- [x] 4.1 Terraform: `hcloud_server.control` attached to the existing `hcloud_network.alpha`, firewall rules for SSH and the PgBouncer TLS port, and DNS-only records
- [x] 4.2 Ansible `postgres` role:
  - PostgreSQL 17 and PgBouncer in transaction mode, plus a session-mode alias for `substrate_owner` migrations;
  - the public certificate issued and renewed on the server through ACME DNS-01, with a reload timer;
  - a public TLS certificate for verify-full;
  - roles `substrate_owner`, `substrate_app`, `exomem_gateway` and `exomem_cellctl`;
  - the public listener admitting only the Substrate roles with SCRAM, and gateway and cellctl direct to Postgres on the private network, never through PgBouncer;
  - PgBouncer per-role and global connection limits (`max_user_connections`, `max_client_conn`, `client_login_timeout`);
  - nftables connection limits (explicit burst);
  - pgBackRest to B2 with WAL archiving, a nightly full and a weekly restore verification.
- [x] 4.3 Disposable-VM or container test of the role, all live in
  `tests/test_hosted_control_db_role_live.py`
  (`RUN_CONTROL_DB_ROLE_TEST=1 ANSIBLE_PLAYBOOK_BIN=$PWD/.venv-hosted-ci/bin/ansible-playbook uv run python -m pytest -q tests/test_hosted_control_db_role_live.py`,
  with the pinned toolchain -- `.venv-hosted-ci` built from `ansible-core==$ANSIBLE_CORE_VERSION`
  plus the collections in `infra/ansible/collections/requirements.yml`, matching CI's own
  `hosted-infrastructure.yml` -- 16 passed):
  - a verify-full TLS connection for each role;
  - public refusal of the gateway and cellctl roles, asserting each side's actual refusal
    wording (PgBouncer's own `no authentication method is found` through the public listener;
    Postgres's own `permission denied for`/`Connection refused` elsewhere -- confirmed live that
    PgBouncer does not reuse Postgres's wording for an HBA miss);
  - refused cross-role writes;
  - the PgBouncer admin console's unix-socket admission is `peer`, not `trust` (a mode-777
    socket directory means any local OS user could otherwise reach it): a non-postgres OS user
    is refused with PgBouncer's own `unix socket login rejected`, postgres itself is admitted;
  - a pgBackRest backup and restore round trip (OM-6, round 6): a full
    backup to the same MinIO the rig stands up, then the role's own real
    weekly restore-verify script, unmodified, restoring and round-tripping a
    marker row written before the backup, with its throwaway instance's own
    postmaster confirmed to hold no TCP-listening socket at all
    (`listen_addresses=''` held, checked against that specific process, not
    just the rendered config).
- [x] 4.4 The role creates the four roles plus the passwordless, peer-only `pgbouncer_auth` lookup role only. Table privileges come from Substrate's `scripts/exomem-cloud-grants.sql`, the single implementation of the C1 privilege table (Substrate D7 and task 3.2). The role test applies that script to the migrated schema and asserts the table:
  applied live in `tests/test_hosted_control_db_role_live.py` against
  `infra/cellctl/tests/fixtures/exomem_cloud_schema.sql` (Substrate migrations
  0056+0057, byte-for-byte) and `exomem_cloud_grants.sql` (Substrate's real
  script, byte-for-byte) -- cellctl updates C1 observed columns only, never
  desired or C1b; the gateway selects only `cell_id`, `tenant_id`,
  `desired_state` and writes nothing on C1-C1d; `substrate_app` updates
  `cancellation_notice_sent_at`; a second grants run changes nothing
  (`pg_class.relacl`/`pg_attribute.attacl` snapshot equality, not just a
  clean exit code).

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

- [ ] 6.1 Scale the old platform's in-cluster gateway, provisioner and workers to zero before the window (Substrate D8 step 1). Then apply the control server and run the database cutover under the Substrate runbook, which sets Neon read-only, lists every consumer, restores with `--no-owner --no-acl`, and runs the grants script. Then verify Endstate and Exomem.
  - Role apply on the control server starts every unit; `systemctl is-active` for postgresql, pgbouncer, the certbot and pgbackrest timers.
  - A pgbackrest full backup to B2 completes and the restore-verify timer's script passes against B2 before the Neon cutover.
- [ ] 6.2 Deploy cellctl and the gateway beside the old platform, and set `cell_image`
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
