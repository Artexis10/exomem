<!-- authority:non-specification -->

# Prepare the Cloud migration without changing personal Exomem

This is the operational companion to
[`adopt-exomem-cloud-plain-cells`](../../openspec/changes/adopt-exomem-cloud-plain-cells/design.md).
Preparation does not authorize a deployment, DNS change, connector edit, personal
service restart, or data migration. Keep the existing personal endpoint serving.
The old hosted deployment-lock procedure is not the Cloud deployment contract.

## Private deployment inputs

Keep machine addresses, DNS record identities, client inventory and captured
live values in an owner-readable directory outside Git. Keep credentials in the
secret manager and SOPS artifacts; never put plaintext in the values file,
shell arguments, a plan report or this runbook. Record observation times and
source revisions. Recheck live observations immediately before a later apply.

Collect these inputs without changing the named systems:

| Input | Source and readiness check |
| --- | --- |
| Personal service | Actual host, service manager/unit, interpreter, vault and external state root; current health and authenticated MCP read |
| Personal tunnel | Connector host, local configuration path and ingress, credentials reference, service manager; a dashboard copy does not prove local configuration ownership |
| Original DNS | Zone, record ID, complete type/content/proxy/TTL settings; preserve the original record for rollback |
| Endpoint pair | Cloud and replacement personal HTTPS hostnames, both with `/mcp`; certificate coverage and connector reauthentication requirements |
| Personal clients | CLI profiles, desktop/web connectors and any other consumers; record which owner has verified each one |
| Fleet and database | Node identity, private database address, direct PostgreSQL roles on 5432, cluster/pod/service CIDRs, current chart release and values |
| Images | Released Cloud cell, cellctl and gateway repository digests, their source revisions, supported architecture and authenticated pull proof |
| Backup account | Dedicated B2 account identity, private bucket name/ID, account-specific S3 endpoint and provider credential references |
| Release evidence | Green full CI on the final main revision, release PR based on that revision, published image metadata and node acceptance still pending |

An empty or placeholder value means **not ready**. A draft with Cloud enabled
flags set to false is an input worksheet, not an apply-ready platform release.

## Backup storage isolation

The Cloud key manager needs account-wide key-management authority. Use the
dedicated Cloud account; do not obtain it by broadening or copying credentials
from the existing durability account. A bucket-restricted child key does not
confine a parent that can create new keys.

Prepare separate Terraform provider credentials and state ownership before
provisioning the bucket. The current `infra/terraform/durability` root owns the
existing recovery/export/database/etcd storage and is not a ready-made Cloud
account root. Replacing its provider credentials would target the wrong account.
The Cloud bucket is private, has no Object Lock, and must not expire live restic
packs by upload age. Restic manages retention; D10 removes every object version.
Record the actual account's S3 endpoint rather than copying a different account's
region. Before acceptance, prove one cell key cannot access another prefix and
prove version-aware deletion and key absence in the dedicated test cell.

## Platform Secret inventory

These names and keys are the defaults consumed by the current chart. Namespace
is `exomem-cloud` except the volume-encryption Secret in `exomem-platform`.
Any override must agree with the chart and destination matrix.

| Kubernetes Secret | Exact keys | Preparation requirement |
| --- | --- | --- |
| `exomem-cellctl-database-dsn` | `dsn` | Dedicated cellctl role, direct private PostgreSQL connection, including LISTEN |
| `exomem-cloud-gateway-database` | `url` | Dedicated gateway role, direct private PostgreSQL connection |
| `exomem-cloud-gateway-control-plane-key` | `key` | Match the authorized Substrate control-plane key |
| `exomem-cloud-cell-token-key` | `current`, `currentVersion`; rotation adds both `previous`, `previousVersion` | Key is 32 random bytes encoded as 64 hex characters; both components read the same current entry |
| `exomem-cloud-backup-master-key` | `keys`, `currentVersion` | `keys` is a JSON version-to-base64-key map; decoded keys are 32 bytes and contain the selected version |
| `exomem-cloud-b2-key-management` | `keyId`, `applicationKey` | Dedicated Cloud account only; never mount this parent key in tenant runtime/backup pods |
| `exomem-cloud-hetzner-read-token` | `token` | Read-only provider authority for deletion observations |
| `exomem-cloudflare-dns-token` | `token` | DNS-01 authority for the chosen zone; namespaced Issuer limits the requested hostname |
| `exomem-cloud-volume-encryption` | `encryption-passphrase` | Separately escrowed Cloud volume key, not an instruction to rotate an existing volume key |

