## Context

See proposal.md for the outcome and three deliveries. The implementation base includes merged RAW admission and compiler corrections. Their current-authority checks and shared classification remain required.

`RequestPrincipal` already separates audience, owner labels, purpose, and governance authorization sessions. OAuth and local ingress already use `SessionAuthority`. The connector ceiling must retain verified client and originating authentication facts without creating another authentication system.

The existing portability reader accepts extra manifest keys but accepts only version 1. Adding an optional requirement field would not stop that reader. Existing runtime compatibility enrollment supplies the durable same-root guard.

## Goals / Non-Goals

The immediate batch implements Delivery A. The same owner keeps legitimate writes and useful admitted recall through differently restricted connectors. Hidden content cannot affect exposed results, errors, ranks, counts, hashes, or conflicts.

Delivery A does not implement private vocabulary promotion or managed-vault import. Those remain required before real consolidation. No deployed connector, vault, gateway, or provider changes during implementation. Tests use temporary state and no external provider calls.

The boundary covers Exomem-mediated operations. Direct filesystem access, manual copying, and uploads outside Exomem are outside its enforcement claim.

## Decisions

### 1. Separate connector identity from owner authority

Add frozen, bearer-free `ClientBinding(issuer, client_id)` to `RequestPrincipal`. Retain only the verified `ExomemSessionAccessToken.client_id` object provenance or current `LocalGrant.client_id`. Arbitrary claims, headers, names, user agents, tool arguments, and shared bearer identity cannot supply a binding.

Preserve the binding through OAuth, local MCP, REST, owner normalization, governance authorization sessions, and signed delegation. `dataclasses.replace` preserves the fields. Unknown and re-registered clients receive the configured default until the host explicitly configures their verified binding.

Also retain a frozen, bearer-free originating authentication binding. OAuth currently drops the validated session record's identity and generation; local grants already carry a session identity. At delegated consumption, existing `SessionAuthority` revalidates the active record, generation, expiry, and refresh family. A governance authorization session alone cannot prove the original login remains active.

The signed v3 transfer decoder explicitly reconstructs both nested frozen bindings as well as its governance authorization context. Signature validity alone does not keep a revoked session alive. Legacy v2 owner-audience transfers receive the restricted default when armed.

Only explicit administrative CLI, stdio, or library ingress may be unrestricted. Missing connector provenance, owner audience, `surface="transfer"`, a REST key, or a shared cell bearer never implies that ingress. Preserve unarmed behavior.

### 2. Use one host-owned configuration and canonical Scope membership

Use one external JSON document selected by `EXOMEM_CONNECTOR_BOUNDARY_CONFIG`. The host owns its path and contents. The document maps verified issuer/client bindings to denied Scope ID sets and defines a restrictive unknown-client default. Names and project values remain configuration data.

`governance/connector_boundary.py` validates the document, resolves a snapshot and revision, and references canonical `policy.Scope` definitions. It does not copy selectors or create another membership authority. Missing referenced scopes and malformed armed configuration are invalid security state.

Evaluate the ceiling through the existing membership kernel and combine it with the existing `Decision` by a meet. Owner status, purpose, grants, bridges, and exact releases cannot widen it. Keep `raw_protection.has_unrestricted_access` RAW-specific. A separate unrestricted-content predicate requires both current RAW authority and an unrestricted connector ceiling.

Protect the host configuration and referenced protective Scope definitions from limited connectors. A limited connector cannot weaken its own membership through metadata edits, moves, replacement, or reclassification. Evaluate before and after membership for those operations.

Restore canonical-reference membership for Markdown by supplying refs from valid, already-parsed frontmatter to the same kernel. Reuse `memory_refs.ID_FIELD`, `normalize_id`, and `memory_ref`. Unresolved canonical identity refuses only membership that requires a canonical reference. Path, project, and tag scopes retain legacy identity behavior and path-reference support. Add no scan, second resolver, or semantic regex.

### 3. Admit contributors before computation and observations

Apply the ceiling in `egress._decide_path`, annotation, path-only records, fast paths, release walks, restricted release filtering, and restricted-caller classification. Empty policy and owner shortcuts execute only after ceiling admission.

Cover downloads, frames, artifact resources, and delegated transfers. In `collection_store`, `OperationAuthorization.decision` and `authorized_rows` exclude denied rows before `values_json` decoding, inspection seals, summaries, or derived calculations.

