## MODIFIED Requirements

### Requirement: Current Relation Review Disposition

Every newly created active governed compiled page SHALL have a current relation-review
disposition satisfied by a qualifying typed inbound/outbound relation, a qualifying
outbound connectivity signal, an explicit reviewed-none decision bound to the page's stable identity, or an automatic no-candidates bootstrap disposition only for a genuinely
empty governed corpus.

A relation qualifies as a **typed edge** only when it is authored or explicitly
reviewer-accepted, its target resolves unambiguously to an eligible governed page at
evaluation time, its canonical registry entry is active and scope-valid, is not marked `generic: true`, its registry
family is not `link`, `citation`, `derivation`, `evidence`, `mention`, `observation`, or
`provenance`, and either (a) its origin is `markdown_relation`, `semantic_relation`, or
`semantic_block`, or (b) its origin is `frontmatter` and its registered family is exactly
`supersession`. The sole inactive-target exception is a canonical supersession edge to the
exact governed predecessor now marked `superseded` whose `superseded_by` resolves back to
the active successor.

When no typed edge qualifies, the disposition MAY be satisfied by an **outbound
connectivity signal**: an authored outbound connection whose target resolves unambiguously
to a connectable governed page, whose canonical registry entry is active and scope-valid,
and whose origin is `wikilink`, `markdown_relation`, `semantic_relation`, or
`semantic_block`, irrespective of registry family. A resolved outbound body wikilink SHALL
also satisfy this lane by normalized membership in the connectable target set without
requiring a relation fact. The connectable set SHALL be the eligible governed set widened
to admit append-only `Sources/` material, and SHALL be computed separately from the
eligible governed set so that the empty-corpus bootstrap disposition is unaffected by
captured sources.

Frontmatter origin SHALL NOT satisfy connectivity. This keeps the disposition aligned with
the relation-debt measurement, which clears a page on authored relation rows or body
wikilinks and ignores `sources:`. Provenance is a vertical edge to raw material and carries
no claim about how a conclusion relates to other conclusions; because every
adoption-compiled note cites a source by construction, counting it would make the
disposition a no-op for exactly the bulk-import case that most needs review.

Inbound edges MUST NOT satisfy connectivity. Unresolved or ambiguous forward targets MUST
NOT satisfy either lane. A disposition satisfied by connectivity SHALL report that signal
distinctly from a typed edge and SHALL emit a non-blocking typed-edge-absent warning, except when the authored-all-generic advisory already describes that pattern. The
disposition SHALL carry any reviewed-none reason and reference it was satisfied by, so
review decisions are observable rather than write-only. No minimum relation count SHALL be
imposed in either lane.

A reviewed-none decision SHALL bind the page's stable identity until an edit of the page
removes its last connection, not its exact bytes. An edit of a page that held a reviewed-none
decision before the edit SHALL carry that decision forward, and report the disposition
with `review_renewed: true`, when the page's identity is unique in the identity census,
the page is an eligible active compiled page before and after, and the operation is not
a creation, replacement, adoption compile or tier-2 creation. A carried disposition SHALL
keep the recorded reason and reference. An edit SHALL NOT carry a decision when the page
held a qualifying relation or connectivity signal before the edit, so an edit that
removes a page's last connection is refused as before. A bootstrap disposition SHALL NOT
carry once a second eligible page exists. Audit and watcher evaluation SHALL carry a
reviewed-none decision that the loader finds for the page and report it as carried, so
audit and writes agree. A disconnection that no edit of the page made, whether an edit of
another page or a change outside the server, SHALL NOT lapse the decision: the older
decision carries and audit reports it as carried. A page whose only decision is a
lifecycle decision that no committed slot names, because an edit connected the page or its
bytes changed outside the writers, asks for relation review once more.

#### Scenario: Superseded predecessor remains a qualifying target
- **WHEN** an active successor canonically supersedes a governed predecessor and the predecessor is marked `superseded` with an exact backlink to that successor
- **THEN** the supersession edge remains qualifying after commit and during exact prepared recovery
- **AND** no other inactive target receives this exception

