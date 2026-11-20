# Design

This design selects a managed original pool and explicit held-upload cancellation. The operator authorized implementation and release; it authorizes no live conversion or cleanup of an existing archive.

## Context

See `proposal.md` for the outcome and capability boundaries.

Cloud offload already reuses a confirmed original within one vault when its plaintext digest, size, and current encryption key match. It preserves random object names and versioned references. Local originals remain ordinary files at their filing paths.

Held uploads use private metadata and payload files. Redemption renames a payload into a claim and removes its active metadata. Failed preservation restores the same handle and expiry. Resumable uploads already support cancellation; held handles do not.

## Goals / Non-Goals

**Goals:**

- Keep storage reuse below filing identity and authorization.
- Make explicit cancellation release waiting upload capacity without changing automatic retry behavior.
- Reuse current storage, claim, publication, and directory durability mechanisms.

**Non-Goals:**

- Change existing logical duplicate outcomes or merge protected filings.
- Cancel a canonical commit, share derived model outputs, or deduplicate across vaults.
- Move live originals, fold existing filings, or introduce automatic legacy cleanup.

## Decisions

### A canonical local pool separates bytes from filenames

Place immutable objects under `<managed-root>/_Originals/sha256/<prefix>/<digest>.blob`. The SHA-256 prefix shards directory entries. Verify the recorded size as well as the digest. This is canonical vault content, never disposable machine-local state.

Register the root through `reserved_paths`. Keep the existing `BinaryBinding` logical path, digest, and size. Extend `OriginalSnapshot` to resolve that binding to local pooled bytes or the existing Cloud reference. The canonical companion remains the binding authority; a blob alone creates no filing.

New filings keep their existing metadata shape. Conversion does not rewrite companions, provenance, references, or grant-bound hashes. Filings with the same bytes can therefore retain different lanes, protection, and permissions. Existing logical `already_stored` reuse remains unchanged.

A pool-only filing exists on disk solely as its companion page `<name>.md`, and the egress artifact census tells that companion from a note by `<name>` carrying a filename suffix. An original whose name has no suffix (`LICENSE`, a dotfile) would give a companion indistinguishable from a note, so like a note or a dataset it stays an ordinary file at its logical path; every decision that creates a pooled filing asks one predicate (`originals.may_share_bytes`), including the hydration of an evicted original, which restores a suffix-less one to its logical path; and a move that would rename a shared original to a suffix-less name is refused. A suffix-less original of a pooled archive is also never offloaded, so its bytes cannot turn pool-only by eviction beside a twin. Residual: suffix-less filings already pooled by an earlier release, and ones evicted before the archive was pooled whose twin later pools, stay pooled and readable, and a restricted caller's output does not redact their bare names.

All original consumers use the resolver: preview, range download, extraction, frames, datasets, processing, backup, and export. A consumer needing a filename receives a bounded temporary materialization with the original name. Callers address filings, never pool keys. No placeholder binary file is left at a logical path.

Hard links were rejected because sharing must work across supported filesystems without shared filesystem ACLs or link-count limits. A separate blob service was rejected because existing local storage and Cloud references already provide custody.

Cloud reference sharing remains the existing mechanism. Backups, staging, recovery replicas, and historical encryption versions must remain visible in storage accounting. Sharing does not mean a single physical copy across every recovery tier.

### Publication and pins belong to the physical object

Reuse the durable publication techniques in `archive_members.py`, not its family ownership model. Stage and verify supplied bytes before publishing a new filing. Publish or verify the immutable object before committing its logical binding. A crash may leave an unreferenced object; it must never leave a committed filing without confirmed bytes.

Keep byte transfer and hashing outside the canonical mutation lease. A new filing stages inside the batch's in-process lock, as an ordinary filing's copy does, and a pool hit confirms the existing object there with one full hash. Retain the verified input and object pins through the final binding check and commit. The lease covers canonical existence, ownership, and publication decisions, not a vault-sized scan.

Extend the existing media ledger and reader pins to the resolved object identity. Pins, active jobs, hydration reservations, and eviction checks must include every filing using that object. Index the derived object identity; do not inspect each filing during a request. Canonical bindings remain authoritative, so no independent reference-count database is added.

Keep the existing private identity inventory and generation checks. Use persistent maps for per-object publication and immutable request snapshots. The existing `rpds-py` dependency supplies these maps; declare it directly without adding a database. Warm metadata work must avoid whole-map copies and overlap scans. Startup and recovery retain the existing inventory walk, with their cost reported separately. This adds a direct dependency declaration and small index maintenance inside the current owner boundary.

