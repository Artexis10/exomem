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
  - eviction skips pending and unconfirmed originals (2026-10-30: M4 in 5.4.3 narrows "pending" to running jobs);
  - a vault restore that brings back a reference to a destroyed version re-offloads the original and never evicts it on that reference.
- [ ] 3.6 Report evicted originals as stored in processing status, and hydrate before an explicit re-process. Verify: processing tests for both.
- [ ] 3.7 Close the recorded pre-canary repairs: terminal verification refusals and targeted retry, current-bound recovery, indexed protections and scoped pins, local staging errors, catalog admission before hydration, bounded partial overview, and exact-path job and index status. Verify: retained-ledger migration, repaired-input recovery, live capacity exclusion, refused and successful moves, and restricted local and evicted-original status through the real dispatcher. Record query plans against a populated temporary ledger. These repairs follow the disabled foundation as one rollout-dependent batch.
  - [ ] 3.7.1 Distinguish recorded lifecycle state from observed activity. Verify that queued work is explicit, a stale running claim has unknown activity, and reconciliation health does not imply inference. Keep all exact activity facts in the result row that governance can withhold.

## 4. Platform (D4–D7, D9, D10)

- [ ] 4.1 Add the media bucket in the durability Terraform. It needs versioning, a lifecycle that only cancels unfinished multipart uploads, no object lock, and an edge key without version deletion. Verify: the plan adds only the bucket and the key, and the provider's key listing shows the capabilities.
  - The bucket and bucket-restricted read/write key are implemented, disabled by default. All five mocked durability plans pass, including two media cases. The provider plan and live key listing remain unverified; no provider resources were created.
- [x] 4.2 In cellctl, mint the media bundle, wrap it under the cell root, and sign the per-cell edge credential. Render the standard-mode bundle and edge credential into selected cells' Secrets. Tenant-lock enablement stays with `cloud-cell-key-custody`; this task adds no unlock channel. Verify: tests show the cell-root wrap and additional data, standard-mode re-rendering, and an edge render that holds neither key.
- [x] 4.3 In the chart, add the edge Deployment and Service (literal ClusterIP, Recreate rollout), the canary list, the fleet switch, and the network and admission rules for selected runtimes. Verify: chart tests show an unchanged default render, only the edge peer added for a selected cell, and nothing added for an unselected cell.
- [ ] 4.4 Build the edge as decided in 2.1, with the active set that cellctl publishes and the generation the edge reports. Verify these with tests:
  - the prefix is taken from the credential;
  - another cell's object is refused;
  - a rotated or deleted cell's validly signed credential is refused;
  - an upload above the per-object bound is refused;
  - no hide, list or delete operation exists.
  - a restart retains uncertain write intent, and missing HEAD does not produce a drained-generation acknowledgement;
  - a lost successful upload reply recovers its exact version, while unrelated cells continue to work.
  - Multipart initiation recovery and retained edge-state drain evidence remain incomplete. Complete them before enabling media cleanup.
- [ ] 4.5 Meter media storage in the cell from referenced versions, meter physical bytes in cellctl, and refuse uploads above the allowance with `MEDIA_ALLOWANCE_EXCEEDED`. Verify: tests show refusal before any byte, status for an original left local, and no charge for versions kept only for backups.
- [ ] 4.5.1 Add a complete owner reference census inside the maintained filesystem boundary and give custody readers a frontmatter mode that refuses unreadable headers while ordinary strict reads keep their meaning. Verify: private state stays excluded; ordinary aliases, read failures and malformed potential companions produce unknown usage; valid plain notes do not.
- [ ] 4.5.2 Preserve canonical references and durable placement metadata through Trash deletion and recovery. Verify: binary, companion and directory paths retain charges until their last reference is removed; interrupted handoff and rollback keep recoverable bytes and known custody.
- [ ] 4.5.3 Configure the allowance once and enforce it before offload encryption or provider upload. Verify: plaintext version deduplication, backup-only exclusion, refusal with local bytes retained, recovery after freed space or changed limits, and publication-time reference changes.
- [ ] 4.5.4 Before allowance enforcement is enabled for a cell, show the vault owner the path and reason of each note that makes media usage unknown. One unreadable note makes the whole allowance unknown and defers every offload, so the owner needs the cause to repair it. Verify: an unknown allowance names the offending path and reason in status and in the refusal.
  - These cell-side steps are partial implementation of 4.5. Physical metering and complete ingress admission remain required; 4.5 and 4.9 stay unchecked until their full contracts are proved.