#### Scenario: Typed relation satisfies disposition
- **WHEN** a new compiled page has a registered non-generic canonical relation to an existing page
- **THEN** its relation disposition is satisfied and reports the typed edge
- **AND** no typed-edge-absent warning is emitted

#### Scenario: Reviewed none is fingerprint-bound
- **WHEN** a reviewer records that a page has no qualifying relation candidates
- **THEN** the decision is recorded against the page identity and the fingerprint of the content reviewed, and that exact match satisfies the disposition on its own
- **AND** the recorded reason is reported on the disposition rather than discarded

#### Scenario: Reviewed none binds the page identity across edits
- **WHEN** a page that holds a reviewed-none decision is edited and still has no qualifying connection
- **THEN** the edit commits without asking for relation review again, writes no new decision and reports `review_renewed: true`
- **AND** the decision's recorded reason and reference stay on the disposition

#### Scenario: Removing the last connection is refused despite an old decision
- **WHEN** an edit removes the last qualifying relation of a page that holds an older reviewed-none decision
- **THEN** the old decision does not satisfy the edited page
- **AND** the edit is refused with `RELATION_DISPOSITION_STALE` unless it records a fresh reviewed-none decision

#### Scenario: A disconnection that no edit of the page made does not lapse the decision
- **WHEN** a page with a reviewed-none decision gains a connection, and either an edit of another page or a change outside the server removes its last connection
- **THEN** audit reports the older decision as carried, with `review_renewed: true` and its recorded reason
- **AND** the next edit of the page commits without asking for relation review again

#### Scenario: A contested identity does not carry a decision
- **WHEN** a second path claims the identity of a page that holds a reviewed-none decision
- **THEN** an edit of the page does not carry the decision and relation review is asked again

#### Scenario: Empty vault bootstraps without fake edge
- **WHEN** the first compiled page is created in a genuinely empty governed corpus and no target can exist
- **THEN** the writer records an automatic bootstrap disposition without fabricating a relation or placeholder

#### Scenario: Captured source does not consume the bootstrap exception
- **WHEN** a vault contains only captured `Sources/` material and the first compiled page is created
- **THEN** the page still receives the automatic bootstrap disposition
- **AND** the connectable target set does not widen the eligible governed set used to detect an empty corpus

#### Scenario: Bootstrap exception expires when a candidate can exist
- **WHEN** a second eligible compiled page is added after the first page received an automatic bootstrap disposition
- **THEN** the first page's bootstrap disposition becomes stale and enters ordinary relation review

#### Scenario: Excluded-family relation satisfies connectivity without a typed edge
- **WHEN** an active compiled page carries no qualifying typed relation but one or more authored relation rows of an excluded family resolving to connectable governed pages
- **THEN** its relation disposition is satisfied and reports the connectivity signal rather than a typed edge
- **AND** a typed-edge-absent warning is emitted at warning severity
- **AND** the write is not blocked

#### Scenario: Resolved body wikilink satisfies connectivity
- **WHEN** an active compiled page carries only an outbound body wikilink that resolves unambiguously to a connectable governed page
- **THEN** the disposition is satisfied and reports the connectivity signal rather than a typed edge
- **AND** a typed-edge-absent warning is emitted at warning severity
- **AND** the write is not blocked

#### Scenario: Cited provenance alone does not satisfy the disposition
- **WHEN** an active compiled page carries no typed relation and no body wikilink, but names existing `Sources/` pages in its `sources:` frontmatter
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** an adoption-compiled page, which always cites a source, still receives relation review

#### Scenario: A body wikilink to a Source does satisfy connectivity
- **WHEN** an active compiled page links a `Sources/` page from its body rather than only from frontmatter
- **THEN** the disposition is satisfied by the connectivity signal
- **AND** the target is not required to be an eligible governed page