Keep the physical binding projection in the existing media ledger, owned by `media_store`. Index logical bindings by physical object and retain each binding's own companion hash, residency, and recovery reference. Capture the physical key when a read or job acquires its pin. A later binding change must not move that pin to another object.

Use canonical-store audit revisions and ordinary-file batch invalidation as the two source adapters. Store-mode recovery follows the bounded audit suffix. File-mode publication withdraws projection completeness before installing canonical changes, then applies the exact committed delta. Existing path custody repairs interrupted updates; observed outside changes withdraw completeness before debounce. A missing or stale checkpoint retains local bytes without refusing uploads or reads. Build complete inventories during startup or background recovery, never during an eviction request. This costs two source adapters until file mode retires; optional governance catalog publication alone cannot prove completeness. The primary-source roster's continuity guidance supports retaining exact source checkpoints rather than trusting stale derived state.

Moves, Trash transitions, hydration, and eviction also withdraw completeness before changing ordinary binary placement. Compound operations retain the canonical boundary through completion or rollback. A census captures its starting checkpoint under that boundary, then streams outside it. Graph epochs omit some binary and excluded-page changes, so they cannot replace these binding hooks.

A read through one filing must prevent eviction initiated through another. Concurrent hydration publishes one verified local object. Shared corruption returns an explicit unavailable or corrupt state and preserves verified recovery copies. It never silently substitutes another version.

Reserve the physical hydration destination in the existing ledger before creating its private stage. Keep this destination separate from the acquired-byte pin. Commit the created stage's stable file identity before consuming plaintext, outside identity coordination to preserve lock ordering. Cleanup removes only that identity and flushes its parent before releasing the byte charge. A crash before identity commit leaves at most an empty stage; preserve an existing unproven or replaced file with its cleanup charge. Release its hydration slot. A rebuilt private inventory proves present exclusion, not the reservation's historical custody.

Every binding also contributes its residency requirement. Retain the local object if any binding is resident-only or lacks its own usable recovery reference. A Source's remote copy cannot make a separately governed dataset evictable. Maintain this derived eligibility through existing indexed metadata and its invalidation; do not scan filings during eviction.

Decide eviction and hydration per physical object across every binding. The latest owner read through any binding sets idle age. An existing job pin, access pin, active read, or drain latch on any binding retains the local object. Claimed jobs keep their pin when returned to pending; an unclaimed pending job does not retain bytes. Eviction requires every binding's own current Cloud reference.

Use that complete binding projection for drain completeness, including resident-only and unbacked bindings. Validate permitted companion placement and normalize historical Trash pointers before publishing the projection. Reuse the existing identity and Trash placement readers; keep displaced-reference admission tied to its exact recorded recovery version. Before replacing an inventory, reconcile omitted bindings against canonical deletion or release. An existing blank or damaged companion must not erase the previous witness.

Stage a grouped physical availability census in temporary tables in the same ledger, outside the canonical guard. Probe each physical object through its existing owner, then publish only while the binding checkpoint, source frontier, and policy still match. Count a missing object once and hydrate through a binding with its own usable reference. A missing object without any usable reference makes the census unknown. Report the unit as physical objects, and invalidate older censuses and latches before accepting them as this all-binding proof. Retain the existing three-token external drain record format and coordinate its reporting labels through the current consumers.

Deletion can leave a historical companion at the old binary path after removing its recovery reference. Path and digest equality cannot distinguish that retired filing from a newer filing at the same path. Record an optional, versioned retirement witness in the existing canonical Trash placement metadata. Bind the exact source identity, before and expected post-removal companion hashes, and the current Trash binding and complete recovery reference. Prepare reference removal through its existing renderer, publish the witness before rename, and recognize completion only from the exact resulting canonical state. An interrupted or ambiguous legacy transition remains unknown. Carry a witnessed ordinary companion edit forward only when its before hash and binding match; invalidate retirement before a new original publication at that source path. No independent journal or inferred legacy backfill is added.

Add one reciprocal metadata backlink during that same companion edit. Include it in the witness's expected hash. This locator keeps edit and publication work proportional to changed paths, even after derived state is lost. The backlink grants no authority. Require the exact source companion and identity, metadata witness, resulting hash, and current Trash recovery binding. Canonical batches carry a proven same-binding edit or invalidate retirement before publishing a new original, including identical bytes. Only the prepared deletion may complete its recorded reference-removal transition. This follows the primary-source roster's context-engineering row: retain exact continuity evidence rather than rebuilding authority from a stale cache.

