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
- The control database has the columns `media_key_wrapped bytea` and
  `media_key_version integer` on `exomem_cloud_cells`, and `exomem_cellctl` may
  update both. Without them cellctl cannot read any cell row. Apply the Substrate
  migration first.
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

Undo is safe only while nothing is evicted. After the cell evicts an original, the edge
holds the only copy of it. Removing the cell from the set then makes that original
unreadable, and a cell without the edge reads it as deleted.

1. Confirm that the cell has evicted no original. If it has, stop: do not undo.
2. Remove the cell from `mediaEdge.cellIds`.
3. Deploy the chart. The cell loses its credential and its store settings at its next re-render.
4. Check that the ConfigMap no longer lists the cell and that `/v1/generation` reports the new number.
5. Set `mediaEdge.enabled=false` only when no cell remains. The state volume `exomem-media-edge-state` stays;
   delete it by hand once no provider write is pending.

cellctl keeps the cell's key bundle after an undo. A later return reuses the bundle and
gets a new credential version, so a credential from before the undo stays revoked.