#### Scenario: Inbound links and provenance back-references never satisfy connectivity
- **WHEN** a compiled page has no outbound connection of any kind, but a `Sources/` page links to it and its own `ingested_into:` back-reference names it
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** automatically written back-references cannot make the disposition vacuous

#### Scenario: Excluded-family relation does not qualify as a typed edge but may connect
- **WHEN** a page's only relation is in the `citation`, `evidence`, or `link` family and resolves to a connectable governed page
- **THEN** it does not qualify as a typed edge
- **AND** it satisfies the disposition as connectivity, reported distinctly, with a typed-edge-absent warning

#### Scenario: Inactive relation does not qualify in either lane
- **WHEN** a page has only a relation whose canonical registry entry is inactive or out of scope
- **THEN** the edge remains visible in its own semantics and the disposition remains unsatisfied

#### Scenario: Unresolved forward target does not qualify
- **WHEN** a registered authored relation points to a missing or ambiguous target
- **THEN** the relation remains visible with its resolution finding but does not satisfy relation review
- **AND** it does not satisfy connectivity

#### Scenario: Self wikilink does not qualify
- **WHEN** a page's only outbound body wikilink resolves back to that same page
- **THEN** the disposition remains unsatisfied and enters ordinary relation review
- **AND** the self-link does not satisfy connectivity

#### Scenario: Reviewed-none is not applicable to a connected page
- **WHEN** a caller submits a reviewed-none decision for a page that satisfies the disposition through connectivity
- **THEN** the write reports the decision as not applicable and names the connectivity satisfaction
- **AND** the page commits without a review artifact

#### Scenario: Generic links do not satisfy typed relation review
- **WHEN** a page contains only inline wikilinks, `links_to`, or source/evidence provenance
- **THEN** those connections do not qualify as typed epistemic edges
- **AND** an eligible resolved outbound connection may satisfy the separate connectivity lane, while frontmatter provenance alone does not

#### Scenario: Inactive or excluded-family relation does not qualify
- **WHEN** a page has only an inactive relation or a relation in the `citation`, `evidence`, or `link` family
- **THEN** the edge remains visible in its own semantics but does not qualify as a typed edge
- **AND** an active scope-valid excluded-family relation to a connectable target may satisfy connectivity, while an inactive relation satisfies neither lane

#### Scenario: Supersession frontmatter qualifies narrowly
- **WHEN** a registered active scope-valid supersession edge is authored through the supported frontmatter lifecycle field
- **THEN** it qualifies without making other frontmatter links relation-review eligible

#### Scenario: Inactive body wikilink target does not connect
- **WHEN** a compiled page's only outbound body wikilink resolves to an inactive page
- **THEN** that link does not satisfy connectivity or qualify as a typed edge

#### Scenario: A registry-marked generic relation is connectivity only
- **WHEN** a page authors an active scope-valid relation marked generic in the registry to a connectable governed page
- **THEN** that relation does not qualify as a typed edge, regardless of its registry family
- **AND** it remains writable connectivity with non-blocking guidance

#### Scenario: A specific extension remains typed
- **WHEN** an authored relation resolves to an active non-generic extension and meets the other typed-edge conditions
- **THEN** it qualifies as a typed edge even when its registry parent is generic