Filter contributors before retrieval cuts, ranking, graph traversal, memory context, context packs, working sets, canonical state, continuity, history, alias resolution, and citations. Preserve compiler classification reuse, floor checks, and canonical-state expansion.

Whole-corpus bootstrap, registries, aggregates, provenance, receipts, and error builders must use admitted contributors or existing honest unavailable results. Source-kind classification-debt advisories and kind-refusal counts require the same ceiling after their delivery merges. They cannot expose hidden keys, hashes, counts, or the existence of hidden targets. Output filtering after computation is insufficient.

Release caches and collection summary dependencies include client binding and configuration revision. Recheck live session and configuration at result or capability consumption. A working-set packet cache may remain unguarded only when request admission precedes every observable derivation.

The shared mutation retry owner separates both explicit and implicit keys by verified issuer/client within the existing logical-vault namespace. Preserve existing audience/principal scope semantics. Exclude session identity, generation, and configuration revision from stable retry identity so reauthentication retains successful retries. Current origin authority remains a separate consumption check. Apply this client separation even before arming.

Both closed transfer operations issue bearer capabilities. They use the existing credential-issuance bypass and never persist or replay a durable mutation terminal. Fresh upload and download minting remains available. The new client namespace never adopts old audience-only terminals. A pre-upgrade unguarded file append retried after upgrade may execute again; the old terminal cannot prove which client owns it. Current-namespace exact retries retain their normal behavior.

### 4. Preserve allowed writes without a new approval workflow

Keep `is_owner` and existing legitimate capture/edit authority. Hidden-target admission precedes existence, collision, or parsing errors. Moves, replacement, and reclassification check both old and proposed membership. Limited connectors cannot mutate protective Scope definitions or configuration.

The host configures capture namespaces in the external boundary document. Stopped arming verifies that each namespace is empty or contains only content admitted under every configured ceiling. All canonical writers preserve that invariant, including unrestricted owners, moves, replacement, reclassification, imports, restores, and Scope or configuration changes. Location never overrides private metadata.

Capture readiness and all-writer enforcement use the existing portable path identity. Arming refuses alternate spellings of namespace components. Writers use the configured spelling, so case or Unicode aliases cannot introduce hidden collision state.

Before existence checks, collisions, or generated-name allocation, limited creation requires an independently eligible capture destination. Creation outside these namespaces returns the same unavailable outcome whether a hidden target exists or not. Existing admitted edits elsewhere continue. Explicit destinations are never silently redirected. A typed capture default may use the configured namespace only when its contract reports the destination.

Do not add blanket read-only owner behavior, a pending queue, or routine human approvals. Public registry writes remain available only where their success, validation, and conflicts do not depend on private definitions.

#### Canonical point writes and scoped retries

Implement this adaptation only after the actual S1 successor merges. S1 owns existing field admission and migrations; Delivery A adds the later scoped retry migration. Preserve private transaction and audit integrity, `mutation=True`, and existing non-owner authority.

Resolve the admitted canonical manifest and target before decoding values or probing conflicts. Hidden and absent targets share the existing not-found result. Reuse S1's complete-field obligation inside its canonical owner for both complete collection writes and point writes. Check old and proposed states before commit. S1's field owner retains fresh `raw_protection.has_unrestricted_access` semantics, separately from connector admission. Partial-field readers receive no full-state write guard.

Limited inspections, guards, and receipts use the same admitted v2 snapshot, including when no hidden rows exist. Stable canonical subject identity replaces physical row allocation. Recheck target concurrency, metadata, manifest/schema, proposed membership, complete fields, session, and configuration before commit. Every served historical contributor requires fresh admission or a complete authorization dependency fingerprint that proves unchanged authority.

Validator owners supply `inputs_unchanged` predicates for constraints that depend on global state. Skip natural-key twin lookup only when the normalized key is unchanged. Changed keys or hierarchy, inserts, bulk writes, and other global-dependent operations return existing unavailable results before probes without an independent admitted namespace. Capture folders do not partition SQL keys. Held-candidate resume admits its separate `exomem://collection-held` subject before decoding and again at deletion or publication. Hidden and absent held IDs remain indistinguishable.

Markdown-items projection publishes only the admitted target. Markdown-log projection remains private and pending under the existing authorized reconcile owner. Never replace a whole container with an admitted-only rendering.

