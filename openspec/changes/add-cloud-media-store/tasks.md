## 1. Contract amendments

- [x] 1.1 Amend the runtime egress statement in `adopt-exomem-cloud-plain-cells` and `move-cloud-cells-to-local-storage` with the media edge peer. Verify: `openspec validate --all --strict` passes.

## 2. Spike (D6)

- [ ] 2.1 Compare a custom media edge with versitygw and `rclone serve s3`. Check four things: per-caller prefixes from a signed credential, a bucket key without version deletion, ranged reads of a named version, and multipart upload with a declared size. Measure hydration of a 1 GB object from the cluster. Verify: the decision and its measurements are recorded in design.md D6.

## 3. Core states and eviction (D1–D4), testable locally

- [ ] 3.1 Add the stored-object reference, version ID included, as a governed sidecar descriptor key, and add the `original_state` function. Verify: unit tests cover the four states and a reference to a foreign store.
- [ ] 3.2 Add the store interface, a filesystem-backed store, and offload jobs on the durable media job ledger. Verify these with tests:
  - the reference is written only after a confirmed upload;
  - a failed store leaves the job retryable;
  - a duplicate of a live-referenced version reuses it;
  - bytes whose version is no longer referenced upload a new version.
- [ ] 3.3 Encrypt with `age` to the cell's recipient and name objects with the keyed digest. Verify: a test shows that stored bytes are `age` ciphertext and that no object name equals an original's SHA-256.
- [ ] 3.4 Route every consumer that tests physical presence through `original_state`: media reconciliation and provenance, governance companions, the deletion fan-out, orphan healing, move and rename, trash recovery and listing, adoption, backlinks and media locators, vault scans, the file watcher, the media worker and frame reads, and export and download. Verify: one end-to-end test evicts an original, runs reconciliation, governance and the deletion fan-out, and asserts that the sidecar, transcript and vectors remain. Verify that the test fails when any one path tests file existence directly.
- [ ] 3.5 Add hydration and eviction.
  - Hydration: verify, then rename; record the cache identity; bound concurrency and temporary space.
  - Eviction: target, margin, minimum idle age, exclusions and order.
  - Neither publishes a deletion or creation to the deletion fan-out.

  Verify these with tests:
  - a corrupt object fails with `MEDIA_ORIGINAL_UNVERIFIED` and leaves the path empty;
  - reconciliation over a hydrated file with a new inode and change time rewrites no sidecar;
  - eviction and hydration fire no fan-out;
  - eviction skips pending and unconfirmed originals.
- [ ] 3.6 Report evicted originals as stored in processing status, and hydrate before an explicit re-process. Verify: processing tests for both.

## 4. Platform (D4–D7, D9, D10)

- [ ] 4.1 Add the media bucket in the durability Terraform. It needs versioning, a lifecycle that only cancels unfinished multipart uploads, no object lock, and an edge key without version deletion. Verify: the plan adds only the bucket and the key, and the provider's key listing shows the capabilities.
- [ ] 4.2 In cellctl, mint and wrap the media key bundle, sign the per-cell edge credential, and render both into selected cells' Secrets. Verify: cellctl tests show the wrap's additional data, re-rendering from the row, and an edge render that holds neither.
- [ ] 4.3 In the chart, add the edge Deployment and Service (literal ClusterIP, Recreate rollout), the canary list, the fleet switch, and the network and admission rules for selected runtimes and the export packing step. Verify: chart tests show an unchanged default render, only the edge peer added for a selected cell, and nothing added for an unselected cell.
- [ ] 4.4 Build the edge as decided in 2.1, with the active set that cellctl publishes and the generation the edge reports. Verify these with tests:
  - the prefix is taken from the credential;
  - another cell's object is refused;
  - a rotated or deleted cell's validly signed credential is refused;
  - an upload above the per-object bound is refused;
  - no hide, list or delete operation exists.
- [ ] 4.5 Meter media storage in the cell from referenced versions, meter physical bytes in cellctl, and refuse uploads above the allowance with `MEDIA_ALLOWANCE_EXCEEDED`. Verify: tests show refusal before any byte, status for an original left local, and no charge for versions kept only for backups.
- [ ] 4.6 Write a reference manifest from each backup's clone. Add cellctl's version destruction in the backup hold after prune, under the three conditions. Verify these with tests using the fake provider:
  - a version stays while a months-old retained snapshot names it;
  - a sidecar deleted between clone and backup keeps its version;
  - an in-flight upload at capture survives;
  - a same-cell restore of the oldest backup reads every pinned version;
  - a missing manifest blocks all destruction for the cell.
- [ ] 4.7 Extend cell deletion. It revokes the edge credential and observes the edge's generation first, aborts unfinished uploads, then deletes the media prefix and the wrapped bundle. Verify these with cellctl deletion tests using the fake provider and a fake edge:
  - `deleted` is reported only after both are absent;
  - the cell stays `deleting` after a failed listing or an unobserved generation;
  - no upload lands after revocation.
- [ ] 4.8 Update the Cloud privacy copy to name the media store. Verify: the copy review in `cloud-operator-access` passes.
- [ ] 4.9 Report vault storage (without cached originals) and media storage (against the allowance) in status and in the usage cellctl publishes for Home. Verify: tests show no double counting with cached originals, and an unknown media figure after a failed listing.

## 5. Gateway, uploads, imports and export (D8, D10)

- [ ] 5.1 Substrate's change adds the signed-in download route with byte ranges, forwarded to the cell. Verify: Substrate tests show refusal without a session, and ranged playback of an evicted recording in rehearsal.
- [ ] 5.2 Add `exomem cloud download` over OAuth with verify-then-rename. Verify: client tests show no file after a tampered, truncated or redirected download.
- [ ] 5.3 Substrate's change proxies resumable uploads to the cell. Lift the Cloud exclusion of the upload routes by its recorded condition. Verify: Cloud surface tests, and a resumable upload in rehearsal.
- [ ] 5.4 Add streaming import and split S2 admission into volume and store checks, amending S2's contract in its owning change. Verify: an import larger than the volume cap lands without the volume passing its cap.
- [ ] 5.5 Split export into a packing step that reaches only the edge and holds the media key, and an upload step without the key. Size staging, parts and deadlines for exports larger than the cell. Verify: export tests with evicted originals pass, a corrupt object fails the export, and chart tests show the upload step has no media key and the packing step has no public egress.

## 6. Rollout (D11)

- [ ] 6.1 Rehearse on a rehearsal cell: capture, offload, evict, hydrate, play, download, export, turn off and drain, then delete the cell. Verify: the evidence is recorded in this change.
- [ ] 6.2 Enable one canary cell, back-fill and evict to target. Collect hydration records for two to four weeks, then set the eviction target, margin and minimum idle age. Verify: the measurements and settings are recorded in design.md D3.
- [ ] 6.3 After 5.5, turn on the fleet switch under an operator-approved live change, with the drain as its undo. Verify: every selected cell reports an empty offload backlog, and a rehearsal export delivers the largest selected cell's media.