### Requirement: Reviewed-None Creation Is Logically Atomic, Crash-Recoverable, And Content-Bound
Creation-capable governed writers SHALL support `validate_only=true`. Validation SHALL preassign a candidate page UUID and return `draft_id`, `draft_hash`, a bounded opaque `draft_token` for server-derived render inputs and ordered project auto-registration intent, deterministic relation findings/candidates, and no filesystem mutation. A reviewed-none commit SHALL repeat identical proposed content and supply the unused `draft_id`, echoed `draft_token`, `relation_disposition="reviewed_none"`, matching `relation_review_hash`, and a non-empty `relation_review_reason`. A creation that names no draft field (`draft_id`, `draft_hash` or `draft_token`) SHALL commit reviewed-none in one call with only `relation_disposition="reviewed_none"` and a non-empty `relation_review_reason`; the writer SHALL then derive the hash itself from the candidate identity and the normalized final content, so the receipt binds the committed content. The writer SHALL recalculate the hash over candidate identity plus normalized final content/destination and reject mismatch/reuse. A bounded portable creation receipt SHALL be prepared for every full active-compiled commit before its deterministic auxiliary writes, and the primary Markdown SHALL be replaced last as the logical commit marker, so a visible page never lacks required review state. Reviewed-none/bootstrap receipts SHALL also carry their disposition; a qualifying-relation receipt SHALL be transaction/recovery state only. Portable state SHALL live in governed Markdown/frontmatter or a vault-managed governed artifact in the same creation batch, not solely in a derived database, and SHALL survive index rebuild or vault transfer. Exact recovery SHALL reconstruct the same ordered auxiliary target/content digest, include already-applied expected targets unchanged, bind every existing leaf to its exact safely read content, and reject unrelated drift rather than overwrite it. An exact existing qualifying primary without an artifact SHALL remain an idempotent already-committed case after upgrade, while a page-less no-artifact attempt has no retroactive prepared-recovery guarantee.

The reviewed-none creation batch SHALL use the shared descriptor-owned atomic
batch primitive and SHALL preserve auxiliary-first, primary-last logical commit
ordering. Abrupt process death MAY leave page-less prepared state or private
transaction-workspace residue. Workspace residue SHALL never be recovery
authority. An exact unchanged retry SHALL reconstruct fresh stages from the
current guarded inputs and SHALL finish the same prepared commit when every
bound primary and auxiliary target is in its exact expected pre-write or
already-applied state. Any mismatch SHALL fail closed and remain reserved for
explicit audit/cleanup. This protocol SHALL NOT claim cross-file all-or-none
power-loss atomicity. A qualifying typed relation or genuinely empty-corpus
bootstrap SHALL not require a reviewed-none decision from the caller.

Review fields SHALL be checked against whether the page needs a relation review, with one check shared by validation and commit. When the page needs no review (a qualifying relation, connectivity signal or empty-corpus bootstrap), a commit that carries `relation_disposition`, `relation_review_hash` or `relation_review_reason` SHALL drop them, commit without a review artifact, and return one warning that names the review fields it dropped. When the page needs a review and the commit names a validated draft, a commit that omits any of the three fields SHALL be refused with `INVALID_RELATION_REVIEW`, whose `missing` list names each absent field and whose message says to copy `relation_review_hash` from the `validate_only` result or to send the other two fields without the draft fields to commit in one call. A commit that names no draft field SHALL NOT be refused for an absent hash. Validation SHALL NOT compare the hash. When the caller names a draft and supplied any review field, validation SHALL report the fields a commit would find missing or ignored under `relation_review_fields`.

#### Scenario: Second disconnected page can be created after review
- **WHEN** validation for a second page returns no qualifying candidate and the commit repeats the draft with matching reviewed-none fields
- **THEN** portable review state is prepared before the page commit marker, and the returned visible page has a current disposition

#### Scenario: Interrupted prepared commit resumes exactly
- **WHEN** a process stops after portable review state or auxiliaries are replaced but before the primary page commit marker, and the caller retries the exact unchanged draft, reviewed disposition/reason, and auxiliary content
- **THEN** the writer reuses the matching prepared state, completes the primary page last, and rejects any non-exact draft from reusing that identity

#### Scenario: Changed draft cannot reuse review
- **WHEN** content, identity, or relation-relevant metadata differs after validation
- **THEN** the reviewed-none commit fails before mutation and requires a fresh validation hash

#### Scenario: Validation does not reserve a conflicting file
- **WHEN** `validate_only=true` returns a draft identity
- **THEN** no Markdown is created, and commit rejects the draft if that identity or destination became occupied