Before admitting an older writer, current-runtime preparation must invalidate retirement authority and physical drain proof. A compatibility export invalidates retirement authority in destination metadata while preserving companion bytes and the source archive. These operations reuse lifecycle custody and the existing export path. Their invalidation coverage is required before delivery; a newer runtime's successful census alone does not prove downgrade safety.

### Physical reuse grants no admission credit

Preserve existing declared-byte allowance admission and full session-held charges. A new filing stages and verifies its supplied bytes on destination storage before a physical pool hit can save allocation. A miss promotes that stage; a hit releases it after the filing commits. Existing logical reuse is not a new filing and keeps its current path.

This retains the current transient-space cost, even when an identical hidden object exists. It prevents a new immediate hit-dependent admission shortcut. Do not add permanent reservations, padding, or another quota system. Return no hidden filing identity, pool-hit flag, or saved-byte figure to a restricted caller.

This is not global resource noninterference. Real storage savings can affect later free-space observations. Existing Cloud background offload also exposes allowance outcomes after hidden-reference reuse (`media_store.py`, D10 of `add-cloud-media-store`). That is existing scope debt, not a new guarantee or a reason to change Cloud policy here.

### Format conversion preserves grants and supports export

Use a versioned canonical pool marker and a `shared-originals-v1` compatibility identifier through the existing state-compatibility mechanism. Record compatibility before publishing the marker or relocating bytes. An interrupted enablement resumes with the compatible runtime; it drops the identifier only after proving no pooled layout was published.

New archives start in this format. The creator records that intent in the archive's external state manifest, not as a format requirement, so the archive stays ordinary and readable by every release until activation. The first service start that proves launcher support enrols the format, publishes the marker and clears the intent before it admits writes; a start without that support keeps the intent and names the launcher update. Existing archives require one explicit owner maintenance operation, using the existing resumable job framework. It requires whole-namespace authority because conversion includes hidden filings. Ordinary deployment leaves existing archives unchanged.

The conversion job inventories canonical bindings, reports unbound or unreadable originals, and processes bounded batches. It verifies the pooled copy and unchanged binding before retiring an ordinary binary path. It waits for existing reader and writer pins. Mixed legacy and pooled storage stays readable after interruption. A retry derives progress from actual bindings and verified bytes, not an authoritative second catalogue.

Keep current Cloud references during conversion; do not initiate offload or rewrite protected companions merely to pool local bytes. New filing references are part of initial publication. Hydration changes local object availability, not companion bytes or grant hashes.

The existing compatibility manifest stops unsupported managed downgrades when its requirement remains enrolled. A canonical marker lets capable restore tools re-enrol it before serving. A capable service start also re-enrols a marker that arrives without its requirement, and refuses to serve it where launcher support is unproven. A pre-feature runtime cannot recognize that marker after a bare-copy restore loses external state, whether started manually or through an older manager. Those starts are unsupported; use capable restore or compatibility export. Do not claim retroactive protection from binaries that lack the format.

Compatibility export writes a separate ordinary-file archive through the existing export path. It materializes every logical original, verifies hashes and sidecar bytes, and preserves canonical metadata through its existing store export. Prove that a declared pre-feature runtime can read that export. Export failure leaves the pooled source intact. No automatic in-place downgrade or orphan collection is added.

### Shared ownership preserves collection and cell isolation

Reuse the existing Cloud reference mechanism within one cell and compatible encryption context. Do not introduce a digest-derived object name or a shared bucket namespace. A new protection record keeps its random name; reusing a live version copies its existing reference without another upload.

Local pool membership creates no new Cloud reuse rule. Each binding keeps its own reference and protection custody, even when existing confirmed-live-version reuse gives two bindings the same remote version.

Each filing retains its own authorization and reference. Retiring one reference records its release before removing that reference. Another live filing, protection record, retained backup, or running export must keep the shared version recoverable. Preserve the collection manifest and release-ledger contracts in `cloud-media-collection`.

Cell deletion purges a complete cell prefix. Shared originals must never cross cell prefixes, credentials, or encryption contexts. Running services gain no version-deletion capability; this design does not change deletion grants or retention grace periods.

Keep two storage figures distinct. The owner allowance counts each referenced plaintext version once, including Trash. The provider meter counts physical versions and retained copies within the cell prefix. Unknown census results remain unknown. Releasing one of several references must neither erase usage nor charge the shared version twice.