Scoped retries bind server-verified logical vault/store identity, issuer/client identity, canonical collection, and canonical target. Resolve aliases before constructing the namespace. Exclude session generation and configuration revision so refresh preserves exact retries. Unknown shared ingress returns existing unavailable results before ledger observations. Caller labels, headers, arguments, and display names cannot establish this namespace.

Add the server-only closed `point:v1:` outer key domain before `completed_terminal` or any other outer lookup. Full and limited verified-client point requests share that domain across authority changes. Existing administrative and unarmed legacy behavior remains. This prefix separates old terminals; it never authorizes disclosure.

Add one nullable `scoped_request_id` column and an independent unique index to the existing inner `txns` owner through a post-S1 migration. Preserve legacy `request_id` values, uniqueness, attribution, hashes, and receipt bytes. New scoped transactions set `request_id=NULL`. Never fall back to a legacy lookup or derive a namespace from old caller labels.

Before either scoped lookup, verify the current client binding, canonical target, and complete target fields. On a hit, compare the original argument digest and project the stored historical envelope under current authority. Do not compare the old precondition with today's state. On a miss, require the current admitted v2 guard and target concurrency before any effect, including a held refusal. A legacy success or held retry cannot hit this namespace; its old guard refuses without creating another candidate or probing its legacy ID.

Store one private versioned envelope in the existing `receipt_json` owner. Bind the original served before/after snapshots, historical contributors, manifest/field basis, and target versions. One decoder/projector handles live, inner replay, and outer completed terminals. Revalidate historical contributors and complete fields against current canonical authority. Never reconstruct historical guards from today's rows. Missing historical proof makes the receipt unavailable while its committed mutation remains committed. Add no receipt table, replay framework, or recovery journal.

False admission refusals cost one allowed write or receipt retry. Unknown provenance and global-dependent operations lose point utility until their dependencies exist. No human approval queue repairs those missing dependencies.

### 5. Make private vocabulary domains a required follow-on

Global key, alias, and folder uniqueness can disclose a hidden definition through success or conflict. Whole-registry hashes disclose private changes. Filtering output or adding a blind additive endpoint does not repair that authority model.

Delivery B follows the vocabulary registry foundation. It defines an explicit public core and owner-selected public extension, plus private domains tied to canonical Scope IDs. Collisions, hashes, aliases, folder resolution, and promotions stay within admitted domains. No private domain automatically joins the public registry.

Delivery A protects any whole registry whose output or mutation depends on hidden definitions and returns the existing unavailable outcome when it cannot compute an admitted view. It cannot claim complete limited-owner vocabulary promotion. The exact public-write seam must be checked against current registry code before implementation proceeds there.

### 6. Arm durably under stopped maintenance authority

Arming requires a stopped and drained destination. Validate the configuration and every canonical Scope reference, enroll `connector-content-ceiling-v1` under the existing manifest lock and durable publication owner, then publish a reserved portable armed requirement. Do not use the store-custody migration method.

Stopped arming also prepares the existing semantic-activation manifest through its canonical writer and verifies capture namespace readiness before serving.

Startup requires agreement between configuration, compatibility state, and the armed requirement. Once enrollment is durable, a crash before requirement publication prevents serving until repair. Missing configuration or environment cannot turn an armed vault into an unarmed vault.

The existing physical-family compatibility guard makes the actual older runtime reject the new capability on the same root. Prove that with the old binary; a patched catalogue is not evidence.

### 7. Preserve arming across supported portability

Armed exports use manifest schema version 2 with a mandatory portable armed requirement. New readers accept versions 1 and 2; unarmed exports remain version 1. An older version-1 verifier rejects an armed archive before extraction.

The reserved artifact contains the referenced protective Scope definitions from the active canonical policy, with a fingerprint of their normalized selectors. Scope IDs alone cannot preserve protection. This artifact carries protection data, without source rules, grants, audiences, client mappings, sessions, custody, or authority. Runtime membership always uses canonical `policy.Scope` definitions; the portable copy never becomes another selector engine.

Classify the reserved artifact explicitly because internal state is otherwise excluded from export. Verify the manifest, definitions, fingerprint, and artifact agree. Whole-vault export remains unavailable to content-limited callers.

Restore installs exact protective definitions through existing canonical Scope and publication owners under stopped destination maintenance authority. An existing same-ID definition with different selectors refuses; only an explicit destination policy amendment may change it. Restore into fresh external state retains required compatibility and remains unserved until destination configuration satisfies the protection and capture namespace invariants.

#### Exact retry retains destination protection and evidence