#### Scenario: Review disposition survives derived-state loss
- **WHEN** all rebuildable indexes/databases are removed and the vault is rebuilt
- **THEN** a still-current portable reviewed-none disposition is recovered from governed vault content

#### Scenario: A page that needs no review ignores the review fields
- **WHEN** a commit for a page with a qualifying connectivity signal carries `relation_disposition="reviewed_none"` and a reason but no hash
- **THEN** the page commits with a receipt of kind `qualifying` and no review artifact
- **AND** the result carries one warning that the review fields were ignored

#### Scenario: A retry that re-sends dropped review fields reports the page as already committed
- **WHEN** a caller repeats the commit of a page that needed no review, with the same review fields it sent the first time
- **THEN** the write is refused with `DRAFT_ALREADY_COMMITTED`, as a repeat without the fields is

#### Scenario: A fresh creation commits reviewed-none in one call
- **WHEN** a creation for a page that needs review names no draft field and carries `relation_disposition="reviewed_none"` and a reason
- **THEN** the page commits with a reviewed-none receipt whose fingerprint equals the committed content
- **AND** the caller sent no hash and made no validate call

#### Scenario: A page that needs review names the missing hash
- **WHEN** a commit for a page that needs review names a validated draft and carries the disposition and reason but no `relation_review_hash`
- **THEN** the write is refused with `INVALID_RELATION_REVIEW` and `missing=["relation_review_hash"]`
- **AND** the message tells the caller to copy the hash from the `validate_only` result or to send the first two fields without the draft fields

#### Scenario: Validation names fields a commit would find missing or ignored
- **WHEN** `validate_only` names a draft and carries a review field for a page that needs review but omits the hash
- **THEN** `relation_review_fields.missing` lists `relation_review_hash`
- **AND** the same call for a page that needs no review lists the supplied fields under `relation_review_fields.ignored`

### Requirement: Existing-Page Review Decisions And Transition Recovery Are Separate
For an existing page with a unique valid stable memory UUID, a reviewed-none lifecycle decision SHALL be an immutable portable artifact at `Knowledge Base/_Schema/relation-reviews/lifecycle/<uuid>/<after-fingerprint>.json`. Its canonical bounded schema SHALL contain the schema/kind, normalized UUID, exact resulting review-content fingerprint, bounded non-empty reason, and a decision hash derived only from those stable decision fields. Its identity SHALL NOT depend on operation, prior bytes/path, transition token, log/index plan, or auxiliary digest. Transition-specific crash recovery SHALL use one separately guarded replaceable slot at `Knowledge Base/_Schema/relation-reviews/lifecycle/<uuid>/prepared.json`. Its canonical bounded schema SHALL contain the schema/kind, canonical transition UUID, operation, normalized page UUID, exact before/after paths and source hashes, after fingerprint, decision reference plus exact decision-bytes hash or JSON null when no decision is needed, transition-token hash, ordered auxiliary digest, and optional `carried_from` decision reference. The prepared slot SHALL NOT itself satisfy relation review. An exact unique UUID plus current-fingerprint decision match SHALL satisfy a reviewed-none disposition. An edit that carries a standing decision forward SHALL record in `carried_from` the lifecycle decision reference it carried and SHALL write no new decision. The loader SHALL read `carried_from` from a committed slot, that is, when the page's path, source hash and fingerprint equal the slot's after values, and from a pending slot when the page's path and source hash equal the slot's before values, because the pending slot copied the pointer from the decision it carried. A pending slot's own decision SHALL NOT review the before state. A slot or decision that cannot be read, a reference to anything other than a lifecycle decision of the same UUID, and a denied reference SHALL read as no decision for that page and SHALL NOT stop the evaluation of other pages. A reviewed-none request on an edit, validated or not, SHALL record a decision only when the edit needs one, so a pre-emptive request does not add decision files toward the bound. An edit that names a transition token and sends reviewed-none without `relation_review_hash` SHALL be refused with `INVALID_RELATION_REVIEW`. A `generic` disposition keeps the validated route: it SHALL be refused with `INVALID_RELATION_REVIEW` and `missing=["relation_review_hash"]` when it lacks the validated hash, and with `LIFECYCLE_TRANSITION_REVIEW_MISMATCH` only when the hash it sends does not match.

