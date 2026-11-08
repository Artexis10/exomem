<!-- authority:non-specification -->

# Cloud media edge: one canary cell

This procedure turns the media edge on for one canary Cloud cell, checks it, and
turns it off again. It implements `add-cloud-media-store`; it does not authorize a
live rollout. The edge is the only workload that holds a key that can write to the
media bucket. cellctl holds a second key that can only list it. A cell holds its own
key bundle and a credential that names only that cell.

Do not set `mediaEdge.allCells` here. Turn the fleet switch on later, after the
canary has served real traffic.

## Before you start

- The runtime image carries `python -m exomem.media_edge`. The edge runs
  `artifactBroker.image`, so set that value to the reviewed digest. A tag is refused.
- The control database has Substrate migration `0081_exomem_cloud_media_keys`. It adds
  `media_key_wrapped` and `media_key_version` to `exomem_cloud_cells`, and the grants
  script that follows every migration lets `exomem_cellctl` update both. Apply it
  before you set `mediaEdge.enabled=true`. With the edge on and the columns missing,
  cellctl stops at startup with an error that names the migration, and the deploy fails.
  With the edge off, cellctl runs on a schema with or without the columns.
- The edge's bucket and its application key are separate from cellctl's backup key.
  Create a bucket key limited to the media bucket.
- cellctl meters each cell's stored bytes with a second key for the same bucket. It can only list.
  Terraform creates it with the edge key, in `infra/terraform/durability/media.tf`.
- The key watcher's pinned set lists both keys and the bucket's settings. Pin them as
  "Pin the media keys" in `cloud-key-custody.md` says, right after the Terraform apply.

## Create the Secrets

Create the signing pair in a scratch directory outside any repository. Delete the
directory when you finish.

1. Create the private key: `openssl genpkey -algorithm ed25519 -out signing-key.pem`
2. Derive the public key: `openssl pkey -in signing-key.pem -pubout -out public-key.pem`
3. Create the signing Secret in `exomem-cloud`:
   `kubectl -n exomem-cloud create secret generic exomem-media-edge-signing-key --from-file=signing-key=signing-key.pem`
4. Create the public-key Secret:
   `kubectl -n exomem-cloud create secret generic exomem-media-edge-public-key --from-file=public-key=public-key.pem`
5. Create the bucket-key Secret:
   `kubectl -n exomem-cloud create secret generic exomem-media-edge-b2 --from-literal=keyId=<key id> --from-literal=applicationKey=<application key>`

6. Create the list-key Secret for cellctl. Take the values from the `cloud_media_list_application_key_id` and
   `cloud_media_list_application_key` outputs of `infra/terraform/durability`:
   `kubectl -n exomem-cloud create secret generic exomem-cloud-media-list --from-literal=keyId=<key id> --from-literal=applicationKey=<application key>`

The private key goes only to cellctl. The edge receives the public key and the
bucket key, and never a cell's keys. cellctl receives the list key, which cannot read or change a file.
For a managed deployment, deliver the same four Secrets through the static-secret
procedure instead of `kubectl`.

## Turn it on for the canary

1. Pick the canary cell ID: 16 lowercase base32 characters.
2. Pick a free address in the Service range for the edge. Record it.
3. Set these values and deploy the chart:
   `mediaEdge.enabled=true`, `mediaEdge.endpoint=http://<address>:8769`,
   `mediaEdge.bucket=<bucket>`, `mediaEdge.bucketId=<bucket id>`, `mediaEdge.cellIds=["<canary>"]`.
   Take the bucket name and ID from the `cloud_media_bucket_name` and `cloud_media_bucket_id` outputs.
   `mediaEdge.physicalHeadroom` defaults to 2: cellctl alerts when a cell's prefix holds more than
   `mediaEdge.storePolicy.allowance_bytes` times that factor. It is a starting figure. Change it from
   the readings in the cellctl log line `holds <n> media byte(s)` once a canary has run for a month.
4. Check that `exomem-media-edge` is Ready and that its Service address matches `mediaEdge.endpoint`.
5. Check that ConfigMap `exomem-media-edge-active-set` lists the canary in `active-set.json`.
6. Wait for cellctl to re-render the canary. Check that its Secret `cell-credentials` has the keys
   `media-edge-credential`, `media-age-identity`, `media-name-key` and `media-store.json`.
7. Check that the canary's namespace carries the label that opens the network to the edge:
   `kubectl get namespace exo-cell-<canary> -o jsonpath='{.metadata.labels.exomem\.io/media-edge-access}'`
   Expect `true`.
8. Read `/v1/generation` on the edge. `loaded` must equal the generation in the ConfigMap, and `set_sha256` must equal
   the SHA-256 of the ConfigMap's `active-set.json` value. `bucket` must equal `mediaEdge.bucket`.
   The edge reports `generation` only after it drains the requests of the earlier set.