Scope installation creates destination-owned Scope mirrors and append-only receipt files absent from the immutable source manifest. Protected publication retains that residue on the published live inode after failure. Strict retry permits only exact mirrors from the canonical governance serializer and event evidence verified by the existing receipt owner. Archive member presence, byte digests, sizes, no-follow checks, and link rules remain unchanged. No generic derived-artifact or `_Governance` exception applies to staging.

Exact stopped protective restoration uses complete archive continuity evidence. One versioned proposal-evidence discriminator selects this evidence through the internally validated restore binding. Proposal creation, legacy and v4 commit validation, and exact recovery share the same evidence owner. The binding covers the complete inventory, manifest digest, destination, state placement, operation, and exact missing protective documents. Existing archive and residue verifiers check the bytes. Ordinary proposals retain affected-membership evidence, including legacy persisted proposals.

Continuity does not classify restored configuration or measure disclosure change. The proposal marks membership consequences unevaluated and explains its conservative transition direction. Grant drift, dependent grants, capture namespaces, and read admission keep canonical membership checks. User-owned YAML registries receive no filename or directory exemption. Unknown or altered files refuse continuation without destructive cleanup.

Receipt verification separates the physical staging evidence root from the actual destination authority root. It binds evidence to that destination's sidecar instance, durable head, exact restore publication, and Scope proposal. A self-consistent JSONL chain alone cannot authorize residue. Preserve valid append-only evidence; recover linked pending events through the existing exact recovery owner. Refuse unexplained files without deleting or truncating them. A pending mutation marker is admitted only through its existing validator against the linked operation and destination authority. Physical marker acquisition retains held-handle, no-follow, and single-link checks.

Bind archive digest, manifest digest, operation identity, destination identity, and exact protective documents to the existing governance proposal/recovery owner. Factor the existing hosted journal mechanics into one owner shared by hosted and standalone restore. Keep hosted journal version 1, paths, binding checks, and its lifetime lock. Standalone journals live in destination external state under the existing mutation coordinator. Before rename, persist the reserved proposal ID and exact restore identity. Persist missing documents before issuing that proposal at the published destination. Before loading policy or returning for existing Scopes, resume the exact previous proposal through canonical `_commit` recovery at the actual destination. Hosted migration passes only this validated binding through its existing owner. After the short migration lock releases, protection recovery completes before readiness. Unarmed migration and adoption keep their existing behavior. Existing Scope presence does not prove receipt and mirror publication completed.

Preserve the destination inode and external state across protected failure and process death. A crash after rename may leave the live tree present. Continue only when durable destination-bound restore evidence and canonical bytes match the exact request. All unrelated or changed requests retain `LIVE_VAULT_EXISTS` and no-overlay behavior. A caller-supplied prepared object cannot adopt an arbitrary live tree. Add no independent recovery journal, signer, installation fence, or authority registry. Protected failures never return the live inode to staging. Existing restore admission runs before publication and preserves unavailable and activated-vocabulary replacement refusals. Standalone publication uses the existing offline migration owner for physical relocation, protection recovery, capture invariants, and readiness.

Incorrect refusal costs the stopped destination an exact retry. Recovery must never discard data, protection, or receipt history to restore readiness. Canonical recovery supplies the decision without a new human gate.

Never transplant source connector mappings, credentials, audiences, sessions, or administrative authority. The destination host configures its own clients. Supported backup/export/restore and same-root downgrade preserve the guard. Raw filesystem copying and arbitrary historic binaries are outside this guarantee.

### 8. Treat the current hosted gateway as unknown-client ingress

The available trusted context identifies a cell bearer, not its end client. No gateway application source is in this repository. Keep the current hosted RAW exemption unchanged, but never extend it to the connector ceiling.

Armed shared-cell requests receive the restricted default until verified end-client provenance exists. Passed HTTP headers and tenant-owner identity cannot replace it. Standalone OAuth/local support remains independently deliverable. Gateway work is a separate dependency, with no implied login or deployment action.

### 9. Retain the offline import contract without the retired protocol

Delivery C uses a stopped destination and current mutation, journal, and restore owners. Its command shape and detailed recovery steps will be specified after Deliveries A and B. No eleven-action online saga, new signer, installation fence, custody registry, or page-acknowledgement protocol is required.

The vault-consolidation delta retains the unique requirements from the previous artifacts:

