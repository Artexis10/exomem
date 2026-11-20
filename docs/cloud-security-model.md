<!-- authority:non-specification -->

# Exomem Cloud security model

Exomem Cloud is the managed Exomem service that Substrate Systems OÜ operates. Each account gets one
isolated cell that runs this repository's open-source server. Each section states one claim, the public
code that enforces it, a check you can do, and its limits today.

The control plane is in the public
[substrate repository](https://github.com/substrate-systems/substrate). The live deployment's values are
not public, so this page names each setting that is off by default.

Terms: _cellctl_ creates and reconciles cells. The _gateway_ routes authenticated requests to cells. The
_edge_ terminates public TLS.

## Isolation between accounts

**Claim.** Each account runs in its own isolated cell. Other accounts cannot reach your data.

**Enforced.**
- `render_cell_namespace` and `render_network_policies` in
  [manifests.py](../infra/cellctl/src/cellctl/manifests.py) give each cell its own namespace, with the
  Pod Security `restricted` profile and default-deny networking. Only gateway pods reach the cell port.
- `handleCloudMcpRequest` in
  [cloud-handler.ts](https://github.com/substrate-systems/substrate/blob/main/src/exomem-gateway/cloud-handler.ts)
  takes the cell from the authenticated token, and refuses a request that names a tenant or cell.
- [exomem-cloud-grants.sql](https://github.com/substrate-systems/substrate/blob/main/scripts/exomem-cloud-grants.sql)
  lets the gateway read only the cell ID, tenant ID and desired state of a cell.

**Check.** Send a request that names a cell in a header. The gateway answers `400 HOSTED_SELECTOR_REJECTED`.

**Limits.** All cells share one cluster, and most share nodes and kernels. Cell Jobs, such as
backups, can reach any public HTTPS address. One fleet-wide gateway key derives every cell's bearer, so
a gateway compromise reaches every cell.

**Changing.** `contain-cloud-controller-compromise` replaces the shared bearer with signed gateway proofs.

## Encryption at rest and in transit

**Claim.** The knowledge store and its backups are encrypted at rest. Connections to the service are
encrypted in transit.

**Enforced.**
- `render_secret` in [manifests.py](../infra/cellctl/src/cellctl/manifests.py) refuses an empty volume
  passphrase. `derive_volume_passphrase` in [secrets.py](../infra/cellctl/src/cellctl/secrets.py) derives
  it from the cell's root key. Dedicated hosts encrypt their disk array with LUKS2
  ([dedicated_host](../infra/ansible/roles/dedicated_host/tasks/main.yml)).
- The cell encrypts media before upload. The [media edge](../src/exomem/media_edge.py) moves only
  ciphertext.
- [config.yaml.j2](../infra/ansible/roles/k3s/templates/config.yaml.j2#L15) encrypts cluster Secrets.
- The edge terminates TLS on the cluster
  ([cloud-ingress.yaml](../infra/helm/platform/templates/cloud-ingress.yaml)). The public DNS record is
  not proxied ([cloudflare.tf](../infra/terraform/foundation/cloudflare.tf)).

**Check.** Inspect the TLS certificate of the Cloud MCP endpoint.

**Limits.**
- Transit encryption covers the client-to-edge hop only. `defaultCellOrigin` in `cloud-handler.ts`
  forwards to the cell over plain HTTP. Pod traffic between nodes uses unencrypted
  [VXLAN](../infra/ansible/roles/k3s/templates/flannel-net-conf.json.j2). Only a node outside the
  provider's private network joins over WireGuard.
- Older cells share one volume passphrase
  ([storage-class.yaml](../infra/helm/platform/templates/storage-class.yaml)). Dedicated-host cells have
  no per-cell volume key, and a network key server unlocks their disks unattended.
- Exports and export staging have only the storage provider's encryption.

**Changing.** `move-cloud-cells-to-local-storage` gives each local cell volume its own cell-derived key
before cells move.

## Who holds which keys

**Claim.** Substrate Systems holds the keys for each cell's storage and backups on the server that runs
the cell. The internet-facing edge cannot read the keys that protect your stored data.

**Enforced.**
- `_resolve_secret_material` in [reconcile.py](../infra/cellctl/src/cellctl/reconcile.py) wraps each
  cell's root key under a master key that cellctl holds. The volume, backup and media keys come from
  that root key. The control database receives wrapped keys only.
- The [key custodian](../infra/helm/platform/templates/key-custodian.yaml) alone creates each cell's
  object-storage key, limited to that cell's prefix.
- The edge has namespaced RBAC and watches only its own namespace (`traefik` in
  [values.yaml](../infra/helm/platform/values.yaml)).

**Check.** Read the admission policy `exomem-key-custodian-scope`.

**Limits.** cellctl can unwrap every cell's keys. It can also render a Job that reads a cell's
object-storage key ([cloud-key-custody](runbooks/cloud-key-custody.md)). The key custodian is off by
default.

**Changing.** `contain-cloud-controller-compromise` closes the pod template and moves the backup password
into the cell.

## What operators can do day to day

**Claim.** Everyday operator access cannot read your data or its keys. It cannot open a session inside
your cell.

**Enforced.** [operator-access.yaml](../infra/helm/platform/templates/operator-access.yaml#L8-L33)
grants read access to workload status and logs, with no Secrets and no exec, attach or port-forward. The
admission policy `exomem-cell-connect-guard`
([L74-L102](../infra/helm/platform/templates/operator-access.yaml#L74-L102)) refuses those operations in
cell namespaces outside the break-glass group.

**Check.** Run `helm template` on `infra/helm/platform` and read both objects.

**Limits.** The everyday role reads every pod's logs, so this claim depends on content-free logging. Its
certificate lasts 30 days. A cluster administrator can delete the admission policy, so the policy stops
accidents, not intent.

**Changing.** `make-cloud-cells-operator-resistant` adds an opt-in tenant lock under the owner's own key.

## Break-glass

**Claim.** Reaching your content needs an emergency credential that expires within an hour, or
administrator access to the server.

**Enforced.** The group `exomem:break-glass` maps to `cluster-admin`
([L48-L62](../infra/helm/platform/templates/operator-access.yaml#L48-L62)), and no standing credential
carries it. [cloud-operator-access](runbooks/cloud-operator-access.md) issues a one-hour certificate and
checks its expiry.

**Check.** Read the binding and the procedure.

**Limits.** The procedure sets the one-hour limit; the cluster does not enforce it. An administrator can
issue a longer certificate.

**Changing.** `make-cloud-cells-operator-resistant` adds presence-gated administrator credentials.

## Audit logging

**Claim.** The server records actions taken through the cluster, including the issue and use of the
emergency credential. The record is not tamper-proof evidence.

**Enforced.** [audit-policy.yaml](../infra/ansible/roles/k3s/files/audit-policy.yaml) records metadata
for every request, and full bodies for namespace, volume claim and StatefulSet changes.
[config.yaml.j2](../infra/ansible/roles/k3s/templates/config.yaml.j2#L30-L33) sets the retention.
[privacy_log.py](../src/exomem/privacy_log.py) keeps content out of the cell's own logs.

**Check.** Run `pytest tests/test_cloud_cell.py -k content_private`.

**Limits.** The log stays on the node for at most 7 days, in at most four 100 MB files. A node
administrator can rewrite it. Actions on the node outside the cluster API leave no entry. The off-node
[access record](../infra/cellctl/src/cellctl/access_record.py) is off by default.

**Changing.** `make-cloud-cells-operator-resistant` turns on the off-node access record and a tenant
access log.

## Export

**Claim.** An export is a plain ZIP, not encrypted to a key of your own. Object storage keeps it for a
stated number of hours, and then the service deletes it. You download it only from Home, through a link
that works for 5 minutes.

**Enforced.** [exports.py](../infra/cellctl/src/cellctl/exports.py) stores the ZIP, signs short links,
and deletes every version at expiry. [decide.py](../infra/cellctl/src/cellctl/decide.py) sets the
defaults: `export_ttl` is 24 hours and `export_link_ttl` is 300 seconds.

**Check.** Open an export link after 5 minutes. It fails.

**Limits.** Substrate and cellctl read the retention time in separate deployments, so they can
disagree. If cellctl misses the deletion, a [bucket rule](../infra/terraform/cell-data/storage.tf)
removes the export after about 8 days.

**Changing.** `contain-cloud-controller-compromise` encrypts owner exports to a recipient in your browser.

## Deletion and what it proves

**Claim.** Deletion revokes sessions and client access, closes routing, and destroys the cell's compute,
storage and keys.

**Enforced.**
- [cloud-deletion-finish.ts](https://github.com/substrate-systems/substrate/blob/main/src/lib/exomem-hosted/cloud-deletion-finish.ts)
  marks the cell deleted, revokes client consent and scrubs the account.
- `retire_content_keys` in [db.py](../infra/cellctl/src/cellctl/db.py) clears the wrapped keys first.
  `_decide_deletion` in [decide.py](../infra/cellctl/src/cellctl/decide.py) then removes the namespace,
  volume and staging objects, in order.
- The custodian purges backups only on a signed
  [deletion grant](../infra/contracts/cell-deletion-grants-v1.json), after a 7-day grace.

**What it proves.** The cell row records when its live keys were retired and destroyed. Its
`keys_expire_at`, when every copy expires, stays empty unless the deployment declares all three
retention sources (`_retention_expiry` in `reconcile.py`). The chart declares none.

**Check.** Read the grant contract. Both grant verifiers test against it.

**Limits.**
- etcd snapshots keep encrypted copies of cell Secrets in object storage for up to 31 days
  ([config.yaml.j2](../infra/ansible/roles/k3s/templates/config.yaml.j2#L54-L63)).
- Control-database backups keep the wrapped keys until their own retention ends.
- Locked backups stay about 38 days by default, plus the provider's delay.

**Changing.** `automate-cell-storage-deletion` still needs its final live verification.

## Backups

**Claim.** Backups are encrypted. Backblaze holds them in its EU region.

**Enforced.** Each cell has its own restic password, wrapped under its root key.
[storage.tf](../infra/terraform/cell-data/storage.tf) adds provider encryption and a compliance Object
Lock of 30 days by default.

**Check.** Read the bucket definition.

**Limits.** The backup password is in a Secret on the node, so the operator can decrypt backups.

**Changing.** `contain-cloud-controller-compromise` moves the backup password into the cell, with escrow.

## Image provenance and admission

**Claim.** Each released image carries a build attestation from this repository's release workflow.

**Enforced.** [release-please.yml](../.github/workflows/release-please.yml) attests each image and
publishes the bundles as `exomem-image-*.sigstore.json` release assets. `verify_attestation` in
[cloud_deploy.py](../infra/scripts/cloud_deploy.py) checks the attestation before it sets the cell
image. The admission policy `exomem-cellctl-scope` in
[cellctl.yaml](../infra/helm/platform/templates/cellctl.yaml) admits only digest-pinned images from the
configured repository.

**Check.** Run `gh attestation verify oci://ghcr.io/artexis10/exomem@sha256:<digest> --owner artexis10
--source-ref refs/tags/v<version>`.

**Limits.** Admission checks no signatures, because the signature policy in
[operator-deploy.yaml](../infra/helm/platform/templates/operator-deploy.yaml) is off by default. Substrate's
admin token can set an image without the deploy check.

**Changing.** `make-cloud-cells-operator-resistant` makes admission verify each image's attestation.

## Secret-key leak detection

**Claim.** The service detects object-storage keys it did not create. CI scans infrastructure
changes for secrets.

**Enforced.** [b2-key-watcher.yml](../.github/workflows/b2-key-watcher.yml) compares the storage
account's keys and buckets with a pinned set every hour, and alerts outside the cluster. When a change
touches `infra/`, CI runs [validate.sh](../infra/scripts/validate.sh), which includes a secret scan.

**Check.** Read the watcher's public run history in the Actions tab.

**Limits.** The watcher finds new or changed keys, not misuse of a known key. Its pinned set is not
public.

## What the service is not

Exomem Cloud is not zero-knowledge or end-to-end encrypted. The edge and gateway see requests and
responses. Substrate Systems holds the keys, so it could deliberately read your content. The
`cloud-operator-access` specification requires this disclosure.

## Claims we could not trace to code

- "Hetzner for the servers in Germany": `server_location` in
  [variables.tf](../infra/terraform/foundation/variables.tf) defaults to a German location, but it also
  accepts one in Finland.
- "Backblaze for encrypted backups in its EU region": the bucket region is a deployment value.
- "Paddle handles payment-card details": no code here can show that.
- "Minimal billing, security, deletion-proof, and legal records may be retained": no code defines that
  set.
- "We test that they do", for content-free logs: CI tests the cell's logging. The live check of every
  service is manual and unpublished.
