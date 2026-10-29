<!-- authority:non-specification -->

# Cloud artifact transport rollout

This procedure activates file-handle preservation for selected Cloud cells: a
list of cells first, then every cell by one switch. The gateway signs short-lived
grants; the internal broker fetches only the authorized public HTTPS descriptors;
the runtime stages the bytes and uses the existing Sources/Evidence writers. The
broker is not a custody store. This runbook implements
`serve-cloud-artifact-handles`; it does not authorize a live rollout.

## Release and key prerequisites

The rollout owner records the reviewed runtime/broker image digest, compatible
cellctl image digest, Substrate gateway image digest and platform chart version,
with the release signatures and completed isolation checks. Use the repository's
normal signed release process. `artifactBroker.image` must be digest-pinned and
contain `python -m exomem.artifact_broker`; do not use a tag or an older runtime
image that lacks the broker entry point.

The dedicated Ed25519 pair is held by the exact BWS entries in
`infra/contracts/bws-cloud-artifact-v1.json`. Read and deliver it through
`secret_handoff.py` using the `cloud_artifact_signing_key` and
`cloud_artifact_public_key` routes in `secret-destinations-v1.json`, source `bws`,
and the next unused destination version. The PEM values use file shape; the
private half goes only to the gateway. The public half goes to the broker and, when
`cloudGateway.uploadCellIds` is set, to cellctl for the cells it lists.
Seal their separate SOPS artifacts, verify the pair in memory, and apply only
those destinations through the existing static-secret deployment procedure.
Refreshing this matrix also requires refreshing its signed active-secret
registry before any subsequent registry-based apply. The gateway alone receives the private PEM from
`cloudGateway.artifactSigningKeySecretName` / `artifactSigningKeySecretKey`
(defaults: `exomem-cloud-artifact-signing-key` / `signing-key`), exposed as
`EXOMEM_CLOUD_ARTIFACT_SIGNING_KEY`. The broker receives only the public PEM from
`artifactBroker.publicKeySecretName` / `publicKeySecretKey`
(defaults: `exomem-artifact-broker-public-key` / `public-key`), exposed as
`EXOMEM_ARTIFACT_BROKER_PUBLIC_KEY`. Both Secrets belong in `exomem-cloud`.
Neither cellctl nor a cell receives the private PEM. The upload handoff
(`cloud-upload-handoff.md`) gives cellctl the same public PEM for the cells it lists. Do not reuse the Cloud
cell-token root or a database credential. Keep key bytes and temporary file
handles out of release receipts, manifests committed to Git and command output.

The broker stays one replica, one process, with Recreate deployment strategy.
Its four transfer slots and replay records are process-local, so overlapping
replicas would invalidate the bounds. It runs as UID/GID 1000 with a read-only
root, no service-account token and only a bounded 1 GiB temporary volume. It has
no vault, database or Kubernetes API access. Broker HTTPS egress reuses
`cells.jobEgressExcept`; the standard private, carrier-grade NAT and metadata
exclusions must remain present. Include any additional private cluster ranges.

## Stage, preflight, activate

1. With `cloudGateway.artifactTransportEnabled=false`, deploy the compatible
   chart/controller and broker using `artifactBroker.enabled=true`, its reviewed
   image, `artifactBroker.cellIds=[]` and `artifactBroker.endpoint=""`. This creates
   the internal Service and broker with no cell ingress. Check the public-key
   reference, ready endpoint `/health/ready` and the broker's actual image digest.
   Owner, ordinary QA and fleet cells retain their environment, digest and
   zero-egress runtime policy.
2. Read the `exomem-artifact-broker` Service in `exomem-cloud` and record its
   actual ClusterIP, port 8767 and endpoints selecting only broker pods. The
   address must be literal RFC1918 IPv4. Set `artifactBroker.endpoint` to
   `http://<actual ClusterIP>:8767` (an optional trailing slash is allowed).
   The chart pins that same address on the existing Service; a mismatch must be
   corrected before activation. Hostnames, TLS URLs, other ports, userinfo,
   paths, queries and fragments are rejected. There is no runtime DNS lookup or
   controller Service-discovery permission.
3. Resolve the reviewer account to its actual cell ID through the authorized
   read-only control-plane view. Set the single canonical
   `artifactBroker.cellIds` list to that reviewer cell only; keep the identifier
   in private operator values, not in repository defaults. Valid identifiers
   are 16 lowercase base32 characters. Cellctl receives the list as JSON in
   `CELLCTL_ARTIFACT_BROKER_CELL_IDS` and the endpoint in
   `CELLCTL_ARTIFACT_BROKER_URL`. Only selected cells receive
   `EXOMEM_CLOUD_ARTIFACT_BROKER_URL` and an updated render digest. Admission
   allows their exact broker namespace/pod peer on TCP 8767. The broker admits
   only those namespaces' runtime pods, excluding backup/restore Job labels.
