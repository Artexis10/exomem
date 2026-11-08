<!-- authority:non-specification -->

# Cloud media edge: one canary cell

This procedure turns the media edge on for one canary Cloud cell, checks it, and
turns it off again. It implements `add-cloud-media-store`; it does not authorize a
live rollout. The edge is the only workload that holds a key that can write to the
media bucket. cellctl holds a second key that can only list it. A cell holds its own
key bundle and a credential that names only that cell.

Each media cell's backup Job holds a second credential, for collection. With it, the Job
condemns through the edge each name that the cell released and that no retained backup
reaches. The edge hides that name. The bucket's lifecycle rule deletes a hidden version
after the grace period that you choose. "Turn on collection" sets this up.

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
`cells/<cell>/` in the media bucket with the list key. It logs one line for the cell with these figures:

- the bytes of all upload versions;
- the count of unfinished large files;
- the count of names with more than one upload version;
- the count of hidden names, followed by each hidden name.

The figure is the operator's. It is not the owner's allowance. A failed listing leaves it unknown, never 0.
cellctl retries a failed listing after five minutes. If the provider does not list the names in order, the
count of repeated names is unknown.

A hidden name is normal: the cell's backup Job hides each name that the owner deleted, once no retained
backup reaches it. cellctl raises no alert for a hide. The key watcher outside the cluster alerts on a burst
of hides, as "Triage a burst of hides" says.

The line is a warning when the count of repeated names is not 0. The lifecycle rule deletes the older
version of a repeated name, so "Turn on collection" requires that count to be 0.

Two platform alerts name the cell and no content:

- `media-over-allowance-<cell_id>` fires when the prefix holds more than the allowance times
  `mediaEdge.physicalHeadroom`. Hidden and superseded versions count in full until the lifecycle rule
  deletes them, so a cell that deletes many originals grows this figure for the grace period.
- `media-meter-unknown-<cell_id>` fires when three listings in a row fail, or after three tries without the
  list key. While it fires, the allowance alert cannot fire for that cell. Check that Secret
  `exomem-cloud-media-list` holds the key that Terraform created, and that the provider is up. cellctl logs
  each failed listing with the error type.

Each alert resolves on the next listing that shows its cause gone, or when the cell is deleted or leaves the
store. A restart of cellctl forgets that an alert fired: it fires again at the next listing, but it does not
resolve one that was fixed during the restart. Resolve that one at the receiver.

An earlier cellctl raised `media-hide-entry-<cell_id>` for any hide. This cellctl never sends or resolves
it, so resolve an open one at the receiver.

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

## Turn on collection

Collection needs a release that carries `collect-released-media`, the bucket's lifecycle rule with a grace
period, and the cell on `mediaEdge.collectCells`. Each backup that prunes then hides the names that its cell
released and that no retained backup reaches. The rule deletes each hidden or superseded version when the
grace period ends. No rule hides a version by age.

Before you start:

- Choose the grace period in days and the hide budget. The grace period must exceed the watcher's
  `window_hours`, plus one hour between its runs, plus your response time.
- Authorize the provider check of task 1 of `collect-released-media`, with sandbox credentials. Its
  results are recorded in that change's design.
- Agree the pinned-set change with the owner of `contain-cloud-controller-compromise`. That change owns
  the watcher's pinned set.

1. Keep `mediaEdge.collectCells` empty. Deploy the edge, cellctl and the chart with the command in step 2 of "Undo".
2. Wait for the edge to roll out: `kubectl -n exomem-cloud rollout status deployment/exomem-media-edge`
   Expect `successfully rolled out`.
3. After step 2, roll the new cell image out through cellctl. A new cell treats a HEAD answer without a
   version status as an outage, so the edge must serve the new image first.
4. Read the canary's last meter line:
   `kubectl -n exomem-cloud logs deployment/cellctl | grep "cell <canary> holds" | tail -n 1`
   Expect `0 name(s) with more than one upload version`.
5. If the count is above 0 or unknown, stop and do not apply the rule. See the gate note below.
6. Set the durability variable `cloud_media_condemned_days` to the grace period, with the root's other private values.
7. Plan the durability root: `infra/scripts/plan.sh durability <plan>`
   Expect two changes: the media bucket's rule gains `days_from_hiding_to_deleting`, and one new key,
   `key_watcher_media_list`. A plan without the variable fails and names it.
8. With the operator's approval, apply that plan: `infra/scripts/apply_saved_plan.sh durability <plan>`
9. Store the watcher's media key ID:
   `terraform -chdir=infra/terraform/durability output -raw key_watcher_media_list_application_key_id | gh secret set B2_WATCHER_MEDIA_KEY_ID --env key-watcher`
10. Store the watcher's media key:
    `terraform -chdir=infra/terraform/durability output -raw key_watcher_media_list_application_key | gh secret set B2_WATCHER_MEDIA_KEY --env key-watcher`