Report the drain census and evicted count per physical object, with that unit explicit. A drained cell's zero count must still prove every binding has local bytes. Local pool accounting counts each object once; remote reference and provider accounting keep their existing units.

`add-cloud-media-store` remains active through canary and fleet adoption. Add these integration requirements to this change's own capability. Archive this change after `add-cloud-media-store`; modify its requirements only once they are canonical.

The local storage design must resolve logical original addresses through `originals.py`. For store-owned pages, the canonical store decides whether the filing exists; a stale or missing disk projection cannot decide existence. Physical payload reuse must not bypass that boundary.

### Cancellation uses the existing private upload store

Expose `cancel_uploads` through one leaf and the generated surfaces. Accept only held-upload handles and verify their existing session binding. Use the transfer commands' dispatch shape outside the canonical writer. Do not fetch handles, run the global expiry sweep, or trigger indexing.

A bounded claim wait returns busy with a retry hint. A missing, foreign, expired, spent, or already claimed upload returns the same unavailable result. Do not infer an active claim by scanning other files.

### A cancellation marker preserves cleanup ownership

Under the existing upload lock, authenticate the active record and require its unclaimed payload. Flush the authenticated record's contents before atomically renaming it to `<key>.cancelling.json`. Flush the directory after the rename. Only then may cleanup remove bytes.

A failed record flush or rename leaves the active upload intact and reports failure. If the rename succeeds but its directory flush fails, retain the marker, payload, and quota charge. The active handle may already be unavailable. Report incomplete cancellation without claiming released capacity. A retry or sweep must establish marker durability before deleting bytes; do not attempt a second rename as rollback.

The marker keeps the original binding, size, and expiry. The existing quota scan already includes its `.json` suffix. Redemption cannot find the former active record.

Strictly remove the payload, flush its removal, then remove the marker and flush that removal before acknowledging completion. Use the existing cross-platform directory flush; do not use the helper that suppresses removal errors. The ordering prevents a crash from persisting marker removal while retaining an untracked payload.

A retry authenticates the marker and can finish after payload deletion succeeded but marker deletion failed. It reports released quota only after cleanup is confirmed. A lost acknowledgement can later produce unavailable; it must not invent a successful release count.

The expiry sweep must recognize cancellation markers before its generic expired-record branch. It must remove the original payload before removing the marker, with the same durability ordering. A cleanup error retains the marker and its quota charge even after the original expiry. Ordinary expiry must not treat `<key>.cancelling.bin` as the payload or discard the only cleanup owner.

The original expiry still governs an active hold. An authenticated retry of an already committed cancellation may finish marker cleanup after that expiry. Foreign markers remain indistinguishable from unavailable handles. Ordinary failure restoration keeps its current behavior. Cancellation never follows a claim or a canonical object reference.

### Upload identity cannot be reassigned during cancellation

Normal held uploads mint fresh random handles. Import handoffs derive a handle from a ticket nonce. The existing ticket claim unlinks its ticket before reading the request body, but does not flush that deletion. It is not yet a durable one-use boundary across power loss.

Make ticket consumption durable under the existing ticket lock before returning authority to read the body. Use the existing directory flush after unlinking. A failed flush refuses that request before body acceptance; the caller may need a fresh ticket. This prevents replay from recreating a completed cancellation's old handle, without adding a permanent tombstone store.

An active cancellation marker owns its key until cleanup completes. Under the upload lock, `hold()` must reject that key before creating its payload temporary or entering cleanup that could remove another operation's payload. Publication must not replace that key's bytes while cancellation can still act on them. Preserve existing exact-byte import handoff behavior.

After older serving workers stop, establish a directory durability barrier for existing ticket consumption before enabling cancellation. This settles deletions performed by older code; the new claim path makes later consumption durable. Do not treat normal-execution one-use behavior as proof of the power-loss guarantee.

Record `held-upload-cancellation-v1` through the existing compatibility manifest at that activation boundary. This prevents an older expiry sweep from discarding a cancellation marker while retaining its payload. Cleanup uses a resumable maintenance pass, not a scan on each cancellation. This guard costs a cleanup step on downgrade; it protects temporary-byte ownership rather than canonical content.

### Lifecycle compatibility precedes feature activation

A current worker can run under an older stable launcher. For managed installations, prove launcher and operator support before enrolling either new compatibility identifier. Otherwise keep the existing layout and retry/expiry behavior, and name the maintained launcher-update action. Deploying a new worker alone is insufficient.