Expected result: the canary's pod restarts once, every other cell keeps its pod, and
the canary reads and writes media through the edge.

## Read the physical meter

After each backup of a cell that has a media store, cellctl lists that cell's prefix
`cells/<cell>/` in the media bucket with the list key. It logs the bytes of all upload versions, the count of
unfinished large files and the count of hide entries. The figure is the operator's. It is not the owner's allowance,
and a failed listing leaves it unknown, never 0. cellctl retries a failed listing after five minutes.

Three platform alerts name the cell and no content:

- `media-hide-entry-<cell_id>` fires on any hide entry. The edge sends no hide request, but its key's
  `writeFiles` capability permits one. So either the edge key or another key hid a name. Check both:
  1. Read the edge's image: `kubectl -n exomem-cloud get deployment exomem-media-edge -o jsonpath='{.spec.template.spec.containers[0].image}'`
     Expect the reviewed digest that `artifactBroker.image` names.
  2. Read the edge's log for errors around the listing: `kubectl -n exomem-cloud logs deployment/exomem-media-edge`
  3. Read the key watcher's last run. Expect no key that its pinned set does not list.
  4. If none of these explains the hide, read the provider's event log for the bucket.
- `media-over-allowance-<cell_id>` fires when the prefix holds more than the allowance times
  `mediaEdge.physicalHeadroom`. Kept versions count in full until the follow-on change destroys them, so a
  cell that rewrites its originals grows this figure without growing the owner's.
- `media-meter-unknown-<cell_id>` fires when three listings in a row fail, or after three tries without the
  list key. While it fires, the other two alerts cannot fire for that cell. Check that Secret
  `exomem-cloud-media-list` holds the key that Terraform created, and that the provider is up. cellctl logs
  each failed listing with the error type.

Each alert resolves on the next listing that shows its cause gone. The allowance and unknown alerts also resolve
when the cell is deleted or leaves the store. The hide alert does not: no later listing shows that the hide entry
is gone, so resolve it at the receiver after you have checked it. A restart of cellctl forgets that an alert
fired: it fires again at the next listing, but it does not resolve one that was fixed during the restart.
Resolve that one at the receiver.

Without the list key, cellctl still starts. It logs once at startup that the media list key is missing, and it
meters no cell. Each try counts as a failed listing, so `media-meter-unknown` fires for each media cell after
three tries. Create the Secret and restart cellctl as in "Upgrade a release that has the edge on".

## Upgrade a release that has the edge on

A release with the physical meter gives cellctl a second key for the media bucket. cellctl starts without it,
but it then meters nothing. Do these steps once, for the first such upgrade.

1. Apply the durability root and pin the new key, as "Pin the media keys" in `cloud-key-custody.md` says.
2. Create the list-key Secret as in step 6 of "Create the Secrets".
3. Set `mediaEdge.bucketId` in the private values file. The chart refuses an edge-on render without it.
4. Deploy the chart with the command in step 2 of "Undo".
5. If you created the Secret after the deploy, restart cellctl so that it reads the key:
   `kubectl -n exomem-cloud rollout restart deployment/cellctl`
6. Read cellctl's startup log: `kubectl -n exomem-cloud logs deployment/cellctl | grep "media list key"`
   Expect no output.

Expected result: after the next backup of each media cell, cellctl logs `holds <n> media byte(s)` for it.

## Replace one cell's credential

1. Raise the cell's number in `mediaEdge.credentialEpochs`, for example `{"<canary>": 1}`.
2. Deploy the chart.
3. Check that the ConfigMap lists the cell with a higher version and that the generation rose.

The old credential stops working when the edge loads the new set. Treat a revocation as
finished only when `/v1/generation` reports that generation as `loaded` and as `generation`,
with the digest of the published set. A higher generation with another digest is not the published set.

## Undo

A cell that leaves the edge drains before it loses its edge access. While it drains,
it keeps its key bundle, its credential, its egress to the edge, its namespace label
`exomem.io/media-edge-access` and its active-set entry. Its store settings say
`"enabled":false`, so it offloads nothing more. Every evicted original stays readable,
because the cell still hydrates it through the edge.

Known gap: the current cell image evicts only while its store is enabled, so a draining
cell evicts nothing. Each original that it hydrates stays on its volume, and a cell with
more evicted originals than free space can fill its volume. A follow-up change to the
cell runtime lets a draining cell evict and report its shortfall.

cellctl removes the edge access only after a backup report that the cell captured after
the turn-off shows no evicted original. The current cell image reports `unknown` for
that count, so a cell that leaves the edge keeps its edge access until a later image
counts its evicted originals.

1. Remove the cell from `mediaEdge.cellIds` in the private values file. Keep
   `mediaEdge.enabled=true`.
2. Deploy the chart:
   `helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform --values infra/helm/platform/values.yaml --values "$EXOMEM_PLATFORM_VALUES" --atomic --wait --timeout 10m`
