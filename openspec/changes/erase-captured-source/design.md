## Context

Ordinary deletion preserves originals and trash. `index_sync.delete_after_remove` dispatches derived cleanup but does not prove every component finished.
Private history can retain cited text after the current citation disappears. Source back-references can also be skipped under restricted write authority.
Capture staging can retain bytes before a Source reference exists. Canonical inventory cannot prove that these copies disappeared.

The closest maintained mechanisms are the canonical writer boundary, private mutation database, producer ownership descriptors and index-removal fan-out.
Their gap is explicit retention ownership and complete, resumable erasure for one Source incarnation.
Reuse these mechanisms. The added maintenance cost is one small custody ledger, producer hooks and bounded cleanup acknowledgements.

## Goals / Non-Goals

Prove logical erasure of an independent captured incarnation through the existing governed operation surfaces.
Keep each persistence owner responsible for its own retained copies and publication fences.
Keep ordinary preservation, capture activity and navigation behavior.

Earlier backups, downloaded exports, caller-held responses, external synchronization history and forensic remnants remain outside the guarantee.
Restoring those copies can reintroduce the Source. Erasure does not infer semantic dependence from prose or ask a model for permission.

## Decisions

### D1. Bind one operation to one incarnation

Expose `erase_captured_source` through the shared command registry as `sources.erase_capture`.
Accept `operation_id`, `source_identity`, `expected_source_ref` and `expected_source_hash`.
Require the existing whole-namespace content authority before lookup, replay or disclosure, and recheck authority at commit and terminal release.
Host configuration explicitly enables identity systems; its default enables none.
The policy is configuration data, without a product-specific vocabulary in code.
`EXOMEM_SOURCE_ERASURE_POLICY` is a typed JSON object whose `enabled_systems` array defaults empty.
Disabling admission does not stop already-admitted cleanup or custody tracking for enrolled Sources.

Validate identity, immutable reference and current hash under the actual canonical writer boundary.
An operation identifier binds that target permanently for its retained lifetime; retries cannot substitute another reference or hash.
Hash drift preserves the target and returns a stale precondition. A fresh checked operation can continue the same incarnation.
An absent canonical page remains eligible when closed-attempt custody proves its allocated reference and hash.
Missing custody is unknown, never proof of successful erasure.

Return `target_source_ref`, `status` and `target_retired`, with bounded component progress and a revision-qualified failure when available.
Statuses are `complete`, `pending`, `failed` and `unknown`. Only complete acknowledged retirement returns `target_retired=true`.
No path or logical identity lookup can redirect delayed cleanup to a replacement Source.
Fresh capture uses a fresh reference. Completed cleanup needs no permanent identity tombstone.

### D2. Retain canonical custody before bytes can survive

Register one reserved, Source-owned structured manifest with the existing canonical persistence owner.
Publish its enrollment, dependency latch, owned-artifact locators and retirement fence in each producer's actual canonical batch.
This metadata follows the Source across replicas in both storage modes.
It contains no duplicate Source bodies or identity catalogue. P4 remains the published identity inventory.
Keep the manifest bounded by producer families; each opaque locator addresses producer-owned, paged retention evidence.
Do not embed an ever-growing fragment or version list in the manifest or public progress response.

Keep attempt-local pre-reference residue and bounded erase cursors beside the existing private attempt ledger.
Reuse its protected connections without changing generic mutation replay or its existing tables.
Source-owned result hooks may fence persistence and refresh, and redact the target's identifying projection.
They preserve row identity, historical outcome and serialization; erasure cannot clear an issued claim and permit its execution again.
Shared results retain unrelated Source authority and mark affected aggregates partial or unavailable instead of inventing a remaining total.
Ordinary retry expiry can admit a fresh capture under its existing policy; issued capture tickets keep their frozen admission and retention.
Local cursors accelerate progress; they never supply canonical independence or completion authority.
Another replica can reconcile owner acknowledgements from canonical custody without trusting the previous replica's cursor.

