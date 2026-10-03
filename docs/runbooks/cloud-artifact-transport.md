<!-- authority:non-specification -->

# Cloud artifact transport rollout

This procedure activates file-handle preservation for explicitly selected Cloud
cells. The gateway signs short-lived grants; the internal broker fetches only the
authorized public HTTPS descriptors; the runtime stages the bytes and uses the
existing Sources/Evidence writers. The broker is not a custody store. This runbook
implements `serve-cloud-artifact-handles`; it does not authorize a live rollout.

## Release and key prerequisites

The rollout owner records the reviewed runtime/broker image digest, compatible
cellctl image digest, Substrate gateway image digest and platform chart version,
with the release signatures and completed isolation checks. Use the repository's
normal signed release process. `artifactBroker.image` must be digest-pinned and
contain `python -m exomem.artifact_broker`; do not use a tag or an older runtime
image that lacks the broker entry point.

Provision a dedicated Ed25519 key pair through the established secret-management
path. The gateway alone receives the private PEM from
`cloudGateway.artifactSigningKeySecretName` / `artifactSigningKeySecretKey`
(defaults: `exomem-cloud-artifact-signing-key` / `signing-key`), exposed as
`EXOMEM_CLOUD_ARTIFACT_SIGNING_KEY`. The broker receives only the public PEM from
`artifactBroker.publicKeySecretName` / `publicKeySecretKey`
(defaults: `exomem-artifact-broker-public-key` / `public-key`), exposed as
`EXOMEM_ARTIFACT_BROKER_PUBLIC_KEY`. Both Secrets belong in `exomem-cloud`.
Neither cellctl nor a cell receives signing material. Do not reuse the Cloud
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
current transfers/grants to finish (grants live at most 60 seconds). Empty
`artifactBroker.cellIds`, then wait for the selected runtime's endpoint and
8767 edge to disappear and verify its zero-egress policy. Keep the broker
enabled while the controller restores that policy, then disable the broker.
Canonical stored artifacts remain intact. A broker restart rejects grants
issued before its startup; start a fresh client call rather than replaying one.
Rotate a broker key pair through a coordinated disabled-issuance interval;
the broker does not persist replay state or accept overlapping verifier keys.