Use the existing offline launcher-update procedure in `docs/managed-service-upgrades.md`. Stage the compatible package first, stop the owned service, prove its processes and listeners stopped, update the launcher, then restart and verify read/write acceptance. This interrupts stable connections once. No new per-operation human approval gate is added; execution remains within the approved release scope.

Add current-runtime cancellation cleanup to the managed upgrade preparation seam, before target compatibility admission. When an explicitly selected target lacks the cancellation format, quiesce new cancellations under lifecycle custody, finish markers, and retire only that identifier. Use the migration lock and trusted authority check. Pending cleanup returns progress or busy; it does not enter an incompatible target or claim completion. Failure retains the identifier and keeps compatible recovery available.

Do not defer that cleanup to the target migrator: current admission rejects unknown identifiers before it runs. A pooled archive still cannot downgrade in place. It needs a compatibility export or a verified pre-conversion restore; cancellation cleanup never retires pooled-format custody.

## Risks / Trade-offs

- Partial cleanup retains the marker and its quota charge. The caller receives a visible failure and can retry; the server claims no released capacity.
- Filesystem metadata exhaustion can refuse the marker rename. Keep the upload intact and report the failure; do not fall back to untracked deletion.
- Older workers do not interpret cancellation markers. Complete the serving-worker handoff before exposing cancellation; specify pending-marker recovery before any supported downgrade.
- Shared originals increase the impact of corruption. The storage design must preserve fixity, verified recovery, and every affected filing's access rules.
- A compatibility export needs space for ordinary copies. A failed export retains the pooled archive and reports incomplete output.
- Pool conversion never rewrites grant-bound companion bytes. Storage operations that already rewrite Cloud references retain their existing contract and need separate proof.

## Migration Plan

1. Deploy capable readers while existing archives retain their layout; ordinary releases require no conversion.
2. Update any incompatible stable launcher through documented offline maintenance; verify the service before activating new formats.
3. On explicit conversion, fence old writers and record compatibility before publishing the pool marker.
4. Convert bounded batches after verified publication; interrupted batches retain readable originals and resume safely.
5. Verify bindings, grants, exports, and backup restore; report incomplete coverage without claiming complete sharing.
6. For older readers, create and verify a separate compatibility export; keep the pooled source recoverable.

For enrolled managed state, the format guard prevents an older runtime from treating relocated originals as missing. Its cost is refusing a converted archive until compatible recovery or export completes. It applies only after format activation, not to ordinary releases.

## Proof Boundaries

Cancellation needs one disposable workflow: failed preservation, retained hold, explicit cancellation, and successful replacement upload within the original cap. Race it against redemption in both orders. Inject payload and marker removal failures and a restart after the marker rename. Verify exact quota release and nondisclosure to another session.

The shared-storage workflow must prove separate protected and ordinary filings, unchanged grants, one eligible canonical payload, exact export and restore, and independent revocation. The owner and ungranted-reader dataset regression remains required.

Cancellation uses the existing held-upload count limit as its batch bound. Its metadata work must remain proportional to submitted handles, not archive size or unrelated holds. Measure a maximum batch with 10,000 unrelated held records on a disposable store; payload size must add no read work. Target a response p95 below one second, with immediate busy responses rather than lock polling. Extend existing command latency and error telemetry for this operation; verify the corresponding budget alert.

Use the owner scale in `add-cloud-media-store/design.md`: tens to hundreds of gigabytes of originals. Prove metadata paths at 100,000 filings and 100 GiB of logical originals in a disposable fixture. Shared representative objects can supply those logical bytes; the fixture need not allocate 100 GiB. These are proof sizes, not customer limits.

Pool lookup, authorization, pin selection, and admission perform bounded indexed metadata work. Target p95 below one second for those operations. Transfer, fixity, conversion, and export stream bytes or use the existing background jobs. Their completion time is reported separately from metadata latency.

Compare before and after through the same instrument and fixture. Show no per-filing query growth and verify index plans. Reuse current cryptography and provider evidence; test our binding, permission, configuration, and data-flow integration.

One author owns the coupled implementation. One independent adversarial review covers both capabilities and their security boundaries. Run the protected-dataset owner/ungranted-reader workflow, independent revocation, exact export and restore, publication interruption, concurrent reuse, and cancellation races. Use the existing latency contract and alert route. No model evaluation is needed unless agent-facing wording changes model behavior.

Task boxes record evidenced work only. Live conversion and cleanup of an existing archive remain outside the authorized work.