11. Pin the new key and the media watch, as "Pin the media watch" in `cloud-key-custody.md` says.
12. Export the operator key for the media bucket, as in step 1 of "Media bucket of a deleted cell" in `cloud-key-custody.md`.
13. Read that key's capabilities:
    `curl -fsS -u "$CELL_CLEANUP_B2_KEY_ID:$CELL_CLEANUP_B2_APPLICATION_KEY" https://api.backblazeb2.com/b2api/v4/b2_authorize_account | jq -c '.apiInfo.storageApi.allowed | {capabilities, buckets: [.buckets[]?.id]}'`
    Expect `deleteFiles` in `capabilities` and the media bucket's ID in `buckets`. A rescue needs both.
14. Add the canary to `mediaEdge.collectCells` in the private values file. Deploy the chart as in step 1.
15. Check that the canary holds the collector credential:
    `kubectl -n exo-cell-<canary> get secret cell-credentials -o jsonpath='{.data.media-edge-collector-credential}'`
    Expect output.
16. Read the active set as in step 2 of "Turn the edge off". Expect the canary under `"collectors"`.
17. After the canary's next backup that prunes, read the collection result in cellctl's log:
    `kubectl -n exomem-cloud logs deployment/cellctl | grep "cell <canary>'s media collection" | tail -n 1`
    Expect `media collection complete (-): <n> name(s) hidden of <m> candidate(s), 0 refused by the edge`.

Expected result: the watcher's run shows `<n> media hides in the window` and no `MEDIA_` token, and
cellctl logs `media collection complete` for the canary after each backup that prunes.

The backup Job's pod is gone before you can read its log, so the Job reports its result to cellctl, which
logs it when the backup ends. The result carries counts and a reason, never a name. "Refused by the edge"
counts the names that the edge refused one at a time; the pass went on past each of them. Another outcome
names its reason in parentheses:

| Result | Meaning |
|---|---|
| `unknown-manifest (-)` | A retained backup has no complete manifest, so the Job hid nothing. Collection starts once the backups from before the release are pruned. |
| `stopped (edge-outage)` | The edge did not answer, or answered with a server error. The next backup tries again. |
| `stopped (edge-refused)` | The edge refused the collector credential. Repeat steps 15 and 16. |
| `stopped (edge-answer)` | The edge answered in a way the Job does not know. Check the edge's image as in step 2 of "Triage a burst of hides". |
| `stopped (restic)` | restic could not list the retained backups. |
| `stopped (credential)` or `stopped (unconfigured)` | The Job could not read its collector credential, or has none. Repeat steps 15 and 16. |
| `stopped (deadline)` | The Job's time ran out during the pass. The next backup goes on from the names that are still visible. |
| `stopped (fault)` or `stopped (no-result)` | The pass failed or printed no result. Read the next backup's Job log while its pod exists. |

Gate note: a name with two upload versions loses the older one when the rule applies. If the canary's
reference names the older version, its original would be lost. This release has no tool that maps a stored
name to its original. The cell offloads an original again under a new name when its version is not current:
at its next eviction pass if it holds the bytes, and after a read that hydrates them if it does not. Ask the
canary's owner to open the evicted originals, then repeat step 4 after the next backup.

No cell collects before step 14, so no hide exists before the rule applies.

The undo is the grace period: rescue a version, as "Rescue a hidden version" says, before it ends.
To stop collection for a cell, take it off `mediaEdge.collectCells` and deploy the chart. Its collector entry
and its credential go, and its backups hide nothing more.

## Triage a burst of hides

The key watcher lists the media bucket with its own list-only key, once an hour. It counts the hide markers
of the last `window_hours` that its pinned media watch names. Above `hide_budget`, it raises
`<account>:MEDIA_HIDE_BURST`. If it cannot list, it raises `<account>:MEDIA_LISTING_FAILURE` and leaves an
open burst alert as it is. The alerts carry no name and no count. The run's log carries the count.

A compromised edge can hide any name, and a compromised backup Job can hide names under its own cell's
prefix. The provider deletes each hidden version when the grace period ends, so act before then.

1. Read the count in the watcher's last run:
   `gh run view "$(gh run list --workflow b2-key-watcher.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --log | grep "media hides"`
   Expect `account <account>: <n> media hides in the window`.
2. Read the edge's image: `kubectl -n exomem-cloud get deployment exomem-media-edge -o jsonpath='{.spec.template.spec.containers[0].image}'`
   Expect the reviewed digest that `artifactBroker.image` names.
3. Read the edge's request log around the hide time:
   `kubectl -n exomem-cloud logs deployment/exomem-media-edge --since=<hours>h | grep "condemn hid"`
   Each hide that the edge made logs `condemn hid <name> for cell <cell>`. If the edge restarted, add `--previous`.
   A hide that the edge finished after a restart logs `settled write intent` for the cell instead.
4. List the hide markers of each cell in step 3, as in steps 1 to 4 of "Rescue a hidden version", with
   `prefix` set to `cells/<cell>/`.
