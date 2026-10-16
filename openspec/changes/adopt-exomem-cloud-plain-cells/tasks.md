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

- [x] 3.1 Scaffold `infra/cellctl/` as a Python package with its own pyproject and tests, plus `tests/fixtures/exomem_cloud_schema.sql` copied from Substrate migration `0056` (every C1 column, including the write-once key columns and the hold columns) and the grants script
  Evidence (#1368): `infra/cellctl/pyproject.toml`; `tests/test_fixture_schema.py` (`test_generation_bumps_only_on_desired_column_change`, `test_cellctl_role_cannot_write_desired_columns`, `test_gateway_role_cannot_write_desired_state`); fixtures refreshed from Substrate `2b38730` (#1387).
- [x] 3.2 Write red-first unit tests for the rendered manifests (D5):
  - Pod Security labels, quota, and a default-deny policy with gateway-only ingress;
  - no runtime egress (not even DNS), and backup egress limited to 443 plus DNS;
  - no ServiceAccount token, `readOnlyRootFilesystem`, the `/tmp` emptyDir, and `fsGroup` 10001 with `fsGroupChangePolicy: OnRootMismatch`;
  - the `cell-init` init container;
  - readiness on `/health/ready`, liveness on `/health`;
  - the encrypted `reclaimPolicy: Delete` class.
  Evidence (#1368, #1396): `tests/test_manifests.py` covers security labels, quota, network isolation, runtime mounts, init, probes and PVC class selection.
  `tests/test_platform_chart_headers.py::test_cloud_storage_class_uses_the_encryption_secret_and_deletes_volumes` covers the rendered encrypted `Delete` class.
  [Cloud cellctl run 38328922734](https://github.com/Artexis10/exomem/actions/runs/38328922734/job/115073944357) passed both suites on exact release source `886a0a9`.
  Its chart step rejects skipped cases. The isolated original-image qualification also passed the manifest cases on 2026-10-13.
- [x] 3.3 Implement the reconcile loop (D4):
  - poll, plus `LISTEN` on a direct session;
  - server-side apply under field manager `cellctl`;
  - readiness from pod conditions;
  - rendering the cell's current image whenever ordinary convergence requires an apply;
  - the `exomem.io/hold` marker pinning image and replicas, recorded on the row and resumed after a restart;
  - observed writes and generation matching;
  - transient retry, identity-conflict failure and the init deadline.
  Test against a disposable Postgres with the fixture schema.
  Evidence (#1368): `tests/test_reconcile.py` covers notification reconnects, holds, readiness changes, retries and the init deadline against disposable Postgres.
  `tests/test_decide.py`, `tests/test_db.py` and `tests/test_k8s_client.py` cover current-image selection, generation matching, observed-column writes and server-side apply.
  [Cloud cellctl run 38328922734](https://github.com/Artexis10/exomem/actions/runs/38328922734/job/115073944357) passed 504 fast cases, with two skips, on exact release source `886a0a9`.
  The settled-row optimization in D4 avoids redundant applies; required renders still preserve the cell's current image.
- [x] 3.4 Implement the rollout (D6) and prove each property:
  - target and initial-image selection, including `last_good_image` set at first provisioning and `NO_GOOD_IMAGE` while paused with none;
  - one attempt at a time, by priority;
  - stop, then backup, then start, with annotations as attempt state;
  - a backup deadline that restarts the cell on its current image with `BACKUP_FAILED`;
  - on timeout, stop, restore the snapshot with `restic restore --delete`, return to the previous image, and pause;
  - a failed restore holding the cell stopped with `RESTORE_FAILED` and retrying;
  - resumption of each hold after a cellctl restart;
  - no flap while paused.
  Evidence (#1368): `tests/test_rollout.py`, `tests/test_decide.py`, `tests/test_jobs.py` and `tests/test_k8s_client.py` cover these source properties.
  The isolated original 0.116 controller image passed all 234 selected boundary cases on 2026-10-13, including those rollout cases.
  Failed-restore, absent-Job retry and recorded-restoration cases jointly prove recovery without repeating Kubernetes TTL behavior.
  This closes implementation and property-test scope; production rollout and recovery acceptance remain in section 6.
- [x] 3.5 Implement secrets (D7):
  - the C4 bearer, with current and previous versions;
  - per-cell data keys, AES-GCM envelope, versioned, with a write-once `backup_key_wrapped`;
  - per-cell prefix-restricted B2 keys stored write-once as `b2_key_id` and `b2_key_wrapped`, deleting the losing key on a race;
  - tenant Secrets rendered from the row whenever an apply is required, never read back.
  Test that plaintext keys never reach the database, and that one cell's key cannot read another cell's prefix.
  Evidence (#1368, #1422): `tests/test_secrets.py` and `tests/test_db.py` cover bearer overlap, authenticated envelopes and atomic, wrapped-only persistence.
  `tests/test_reconcile.py` covers losing-key deletion and rendering tenant Secrets from stored rows; `tests/test_b2.py` checks the requested bucket and cell prefix.
  Backblaze's [application-key contract](https://www.backblaze.com/apidocs/b2-create-key) restricts access to the requested prefix; [older Native API versions remain supported](https://www.backblaze.com/docs/cloud-storage-native-api-versions).
  The exact-source fast suite above passed. Prefix isolation combines the tested adapter request with that provider contract, without claiming a live cross-prefix refusal probe.
- [x] 3.6 Implement backups (D8):
  - the nightly stop-backup-start window under a `backup` hold, with its deadline;
  - the pre-upgrade backup;
  - Jobs on the cell image as UID 10001 with the pod-level `fsGroup` and a cache emptyDir;
  - retention and `last_backup_*` writes;
  - a restore Job using `--delete`.
  The operator export runbook moved to 6.4 (deferred).
  Evidence (#1368): `tests/test_decide.py` (`test_nightly_backup_due_respects_the_window_the_interval_and_the_backoff`, `test_nightly_backup_deadline_miss_restarts_cell_with_backup_failed_and_sets_backoff`, `test_a_successful_pre_upgrade_backup_also_records_last_backup`), `tests/test_jobs.py` (`test_backup_job_runs_as_uid_10001_with_fsgroup`, `test_backup_job_mounts_the_volume_read_only_with_a_cache_emptydir`, `test_backup_job_backs_up_vault_and_host_with_retention`, `test_restore_job_uses_delete_and_writes_the_volume`); the live K3s suite, run 36199720123 on `feat/cloud-cellctl` `24fcb49`, the head that merged as #1368.
- [x] 3.7 Implement verified deletion (D10) in this order:
  1. namespace;
  2. PV and Hetzner `volume_id`;
  3. every B2 object version under the prefix;
  4. the per-cell B2 key, by `b2_key_id`;
  5. the wrapped keys.
  A failed observation stays `deleting`. Document the etcd snapshot residual in the runbook.
  Evidence (#1368, #1395, #1402): `tests/test_reconcile.py` and `tests/test_decide.py` cover ordered absence checks and retaining wrapped keys after failed observations.
  `tests/test_b2_pagination.py` covers every object version and refuses incomplete listings; `docs/runbooks/cloud-cell-deletion.md` records the etcd snapshot residual.
  The exact-source fast suite above passed. Task 3.10 records disposable-cluster deletion acceptance.
- [x] 3.8 Implement capacity publication (D9), with `cell_slots` = limit − headroom − non-cell attachments
  Evidence (#1368): `capacity.py`; `tests/test_capacity.py` (`test_cell_slots_subtracts_headroom_and_non_cell_attachments`, `test_fallback_limit_is_used_when_the_node_has_no_allocatable_count`), `tests/test_db.py::test_write_capacity_inserts_then_updates`, `tests/test_reconcile.py::test_a_vanished_node_gets_zero_slots_and_a_rejoining_node_its_count_back`.
- [ ] 3.9 Add platform chart entries:
  - cellctl, single replica, `Recreate`, with the RBAC in D4 and the `ValidatingAdmissionPolicy` on its ServiceAccount (cell namespaces only, restricted labels, digest-pinned images from the cell repository);
  - the gateway Deployment and Service consuming the Substrate gateway image digest, rendering exactly the gateway's environment contract (C3), with a chart test pinning that set;
  - the certificate hostname resolving through public DNS in the gateway and cellctl pods, with the database's public `/32` in their egress; preserve tenant admission, and verify both image clients' TLS hostname and trust checks before sealing role DSNs;
  - the cellctl image, built from `infra/cellctl/Dockerfile` (digest-pinned base, non-root, read-only root filesystem) and published to GHCR by digest on the same release trigger as the `cloud` image, which the chart consumes by digest;
  - the `exomem-cloud-encrypted` StorageClass;
  - Traefik `websecure` on hostPort 443 only, with no `trustedIPs`;
  - a NetworkPolicy admitting gateway ingress only from the Traefik pods;
  - cert-manager with a Cloudflare DNS-01 issuer and the gateway certificate and IngressRoute, whose middleware sets the gateway's trusted-ingress source header;
  - SOPS-sourced Secrets.
  - retire legacy Hosted workloads and hooks; the Cloud lifecycle scheduler stays active while either Cloud control workload is enabled and suspends when both are disabled;
  - Extend the existing SOPS handoff, ciphertext validation and apply path to accept complete, exact-key-set Cloud platform Secret bundles while retaining scalar destinations; test bundle rotation shapes and reject incomplete, malformed or plaintext artifacts before publication or apply.
- [x] 3.9a Classify every hosted scheduler job explicitly in chart values and render `exomem-reconcile` unsuspended whenever either Cloud control workload is enabled, while legacy-only jobs remain suspended; cover Cloud-on, legacy-pause-only and all-off renders.
- [x] 3.9b Before deploying the lifecycle scheduler chart change, gate Substrate's legacy hosted lifecycle lane inside `/api/cron/exomem-reconcile` when Cloud is enabled, while retaining the Paddle and Cloud lanes. Substrate #190 is deployed; the authenticated production sweep returned zero legacy operations and a successful Cloud reconciliation on 2026-09-30.
- [x] 3.9c Deploy the scheduler chart after the Substrate gate and verify a successful scheduled Cloud sweep while every legacy-only job remains suspended. Exomem #1490 (`9cdcea434`) deployed as platform revision 71 on 2026-09-30; the actual manifest matched the reviewed server dry-run, changing only reconcile `suspend: true` to `false`. The first scheduled job completed successfully at 19:50:15 UTC, and the scheduler ledger recorded a fresh success with zero consecutive failures.
- [x] 3.10 Integration test on disposable K3s:
  - create, then pod kill with a governed write after it;
  - owner-only modes asserted after first start, pod replacement and restore;
  - stop, resume and read-only;
  - an upgrade, and a forced canary failure with restore-based return;
  - a desired-state flip during a nightly backup hold, a cellctl kill mid-upgrade, and an object-storage outage during a pre-upgrade backup;
  - cross-cell network denial and runtime egress denial;
  - admission denial of an out-of-scope cellctl write;
  - deletion with absence proofs.
  Evidence ([#1746](https://github.com/Artexis10/exomem/pull/1746), merged 2026-10-14): the [disposable K3s suite](https://github.com/Artexis10/exomem/actions/runs/38480038320/job/115581227399) passed 15 cases with zero skips in 1,423.34 seconds.
  The [Cloud rehearsal](https://github.com/Artexis10/exomem/actions/runs/38480038348/job/115581697400) passed all 12 steps with zero failures or blocked steps and `gates_node: true`.
  The rehearsal interrupts the actual disposable controller mid-canary and verifies the held timestamp through later reconciliation, restore and a committed write.
  Lifecycle proof explicitly stops and resumes the cell while retaining its PVC, data and owner-only modes.
  This closes disposable-controller acceptance; production owner and fleet acceptance remain in section 6.
  The original harness execution timeout cause remains unknown; this proof does not claim a timeout repair.

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

- [x] 5.1 Write one command that stands up disposable K3s, Postgres with the real Substrate migrations, Substrate, the gateway, cellctl and the real cell image
- [x] 5.2 The command runs:
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
- [x] 5.3 Measure warm p95 initialize and list, capture and cited recall, provisioning time and per-cell upgrade time, and record them in the report. The targets that gate the node:
  - warm p95 `initialize` and `tools/list` at most 500 ms each;
  - p95 capture and cited recall at most 1 s each;
  - provisioning under 3 minutes (5.2);
  - an upgrade under 60 s per cell, including its pre-upgrade backup.

  The command is `infra/cloud-rehearsal` (`uv run --frozen exomem-cloud-rehearsal run`). The `Cloud rehearsal` workflow runs it on a GitHub runner with the cell image built from `Dockerfile --target cloud`. These boxes record that the command, its steps and its measurements exist. They do not record that the node gate passed: that is the report's `outcome.gates_node`, which a manual dispatch of the workflow must show true before P4.

  First run on a real image (#1378, head `65bad18`): 9 of 12 steps passed. Steps 9 and 10 failed on the runtime's readiness after a read_only → running recovery; #1386 fixed it.

  Run on `main` with #1368, #1384 and #1386 and no product overlays (#1378, head `78957a8`): 11 of 12 steps passed and `gates_node` was false. What keeps the node gate closed:
  - step 3, Substrate's PKCE verifier grammar, a cross-lane finding and the only entry in `infra/cloud-rehearsal/known-findings.json`;
  - capture p95 of 1.71 s against its 1 s target.

  All other targets were met: provisioning 24.7 s, upgrade 46.0 s, warm `initialize` p95 0.017 s, `tools/list` p95 0.050 s, cited recall p95 0.44 s.

  Run with Substrate `2b38730` (#181 and #183) and the stock MCP Python SDK OAuth client (#1387, head `4366d87`): 12 of 12 steps passed, `gate_blockers` was empty and `gates_node` was true on the PR's harness check. Provisioning 24.7 s, upgrade 46.9 s, warm `initialize` p95 0.019 s, `tools/list` p95 0.061 s, cited recall p95 0.77 s; capture p95 2.12 s, informational per 5.4. 5.4 still needs the manual dispatch on `main`.
- [x] 5.4 A manual dispatch of the `Cloud rehearsal` workflow on `main`, with the cell image built from `Dockerfile --target cloud`, writes a valid report with `outcome.gates_node` true. P4 does not start until it does.
  Ruling: on a CI runner, capture p95 is reported as measured and informational and does not hold `gates_node`; its 1 s target is for the node, where it is re-measured at P4 owner acceptance (6.3), and a runtime lane takes it if it misses there.

  Dispatched on `main` at `45048816` (run 36225283096, 2026-09-26): 12 of 12 steps passed, `gate_blockers` was empty and `gates_node` was true. Provisioning 25.7 s, upgrade 53.9 s per cell, warm `initialize` p95 0.019 s, `tools/list` p95 0.056 s, cited recall p95 0.52 s; capture p95 2.03 s, informational per the ruling above.

## 6. Node deployment and owner acceptance (P4)

- [ ] 6.0 Prepare the migration packet before any live deployment or hostname change:
  - identify the personal service manager, interpreter, state root and locally managed tunnel configuration without modifying them;
  - record both MCP URLs and all personal connector clients, with read/write and reauthentication checks;
  - record canonical OAuth issuer/resource and endpoints; prepare fresh authorization and refresh acceptance independent of the hostname Cloud will take, including existing registration/session transition;
  - prepare an opt-in private Cloud bucket and separately named controller key in the existing business B2 account and durability HCP workspace; record the parent key's account-wide authority and tenant bucket/prefix restrictions; keep provider/master credentials out of cellctl and leave provisioning unapplied;
  - prepare every required platform Secret destination in a private matrix draft; seal exact key sets and verify recovery custody without activating them; publish the reconciled matrix, selection, ciphertext and signed registry together rather than invalidating the active registry with a matrix-only change;
  - resolve published image digests from the final reviewed release and render the complete platform change against captured live values;
  - prepare original DNS/tunnel snapshots, Terraform ownership reconciliation and phase-specific rollback;
  - retain the preparation-only boundary until the owner agrees a deployment/cutover window.
- [ ] 6.1 At the start of the agreed deployment window, scale the old platform's in-cluster gateway, provisioner and workers to zero (Substrate D8 step 1). Then apply the control server and run the database cutover under the Substrate runbook, which sets Neon read-only, lists every consumer, restores with `--no-owner --no-acl`, and runs the grants script. Then verify Endstate and Exomem. If the database cutover has already completed, verify its closure evidence and current live state rather than repeating it.
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
- [x] 6.4 Operator export runbook: restore into a scratch namespace and produce the tenant vault archive; needed before P5 friends.
  Evidence: [#1409](https://github.com/Artexis10/exomem/pull/1409) delivered [the export procedure](../../../docs/runbooks/cloud-operator-export.md); #1418 added break-glass access.
  The procedure binds the tenant, snapshot and recipient set, streams only the vault to age, and verifies recipient decryption before scratch cleanup.
  The runbook records a successful 2026-09-28 export using the earlier admin identity.
  Task 6.12 retains current-release acceptance through the current operator identity; this closes the delivered procedure only.

### Launch and resilience roadmap

Current evidence separates healthy service from recovery and launch acceptance.
On 2026-10-13, isolated qualification accepted the published 0.116 Cloud and cellctl images by original digest, source and provenance.
That qualification alone does not prove a production rollout, OAuth refresh or owner-sized admission.

On 2026-10-13, the operator applied the signed platform Secret set and deployed Helm revision 84.
All 71 retired objects were absent, the retained capacity receipt remained present, and the controller and gateway were Ready.
On 2026-10-14, the remaining rollout received window approval, and the owner cell became Ready on qualified 0.116.
Fresh runtime verification confirmed owner-only roots, UID/GID 10001, zero container restarts and zero OOM events.
Requests remain `1Gi/250m`, with limits of `3Gi/2CPU`.

Authenticated capture, exact and paraphrased recall, and the content-free log canary now pass after connector reconnection.
Owner performance and memory headroom acceptance remain open.
The existing ChatGPT connection refreshed over 15 minutes after sign-in and read back the unchanged canary on 2026-10-14.
Rotation metadata binds the consumed refresh token, its replacement and the new access-token expiry.
This closes that client's refresh check; Claude-specific acceptance, node latency and owner-sized admission remain unproved.
QA and reviewer cells also became Ready on qualified 0.116; owner-only custody and authenticated MCP reads passed with unchanged resources.
The fleet default retains its previous image, and the two stopped cells remain stopped and unpinned.
Capacity qualification still holds fleet-default promotion and friend admission.
Shared selection remains off. Warm-peak qualification remains open; current capacity charges scheduler requests rather than measured peaks.

A deployed-image B2 backup and scratch restore matched both vault and host state, with scratch cleanup verified.
That proof does not close in-place restore, historical rollback, connector sign-in or token refresh.
Existing per-cell pins do not prove that the fleet default selects the qualified release.
The Neon cutover and project deletion are closed; do not repeat them.
Tasks 6.0–6.3 stay unchecked until their exact closure evidence is reconciled.

- [x] 6.5 Resilience 1: make B2 restore verification repeatable through `infra/scripts/cloud_restore.sh drill` and its existing renderer.
  Evidence from 2026-10-09: scratch restores matched 45/45 and 9,324/9,324 vault files.
  That historical proof did not compare `/data/host` runtime state.
  The reviewed [helper correction](https://github.com/Artexis10/exomem/pull/1704) compares every declared backup root; its 20 scoped cases passed.
  On 2026-10-13, maintained helpers restored a snapshot correlated with stopped-source references: all 45 vault and 116 host entries matched.
  Custody, service authentication and scratch absence checks passed.
  The current top-level wrapper was not invoked; its source wiring, existing tests and maintained-helper recovery establish repeatability.
  This closes repeatable dual-root comparison and deployed-image scratch recovery, without claiming a current-wrapper production run, in-place recovery or historical rollback.
- [ ] 6.6 Resilience 2: qualify a per-cell historical rollback and return through the existing release API.
  Use an exclusive writer window for the QA cell, a fresh post-write pre-attempt backup S1 and a copied vault plus host state.
  Verify old-image route reads/writes, custody and durable debt before any live pin.
  Schema 8 in 0.114 differs from current schema 10; readiness and manifest compatibility IDs do not prove route compatibility.
  A synthetic schema-10 negative probe with the cached 0.114 image refused without changing the store on 2026-10-11.
  It does not prove the restored vault-plus-host copy or successful historical rollback.
  If the copy is incompatible, prove refusal and current-image recovery, avoid the live pin and leave successful historical rollback open.
  Never restore pre-upgrade S0 over later committed writes or add a downmigration.
- [ ] 6.7 Resilience 3: recheck damaged-volume or bad-import recovery through the existing in-place B2 restore helper.
  The 2026-10-09 QA restore matched 45/45 vault files with 93 seconds of downtime.
  Host-state comparison was absent from that historical in-place run.
  The 2026-10-13 deployed-image scratch recovery matched both roots and verified custody, service authentication and cleanup.
  Scratch recovery does not close the in-place failure path or gateway OAuth acceptance. Keep those scopes open.
- [ ] 6.8 Resilience 4: prove recovery after actual CSI node loss on approved disposable infrastructure.
  Verify surviving-volume reattachment or B2 recovery, canonical data, host custody and service readiness.
  The older local-storage CI node-loss run does not prove the current CSI deployment's failure path.
  Price any rental before obtaining its authority; no rental is authorized by this task record.
- [ ] 6.9 Resilience 5: rehearse a server move with the existing backup, stop, volume-release, placement and resume procedures.
  Compare canonical vault and host state, preserve post-backup writes and report downtime after the replacement node exists.
- [x] 6.10 Resilience 6: verify control database recovery from pgBackRest and WAL in an isolated environment.
  The 2026-10-08 isolated restore replayed nine WAL segments, matched database lineage and passed `pg_amcheck`.
  Network isolation, no TCP listener, unchanged source processes and scratch removal also passed.
  Reconcile provider, stored-row and derived counts and ownership before closing this task; the restore receipt does not prove those comparisons.
  Price any rental before obtaining its authority and keep production writes disabled during the drill.
  Evidence (2026-10-11): the isolated restore matched the captured table scope, complete-row digests, row counts, spot checks, and stored tenant/cell ownership.
  The archived backup and WAL inputs corresponded to the restored database and its selected exclusive recovery cut.
  The comparison ran while recovery was paused. Source process identities matched, and the isolated unit, scratch data, and jail were removed.
  An independent saved-artifact recheck on 2026-10-12 confirmed those comparisons.
  This closes the one-time recovery scope. Catalog ownership/grants, sequence values, view outputs, current-run physical integrity, promotion, and recurring weekly execution remain unclaimed.
- [ ] 6.11 Resilience 7: qualify memory and CPU admission through the existing shared-worker envelope, default off.
  Measure the worst supported warm peak per slot and select conservative occupancy and fixed reserves.
  Charge `max(request, qualified peak)` and retain at least 20% node memory headroom; do not introduce a dynamic estimator or ledger.
  Use selected-empty atomic zero publication, inventory, stop, completed holds, volume release, selection and resume before all-shared promotion.
  Verify full-capacity refusal preserves the unconsumed invitation and keeps existing access working.
  A production request change to `512Mi/125m` requires separate operator restart go.
  If qualification fails, retain `1Gi/250m` for an image-only roll; do not enable shared-node MemoryQoS.
  The maintained memory profiler preserves and reports the selected Cloud resource policy instead of silently measuring the legacy profile.
  Its configuration regression test protects that binding; worst-supported warm-peak qualification remains open.
- [x] 6.12 Resilience 8: repeat the existing recipient-encrypted operator export on the reviewed current release.
  Restore from the selected B2 snapshot into scratch, encrypt to the tenant's age recipient and verify recipient decryption and canonical content.
  The 2026-09-28 exercise does not close this current-release acceptance.
  - Done on 2026-10-16 with the 0.116 cell image (`sha256:5d665b47…`), following `docs/runbooks/cloud-operator-export.md` at `886a0a9bf`.
    - The QA cell's 02:00 UTC B2 snapshot was restored into a scratch namespace (45 files).
    - The archive was encrypted to the QA tenant's age recipient, which the operator holds (543,048 bytes). It was then decrypted and compared with the restored scratch content: identical.
    - The scratch namespace, its volume, the break-glass directories and the work directory were then confirmed absent.
    - Deviations from the runbook: the node's system Python rendered the export, in-pod error text was reduced to a generic message, and the control-database read ran as the database superuser inside a verified read-only transaction with fixed SQL.
- [ ] 6.13 Complete the friend end-to-end journey through invite, paid entitlement, provisioning and connector sign-in.
  The inline checkout accepts no typed discount code; the friend price comes from the invitation through the configured friend discount, and the paid step is a real friend checkout. A complimentary ("Covered") invitation exercises the same invite, provisioning and connector steps without a payment.
  Verify save, cited recall, token refresh, a restorable backup, recipient export and ordered deletion without operator repair.
  Select the friend discount through the product configuration; do not encode a historical price.
  After capacity qualification, set and verify the fleet default `cellImage` through the existing release API.
  Existing per-cell pins alone do not qualify a new friend's image. Keep invitations pending until the required journey and resilience evidence passes.

## 7. Retirement (R)

- [x] 7.1 Delete the hosted-only runtime modules and their tests, provisioner v1, the old platform chart pieces, deployment locks, plugin candidates and `cloudflared`
  Evidence (branch `chore/retire-hosted-platform`): commits "refactor: cut the Cloud paths free of the Hosted platform" and "chore: delete the retired Hosted platform code"; the rebuilt `cloud` target passed two of three container-test runs, matching the baseline readiness first-poll flake, and the platform render loses only Hosted objects and the `exomem-export-gc` job.
- [ ] 7.2 Remove superseded changes and Hosted-only canonical requirements with their code; retain generic contracts until their active deltas are reconciled.
  Progress: the 12 superseded change directories and three original release capabilities are retired.
  The original boundary had five blocked specs: `hosted-gateway-contract`, `hosted-agent-surface`,
  `hosted-marketplace-release`, `hosted-vault-portability` and `hosted-tenant-cell`.
  The reviewer-bootstrap change is now archived as superseded, with three historical gaps still unchecked.
  `hosted-marketplace-release` has no active delta and is retired.
  `hosted-agent-surface` also has no active delta and is retired after its generic contracts were reconciled.
  Registry, bootstrap, capture doctrine, and Cloud parity remain in their current command-surface, bootstrap, portable-agent, and Cloud cell contracts.
  Historical Hosted packages and their original receipts remain immutable. Three capabilities remain blocked by active deltas.
  The retired browser transfer routes, provisioning v2, security SQLite authority, credential state machine and operator probe requirements are removed.
  Private authenticated ingress retains its generic authentication and tenant-isolation requirements without the deleted transfer exceptions.
  Session outage tolerance, portability, memory limits and frozen-verifier requirements remain with their active consumers and deltas.
  Generic V1/V2 archive, protected restore, collection replica, history, and schema contracts remain in `hosted-vault-portability` under `vault_portability.py`.
  Hosted delivery, lifecycle, release, deletion, fingerprint, and restore-candidate bindings are retired.
  Mixed changes retain their open local and Cloud tasks. Task 7.2 remains open until their active deltas are reconciled.
- [ ] 7.3 After seven clean days, delete the Neon project, and retire merged worktrees and branches

- [ ] 7.4 Complete the [Hosted retirement disposal inventory](../../../docs/runbooks/cloud-hosted-retirement.md).
  Repository deletion leaves provider credentials, manually managed Secrets and retained data intact.
  Revoke all 34 original destination records and eight collector destination records after confirming no kept consumer.
  Each of the eight obsolete BWS entries requires a separate explicit operator go.
  Keep Secret/ConfigMap cleanup, tenant data disposal and Terraform disposal pending until their exact absence proofs exist.