3. Wait for cellctl to re-render the cell. Its pod restarts once.
4. Read the cell's store settings:
   `kubectl -n exo-cell-<cell> get secret cell-credentials -o jsonpath='{.data.media-store\.json}' | base64 -d`
   Expect `"enabled":false` beside the cell's `key_version` and `recipient`.
5. Check that the credential stays:
   `kubectl -n exo-cell-<cell> get secret cell-credentials -o jsonpath='{.data.media-edge-credential}'`
   Expect output.
6. List the runtime's egress ports:
   `kubectl -n exo-cell-<cell> get networkpolicy runtime-ingress -o jsonpath='{.spec.egress[*].ports[*].port}'`
   Expect `8769`.
7. Read the cell's drain record:
   `psql "$EXOMEM_CELLCTL_DSN" -At -c "SELECT custody_observation->'media_store' FROM exomem_cloud_cells WHERE cell_id='<cell>'"`
   Expect a `turned_off_at` time.
8. After each nightly backup, read the record again. The cell has drained when
   `evicted` is `0` and `captured_at` is later than `turned_off_at`.
9. When the cell has drained, wait for cellctl to re-render it. Repeat steps 5 and 6.
   Expect no credential and no `8769`.
10. Read the namespace label:
    `kubectl get namespace exo-cell-<cell> -o jsonpath='{.metadata.labels.exomem\.io/media-edge-access}'`
    Expect no output.
11. Check that the ConfigMap no longer lists the cell and that `/v1/generation` reports
    the new number and the digest of the published set.

Expected result: the cell serves its local files and keeps its store identity alone, so
it reads no original as deleted. It has no key, credential or edge access.

cellctl keeps the cell's key bundle after an undo. A later return reuses the bundle and
gets a new credential version, so a credential from before the undo stays revoked.

## Turn the edge off

Do not set `mediaEdge.enabled=false` while a cell drains. Without the edge settings,
cellctl can sign no credential, so the cell would lose its edge access with its evicted
originals still on the edge, and they would become unreadable. The chart's upgrade
therefore reads ConfigMap `exomem-media-edge-active-set` and refuses while
`active-set.json` lists any cell. Draining cells stay listed. The refusal names each
cell, and the upgrade changes nothing.

`helm template`, a client-side dry run and `helm rollback` cannot read the cluster, so
they skip this check. Use `helm upgrade` or `helm upgrade --dry-run=server` to turn the
edge off.

1. Take each cell off the edge with the undo above.
2. Read the active set:
   `kubectl -n exomem-cloud get configmap exomem-media-edge-active-set -o jsonpath='{.data.active-set\.json}'`
   Expect `"cells":{}`.
3. If a cell is still listed, read its drain record as in step 7 of the undo. Wait for
   its next backup, then repeat step 2.
4. Set `mediaEdge.enabled=false` and deploy the chart with the command in step 2 of the undo.

Expected result: the upgrade succeeds, and no cell has `8769` in its egress. The state
volume `exomem-media-edge-state` stays after `mediaEdge.enabled=false`. Delete it by hand
only after `/v1/pending` on the edge reported `0` before you turned it off.

## Delete a cell that used the media store

`cellctl` retires the cell's content keys first, then drops the cell from the active set. It reports the cell
`deleted` only after the edge shows that exact set (`GET /v1/generation`), so the alert
`cell-storage-cleanup-pending-<cell_id>` means the edge no longer admits the cell. A cell that stays `deleting`
with the edge up means the edge has not loaded the new set: read the active set and the edge's evidence as in
"Turn the edge off". Then delete the cell's objects with the media mode of the clean-up script. See
"Media bucket of a deleted cell" in `cloud-key-custody.md`.

## Roll back the chart

`helm rollback` skips the active-set check of "Turn the edge off". Check the target
revision before you roll back:

- With this cellctl and the edge on, a rollback acts as the undo for each cell that the
  target's values take off the edge.
- With the edge off, a rollback takes every listed cell to its store identity alone.
  Each cell that has not drained loses access to its evicted originals.
- With an older cellctl, a rollback drops the store settings of every cell. Those
  cells then read their evicted originals as deleted.

1. Read the active set as in step 2 of "Turn the edge off".
2. If it lists a cell, do not roll back to a revision with the edge off.
3. Count the cells that hold a key bundle:
   `psql "$EXOMEM_CELLCTL_DSN" -At -c "SELECT count(*) FROM exomem_cloud_cells WHERE media_key_wrapped IS NOT NULL"`
4. If the count is above `0`, do not roll back past this release. Fix forward.

`--atomic` rolls back a failed deploy by itself, and skips the check too. Deploy this
release once with the edge off before you turn the edge on. A failed first deploy with
the edge on then rolls back to this cellctl, which keeps each keyed cell's store
identity, and not to an older cellctl, which drops it.