- [ ] 4.6 Write a reference manifest from each backup's clone. Add cellctl's version destruction in the backup hold after prune, under the three conditions. Verify these with tests using the fake provider:
  - a version stays while a months-old retained snapshot names it;
  - a sidecar deleted between clone and backup keeps its version;
  - an upload whose ledger commit is newer than the clone's recovered state, but whose protection record was fully synced first, survives;
  - a reuse whose source sidecar is deleted before capture survives;
  - a same-cell restore of the oldest backup reads every pinned version;
  - a missing manifest blocks all destruction for the cell.
- [ ] 4.7 Extend cell deletion. Retire media content keys under `cloud-cell-key-custody` independently of provider cleanup. Keep edge revocation and generation observation before upload aborts and prefix deletion. Verify these with cellctl deletion tests using the fake provider and a fake edge:
  - `deleted` is reported only after the versions and bundle are absent and the existing cell deletion proof holds;
  - the cell stays `deleting` after a failed listing or an unobserved generation;
  - no upload lands after revocation.
  - a provider outage does not postpone content-key retirement or produce an unsupported destruction claim.
- [ ] 4.8 Update the Cloud privacy copy to name the media store and its enforced custody mode. Verify: the copy review in `cloud-operator-access` passes, with no tenant-held claim before `lock_complete_at`.
- [ ] 4.9 Report vault storage (without cached originals) and media storage (against the allowance) in status and in the usage cellctl publishes for Home. Verify: tests show no double counting with cached originals, an unknown media figure when the cell cannot read its references, and an unchanged owner figure when only cellctl's physical listing fails.

## 5. Gateway, uploads, imports and export (D8, D10)

- [ ] 5.1 Substrate's change adds the signed-in download route with byte ranges, forwarded to the cell. Verify: Substrate tests show refusal without a session, and ranged playback of an evicted recording in rehearsal. Agent half done: the cell serves `GET /download` behind a per-request gateway proof (D8), and the gateway's OAuth-authorized route is Substrate's. The browser signed-in link is still open.
- [x] 5.2 Add `exomem cloud download` over OAuth with verify-then-rename. Verify: client tests show no file after a tampered, truncated or redirected download.
- [ ] 5.3 Substrate's change proxies resumable uploads to the cell. Lift the Cloud exclusion of the upload routes by its recorded condition. Verify: Cloud surface tests, and a resumable upload in rehearsal.
  - [ ] 5.3.1 Reconcile accepted offsets after interrupted or timed-out parts, reduce subsequent part sizes, and retain resumable state during an outage. Verify the actual uploader against the service with a lost reply after accepted bytes; do not retry session creation blindly.
  - [ ] 5.3.2 Add Source sessions through the existing canonical writer and mutation replay protocol. Verify metadata refusal before bytes, lane isolation, a recovered acknowledgement without duplicate captures, and unknown outcomes with retained uploads. Reuse the existing staged file; keep semantic compilation agent-authored.
  - [ ] 5.3.3 Verify resource bounds before advertising larger Source uploads in each supported authority mode. Activated vocabulary writes currently materialize staged images and reject arbitrary binary changes; preserve that authority while preventing an oversized allocation just to discover a refusal.
