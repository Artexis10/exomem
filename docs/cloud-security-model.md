<!-- authority:non-specification -->

# Evermatter Cloud security model

Evermatter Cloud is the managed Evermatter service of Substrate Systems OÜ. Each account gets one cell that runs
this repository's server. Each section states a claim, mostly from the
[privacy page](https://evermatter.ai/privacy), the public code behind it, a check and its
limits. The control plane is in the public [substrate repository](https://github.com/substrate-systems/substrate).
Deployment values are not public, so this page marks each control that is off by default.

Terms: _Substrate_ is the control-plane web app on Vercel, which issues access tokens. _Home_ is its
signed-in account page. _cellctl_ creates and reconciles cells. The _gateway_ routes authenticated
requests to cells. The _edge_ terminates public TLS.

## Isolation between accounts

**Claim.** Each account runs in its own isolated cell. Other accounts cannot reach your data.

**Enforced.**
- `render_cell_namespace` and `render_network_policies` in
  [manifests.py](../infra/cellctl/src/cellctl/manifests.py) give each cell its own namespace, with the
  Pod Security `restricted` profile and default-deny networking. Only gateway pods reach the cell port.
- `handleCloudMcpRequest` in
  [cloud-handler.ts](https://github.com/substrate-systems/substrate/blob/main/src/exomem-gateway/cloud-handler.ts)
  takes the cell from the access token, and refuses a request that names a tenant or cell.
- [exomem-cloud-grants.sql](https://github.com/substrate-systems/substrate/blob/main/scripts/exomem-cloud-grants.sql)
  lets the gateway read only the cell ID, tenant ID and desired state of a cell.

**Check.** POST a request that names a cell in a header. The gateway answers `400 HOSTED_SELECTOR_REJECTED`.

**Limits.** All cells share one cluster, and most share nodes and kernels. Cell Jobs, such as backups,
can reach any public HTTPS address. One fleet-wide bearer key, which the gateway holds, derives every
cell's bearer. Substrate issues every token that the gateway accepts, and mints Home's own
(`mintHomeAccessToken` in
[cloud-home-mcp.ts](https://github.com/substrate-systems/substrate/blob/main/src/lib/exomem-hosted/cloud-home-mcp.ts)).
Its database role can write the access-token tables, and only named columns of the cell tables. A compromise of Substrate or of that credential
reaches every cell.

**Changing.** [`contain-cloud-controller-compromise`](../openspec/changes/contain-cloud-controller-compromise/)
replaces the shared bearer with signed gateway proofs.

## Encryption at rest and in transit

**Claim.** The knowledge store and its backups are encrypted at rest. Connections from your client are
encrypted in transit. Traffic from the edge to the gateway, and from the gateway to your cell, is not yet
encrypted.

**Enforced.**
- `render_secret` in [manifests.py](../infra/cellctl/src/cellctl/manifests.py) refuses an empty volume
  passphrase, except for the shared legacy class and local volumes.
- Dedicated hosts encrypt their disk array with LUKS2
  ([dedicated_host](../infra/ansible/roles/dedicated_host/tasks/main.yml)).
- The cell encrypts media before upload ([media_store.py](../src/exomem/media_store.py)). The media
  edge moves only ciphertext, and `mediaEdge.enabled` is off by default.
- [config.yaml.j2](../infra/ansible/roles/k3s/templates/config.yaml.j2#L15) encrypts cluster Secrets.
- The edge terminates TLS on the cluster
  ([cloud-ingress.yaml](../infra/helm/platform/templates/cloud-ingress.yaml)). Its optional record
  `cloudflare_dns_record.gateway` is not proxied, and `gateway_hostname` defaults to empty
  ([cloudflare.tf](../infra/terraform/foundation/cloudflare.tf#L44-L74)). The control and transfer
  records belong to the retired Hosted platform and carry no Cloud traffic: their tunnel targets that
  platform's edge ([L15-L40](../infra/terraform/foundation/cloudflare.tf#L15-L40)), not the Cloud edge.

**Check.** Inspect the TLS certificate of the Cloud MCP endpoint.

**Limits.**
- Two hops use plain HTTP. The edge reaches the gateway through an ExternalName Service on port 8080
  (`cloud-ingress.yaml`), and `EXOMEM_GATEWAY_INTERNAL_ORIGIN` in
  [cloud-gateway.yaml](../infra/helm/platform/templates/cloud-gateway.yaml) is `http://`. The gateway
  then forwards to the cell (`defaultCellOrigin` in `cloud-handler.ts`). Pod traffic between
  nodes uses unencrypted [VXLAN](../infra/ansible/roles/k3s/templates/flannel-net-conf.json.j2). Only a
  node outside the provider's private network joins over WireGuard.
- Older cells share one volume passphrase
  ([storage-class.yaml](../infra/helm/platform/templates/storage-class.yaml)). Dedicated-host cells have
  no per-cell volume key. A Tang key server on a K3s server node unlocks their disks unattended
  ([L8-L12](../infra/ansible/roles/dedicated_host/tasks/main.yml#L8-L12)).
- Exports and export staging have only the storage provider's encryption.

**Changing.** [`move-cloud-cells-to-local-storage`](../openspec/changes/move-cloud-cells-to-local-storage/)
gives each local cell volume its own cell-derived key before cells move.

## Who holds which keys

**Claim.** Substrate Systems holds each cell's storage and backup keys on the server that runs the cell,
and in encrypted recovery copies whose key it keeps off its servers. The internet-facing edge cannot
read them.

**Enforced.**
- `_resolve_secret_material` in [reconcile.py](../infra/cellctl/src/cellctl/reconcile.py) wraps each
  cell's root key under a master key that cellctl holds. The volume passphrase derives from that root
  key ([secrets.py](../infra/cellctl/src/cellctl/secrets.py)), which also wraps the backup password and
  media keys. The control database receives wrapped keys only.
- The master key, the fleet-wide bearer key and the shared volume passphrase are also SOPS/age ciphertext
  in this public repository, under `infra/secrets/cloud/`. Only their age key is kept off the servers
  ([cloud-platform-operations](../openspec/specs/cloud-platform-operations/spec.md)).
- The [key custodian](../infra/helm/platform/templates/key-custodian.yaml), off by default, alone
  creates each cell's object-storage key, limited to that cell's prefix.
- The edge has namespaced RBAC and watches only its own namespace (`traefik` in
  [values.yaml](../infra/helm/platform/values.yaml)).

**Check.** Run `helm template` on `infra/helm/platform`. The edge gets a Role, not a ClusterRole.

**Limits.** cellctl can unwrap every cell's keys. It can also render a Job that reads a cell's
object-storage key ([key-custodian.yaml](../infra/helm/platform/templates/key-custodian.yaml#L8-L10)).

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

**Limits.** The everyday role reads every pod's logs, so this claim depends on content-free logging. A
cluster administrator can delete the admission policy, so the policy stops accidents, not intent.

**Changing.** [`make-cloud-cells-operator-resistant`](../openspec/changes/make-cloud-cells-operator-resistant/)
adds an opt-in tenant lock under the owner's own key.

## Break-glass

**Claim.** Reaching your content needs an emergency credential that the procedure issues for at most an
hour, or administrator access to the server. The cluster does not yet enforce that limit.

**Enforced.** The group `exomem:break-glass` maps to `cluster-admin`
([L51-L62](../infra/helm/platform/templates/operator-access.yaml#L51-L62)), and no standing credential
carries it. The issue procedure, not the cluster, sets certificate lifetimes: one hour for break-glass
([L48-L50](../infra/helm/platform/templates/operator-access.yaml#L48-L50)) and 30 days for everyday
access ([design](../openspec/changes/archive/2026-09-28-harden-exomem-cloud-operator-access/design.md)).

**Check.** Read the binding.

**Limits.** An administrator can issue a longer certificate. Substrate's database credential can mint a
token for any account, and Substrate's admin token can set a cell image. The cluster audit log does not
record either Substrate request. An image change appears there only as its effect, cellctl's StatefulSet
update.

**Changing.** `make-cloud-cells-operator-resistant` adds presence-gated administrator credentials.

## Audit logging

**Claim.** The server records actions taken through the cluster, including the issue and use of the
emergency credential. The record is not tamper-proof evidence.

**Enforced.** [audit-policy.yaml](../infra/ansible/roles/k3s/files/audit-policy.yaml) records metadata
for every request except system lease renewals
([L19-L46](../infra/ansible/roles/k3s/files/audit-policy.yaml#L19-L46)). It records full bodies for
namespace, volume claim and StatefulSet changes.
[config.yaml.j2](../infra/ansible/roles/k3s/templates/config.yaml.j2#L30-L33) sets the retention.
[privacy_log.py](../src/exomem/privacy_log.py) keeps content out of the cell's logs.

**Check.** Run `pytest tests/test_privacy_structured_redaction.py` and the `never_carry` tests in
`tests/test_cloud_cell.py`.

**Limits.** The log stays on the node for at most 7 days, in at most four 100 MB files. A node
administrator can rewrite it. Actions outside the cluster API leave no entry. The off-node
[access record](../infra/cellctl/src/cellctl/access_record.py) is off by default. After each change to
the cell, cellctl or the gateway, a canary procedure searches their logs. It does not scan the edge or
Substrate, and its results are not published.

**Changing.** `make-cloud-cells-operator-resistant` turns on the off-node access record and a tenant
access log.

## Export

**Claim.** An export is a plain ZIP, not encrypted to your own key. Home links to it for 5 minutes, and
the service deletes it after a stated number of hours.

**Enforced.** [exports.py](../infra/cellctl/src/cellctl/exports.py) stores the ZIP, signs short links
and deletes every version at expiry. [decide.py](../infra/cellctl/src/cellctl/decide.py) defaults
`export_ttl` to 24 hours and `export_link_ttl` to 300 seconds.

**Check.** Open an export link after 5 minutes. It fails.

**Limits.** `cloudExportsEnabled` in
[cloud-exports.ts](https://github.com/substrate-systems/substrate/blob/main/src/lib/exomem-hosted/cloud-exports.ts)
is off by default, so Home offers no export. Substrate and cellctl read the retention time separately, so
they can disagree. A [bucket rule](../infra/terraform/cell-data/storage.tf) removes a missed export after
about 8 days by default.

**Changing.** `contain-cloud-controller-compromise` encrypts owner exports to a recipient in your browser.

## Operator-run export

**Claim.** While Home's export is off, an operator runs exports. The archive is encrypted to age
recipients that you supply.

**Enforced.** [cloud_restore_manifests.py](../infra/scripts/cloud_restore_manifests.py) restores a
backup into a scratch namespace. [cloud_operator_portability.py](../src/exomem/cloud_operator_portability.py)
builds the archive from it. The connect guard covers scratch namespaces, so a session there needs
break-glass ([L64-L66](../infra/helm/platform/templates/operator-access.yaml#L64-L66)).

**Check.** Read the namespace pattern in the guard's expression.

**Limits.** The procedure, not code, encrypts the archive and refuses any recipient that the operator or
escrow holds a key for. Root on the node can read the plaintext restore, so keeping it from the operator
rests on the procedure.

## Deletion and what it proves

**Claim.** Deletion revokes sessions and client access, closes routing, and destroys the cell's compute,
storage and keys. Where the key custodian manages a cell's backups, they expire automatically, normally
within about 40 days. Otherwise an operator deletes them by hand after deletion completes.

**Enforced.**
- Once every cell row reads deleted,
  [cloud-deletion-finish.ts](https://github.com/substrate-systems/substrate/blob/main/src/lib/exomem-hosted/cloud-deletion-finish.ts)
  marks the account deleted, cancels billing, revokes consent, deletes sessions and scrubs the account.
- `retire_content_keys` in [db.py](../infra/cellctl/src/cellctl/db.py) clears the wrapped keys first.
  `_decide_deletion` in [decide.py](../infra/cellctl/src/cellctl/decide.py) then removes the namespace,
  volume and staging objects, in order.
- The key custodian purges backups only on a signed
  [deletion grant](../infra/contracts/cell-deletion-grants-v1.json), after a 7-day grace.

**What it proves.** The cell row records when its live keys were retired and destroyed. Its
`keys_expire_at`, when every copy expires, stays empty unless the deployment declares all three
retention sources (`_retention_expiry` in `reconcile.py`). The chart declares none.

**Check.** Read the grant contract. Both grant verifiers test against it.

**Limits.**
- etcd snapshots keep encrypted copies of cell Secrets in object storage for up to 31 days
  ([storage.tf](../infra/terraform/durability/storage.tf#L169-L187)).
- Control-database backups keep the wrapped keys until their own retention ends.
- Locked backups stay about 31 days by default, plus the provider's delay, because their 30-day lock
  outlasts the 7-day grace. `backup_retention_days` in
  [variables.tf](../infra/terraform/cell-data/variables.tf) allows a lock of up to 60 days.
- With the key custodian off, no component purges backups. An old cell's backups in the legacy bucket
  wait for an operator to empty them (`storage_cleanup_pending` in `decide.py`).

**Changing.** [`automate-cell-storage-deletion`](../openspec/changes/automate-cell-storage-deletion/)
still needs its final live verification.

## Backups

**Claim.** Backups are encrypted. Backblaze holds them in its EU region.

**Enforced.** Each cell's restic repository has its own backup password, wrapped under the root key.
[storage.tf](../infra/terraform/cell-data/storage.tf) adds provider encryption and a compliance Object
Lock of 30 days by default.

**Check.** Read the bucket definition.

**Limits.** The backup password is in a Secret on the node, so the operator can decrypt backups.

**Changing.** `contain-cloud-controller-compromise` moves the backup password into the cell, with escrow.

## Image provenance and admission

**Claim.** Each released image carries a build attestation from the release workflow of the repository that built it.

**Enforced.** [release-please.yml](../.github/workflows/release-please.yml) attests each image and
publishes the bundles as `exomem-image-*.sigstore.json` release assets. `verify_attestation` in
[cloud_deploy.py](../infra/scripts/cloud_deploy.py) checks the attestation before it sets the cell
image. It derives the builder from the image name: `ghcr.io/substrate-systems/evermatter` from
`substrate-systems/evermatter`, and `ghcr.io/artexis10/exomem` (every release cut before the repository
moved) from `Artexis10/exomem`. It refuses any other image. The admission policy `exomem-cellctl-scope` in
[cellctl.yaml](../infra/helm/platform/templates/cellctl.yaml) admits only digest-pinned images from the
configured repository.

**Check.** Run `gh attestation verify oci://ghcr.io/substrate-systems/evermatter:<version>-cloud --repo
substrate-systems/evermatter --signer-workflow substrate-systems/evermatter/.github/workflows/release-please.yml --source-ref
refs/tags/v<version>`.

**Limits.** Admission checks no signatures, because the signature policy in
[operator-deploy.yaml](../infra/helm/platform/templates/operator-deploy.yaml) is off by default.
Substrate's admin token can set an image without the deploy check.

**Changing.** `make-cloud-cells-operator-resistant` makes admission verify each image's attestation.

## Secret-key leak detection

**Claim.** The service detects object-storage keys it did not create.

**Enforced.** [b2-key-watcher.yml](../.github/workflows/b2-key-watcher.yml) compares the storage
account's keys and buckets with a pinned set every hour, and alerts outside the cluster. CI runs a
secret scan ([validate.sh](../infra/scripts/validate.sh)) on changes under `infra/`.

**Check.** Read the watcher's run history in the Actions tab.

**Limits.** The watcher finds new or changed keys, not misuse of a known key. Its pinned set is not
public.

## What the service is not

Evermatter Cloud is not zero-knowledge or end-to-end encrypted. The edge and gateway see requests and
responses. Substrate Systems holds the keys, so it could deliberately read your content. When Home and
its viewer are on, Substrate on Vercel sees what they show (`callCloudViewerTool` in `cloud-home-mcp.ts`).
`cloudHomeChannelEnabled` and `cloudViewerEnabled` are off by default. Home's own grants do not appear
in your app list (`listOwnerApps` in
[owner-apps.ts](https://github.com/substrate-systems/substrate/blob/main/src/lib/exomem-hosted/owner-apps.ts)).

## Claims that rest on deployment values or third parties

No code here proves these claims from the privacy page
([source](https://github.com/substrate-systems/substrate/blob/main/src/app/exomem/privacy/page.tsx)).

- "Hetzner for the servers in Germany" rests on a deployment value. `server_location` in
  [variables.tf](../infra/terraform/foundation/variables.tf) defaults to a German location, but also
  accepts one in Finland.
- "Backblaze for encrypted backups in its EU region" rests on a deployment value: the bucket region.
- "Paddle handles payment-card details" rests on a third party, Paddle.
