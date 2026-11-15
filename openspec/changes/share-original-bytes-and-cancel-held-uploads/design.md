# Design

This design is incomplete. The local storage compatibility choice blocks the shared-storage specification and its implementation tasks. Do not apply this change.

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

### Local storage compatibility remains a blocking choice

A managed pool can preserve one local original while filings keep independent logical addresses. Ordinary applications would need compatible exports or materialized paths. Existing bindings, citations, grants, previews, and downloads must remain valid.

Keeping ordinary binary files preserves today's filesystem layout. Hard links cannot guarantee sharing across supported filesystems and volumes. The proposal does not select this trade-off silently.

Cloud reference sharing remains the existing mechanism. Backups, staging, recovery replicas, and historical encryption versions must remain visible in storage accounting. Sharing does not mean a single physical copy across every recovery tier.

### Shared ownership preserves collection and cell isolation

Reuse the existing Cloud reference mechanism within one cell and compatible encryption context. Do not introduce a digest-derived object name or a shared bucket namespace. A new protection record keeps its random name; reusing a live version copies its existing reference without another upload.

Each filing retains its own authorization and reference. Retiring one reference records its release before removing that reference. Another live filing, protection record, retained backup, or running export must keep the shared version recoverable. Preserve the collection manifest and release-ledger contracts in `cloud-media-collection`.

Cell deletion purges a complete cell prefix. Shared originals must never cross cell prefixes, credentials, or encryption contexts. Running services gain no version-deletion capability; this design does not change deletion grants or retention grace periods.

Keep two storage figures distinct. The owner allowance counts each referenced plaintext version once, including Trash. The provider meter counts physical versions and retained copies within the cell prefix. Unknown census results remain unknown. Releasing one of several references must neither erase usage nor charge the shared version twice.

The local storage design must resolve logical original addresses through `originals.py`. For store-owned pages, the canonical store decides whether the filing exists; a stale or missing disk projection cannot decide existence. Physical payload reuse must not bypass that boundary.

### Cancellation uses the existing private upload store

Expose `cancel_uploads` through one leaf and the generated surfaces. Accept only held-upload handles and verify their existing session binding. Use the transfer commands' dispatch shape outside the canonical writer. Do not fetch handles, run the global expiry sweep, or trigger indexing.

A bounded claim wait returns busy with a retry hint. A missing, foreign, expired, spent, or already claimed upload returns the same unavailable result. Do not infer an active claim by scanning other files.

### A cancellation marker preserves cleanup ownership

Under the existing upload lock, authenticate the active record and require its unclaimed payload. Flush the authenticated record's contents before atomically renaming it to `<key>.cancelling.json`. Flush the directory after the rename. Only then may cleanup remove bytes. A failed flush or rename leaves the upload intact and reports failure.

The marker keeps the original binding, size, and expiry. The existing quota scan already includes its `.json` suffix. Redemption cannot find the former active record.

Strictly remove the payload, flush its removal, then remove the marker and flush that removal before acknowledging completion. Use the existing cross-platform directory flush; do not use the helper that suppresses removal errors. The ordering prevents a crash from persisting marker removal while retaining an untracked payload.

A retry authenticates the marker and can finish after payload deletion succeeded but marker deletion failed. It reports released quota only after cleanup is confirmed. A lost acknowledgement can later produce unavailable; it must not invent a successful release count.

The expiry sweep must recognize cancellation markers before its generic expired-record branch. It must remove the original payload before removing the marker, with the same durability ordering. A cleanup error retains the marker and its quota charge even after the original expiry. Ordinary expiry must not treat `<key>.cancelling.bin` as the payload or discard the only cleanup owner.

The original expiry still governs an active hold. An authenticated retry of an already committed cancellation may finish marker cleanup after that expiry. Foreign markers remain indistinguishable from unavailable handles. Ordinary failure restoration keeps its current behavior. Cancellation never follows a claim or a canonical object reference.

### Upload identity cannot be reassigned during cancellation

Normal held uploads mint fresh random handles. Import handoffs derive a handle from a ticket nonce; the existing ticket claim consumes that nonce before reading the request body. Keep that one-use boundary.

An active cancellation marker owns its key until cleanup completes. Publication must not replace that key's bytes while cancellation can still act on them. Preserve existing exact-byte import handoff behavior.

## Risks / Trade-offs

- Partial cleanup retains the marker and its quota charge. The caller receives a visible failure and can retry; the server claims no released capacity.
- Filesystem metadata exhaustion can refuse the marker rename. Keep the upload intact and report the failure; do not fall back to untracked deletion.
- Older workers do not interpret cancellation markers. Complete the serving-worker handoff before exposing cancellation; specify pending-marker recovery before any supported downgrade.
- Shared originals increase the impact of corruption. The storage design must preserve fixity, verified recovery, and every affected filing's access rules.
- Moving storage metadata can invalidate grants that bind companion hashes. The completed storage design must prove compatibility before any relocation.

## Proof Boundaries

Cancellation needs one disposable workflow: failed preservation, retained hold, explicit cancellation, and successful replacement upload within the original cap. Race it against redemption in both orders. Inject payload and marker removal failures and a restart after the marker rename. Verify exact quota release and nondisclosure to another session.

The shared-storage workflow must prove separate protected and ordinary filings, unchanged grants, one eligible canonical payload, exact export and restore, and independent revocation. The owner and ungranted-reader dataset regression remains required.

Cancellation uses the existing held-upload count limit as its batch bound. Its metadata work must remain proportional to submitted handles, not archive size or unrelated holds. Measure a maximum batch with 10,000 unrelated held records on a disposable store; payload size must add no read work. Target a response p95 below one second, with immediate busy responses rather than lock polling. Extend existing command latency and error telemetry for this operation; verify the corresponding budget alert.

The shared-storage load targets, migration steps, and task breakdown still require the storage choice and integration review. That dependency does not block cancellation design. No implementation is authorized by this partial design.
