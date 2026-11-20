# Tasks

The operator authorized implementation and release of both capabilities. Held-upload cancellation ships first; shared original storage follows in its own release. No task authorizes live conversion, fold, or cleanup of an existing archive.

## 1. Canonical pool and original resolution

- [x] 1.1 Register the canonical pool and versioned marker through existing placement and compatibility mechanisms. Verify that derived-state cleanup cannot remove originals.
- [x] 1.2 Reuse durable publication for verified pool objects before filing commits. Prove concurrent identical uploads publish one object, and interrupted publication preserves existing filings.
- [x] 1.3 Extend the original resolver and snapshot interface while retaining logical paths and canonical-store existence. Exercise preview, download, datasets, extraction, frames, and export through that interface.
- [x] 1.4 Preserve existing logical duplicate results and declared-byte admission. Prove equivalent restricted-writer outcomes in equal-capacity hidden-match and hidden-nonmatch archives, with no physical-hit disclosure.
- [x] 1.5 Document pooled layout, ordinary-path compatibility, and the difference between canonical, staging, and backup copies. Verify each documented operation on a disposable archive.

## 2. Shared lifetime and governance

- [x] 2.1 Extend existing pins, residency requirements, reservations, jobs, and eviction selection to indexed physical-object identities. Prove aggregate owner-read age and existing job/read/pin/drain retention without per-filing queries; keep unclaimed pending jobs evictable.
- [x] 2.2 Share verified hydration while preserving separate filings and unchanged companion bytes. Prove independent revocation, owner repeat reads, and no granted-content transfer between filings.
- [x] 2.3 Extend the existing raw-protected dataset owner/ungranted-reader workflow to shared originals. Exercise warmed owner access, distinct ungranted metadata/query/stream requests, and unchanged original and sidecar hashes.
- [x] 2.4 Integrate existing Cloud reference reuse without changing cell prefixes, keys, random names, release ordering, or retained-backup protection. Prove every binding has its own current recovery reference before eviction; retain existing remote accounting units.
- [x] 2.5 Document the admission guarantee and existing Cloud resource-oracle limitation. Verify that receipts do not promise global resource noninterference or expose hidden pool identities.

## 3. Conversion, export, and recovery

- [x] 3.1 Add one owner maintenance conversion operation through the existing resumable job framework. Prove ordinary deployment preserves existing layouts and unsupported managed downgrades refuse enrolled pooled state.
- [x] 3.2 Convert canonical bindings in bounded batches without rewriting grant-bound companions. Inject publication, unlink, and restart failures; verify mixed-layout reads and safe continuation.
- [x] 3.3 Materialize a separate compatibility export through the existing export path. Verify exact pages and originals with a declared pre-feature runtime, including protected filings and incomplete-export recovery.
- [x] 3.4 Restore the pool, canonical bindings, format declaration, and retained Cloud references without derived indexes. Verify compatible admission, exact bytes, and independent permissions after recovery.
- [x] 3.5 Document conversion, recovery, downgrade export, and required free space. Verify the runbook on disposable data; do not activate conversion on a live archive.

## 4. Held-upload cancellation

Section 4 and task 5.2 ship in their own delivery, ahead of shared original storage. The two capabilities share no runtime path; each format has its own launcher declaration.

- [x] 4.1 Make ticket consumption durable before body authority and establish the old-worker activation barrier. Prove a replay after power loss cannot recreate a completed cancellation's handle.
- [x] 4.2 Implement authenticated cancellation markers with strict flush and removal ordering. Cover rename-flush failure, payload-removal failure, marker-removal failure, lost acknowledgement, and cleanup after expiry.
- [x] 4.3 Preserve the existing claim race and failed-preservation restoration. Exercise both winners, foreign/absent handles, and replacement upload within the released session cap.
- [x] 4.4 Expose one bounded cancellation leaf through generated MCP, REST, and CLI surfaces. Verify ordered honest outcomes, secret-free diagnostics, and no canonical writer, fetch, indexing, or unrelated-store scan.
- [x] 4.5 Prove launcher/operator compatibility before format activation and integrate current-runtime cleanup before target admission. Verify old-sweeper exclusion and downgrade after cleanup; exercise the documented offline launcher update.
- [x] 4.6 Update generic operation guidance and generated contracts through their source generators. Verify schema fidelity, privacy, and documented cancellation without adding a model dependency.

## 5. Integrated proof and delivery

- [x] 5.1 Measure metadata paths before and after on the same 100,000-filing fixture and representative shared objects. Verify indexed plans, bounded query counts, and p95 below one second.
- [x] 5.2 Measure a maximum cancellation batch beside 10,000 unrelated holds. Verify work depends on submitted handles, not payload size; exercise busy responses and the existing budget alert.
- [x] 5.3 Run the combined disposable custody workflow: independent protected and ordinary filings, one object, exact export/restore, and failed-upload cancellation. Verify drain counts name physical objects and zero proves every binding has local bytes.
- [x] 5.4 Run scoped author checks, public privacy validation, strict OpenSpec validation, and the normal completion CI. Obtain one independent adversarial review covering correctness, security, C4/C6, scale, and proof weight.
- [x] 5.5 Deliver the approved implementation as one cohesive batch through normal release and installed verification. Keep existing live archives unconverted and preserve explicit live-conversion authority boundaries.
- [ ] 5.6 After all mandatory work is evidenced and `add-cloud-media-store` archives, sync canonical specs and archive through the OpenSpec CLI. Validate strictly before and after; leave optional live conversion or cleanup outside closure claims.
- [x] 5.7 Keep suffix-less originals as ordinary files so the egress census can tell their companions from notes. Gate every pooled-filing decision (`add`/`preserve`, conversion inventory, `SHARED_ORIGINAL_NEEDS_EXTENSION` on move) on `originals.may_share_bytes`; leave already-pooled suffix-less filings readable. Prove redaction of a withheld suffix-less original's name in a pooled archive.
