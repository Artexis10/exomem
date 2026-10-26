## 1. Contract amendments

- [x] 1.1 Amend the runtime egress statement in `adopt-exomem-cloud-plain-cells` and `move-cloud-cells-to-local-storage` with the media edge peer. Verify: `openspec validate --all --strict` passes.

## 2. Spike (D6)

- [ ] 2.1 Compare a custom media edge with versitygw and `rclone serve s3`. Check four things: per-caller prefixes from a signed credential, a bucket key without version deletion, ranged reads, and multipart upload with a declared size. Confirm that the provider's hide works without version-delete capability, and measure hydration of a 1 GB object from the cluster. Verify: the decision and its measurements are recorded in design.md D6.

## 3. Core states and eviction (D1–D4), testable locally

- [ ] 3.1 Add the stored-object reference as a governed sidecar descriptor key and the `original_state` function. Verify: unit tests cover the four states and a reference to a foreign store.
- [ ] 3.2 Add the store interface, a filesystem-backed store, and offload jobs on the durable media job ledger. Verify: tests show that the reference is written only after a confirmed upload, that a failed store leaves the job retryable, and that duplicate originals share one object.
- [ ] 3.3 Encrypt with `age` to the cell's recipient and name objects with the keyed digest. Verify: a test shows that stored bytes are `age` ciphertext and that no object name equals an original's SHA-256.
- [ ] 3.4 Route the reconciler, the governance companion check, the deletion fan-out, media processing, export, download and frame reads through `original_state`. Verify: one end-to-end test evicts an original, runs reconciliation, governance and the deletion fan-out, and asserts that the sidecar, transcript and vectors remain. Verify that the test fails when any one path tests file existence directly.
- [ ] 3.5 Add hydration (verify, then rename; restore the modification time; bound concurrency and temporary space) and eviction (target, margin, minimum idle age, exclusions, order). Verify: tests show that a corrupt object fails with `MEDIA_ORIGINAL_UNVERIFIED` and leaves the path empty, that hydration creates no job or sidecar change, and that eviction skips pending and unconfirmed originals.
- [ ] 3.6 Report evicted originals as stored in processing status, and hydrate before an explicit re-process. Verify: processing tests for both.

## 4. Platform (D4–D7, D9, D10)

- [ ] 4.1 Add the media bucket in the durability Terraform. It needs versioning, a lifecycle that deletes hidden versions after at least 35 days, no object lock, and an edge key without version deletion. Verify: the plan adds only the bucket and the key, and the provider's key listing shows the capabilities.
- [ ] 4.2 In cellctl, mint and wrap the media key bundle, sign the per-cell edge credential, and render both into selected cells' Secrets. Verify: cellctl tests show the wrap's additional data, re-rendering from the row, and an edge render that holds neither.
- [ ] 4.3 In the chart, add the edge Deployment and Service (literal ClusterIP, Recreate rollout), the canary list, the fleet switch, and the network and admission rules for selected runtimes and their export Jobs. Verify: chart tests show an unchanged default render, only the edge peer added for a selected cell, and nothing added for an unselected cell.
- [ ] 4.4 Build the edge as decided in 2.1. Verify: tests show the prefix taken from the credential, refusal of another cell's object, refusal of an upload above the allowance, and no version deletion.
- [ ] 4.5 Meter stored bytes and refuse uploads above the allowance with `MEDIA_ALLOWANCE_EXCEEDED`. Verify: tests show refusal before any byte and status for an original left local.
- [ ] 4.6 Add the last-reference hide and the orphan sweep. Verify: tests show the hide after trash is emptied and the sweep after the grace period, and no hide while any sidecar still references the object.
- [ ] 4.7 Extend cell-deletion proof to the media prefix and the wrapped bundle. Verify: cellctl deletion tests with the fake provider show `deleted` only after both are absent, and `deleting` after a failed listing.
- [ ] 4.8 Update the Cloud privacy copy to name the media store. Verify: the copy review in `cloud-operator-access` passes.

## 5. Gateway, uploads, imports and export (D8, D10)

- [ ] 5.1 Substrate's change adds the signed-in download route with byte ranges, forwarded to the cell. Verify: Substrate tests show refusal without a session, and ranged playback of an evicted recording in rehearsal.
- [ ] 5.2 Add `exomem cloud download` over OAuth with verify-then-rename. Verify: client tests show no file after a tampered, truncated or redirected download.
- [ ] 5.3 Substrate's change proxies resumable uploads to the cell. Lift the Cloud exclusion of the upload routes by its recorded condition. Verify: Cloud surface tests, and a resumable upload in rehearsal.
- [ ] 5.4 Add streaming import and split S2 admission into volume and store checks, amending S2's contract in its owning change. Verify: an import larger than the volume cap lands without the volume passing its cap.
- [ ] 5.5 Write evicted originals into exports byte-identical through the edge. Verify: export tests with evicted originals pass, and a corrupt object fails the export.

## 6. Rollout (D11)

- [ ] 6.1 Rehearse on a rehearsal cell: capture, offload, evict, hydrate, play, download, export, turn off and drain, then delete the cell. Verify: the evidence is recorded in this change.
- [ ] 6.2 Enable one canary cell, back-fill and evict to target. Collect hydration records for two to four weeks, then set the eviction target, margin and minimum idle age. Verify: the measurements and settings are recorded in design.md D3.
- [ ] 6.3 Turn on the fleet switch under an operator-approved live change, with the drain as its undo. Verify: every selected cell reports an empty offload backlog.