Lifecycle decisions and prepared slots SHALL NOT be created or loaded for path-only legacy identities. A legacy page requiring reviewed-none SHALL first acquire a stable UUID through the explicit backfill workflow. Decision lookup SHALL be direct by UUID/current fingerprint, alias/symlink/reparse safe, and bounded to 256 conforming decision files per UUID; overflow or unsafe/malformed exact lifecycle state SHALL fail closed with governed cleanup guidance. The immutable 6D v1/v2 receipt at `relation-reviews/<uuid>.json` SHALL NOT be overwritten, deleted, reinterpreted, or migrated. Any conforming lifecycle artifact SHALL reserve its UUID against later creation even when the page is absent and no creation receipt exists; unsafe or malformed lifecycle state at that exact UUID SHALL likewise fail creation closed as identity-in-use. Trash/delete SHALL preserve that history. Recovery MAY replace the last committed prepared slot only when the trash sidecar, original path, UUID, and trashed bytes exactly prove its committed after state and no live page owns the UUID. Reconcile SHALL preserve and report that `trashed_committed` proof and SHALL remove only a neither-side prepared slot with no exact trash proof.

Lifecycle decisions and prepared slots SHALL inherit operational admission only from their exact admitted primary UUID and transition. Both current and proposed primary images SHALL remain admitted. Auxiliary path and reference restrictions and access exclusions SHALL still apply. Publication SHALL bind exact generated lifecycle paths, bytes, hashes, and guards through the existing sealed auxiliary manifest. Creation receipts, operation logs, history, indexes, and backreferences SHALL retain independent admission.

The writer SHALL establish admission before inspecting required prepared slots, requested decisions, or decisions referenced by admitted prepared slots. Denied absent and present targets SHALL produce the same content-free refusal. Directory enumeration SHALL filter denied names before leaf inspection, alias checks, encoding, residue classification, and capacity counts. Forward rechecks SHALL preserve the captured admitted census and detect source, permission, directory, or leaf changes.

For a restricted caller, the 256-decision bound SHALL count only admitted lifecycle decisions. Denied siblings SHALL affect neither advice nor transition outcomes, including when physical directories exceed the admitted decision or residue bounds. Unrestricted callers SHALL retain the 256-decision bound and all unsafe-state checks. Publication entry, publication completion, and committed replay SHALL recheck primary admission and source bindings.

After an ordinary publication failure, rollback SHALL restore only exact unchanged installed artifacts through retained full ancestry and directory identities. Rollback SHALL NOT invoke live caller admission or enumerate unrelated lifecycle siblings. Each inverse attempt and retry SHALL prove installed content at the maintained operation boundary. An intervening owned edit or namespace substitution SHALL remain untouched and produce an explicit incomplete rollback. An unrelated new sibling SHALL remain untouched without preventing restoration of unchanged owned artifacts.

#### Scenario: Restricted stable-identity edit retains operational state
- **WHEN** a restricted caller edits an admitted compiled page with a stable UUID
- **THEN** exact bound lifecycle artifacts are admitted without granting the enclosing schema directory or unrelated auxiliary writes

#### Scenario: Denied lifecycle siblings do not change an admitted transition
- **WHEN** two otherwise identical edits differ only in denied lifecycle siblings, including siblings beyond physical history or residue bounds
- **THEN** both edits have the same advice and transition outcome while admitted history still obeys its bound

#### Scenario: Required lifecycle denial precedes existence
- **WHEN** a required prepared slot or decision is denied, whether absent or present
- **THEN** the writer returns the same content-free refusal before inspecting that target