Begin attempt custody at `(ledger_id, boundary_id, ticket)` before surviving staging bytes are written.
Bind newly allocated Source references and rendered hashes before staging complete Source bytes.
Record a reused Source as an observed result, never an allocation owned by that attempt.
Each trusted producer reserves its opaque locator and version before persistence, then resolves it with its existing commit witness.
Interruption leaves visible uncertain custody until that producer resolves the witness.
File batches can partially publish after process loss. Persist custody reservations before staging and do not infer all-or-nothing publication from a returned path list.
An exact primary post-image can prove that file published; it does not prove the rest of its batch completed.

Attempt residue remains replica-local. Scope changes or missing attempt ownership cannot prove that residue absent.
Existing handoff and recovery must preserve that custody or route cleanup to its owner.
An invocation-local commit generation cannot qualify canonical dependency coverage: it can reset and permits persistence failure.
Store reconciliation binds its checkpoint to the physical store instance and canonical revision.
No new global ledger, scheduler or cell-wide refusal is added.

### D2a. Keep generated ownership durable and its lookup derived

Reuse guarded canonical metadata writes, the existing fragment representation and SQLite for indexed local lookup.
Those mechanisms already supply persistence and indexing; none records exact generated-image and native-copy ownership today.
Add one producer-owned catalogue rather than a general dependency graph or a second Source inventory.
Its maintenance cost is one metadata format, bounded reconciliation and exact publication hooks at each surviving-copy boundary.
The architecture applies the stable-interface principle in [Managed Agents](https://www.anthropic.com/research/managed-agents): durable producer custody survives execution replacement.

Persist bounded canonical image records and copy reservations under the generated-output owner.
One image map binds exact bytes; byte-identical stage, history and rotation copies reuse its image identifier.
Copy reservations bind the image, actual owner, origin ledger, opaque locator and expected version before bytes survive.
The private owner retains native paths and filesystem identities; canonical locators expose neither.
Keep only one generated-owner locator in each Source manifest, rather than rewriting every Source manifest for every shared image.

A whole Source page or sealed original uses an immutable `whole_source_ref` on its image header, without textual fragment pages.
The allocation and sealed original declaration establish that ownership; matching bytes, Markdown markers and nearby paths do not.
Binary originals therefore reuse image and copy custody without passing through UTF-8 fragment validation.
The Source manifest retains bounded, immutable original SHA-256 and size declarations before either storage mode publishes capture payloads.
Missing declarations mean unknown ownership; an explicitly empty declaration means the capture had no originals.
An undeclared rehydrated payload keeps an opaque image and its finite uncertainty horizon; matching bytes cannot upgrade it to whole-Source ownership.
Rehydration proves the current Source incarnation and its sealed declaration before reserving a copy and creating temporary bytes.
It retains exact native custody for partial plaintext, installation and cleanup, including unresolved copies after failed cleanup.
Acquire and pin required originals outside the collection checkout before admitting effective collection dependencies.
Commit revalidation opens present originals without hydration; acquisition failure preserves the existing per-row rejection semantics.
An original can survive a rejected collection row only under its Source copy owner, without a collection dependency admission.
Opaque result images use explicit producer-declared Source membership in their bounded headers, without textual fragment pages.
Their hashes bind exact serialized payload bytes; membership never comes from parsing or matching those bytes.
The existing metadata byte bound and positive-membership index also cover this variant.
One Source-private publication table selects the exact canonical image and Copy; it stores no payload or rollback bytes.
Existing row identities and hashes cannot select different ownership maps for identical serialized bytes after a crash.
The witness adds one bounded recipe format and transactional hooks at the existing result owners.
Prepare its content-free publication identifier, physical ledger, opaque row pointer and attempt incarnation before reserving the canonical Copy.
Publish the exact existing pickle or JSON bytes and selected witness in one SQLite transaction.
Read those bytes and their witness from the same SQLite snapshot, then close the connection before checking canonical retirement.
Every refresh compares the attempt incarnation and prior publication identifier, including completion fallback and delayed acknowledgment.
Replacement and expiry close the old witness atomically and clear its attribution; retain content-free closure until canonical Copy acknowledgment.
Prepared witnesses can prove that publication never occurred. A missing witness remains unknown and cannot be reconstructed from equal bytes.
Trusted typed producers supply bounded schema selectors and ordinals before compact projection discards attribution.
Compilation guidance binds its actual resolved Source pages, including correction resolution.
These selectors redact the target projection while preserving unrelated Source authority and historical admission facts.
Canonical copies define the expected obligations, including when a private native binding disappears.
An empty local lookup never proves completion. Missing birth coverage preserves known positive copies while leaving completeness unknown.

The native owner uses one protected table in the existing idempotency database, without storing bodies or rollback bytes there.
It seals root, parent, allocation identity and verified version before publication, then supplies exact cleanup and installation witnesses.
Installation retains the original allocation handle through the native effect and persists the exact destination version before releasing that handle.
A later observation requires that version; equal bytes cannot recover a missing witness or reconnect a reused file identity.
Partial-file cleanup requires the original live allocation handle. After restart, missing continuity leaves the copy unresolved and preserves its bytes.
This limit prevents deleting an unrelated replacement. Its cost falls only on that copy's cleanup, without blocking ordinary capture.
Replacement retirement retains the old allocation through the native rename.
The backend checks the expected old version and records its zero-link state and raw version before publisher callbacks.
Its sealed name receipt binds that replacement and the affected managed parents' durability barriers.
The publisher requires that same raw version before closing the old handle, then emits the retirement witness.
No later zero-link observation can replace a missing backend receipt.
An alias, moved old name or missing native evidence leaves the old copy open; ordinary publication can still succeed.
An overlapping reader may finish from its borrowed handle without becoming a reusable managed copy.
Canonical retirement requires the exact saved installed version and matching native allocation, rather than displacement from a slot.
The command-surface owner-access boundary and transactional-vault-writes pathname-window exclusion still apply.
Neither a writer lease nor a zero-link observation proves universal exclusion of unobservable direct OS-owner interference.
This follows [How we contain Claude](https://www.anthropic.com/research/how-we-contain-claude): native capabilities bound destruction while canonical records preserve obligations.

Keep one canonical slot for each generated-file locator and physical owner scope.
Its deterministic opaque key selects the current copy directly, with at most one pending candidate copy and publication identifier.
Read its copy records and image headers directly when rendering; a cold local index cannot block capture or erase healthy ownership evidence.
Pending catalogue metadata retains independently witnessed positive fragments and its uncertainty horizon during read-only planning.
Before validating a generated commit, recover bounded canonical metadata without requiring derived-index readiness; previews and preparation never recover or allocate custody.
Reserve the candidate copy and prepare the slot before selecting its private temporary name.
Each native publication retry prepares a fresh candidate and publication right before selecting its new temporary name.
Before allocating that name, recheck the open canonical reservation and immutable image content; uncertainty may only increase.
Normal success settles the slot from the native owner's exact installation witness after the rollback-capable batch finishes.
Equal content hashes cannot select between different ownership maps without that witness.
A takeover changes the slot revision and cancels delayed forward or inverse publication rights.
Displacing a copy does not retire its physical obligation; slot references also pin its image until the owner closes them.
Missing canonical custody remains different from a missing derived cache.

An authorized writer can replace an exact pending publication right without proving what its earlier native publication did.
The takeover preserves every displaced copy and records uncertainty; it grants no ownership, absence or cleanup fact about the earlier allocation.
The new publisher reads guarded current bytes and receives its own installation witness instead of selecting an earlier map by hash.

Direct reads combine each image's canonical uncertainty horizon with a global floor on the catalogue head.
The head records the checkpoint through which that summary is complete; recovery folds uncertainty before clearing its pending metadata unit.
A legacy catalogue without summary coverage starts conservatively at its existing epoch, without scanning history before ordinary capture.
Repeated observation of the same recorded uncertainty does not raise that finite floor.

An inverse publication creates a fresh native file, even when it restores the before-image bytes.
First settle the exactly observed installed candidate to consume its forward publication right, without claiming batch success.
Reserve a fresh restore copy on the before-image, then prepare and install it under a new slot publication right.
Never bind the earlier before-copy identity to the newly created restore file.
A crash between these steps retains truthful current custody and every unresolved physical obligation.

Qualify the derived SQLite membership index against the canonical catalogue identity and ordered checkpoint.
Advance canonical evidence before changing tracked ownership or publishing bytes; interrupted publication remains unresolved.
Reconciliation is bounded and resumable. A cold or inconsistent index reports its missing coverage instead of scanning history during erasure.
Warm lookup batches the target's image and copy obligations with an indexed uncertainty query.
Work grows with returned obligations, rather than unrelated Sources or one SQL request per fragment.
Trusted producers reserve new copies from directly verified canonical image evidence; collection and historical lookup still require a qualified index.

A freshly allocated Source can receive `generated_birth={catalogue_id,birth_epoch}` only before any generated output can survive.
Issuance requires participation by every relevant managed producer and native-copy hook; configuration alone is insufficient.
Replay or legacy adoption cannot manufacture a fresh birth witness.
Each exact image carries `unknown_through_epoch`: ownership may be unknown for Sources born at or before that checkpoint.
A matching catalogue and a later Source birth can prove target ownership despite unattributed legacy bytes in the same image.
Exact copies and slices preserve that horizon; joins take the greatest input horizon; tracked insertions preserve it.

Missing maps, hash drift or unexplained publication raise the affected horizon to the current canonical checkpoint.
Retain earlier positive membership as uncertain until its copies resolve or retire.
An indexed query includes every retained image whose horizon reaches the target birth, even when its fragment membership disappeared.
Global historical uncertainty remains visible, but does not become a permanent refusal for every later Source.
Do not infer coverage by matching renderer diagnostic strings or inspecting prose.

### D3. Use one dependency latch instead of a reverse history graph

Track `clear`, `present` or `uncertain`, coverage completeness and the first producer commit witness for each Source reference.
Before the first dependent knowledge commit, reserve an uncertain latch under the canonical boundary.
Successful publication marks it present. Proven no-effect or complete rollback restores its previous state.
Interrupted publication stays uncertain until existing producer recovery resolves the exact publication identity.
Resolving one no-effect reservation cannot clear another unresolved publication; any successful dependency makes present sticky.

Use one recovery epoch per Source and producer family for collection writers.
Concurrent operations join that epoch; exact operations remain in existing audit, version and import records.
This reuses producer recovery instead of adding a reverse dependency graph or another witness database.
Its maintenance cost is a versioned Source manifest, incarnation bindings in existing records, and producer recovery hooks.
The stable-interface principle in [Managed Agents](https://www.anthropic.com/research/managed-agents) applies: execution can change while producer custody remains durable.

Each admission advances the epoch revision. Before each surviving stage or commit, advance it again under the actual writer boundary.
Keep that boundary through the effect. A crash before positive settlement therefore invalidates an earlier census.
The census captures its epoch and revision before its first page and closes only that exact revision after complete producer reconciliation.
Closing revokes delayed publication rights. A later admission uses a fresh epoch identifier; closed identifiers never reopen.
Individual rollback cannot close a shared epoch. Version-one displaced uncertainty remains conservative legacy evidence.

Explicit collection sources retain their canonical incarnation alongside their presentation paths.
Read both final and historical origin carriers through the existing parser, including unchanged carriers that need no new authoring-input preparation.
File audit events bind each effect's exact references and shared operation identifier; kept history carries the same bindings with its retained payload.
Reserve the union of current and prior dependencies, but label each binding with its owning payload.
A primary and its audit prove current membership; surviving history proves only prior membership.
Partial rollback cannot transfer a failed replacement's inputs to an independent prior snapshot.
Store version associations bind exact references, epoch and operation to immutable row versions and the authenticated event.
Producer checkpoints bind logical store identity, physical instance, immutable transition and effect membership; paths and transaction integers cannot supply that identity.

An import job binds the SHA-256 of its exact input bytes, and an incarnation reference can come only from those bytes.
A restart that proves the bound bytes therefore cannot adopt another incarnation, and a recaptured Source carries a new reference.
Import inputs carry no canonical incarnation today: export manifests are plain JSON, and raw row files have no frontmatter.
Their store effects record incomplete current dependency. Derived member rows live outside the store, behind import log rows only.
The census therefore reports import ownership as missing evidence, never as independence.
Member witnesses and a durable derived revert closure arrive with the first import input that proves an incarnation.
Filesystem rollback needs the publication owner's exact cleanup outcome; SQL rollback and a suppressed cleanup exception cannot establish absence.
Close active SQL snapshots and release request checkouts before canonical Source metadata work; retain only detached preparation between phases.

Keep ordered presentation citations in `item_sources`; existing v1 events authenticate their exact order and duplicates.
Add canonical version associations inside the existing store, with one row per immutable version and Source reference.
The existing citation table lacks incarnation identity; extending it with synthetic origin rows would change legacy readers and audit material.
The added maintenance cost is one indexed association table, a v2 event envelope and a held-body descriptor.
An association names its authenticating transition and effect, owner version, Source reference, operation and optional admission epoch.
Persist a verified canonical reference even when its Source is unenrolled; absence of admission cannot erase known provenance.
The v2 envelope authenticates newly established associations through logical item keys, owner payload hashes and owner creation transitions.
An update can authenticate a previously unrepresented prior version without assigning its inputs to the replacement version.
Reuse existing associations for that exact owner instead of adding them again on every update.
Record current and prior membership completeness on the effect; missing legacy membership remains visibly unknown.
Held descriptors bind their own publication identifier and exact candidate/body hashes, including replacements with equal bytes.
Carry the corresponding descriptor into existing projection ownership when a stage or displaced input outlives its held row.

Reuse the existing detached view observations and extend them to exact native versions and recovery slots.
The input phase selects accepted rows, held overrides and every native payload preservation can retain, then closes its SQL snapshot.
The runtime releases request borrowing for canonical custody, then supplies the current writer for one uninterrupted mutation unit.
Advance the epoch before that unit and retain the actual boundary through preservation, staging and SQL commit.
An unobserved or changed native input waits for a fresh admitted observation before preservation can retain it.
Preparation cannot call the current staging-and-abort path or hydrate an original as if that were a pure read.

Run the Records or Planning command envelope on a bounded request worker when its stored operation needs detached input acquisition.
Reuse the store request's acceptance, completion and acknowledgement deadlines; an accepted request keeps its resources until execution ends.
The worker sends only preparation and final publication units to the store thread, so downloads cannot occupy that thread.
Preparation returns plain candidate values and exact basis fingerprints, with no writer, cursor, authorization object or borrowed resource.
Before publication, reauthorize and compare store identity, physical instance, manifest, container, item, held input and native input versions.
Transport each unit's committed state explicitly; a copied execution context cannot propagate that state to the command envelope.
Keep the actual guard from final effective dependency reservation through the effect, commit and producer settlement.
Process bulk candidates in order, claiming natural keys and filenames only after successful input acquisition and remaining row checks.
An import tick that needs acquisition hands the batch to the same bounded worker path; its final checkpoint remains inside the collection transaction.
Direct writer calls reuse these phases synchronously and preserve any actual guard their caller explicitly owns.

Preview lifetime access resolves each fresh checkout; saved methods, SQL cursors and authorization objects expire with their original checkout.
The preview owner closes the checkout instead of removing borrower counters or reactivating an expired scope.
An active caller snapshot prevents departure without rolling back caller work.
Canonical custody uses a trusted producer binding on the same idle handle because its reserved records can live in the vault store.
The runtime rechecks that handle's authority and accounts for metadata commits before opening the fresh request checkout.
Collection-only replicas retain their existing scope; this boundary does not claim whole-store snapshot or erasure coverage.

Semantic writes include resolved claims whose Source back-reference was skipped, retained origin bindings and historical before-images.
Restricted writers still publish service-owned custody in the same batch when they cannot write the Source back-reference.
Collection writes include item versions, imports and bulk operations. Both correction endpoints refuse erasure through existing correction facts.
Removing a current citation does not clear retained historical dependence.
Clearing a positive latch requires complete producer reconciliation, rather than disappearance of its first witness.

Reuse existing watcher ingress to invalidate coverage for unexplained external changes.
A bounded, caller-driven producer census can establish legacy coverage from structured references and exact ownership evidence.
Age alone neither proves independence nor prevents erasure. Ambiguous ownership reports the actual missing evidence.
The erasure request never scans all history or runs a semantic text search.

Under one actual writer-boundary step, recheck coverage, independence and the expected hash, then persist retirement and unpublish or fence the target.
Perform no auxiliary destruction before that cutoff.
Dependent writes and deferred publishers check the target's retirement fence before publication.

### D4. Keep cleanup with each persistence owner

Owned artifacts include canonical pages and originals, generated navigation and activity entries, capture guidance, replay and recovery records, staging and rollback copies.
Owner locators identify exact entries or versions. Cleanup never removes text by matching a title, URL or record identifier globally.
Shared navigation and rotated logs need ownership-aware removal or regeneration that also retires their retained versions.
Generated fragments carry exact producer identity through trimming, copying, rotation and batch before-images.
A dropped activity excerpt copied into another Source's log entry keeps its original fragment ownership.
Bound both named log segments and bytes processed in each invocation.
Do not omit existing capture prose as an incidental shortcut.

Store mode also requires page rows, held content, identifying audit material, projection intents/files and recovery copies to retire through its owner.
Its purge contract and historical-provenance seam require agreement with the canonical store owner before store-owned implementation.
File-mode custody and producer work can proceed independently. Store completeness remains unproved until that integration finishes.

Reuse index-removal fan-out, but obtain completion acknowledgements from every required component.
Registered, accepted or deferred work is pending. It is not a purge acknowledgement.
Fence deferred publishers by Source reference and expected incarnation at publication, including path reuse and rollback.
Each derived owner compares its retained identity, hash or generation inside the same transaction that deletes its rows.
A canonical path check before dispatch cannot authorize a later path-only delete.
Each owner checks its bounded target set; database work does not grow with unrelated Sources.

### D4b. Preserve store sequencing while retiring identifying material

The store owner retains existing store, projection, rollback and replica-publication modules.
This change owns the new `vault_store/purge.py` after the merged projector baseline.
Its only existing store-schema edit narrows the audit trigger through one migration, reviewed by the schema owner.
The integration uses existing guarded store commits: compare the target revision and hash inside the delete transaction.
Store-wide monotonic revisions distinguish a replacement page even when its path or bytes match an earlier incarnation.
Resolve only held rows proved to belong to the target; an unrelated person's edit is not erasure authority.

After target-qualified projection and vector cleanup finish, redact identifying audit fields rather than deleting audit rows.
Replace `path`, and `from_path` when set, with one opaque token per target life and original path.
Never reuse a token across lives or targets. Both move halves use the same token for the same original path.
Require `reserved_paths.classify_logical` to block each token and `ownership.classify` to classify it as something other than a page.
Keep a null `from_path` null: audit readers use that distinction to identify move halves.
Never make `path` null: vector readers interpret that value as a vault-wide reset.
Set `sha256` null and replace the unstructured `reason` with fixed redaction text.
Retain `seq`, `revision`, `operation`, the closed origin token and timestamp so existing cursors keep their order.
Follow exact move lineage for the target's life; do not redact a replacement incarnation's later life at a reused path.
The schema migration permits only this qualified redaction update, with sequencing fields unchanged, null move origins preserved and `sha256` cleared.
It refuses all other audit updates and every audit deletion.

Managed reusable store snapshots and published replicas must retire or replace versions older than the purge revision.
The component receipt binds the physical store and purge revision, including held content, projection and replica publication acknowledgements.
Collection transaction sequence alone cannot prove freshness because page commits advance the store revision separately.
An active overlapping reader may finish; a managed snapshot must not later republish the retired data.

The guarantee excludes forensic remnants in SQLite free pages and WAL storage, as it excludes filesystem free blocks.
Do not add `secure_delete`, checkpoint or vacuum work to claim physical erasure that this contract does not promise.
The store owner has agreed this contract; implementation still requires the merged projector baseline.
Run the projector, migration and served-store suites before delivery because each reads this audit trail.

### D4a. Qualify configured erasure policy separately from discovery

Static operation features prove implementation, not that a configured identity system is enabled.
Expose authenticated `inspect_capture_erasure_policy` through `sources.erasure_policy`, accepting one identity system.
Return observed `enabled` and `policy_revision`, or explicit unknown values when configuration cannot be read.
Require whole-namespace authority before inspecting this service policy.
The consumer uses this read for registration and background readiness, without adding it to each passage query.
Every erase still checks current policy before admission.
After a stale hash, the consumer can issue a new checked operation for its existing target reference; its durable retirement intent never retargets.

### D5. Preserve snapshot reads and fence reusable publication

An overlapping read can finish from its existing snapshot or detached invocation bytes.
After complete retirement, new reads cannot reach that incarnation through managed storage or reusable caches.
This requires no new general reader pin, global reader drain or process-retirement prerequisite.

Each reusable cache owner advances its relevant generation and invalidates affected entries under its owner lock.
A builder captures that generation and checks it inside the same lock before publishing its result for reuse.
An obsolete builder can finish its overlapping invocation but cannot republish retained Source bytes.
Where dependencies are unavailable cheaply, invalidate that vault's cache namespace.
Existing retained projection pins still constrain their owner's collection; they do not become a general Source barrier.

### D6. Close attempt residue separately from admission

Extend settlement with `custody_state`, `owned_targets` and `attempt_residue` without changing `past_outcome`.
Each owned target reports only its allocated reference and retained hash.
Keep allocation paths private. Settlement reads only its ledger; the caller obtains current presence, hash and indexing evidence through complete P4 inventory.
Unknown capture outcome can coexist with a known allocation; neither proves canonical publication.

Ordinary settlement only closes admission and reports custody. An explicit `retire_owned` continuation requests cleanup of pre-reference residue.
The caller first closes admission, retires allocated incarnations, then continues residue cleanup.
It declares withdrawal complete only after every target and residue acknowledges retirement.
Keep uncertain current capture visible until the caller chooses recovery or withdrawal; ordinary settlement never destroys its preserved original.
P2 collection retains the last recovery link while residue or retention reservations remain unresolved.
After producer acknowledgements, normal bounded receipt retention can collect the attempt.

### D7. Reuse bounded caller-driven progress

Persist a target-qualified operation and component cursors before the first destructive step.
Each invocation performs a bounded unit through the existing writer and owner APIs, then returns progress.
The caller's existing durable job queue retries pending work. Add no daemon, workflow engine or global sweep.
Never hold a database connection while waiting on another component or network operation.

Use the repository's 20,000-Source generated reference and existing public warm-call budget: p95 below one second for a bounded progress invocation.
Record terminal failures and pending outcomes, component work bounds, query count, lock contention and resources with the maintained instrument.
Whole-retirement duration is a separate observed value. A new operation has no earlier latency baseline or measured improvement claim.

[Writing effective tools](https://www.anthropic.com/research/writing-tools-for-agents) supports one operation that exposes useful progress.
[Containment](https://www.anthropic.com/research/how-we-contain-claude) supports enforcing effects at the existing runtime boundary.
These principles require no additional agent machinery or human approval gate.

## Risks / Trade-offs

- Missing ownership delays only the affected target; retain its obligation and identify missing evidence.
- A positive dependency latch favors preservation; exceptional clearing costs a producer census.
- Shared logs and recovery records require precise ownership; an untracked copy prevents complete retirement.
- Path reuse can race delayed cleanup; every owner must fence by incarnation and retire only the old version.
- Replica loss can lose custody evidence; preserve it through supported recovery and keep unknown visible otherwise.

## Migration Plan

1. Ship the reviewed operation with identity-system policy disabled; verify ordinary capture and deletion remain available.
2. Prove generated capture, lost-response recovery, dependency refusal and complete managed cleanup in isolated file and store cells.
3. Qualify bounded public progress and new-reader absence on the delivered candidate; retain target and component receipts.
4. Enable a cell only under its existing operational authority; stop new erasure admission to roll back policy.

Policy rollback cannot restore erased bytes. Restoration requires separately authorized retained backups and can reintroduce the Source.