5. If a marker of the burst has no line in step 3, or the image differs, rescue every marker of the burst.
   Then read the provider's event log for the bucket, and the key watcher's other tokens for an unknown key.
6. If every marker has a line, read each cell's backup Job log as in step 4 of "Turn on collection".
   A cell whose owner deleted many originals at once explains its count.
7. If nothing explains a cell's count, rescue its markers of the burst.

A rescue of a correct hide costs one more hide: the cell's next backup hides the released name again.
So rescue when in doubt.

## Rescue a hidden version

A rescue deletes a hide marker. The version becomes current again, with its content unchanged. Rescue before
the grace period ends: after it, the provider has deleted the version. You need the operator key for the
media bucket from "Media bucket of a deleted cell" in `cloud-key-custody.md`, because it has `deleteFiles`.

1. Export the operator key: `export CELL_CLEANUP_B2_KEY_ID=<key id> CELL_CLEANUP_B2_APPLICATION_KEY=<application key>`
2. Authorize into a private file:
   `(umask 077; curl -fsS -u "$CELL_CLEANUP_B2_KEY_ID:$CELL_CLEANUP_B2_APPLICATION_KEY" https://api.backblazeb2.com/b2api/v4/b2_authorize_account > /run/user/$UID/b2-media.json)`
3. Read the API address and the token:
   `export B2_API="$(jq -r .apiInfo.storageApi.apiUrl /run/user/$UID/b2-media.json)" B2_TOKEN="$(jq -r .authorizationToken /run/user/$UID/b2-media.json)"`
4. List the versions of the name, newest first:
   `curl -fsS -H "Authorization: $B2_TOKEN" "$B2_API/b2api/v4/b2_list_file_versions" -d '{"bucketId":"<media bucket id>","prefix":"<name>","maxFileCount":1000}' | jq -c '(.files[] | {fileName, action, fileId, uploaded: (.uploadTimestamp / 1000 | floor | todate)}), {nextFileName, nextFileId}'`
   Expect the hide marker, with `"action":"hide"`, above the version, with `"action":"upload"`.
   If the last line shows a `nextFileName`, add `startFileName` and `startFileId` from it and run the step again.
5. Copy the `fileId` of the hide marker. Never copy the `fileId` of an `upload` entry: deleting it destroys the original.
6. Delete the hide marker:
   `curl -fsS -H "Authorization: $B2_TOKEN" "$B2_API/b2api/v4/b2_delete_file_version" -d '{"fileName":"<name>","fileId":"<hide marker file id>"}'`
7. Repeat step 4. Expect the `upload` entry at the top, with the same `fileId` as before.
8. Delete the authorization file: `rm /run/user/$UID/b2-media.json`

Expected result: the edge's HEAD reports `current` for the version. If the cell already offloaded the
original again under a new name, the rescued name is released, and the cell's next backup hides it again.

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
11. Check that the ConfigMap no longer lists the cell under `"cells"` and that `/v1/generation`
    reports the new number and the digest of the published set. A cell on `mediaEdge.collectCells`
    stays under `"collectors"`.

Expected result: the cell serves its local files and keeps its store identity alone, so
it reads no original as deleted. Its runtime has no key, credential or edge access. While
the cell stays on `mediaEdge.collectCells`, its backup Job keeps the collector credential and
its egress to the edge, so its backups keep condemning the names that it releases.

cellctl keeps the cell's key bundle after an undo. A later return reuses the bundle and
gets a new credential version, so a credential from before the undo stays revoked.

## Turn the edge off

Do not set `mediaEdge.enabled=false` while a cell drains. Without the edge settings,
cellctl can sign no credential, so the cell would lose its edge access with its evicted
originals still on the edge, and they would become unreadable. The chart's upgrade
therefore reads ConfigMap `exomem-media-edge-active-set` and refuses while
`active-set.json` lists any cell. Draining cells stay listed. The refusal names each
cell, and the upgrade changes nothing.

The check also refuses while the set lists any collector. A cell on `mediaEdge.collectCells`
keeps its collector entry while it holds a store identity, a drained cell included, so its
backups keep collecting. Empty that list before you turn the edge off.

`helm template`, a client-side dry run and `helm rollback` cannot read the cluster, so
they skip this check. Use `helm upgrade` or `helm upgrade --dry-run=server` to turn the
edge off.

1. Take each cell off the edge with the undo above.
2. Read the active set:
   `kubectl -n exomem-cloud get configmap exomem-media-edge-active-set -o jsonpath='{.data.active-set\.json}'`
   Expect `"cells":{}` and no `"collectors"`.
3. If a cell is still listed, read its drain record as in step 7 of the undo. Wait for
   its next backup, then repeat step 2.
4. If a collector is still listed, remove it from `mediaEdge.collectCells`. Deploy the chart
   with the command in step 2 of the undo, then repeat step 2.
5. Set `mediaEdge.enabled=false` and deploy the chart with the command in step 2 of the undo.

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