- [ ] 5.4 Add streaming import and split S2 admission into volume and store checks, amending S2's contract in its owning change. Verify: an import larger than the volume cap lands without the volume passing its cap.
  - Design: D12, with the amendments to D3, D8 and D10 (2026-10-30). The shape, the Home sender in slice 1, eviction for every original, the store check on the whole allowance and the hold cost are pending operator confirmation.
  - Prerequisite: `scale-agent-led-imports` merges first. It supplies the stage manifests, consume-on-filing, paging, collection identity and the space stop.
  - Slices: slice 1 is 5.4.1 to 5.4.8, 5.4.7a and 5.4.12, proved on a local-storage cell. Slice 2 is 5.4.9 and 5.4.10. Task 5.4.11 lands before version destruction is enabled. Slice 3 is pipelined uploads and, if holds hurt, option R. Task 5.4.13 closes all slices.
  - [ ] 5.4.1 Amend D3, D8 and D10 of this change, the Substrate change `admit-cloud-imports-against-published-limits` and `publish-cloud-import-limits` as dated amendments. Verify: `openspec validate --all --strict` passes in both repositories.
    - Done in this change on 2026-10-30: D3, D8, D10, D12, the specs and these tasks. Still open: the Substrate change and `publish-cloud-import-limits`.
  - [ ] 5.4.2 Add the shared store-bound predicate and import it in the CLI, stage offload and the inventory. Verify: a test shows that the CLI's declared store bytes for a generated folder equal the bytes that stage offload stores.
  - [ ] 5.4.3 Apply M1 to M4. Verify these with tests:
    - a filed PDF offloads without a restart;
    - an offload is claimed before queued transcription;
    - a never-read original is evicted under pressure, and an owner-read one is not;
    - extraction of an evicted original leaves it evictable;
    - eviction reads a bounded candidate set.
  - [ ] 5.4.4 Add stage references, stage offload and the stage state function. Verify these with tests:
    - removal happens only after a confirmed reference;
    - text, over-bound and changed members stay;
    - a crash before the reference write retries without loss;
    - discard releases references at the next start;
    - a duplicate uploads nothing.
  - [ ] 5.4.5 File by reference. Verify these with tests:
    - a 2 GiB stored member files with no volume bytes written;
    - a stop between the sidecar and the release settles as already applied;
    - the hydrated original matches the manifest SHA-256;
    - the residual names stored members.
  - [ ] 5.4.6 Report the window budget and the per-stage drain in `adoption_studio status`. Verify: tests for the arithmetic, and for unknown when an input cannot be read.
  - [ ] 5.4.7 Add CLI windows, the series checkpoint, the budget wait and the fallbacks. Verify these with tests against a synthetic API and MCP:
    - three times the budget becomes three or more windows;
    - a restart after window 2 resends nothing;
    - a refused window is planned again smaller;
    - an older cell gets one import.
  - [ ] 5.4.7a Add the Home sender in Substrate: the browser uploader windows a selected folder from the same window budget and store-bound rule that the cell reports, through the owner channel. The filing flow stays in MCP tools. Verify: browser tests against a synthetic cell show the same partition as the CLI for one generated folder, a restart that resends no landed window, and one import for an older cell.
  - [ ] 5.4.8 Update the tool text and regenerate the surface and plugins. Verify: the tool-surface and scaffold-leak tests pass, and descriptions stay within the plugin description limit.
  - [ ] 5.4.9 Make cellctl publish `media_limits` (control-plane review). Verify: tests for selected, unselected and retained-only cells, and no write when nothing changed.
  - [ ] 5.4.10 Add the Substrate S2 split in the Substrate change `admit-cloud-imports-against-published-limits`. Verify these with PostgreSQL integration tests:
    - windowed admission without growth;
    - the whole-allowance refusal;
    - the old formula for older senders and cells.
  - [ ] 5.4.11 Make the backup-retention manifests list captured stage references, before version destruction is enabled. Verify: a version named only by a stage reference survives collection.
  - [ ] 5.4.12 Extend the large-import rehearsal of `scale-agent-led-imports` (its task 5.1) with the media store on a fixed volume V, in a B2 sandbox bucket. Import at least 2.5 times V, with files over 200 MiB and one file above V/4. Verify:
    - the logs show no ENOSPC, `INSUFFICIENT_SPACE` or `STORAGE_ALLOWANCE_EXCEEDED`;
    - the volume claim and `storage_gib` equal V before and after;
    - the sampler's peak, a lower bound, is at most V less the reserve;
    - every eligible Source downloads to the manifest SHA-256;
    - the residual is as expected;
    - the report records the window count and the hold downtime per window;
    - the report records whether a note links to an attachment that an earlier window stored.
  - [ ] 5.4.13 Run the full corpus, get an independent review, merge, then roll out with the operator. Verify: the review names C4 and C6 verdicts, and the rollout steps in D10 each have an operator approval.
- [ ] 5.5 Split export into a packing step that reaches only the edge and holds the media key, with its network and admission rules (the Job and its policy do not exist before this task), and an upload step without the key. Size staging, parts and deadlines for exports larger than the cell. Verify: export tests with evicted originals pass, a corrupt object fails the export, and chart tests show the upload step has no media key and the packing step has no public egress.

## 6. Rollout (D11)

- [ ] 6.1 Rehearse on a rehearsal cell: capture, offload, evict, hydrate, play, download, export, turn off and drain, then delete the cell. Verify: the evidence is recorded in this change.
- [ ] 6.2 Enable one canary cell, back-fill and evict to target. Collect hydration records for two to four weeks, then set the eviction target, margin and minimum idle age. Verify: the measurements and settings are recorded in design.md D3.
- [ ] 6.3 After 5.5, turn on the fleet switch under an operator-approved live change, with the drain as its undo. Verify: every selected cell reports an empty offload backlog, and a rehearsal export delivers the largest selected cell's media.