#### Scenario: Revocation after publication restores owned lifecycle state
- **WHEN** permission is revoked after the primary replacement and completion validation refuses the transition
- **THEN** rollback restores unchanged primary bytes and removes newly installed lifecycle artifacts without inspecting revoked sibling state

#### Scenario: Concurrent owned edit survives rollback
- **WHEN** an outside writer changes installed bytes or ancestry before an inverse operation or retry
- **THEN** rollback preserves that state and reports an explicit incomplete outcome

#### Scenario: Inactive page activates with reviewed-none
- **WHEN** an inactive stable-ID page is validated and committed into an active state with an exact reviewed-none decision
- **THEN** the immutable decision and prepared transition are installed before the active primary becomes visible

#### Scenario: Pending transition does not review the before state
- **WHEN** an `A -> B` prepared transition exists but the current primary still exactly matches `A`
- **THEN** the decision for `B` does not satisfy `A`, and a different transition is refused until the exact pending transition is retried or reconciled
- **AND** a decision that was in force at `A` through the slot's `carried_from` stays in force at `A`

#### Scenario: Interrupted existing-page transition resumes exactly
- **WHEN** a process stops after preparing the decision, transition slot, or deterministic auxiliaries and an exact retry repeats the same bound transition
- **THEN** the writer adopts the prepared slot, commits the resulting primary last, and rejects token, reason, content, path, or auxiliary drift
- **AND** when the page held a carried decision before the interrupted edit, the exact retry commits without asking for relation review

#### Scenario: Committed replay is mutation-free
- **WHEN** an exact retry finds that the current primary already matches the prepared transition's after state
- **THEN** it returns the existing committed outcome without writing the primary, decision, slot, log, or indexes again

#### Scenario: Prior reviewed fingerprint is reusable across transitions
- **WHEN** a page follows `A -> B`, later changes to `C`, and then returns exactly to reviewed fingerprint `B`
- **THEN** the immutable `B` decision is reused while a distinct `C -> B` prepared transition binds the new before state and auxiliaries

#### Scenario: Changed content cannot reuse lifecycle review
- **WHEN** the proposed resulting fingerprint differs from every exact immutable decision for the page UUID and the edit does not carry a standing decision
- **THEN** a prior reviewed-none decision cannot satisfy it and fresh review is required before mutation
- **AND** an edit that carries a standing decision writes no new decision and needs no fresh review

#### Scenario: A decision made through an edit stays in force on later edits
- **WHEN** an edit records a reviewed-none decision and two later edits send no review field
- **THEN** both later edits commit in one call and the page holds one decision file
- **AND** the committed slot's `carried_from` names that decision

#### Scenario: A pre-emptive reviewed-none request records no decision
- **WHEN** an edit sends reviewed-none and a reason for a page whose standing decision already covers the edit
- **THEN** the edit commits and writes no decision file

#### Scenario: A validated reviewed-none request on a carried page records no decision
- **WHEN** an edit validates, then commits with its `transition_token`, `relation_review_hash`, reviewed-none and a reason, for a page whose standing decision already covers the edit
- **THEN** the edit commits and writes no decision file, however many times it repeats
- **AND** an exact replay of that commit is mutation-free

#### Scenario: An unreadable prepared slot does not stop an audit
- **WHEN** one page's `prepared.json` cannot be read
- **THEN** that page reads as having no carried decision and the audit still evaluates every other page

#### Scenario: Stale prepared slot is cleaned only without recovery proof
- **WHEN** current live bytes match neither side of a prepared slot
- **THEN** dry-run reconcile reports the state, and write-mode reconcile may guarded-remove it only when no exact trash-sidecar/UUID/bytes proof establishes `trashed_committed`

#### Scenario: Deleted identity cannot be stolen
- **WHEN** a stable-ID page is trashed or deleted while conforming lifecycle state remains and a new creation attempts to reuse that UUID
- **THEN** creation fails as identity-in-use, while exact guarded recovery of the original trashed bytes remains allowed
