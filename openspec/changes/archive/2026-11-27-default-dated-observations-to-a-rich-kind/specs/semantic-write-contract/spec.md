## MODIFIED Requirements

### Requirement: Rich-Kind Refusals Name The Governed Kinds

The core kinds pack SHALL include a `correction` kind, a unit that states the current view and
replaces an earlier unit. `COMPACT_RELATIONS_REQUIRE_RICH_KIND` and
`COMPACT_METADATA_REQUIRES_RICH_KIND` SHALL list the kind keys that the page's selected registry
admits, in their remediation. When the caller's `category` is itself an admitted kind key, the
remediation SHALL say to pass it as `kind`. The list SHALL come from the registry, so a vault kind
appears and no kind list lives in code.

#### Scenario: Links on a compact unit name the valid kinds
- **WHEN** `add_observation` sends links with no kind
- **THEN** it refuses with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, and the remediation lists the
  admitted kinds, `correction` among them

#### Scenario: as_of on a compact unit names the valid kinds
- **WHEN** `add_observation` sends `as_of` with `kind="observation"`, the compact kind
- **THEN** it refuses with `COMPACT_METADATA_REQUIRES_RICH_KIND`, and the remediation lists the
  admitted kinds

#### Scenario: A kind passed as a category is named
- **WHEN** `add_observation` sends `category="correction"`, links and no kind
- **THEN** the remediation says to pass `kind="correction"`

#### Scenario: A vault kind is listed
- **WHEN** the vault overlay adds a kind
- **THEN** the remediation lists that kind too

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

Review fields SHALL be checked against whether the page needs a relation review, with one check shared by validation and commit. When the page needs no review (a qualifying relation, connectivity signal or empty-corpus bootstrap), a commit that carries `relation_disposition`, `relation_review_hash` or `relation_review_reason` SHALL drop them, commit without a review artifact, and return one warning. A grouped tool's warning SHALL name the review fields it dropped. A typed tool's warning SHALL name the unlinked reason, the one review field it takes. When the page needs a review and the commit names a validated draft, a commit that omits any of the three fields SHALL be refused with `INVALID_RELATION_REVIEW`, whose `missing` list names each absent field and whose message says to copy `relation_review_hash` from the `validate_only` result or to send the other two fields without the draft fields to commit in one call. A commit that names no draft field SHALL NOT be refused for an absent hash. Validation SHALL NOT compare the hash. When the caller names a draft and supplied any review field, validation SHALL report the fields a commit would find missing or ignored under `relation_review_fields`.

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

#### Scenario: A typed tool's warning names the unlinked reason
- **WHEN** a typed tool commits to a page that needs no review with `unlinked_reason`
- **THEN** the result carries one warning that says no link review applies, so the unlinked reason was not recorded
- **AND** the warning does not name `relation_disposition` or `relation_review_reason`, which the caller never sent

## ADDED Requirements

### Requirement: A Dated Observation With No Kind Keeps Its Dates On A Default Kind

When `add_observation`, or `edit_note(action="edit_observation")` on a compact unit, carries `as_of`
or `until` and no `kind`, the write SHALL commit a rich unit of the kind that carries the
`dated_default` trait, and the reply SHALL say so plainly. `verdict` or `check_by`, and an explicit
compact `kind`, SHALL keep the refusal. No refusal SHALL offer to omit `as_of` or `until`.

#### Scenario: Dates alone land in a dated block and the reply says so
- **WHEN** `add_observation` sends `as_of` and `until`, a category and a text, and no `kind`
- **THEN** the page gains one rich `record` unit with the category, the text and both dates, at the end of the page body
- **AND** the reply says "Saved with its dates."

#### Scenario: Dating a compact observation moves it to a dated block
- **WHEN** `edit_note(action="edit_observation")` sends `as_of` and no `kind` for a compact unit
- **THEN** the compact line leaves the observation list and a `record` block with the same anchor goes at the end of the page body
- **AND** the reply returns the unit id, whose version suffix differs from the id sent

#### Scenario: Only the shipped pack sets the trait
- **WHEN** a vault kind declares `dated_default`
- **THEN** a dated observation still lands on `record`
- **AND** the vault's schema diagnostics carry a warning that the trait was ignored

#### Scenario: Verdict and check_by keep the refusal and keep the date in view
- **WHEN** `add_observation` sends `verdict` or `check_by` with `as_of` and no `kind`
- **THEN** it refuses with `COMPACT_METADATA_REQUIRES_RICH_KIND` and the page is unchanged
- **AND** the remediation offers to pass a kind that takes the fields, or to omit `verdict` and `check_by` so the date lands in a dated block
- **AND** it does not offer to omit `as_of`

#### Scenario: The busy-write intake leaves a dated call to the page
- **WHEN** the service is busy and `add_observation` sends `as_of` and no `kind`
- **THEN** the intake queues the save and the replay writes it in a dated block
- **AND** the applied save reads "Saved with its date."
- **AND** a call that sends `verdict` or `check_by` is refused at once and never queued

### Requirement: An Existing Page That Takes No Link Review Ignores The Review Fields

A write to an existing page that takes no relation review, such as an entity page, SHALL accept
`relation_disposition`, `relation_review_hash`, `relation_review_reason` and `unlinked_reason` as a
no-op. It SHALL record no decision. A preview SHALL list the fields the caller sent under
`relation_review_fields`, and a commit SHALL return one warning. A page that needs a review SHALL
keep its checks.

#### Scenario: An unlinked reason on an entity page is ignored and the reply says so
- **WHEN** `add_observation` or `edit_note` sends `unlinked_reason` for an entity page
- **THEN** the write commits and no review decision is recorded
- **AND** the reply says that no link review applies to the page, so the unlinked reason was not
  recorded

#### Scenario: A preview names the ignored review fields
- **WHEN** a preview of an observation on an entity page carries `relation_disposition` and
  `relation_review_reason`
- **THEN** the preview lists both under `relation_review_fields.ignored`
- **AND** a typed tool's preview that sent `unlinked_reason` lists only `unlinked_reason`

#### Scenario: A page that needs review keeps its checks
- **WHEN** a write to an active compiled page that needs a review carries `reviewed_none` with a
  hash that does not match the validated transition
- **THEN** it refuses with `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`