| Retained worth | Required behavior |
| --- | --- |
| Complete inventory | Account for every canonical source and destination object; a filtered MCP crawl is not a complete inventory. |
| Conflict resolution | Distinguish identity, normalized path, content, and dependent structural conflicts before publication. |
| Exact duplicates | Preserve the source-to-destination provenance mapping even when publication is a no-op. |
| Canonical integrity | Preserve Sources, Evidence, Records, media, sidecars, units, identities, history, relations, citations, and review provenance. |
| Authority separation | Never transplant credentials, audiences, grants, sessions, connector mappings, runtime authority, or indexes. |
| Private intake | Use bounded archive validation and staging outside recall and active roots. |
| Exact state | Bind fingerprints, before/after bytes, copy verification, and complete preimages. |
| Recovery | Retry the same identity and payload; classify durable state without repeating semantic decisions or overwriting later work. |
| No loss | Keep verified surviving copies of every imported bundle; a pre-import preimage does not preserve imported bytes. |
| Evidence | Preserve append-only plaintext-free receipts outside policy, knowledge, and restored content snapshots. |
| Operational authority | Keep capability proof, real import/cutover, and destructive source retirement separate. |

Canonical census fingerprints include authored content, policy, access, review, and history. Fixed exclusions cover rebuildable derivatives and operational evidence/control state. Receipt or run churn cannot stale a content fingerprint, and restoration never rewinds evidence.

The source remains unchanged during import. If a source resumes writes after its snapshot, a later cutover must reconcile fresh bytes. Rollback accounts for every later destination create, edit, move, and delete. Before any last source/archive copy is retired, retain a verified forward copy and prevent a rollback that would discard the last imported bundle.

### 10. Prove the product boundary with one reusable twin fixture

Use real authenticated local/OAuth transports against temporary twin vaults with identical allowed content and different protected canaries. Two clients retain the same owner status and different ceilings. Assert useful allowed reads/writes alongside negative disclosure through every affected producer and transport.

Cover live configuration changes, re-registration, revocation, refresh-family invalidation, governance sessions, cache reuse, signed v3 delegation, and legacy v2 transfer. Test the actual old runtime on an armed root, armed version-2 restore into fresh state, and unchanged unarmed version-1 portability.

Run focused tests during development, then the full corpus in completion CI. Run lint, build, generated-artifact checks, generic privacy checks, strict OpenSpec validation, and independent security review of the exact integrated diff. Optional model lanes remain explicitly unavailable when not run; they do not waive admission tests.

## Controls and costs

| Control | Prevents | Wrong-firing cost and owner |
| --- | --- | --- |
| Restricted unknown default | Unverified or re-registered clients inheriting broader access | Host must configure a legitimate new client; its user temporarily loses denied-scope access. |
| Admission before observation | Hidden content influencing results or write errors | Caller loses only results the configured ceiling excludes; admitted utility is tested. |
| Origin-session revalidation | Delegation surviving authentication revocation | Caller retries or signs in after invalidation; existing session authority owns availability. |
| Capture namespace invariant | Hidden-path collisions disclosing private content | Limited callers choose a configured destination; the host maintains its all-writer visibility invariant. |
| Protected configuration and scopes | Limited clients widening their ceiling | Host performs protective policy maintenance; ordinary allowed writes continue. |
| Armed startup agreement | Missing state silently disabling protection | Operator repairs invalid security state before service resumes. |
| Offline arming and import | Readers seeing partially protected or imported data | Operator schedules a maintenance window; no new per-step approval owner is required. |
| Unavailable hidden registry | Global uniqueness and hashes disclosing private definitions | Limited users lose that registry operation until Delivery B; public-independent writes remain. |
| Verified surviving copy | Import or rollback deleting the last copy | Operator retains storage and repairs missing copies before destructive work. |

## Migration Plan

1. Deliver connector admission and portability with authenticated twin-vault proof, independent review, and ordinary release evidence.
2. Deliver private vocabulary domains after the registry foundation, with private-independent public writes verified.
3. Specify and deliver offline managed-vault import using the retained preservation and recovery requirements.
4. Rehearse import and rollback on disposable copies, with allowed utility and negative disclosure verified.
5. Perform real import and connector cutover only under explicit operational authority, with source copies retained.
6. Retire source copies only under destructive-operation authority, with every surviving imported bundle verified.

Product rollback restores physical isolation while retaining the ceiling and its compatibility guard. Never remove the guard to regain readiness. Delivery A completion leaves Deliveries B and C open; archive this change only after all required product work has shipped.
