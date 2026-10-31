<!-- authority:non-specification -->

# Cloud media edge: one canary cell

This procedure turns the media edge on for one canary Cloud cell, checks it, and
turns it off again. It implements `add-cloud-media-store`; it does not authorize a
live rollout. The edge is the only workload that holds the bucket credential. A
cell holds its own key bundle and a credential that names only that cell.

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
  With the edge off, cellctl runs on a schema with or without the columns, unless a
  cell still drains (see "Turn the edge off").
- The edge's bucket and its application key are separate from cellctl's backup key.
  Create a bucket key limited to the media bucket.

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

The private key goes only to cellctl. The edge receives the public key and the
bucket key, and never a cell's keys. For a managed deployment, deliver the same
three Secrets through the static-secret procedure instead of `kubectl`.

## Turn it on for the canary

1. Pick the canary cell ID: 16 lowercase base32 characters.
2. Pick a free address in the Service range for the edge. Record it.
3. Set these values and deploy the chart:
   `mediaEdge.enabled=true`, `mediaEdge.endpoint=http://<address>:8769`,
   `mediaEdge.bucket=<bucket>`, `mediaEdge.cellIds=["<canary>"]`.
4. Check that `exomem-media-edge` is Ready and that its Service address matches `mediaEdge.endpoint`.
5. Check that ConfigMap `exomem-media-edge-active-set` lists the canary in `active-set.json`.
6. Wait for cellctl to re-render the canary. Check that its Secret `cell-credentials` has the keys
   `media-edge-credential`, `media-age-identity`, `media-name-key` and `media-store.json`.
7. Read `/v1/generation` on the edge. It must report the generation in the ConfigMap.
   The edge reports a generation only after it drains the requests of the earlier set.

Expected result: the canary's pod restarts once, every other cell keeps its pod, and
the canary reads and writes media through the edge.

## Replace one cell's credential

1. Raise the cell's number in `mediaEdge.credentialEpochs`, for example `{"<canary>": 1}`.
2. Deploy the chart.
3. Check that the ConfigMap lists the cell with a higher version and that the generation rose.

The old credential stops working when the edge reports the new generation. Treat a
revocation as finished only when `/v1/generation` reports that number.

## Undo

A cell that leaves the edge drains before it loses its edge access. While it drains,
it keeps its key bundle, its credential, its egress to the edge, its namespace label
`exomem.io/media-edge-access` and its active-set entry. Its store settings say
`"enabled":false`, so it offloads nothing more. Eviction keeps running, so a hydrated
copy leaves the volume again, and every evicted original stays readable.

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
    the new number.

Expected result: the cell serves its local files and keeps its store identity alone, so
it reads no original as deleted. It has no key, credential or edge access.

cellctl keeps the cell's key bundle after an undo. A later return reuses the bundle and
gets a new credential version, so a credential from before the undo stays revoked.

## Turn the edge off

Do not set `mediaEdge.enabled=false` while a cell drains. Without the edge settings,
cellctl can sign no credential, so the cell would lose its edge access with its evicted
originals still on the edge, and they would become unreadable. cellctl therefore stops
at startup and names each cell that has not drained, and `--atomic` restores the
release before.

1. Take each cell off the edge with the undo above.
2. List the drain record of each cell that holds a key bundle:
   `psql "$EXOMEM_CELLCTL_DSN" -At -c "SELECT cell_id, custody_observation->'media_store' FROM exomem_cloud_cells WHERE media_key_wrapped IS NOT NULL AND desired_state <> 'deleted'"`
3. If a record does not show a drain as in step 8 of the undo, wait for its next backup.
4. Set `mediaEdge.enabled=false` and deploy the chart with the command in step 2 of the undo.

Expected result: cellctl starts, and no cell has `8769` in its egress. The state volume
`exomem-media-edge-state` stays after `mediaEdge.enabled=false`. Delete it by hand only
after `/v1/pending` on the edge reported `0` before you turned it off.

## Roll back the chart

`helm rollback` to a revision with this cellctl acts as an undo for every cell that
revision's values take off the edge. A rollback to a revision with
`mediaEdge.enabled=false` fails while a cell drains, because cellctl stops at startup.
A revision with an older cellctl drops the store settings of every cell, and those
cells then read their evicted originals as deleted.

1. Before you roll back past this release, count the cells that hold a key bundle:
   `psql "$EXOMEM_CELLCTL_DSN" -At -c "SELECT count(*) FROM exomem_cloud_cells WHERE media_key_wrapped IS NOT NULL"`
2. If the count is above `0`, do not roll back past this release. Fix forward, and take
   cells off the edge with the undo above.

`--atomic` rolls back a failed deploy by itself. Deploy this release once with the edge
off before you turn the edge on, so that an automatic rollback finds no key bundle.