4. Wait for the reviewer runtime to converge on the compatible image and
   endpoint. Verify the actual admission policy and both NetworkPolicies before
   enabling issuance. Turn on `cloudGateway.artifactTransportEnabled`; the
   gateway receives `EXOMEM_CLOUD_ARTIFACT_TRANSPORT_ENABLED=true`, the same
   `EXOMEM_CLOUD_ARTIFACT_CELL_IDS` JSON list and its separate signing-key
   reference. Enabling issuance without an enabled broker is rejected. The
   gateway's network policy remains unchanged; it does not download files.

Keep every existing resource/lifecycle setting, identity, saved purpose and
preference unchanged. Do not change owner or ordinary QA cell images merely to
activate the reviewer. The live rollout belongs to its named owner and the
reviewed source/image/chart prerequisites; source verification alone is not live
acceptance.

## Enable the broker for every cell

Do this after the broker has served the listed cells, as the previous section
describes. `artifactBroker.allCells` gives the broker to every existing cell and
to each cell created later, so a new tenant needs no edit. Keep `cellIds` as
set: it stays valid beside the switch. A listed cell keeps its pod. Only the
cells that are not listed restart, once. The chart renders as before while
`allCells` is `false`.

The gateway signs grants only for the cells it selects. The gateway image must
read `EXOMEM_CLOUD_ARTIFACT_ALL_CELLS`, which the chart sets from `allCells`. An
older gateway image ignores it and signs for the listed cells only. With an older
gateway, the unlisted cells still restart, receive the broker URL and egress,
and then fail every file call with `SAFE_FETCH_FAILED`, because no grant
arrives. Deploy the gateway change first, or in the same release.

The broker has four global transfer slots and keeps 1024 nonce records. Those
bounds suit one canary cell. Under `allCells` every tenant shares them, so
revisit them (decision 4 of `serve-cloud-artifact-handles`) before the number
of tenants grows.

1. Check that the `exomem-artifact-broker` Deployment is Ready and `/health/ready` answers.
2. Check that a listed cell preserved a real client attachment, which qualifies the broker.
3. Check that the gateway image to deploy contains Substrate pull request 262. In the Substrate repository, run `git merge-base --is-ancestor <merge commit of 262> <image source commit>`.
4. Run `kubectl get pods -A -l 'app.kubernetes.io/name=exomem-cell,!exomem.io/cell-job' -o custom-columns=NAMESPACE:.metadata.namespace,STARTED:.status.startTime` and save the output as the before snapshot.
5. Set `artifactBroker.allCells` to `true` in the private operator values.
6. Deploy the platform chart and the compatible cellctl and gateway images by the normal release path.
7. Run the command from step 4 again and compare it with the before snapshot.
8. Check that only cells outside `cellIds` have a new start time, and that each listed cell kept its start time.
9. Pick one newly covered cell. Read its `cell` StatefulSet and check that `EXOMEM_CLOUD_ARTIFACT_BROKER_URL` is the broker endpoint.
10. Read that cell's `runtime-ingress` NetworkPolicy and check that its one egress rule reaches the broker pod on TCP 8767.
11. Operator step: attach a synthetic file in a real client to a tenant on a newly covered cell.
12. Ask the client to preserve the file, then read it back and compare its size and SHA-256 with the original.

Expect step 12 to return the stored path, size and SHA-256 of the attached file.
A real client is required because only a client can produce an attachment handle;
a test call cannot prove the gateway signs for the cell. If step 3 fails, ship
the gateway change before step 5. If step 9 or 10 fails, set `allCells` to
`false` and deploy. Cells then return to the explicit list and render as before.

## Acceptance and rollback

Use isolated synthetic files for write probes. Before a live native attachment
probe, record its exact authorized reviewer tenant and confirm that provider
sends, publishing, payments and other unrelated effects are blocked. Verify a
native file-handle call commits through the canonical writer and returns its
stored reference, path, size and SHA-256. Make a fresh read through the product
and compare the bytes/hash independently with the original synthetic file.
Never infer preservation from a tool menu or a successful HTTP hop.

Verify selected runtime access to the broker on 8767 and denial of DNS, direct
public HTTPS, metadata, other cells, wrong broker labels/namespaces and wrong
ports. Verify unselected runtime and Job access to the broker remains denied.
Record the owner and ordinary QA digests/environment/policy before and after;
they must be unchanged. Check ordinary recall and saved preferences as well.
Bounded transport failures must report file failure without a stored path/hash
or direct-network fallback. Directory Submit/Publish remains held for review.

To roll back, disable `cloudGateway.artifactTransportEnabled` first and wait for
current transfers/grants to finish (grants live at most 60 seconds). Set
`artifactBroker.allCells` to `false` if it is on, empty
`artifactBroker.cellIds`, then wait for the selected runtime's endpoint and
8767 edge to disappear and verify its zero-egress policy. Keep the broker
enabled while the controller restores that policy, then disable the broker.
Canonical stored artifacts remain intact. A broker restart rejects grants
issued before its startup; start a fresh client call rather than replaying one.
Rotate a broker key pair through a coordinated disabled-issuance interval;
the broker does not persist replay state or accept overlapping verifier keys.
