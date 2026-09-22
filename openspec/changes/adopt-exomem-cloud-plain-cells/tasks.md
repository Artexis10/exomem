## 1. Specification and supersession (S)

- [ ] 1.1 Get an independent critic review of this change and the companion Substrate change, and resolve every blocking finding in design.md
- [ ] 1.2 Add a one-line superseded banner to the proposal of every change listed as superseded in design.md; do not change their tasks or specs
- [ ] 1.3 Close the superseded hosted PRs with a one-line reason, and keep their branches

## 2. Cloud cell mode and image (lane A)

- [ ] 2.1 Write red-first tests. With `EXOMEM_CLOUD_CELL=1`:
  - the server authenticates only the configured bearer, in constant time;
  - `/api/*`, `/upload` and `/download` are absent;
  - `/health` answers without content;
  - no `.env` file is read.
- [ ] 2.2 Add `CloudCellTokenVerifier` to `server_auth.py`, and add the cloud branch of `build_server` in `server.py` on the standalone path (D1)
- [ ] 2.3 Add `CLOUD_SURFACE_EXCLUSIONS` (`transfer_artifact`, `adopt_vault`, `process_media`, `read_media`, each with a reason and a lifting condition) and remove its members from the served surface. Test the listed surface.
- [ ] 2.4 Enable content-private logging in cloud mode. Test that a distinctive query phrase never reaches runtime or access logs.
- [ ] 2.5 Add a `cloud` Dockerfile target: UID/GID 10001, home `/data/host`, offline models, command `exomem --transport http --host 0.0.0.0 --port 8765`. Prove standalone custody resolves under `/data/host` in the built image.
- [ ] 2.6 Container test on the built image:
  1. first start on an empty volume;
  2. a governed write;
  3. container replacement on the same volume;
  4. recall of the write;
  5. a further governed write.
- [ ] 2.7 Add a CI job that publishes the `cloud` image to GHCR by digest on release tags, and records tag and digest in the release notes

## 3. cellctl, manifests and ingress (lane B)

- [ ] 3.1 Scaffold `infra/cellctl/` as a Python package with its own pyproject and tests, plus `tests/fixtures/exomem_cloud_schema.sql` copied from the Substrate migration
- [ ] 3.2 Write red-first unit tests for the rendered manifests (D4):
  - Pod Security labels, quota and default-deny network policy with gateway-only ingress;
  - no runtime egress, no ServiceAccount token, `readOnlyRootFilesystem`, `fsGroup` 10001;
  - the `/health` probe, the encrypted `reclaimPolicy: Delete` class and the backup CronJob.
- [ ] 3.3 Implement the reconcile loop (D3): poll and `LISTEN`, server-side apply under field manager `cellctl`, observed writes, generation matching, transient retry, identity-conflict failure. Test against a disposable Postgres with the fixture schema.
- [ ] 3.4 Implement rollout (D5): pre-upgrade snapshot, one cell at a time by priority, readiness wait, canary return and pause after 10 minutes, per-row override
- [ ] 3.5 Implement secrets (D6): the HMAC cell bearer, and per-cell data keys with AES-GCM envelope and versions. Test that plaintext keys never reach the database.
- [ ] 3.6 Implement verified deletion (D8): namespace, PV, provider volume, backup prefix, then wrapped-key destruction. A failed observation stays `deleting`.
- [ ] 3.7 Implement capacity publication (D9) from the provider attachment count, limit and headroom
- [ ] 3.8 Add platform chart entries:
  - cellctl Deployment, single replica, `Recreate` strategy, with RBAC and one namespace-name `ValidatingAdmissionPolicy`;
  - gateway Deployment and Service consuming the Substrate gateway image digest;
  - the `exomem-cloud-encrypted` StorageClass;
  - Traefik ACME TLS-ALPN-01 resolver and the gateway IngressRoute on the MCP hostname;
  - SOPS-sourced Secrets.
- [ ] 3.9 Integration test on disposable K3s: create, pod kill, stop, resume, upgrade with canary return, cross-cell network denial, deletion with absence proofs

## 4. Control database server (lane D)

- [ ] 4.1 Terraform: `hcloud_server.control`, an `hcloud_network` shared with the fleet node, firewall rules for 22 and PgBouncer TLS, and DNS-only records
- [ ] 4.2 Ansible `postgres` role:
  - PostgreSQL 17 and PgBouncer in transaction mode;
  - public TLS certificate for verify-full;
  - roles `substrate_app`, `exomem_gateway` and `exomem_cellctl` with the grants in D11;
  - pgBackRest to B2 with WAL archiving, a nightly full and a weekly restore verification.
- [ ] 4.3 Molecule or disposable-VM test of the role: TLS verify-full connection per role, refused cross-role writes, and a pgBackRest backup and restore round trip
- [ ] 4.4 Cutover script and runbook:
  1. Neon `pg_dump` in a maintenance window;
  2. restore;
  3. row-count and checksum comparison per table;
  4. switch Vercel `DATABASE_URL`;
  5. post-checks for Endstate and Exomem;
  6. retain the Neon export for rollback.

## 5. Local rehearsal (P3)

- [ ] 5.1 Write one command that stands up disposable K3s, Postgres with the real Substrate migrations, Substrate, the gateway, cellctl and the real cell image
- [ ] 5.2 The command runs:
  1. invite;
  2. provision, in under 3 minutes;
  3. OAuth MCP `tools/list`;
  4. capture;
  5. paraphrased cited recall;
  6. pod kill and recall;
  7. upgrade and recall;
  8. second-tenant denial;
  9. backup and scratch restore;
  10. deletion with absence proofs.
  It writes a machine-readable report.
- [ ] 5.3 Measure warm p95 initialize and list, capture and cited recall, provisioning time and per-cell upgrade time, and record them in the report

## 6. Node deployment and owner acceptance (P4)

- [ ] 6.1 Apply the control server, run the database cutover, and verify Endstate and Exomem
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