Add and review destinations in `infra/contracts/secret-destinations-v1.json`
before sealing. First verify the complete-bundle handoff support from
[PR #1398](https://github.com/Artexis10/exomem/pull/1398) has merged and is present
in the checkout used to seal and apply. The earlier scalar-only tooling rejects
these bundles; do not work around that by splitting fields into competing
Secrets. Multi-field Secrets require:
`value_shape: json-object` and declared `key_sets`, with no scalar `key` field.
The current single-key forms remain scalar destinations. Use named secret-manager
bindings or stdin; validate the runtime-specific key encodings before sealing.
Shape validation alone does not establish cryptographic key validity.

Produce only immutable versioned ciphertext using `infra/scripts/secret_handoff.py`.
Verify every leaf is encrypted, decrypt in memory for exact shape comparison, and
prove the owner can recover the key material. Prepare the active selection and
registry changes as reviewed artifacts; do not activate or apply them during
preparation. After a future apply, verify exact key names without printing values.
Do not use forced server-side-apply ownership to hide retained foreign fields.

## Render and review

The values worksheet must resolve these current chart inputs:

- `cellctl.image`, `cellctl.cellImageRepository`, the four `cellctl.b2*` identity/
  endpoint values, and `cellctl.databaseEgressCidrs`;
- `cloudGateway.image`, `hostname`, `publicBaseUrl`, `/mcp`, matching database
  CIDRs, and the unguessable trusted-ingress source value;
- `cloudIngress.hostname` equal to the gateway hostname, ACME contact and DNS
  token reference; all Secret references from the inventory above;
- the initial cell image digest in the control database's Cloud settings,
  separately from `cellImageRepository`, which is the admission repository;
- the captured existing platform values, including the intentionally stopped
  legacy workers and suspended schedules.

Use the pinned toolchain from `infra/tool-versions.env`. An offline render is
safe; it is not a rollout and does not prove credentials, image pulls or live CSI.

```bash
umask 077
helm dependency build infra/helm/platform
helm template exomem-platform infra/helm/platform \
  --namespace exomem-platform \
  --values "${PLATFORM_BASELINE_VALUES:?captured current platform values required}" \
  --values "${CLOUD_VALUES:?completed reviewed Cloud values required}" \
  > "${CLOUD_RENDER_OUTPUT:?private output path required}"
```

Review the complete release diff: namespace isolation, single/Recreate cellctl,
digest pins, database CIDRs, admission policy, encrypted Delete StorageClass,
gateway network policy, DNS-01 Issuer and hostPort 443. Preserve legacy stopped
replicas and suspended schedules. A defaults-only Helm upgrade can revive them.
Check cert-manager installation/CRD/webhook ordering before any dependent Issuer.

Prepare a foundation plan only after resolving DNS ownership. The existing
`cloudflare_dns_record.gateway[0]` address may already own another record: inspect
state before deciding whether import is appropriate. Changing `gateway_hostname`
to an occupied personal hostname can replace or collide with a record. Do not
blindly import, delete the personal record, or apply a broad platform plan.
Remote state imports, refresh/apply operations and DNS changes wait for cutover.

## Later cutover checkpoints

These are future execution gates, not commands to run during preparation.

1. Agree the deployment window and recoverability checks. Reverify personal
   health, live control-database backups and the exact saved deployment inputs.
   Reconcile prior cutover closure evidence; do not repeat an already completed
   control-database cutover or restore over a database that has accepted writes.
2. Bring up and validate Cloud without repointing personal DNS. Certificate
   DNS-01 writes and any node/Helm changes occur only in that agreed window.
   Verify direct TLS using the intended hostname/SNI and node address, then the
   gateway, database roles, namespace isolation and a disposable owner cell.
3. Add the personal replacement route against the same personal backend. Choose
   a method supported by the actual locally managed tunnel. Do not restart the
   personal runtime. Keep the old route and observe it while testing the new one.
4. Verify each personal client on the replacement: authentication, tools listing,
   cited recall, an agreed non-sensitive write and recall, and any required
   reauthentication. Preserve original client settings for rollback.
5. Only after all personal clients pass, agree the DNS ownership transition and
   repoint the original hostname to Cloud with DNS-only TLS. Preserve the old
   personal tunnel ingress until rollback is no longer needed.
6. Run Cloud owner acceptance, including token refresh after 15 minutes, node
   latency checks, restart/upgrade recall, real B2 backup and scratch restore.
   Friends remain gated on owner acceptance and the export runbook.

For hostname failure, restore the captured original DNS record and client
settings; preserve both services' data. For Cloud rollout failure, use D6's
previous image and restore procedure. Never delete tenant namespaces or restore
the control database merely to undo DNS. A database incident needs its own
write-preserving recovery plan; the frozen Neon export is not current data.

## Ready means evidence exists

The deployment inputs are complete only when the private input table has no
unresolved deployment values, encrypted artifacts and recovery custody are
verified, release digests are published, offline checks pass, and both the
personal acceptance checklist and rollback steps have named owners. A reviewable
preparation packet can name outputs still awaiting separately authorized
provisioning; it is not yet apply-ready. Report those missing inputs explicitly.
Do not mark deployment or owner acceptance
tasks complete because a runbook, a draft values file or merged code exists.
