## MODIFIED Requirements

### Requirement: Reviewed-None Creation Is Logically Atomic, Crash-Recoverable, And Content-Bound
Creation-capable governed writers SHALL support `validate_only=true`. Validation SHALL preassign a candidate page UUID and return `draft_id`, `draft_hash`, a bounded opaque `draft_token` for server-derived render inputs and ordered project auto-registration intent, deterministic relation findings/candidates, and no filesystem mutation. A reviewed-none commit SHALL repeat identical proposed content and supply the unused `draft_id`, echoed `draft_token`, `relation_disposition="reviewed_none"`, matching `relation_review_hash`, and a non-empty `relation_review_reason`. The writer SHALL recalculate the hash over candidate identity plus normalized final content/destination and reject mismatch/reuse. A bounded portable creation receipt SHALL be prepared for every full active-compiled commit before its deterministic auxiliary writes, and the primary Markdown SHALL be replaced last as the logical commit marker, so a visible page never lacks required review state. Reviewed-none/bootstrap receipts SHALL also carry their disposition; a qualifying-relation receipt SHALL be transaction/recovery state only. Portable state SHALL live in governed Markdown/frontmatter or a vault-managed governed artifact in the same creation batch, not solely in a derived database, and SHALL survive index rebuild or vault transfer. Exact recovery SHALL reconstruct the same ordered auxiliary target/content digest, include already-applied expected targets unchanged, bind every existing leaf to its exact safely read content, and reject unrelated drift rather than overwrite it. An exact existing qualifying primary without an artifact SHALL remain an idempotent already-committed case after upgrade, while a page-less no-artifact attempt has no retroactive prepared-recovery guarantee.

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

Review fields SHALL be checked against whether the page needs a relation review, with one check shared by validation and commit. When the page needs no review (a qualifying relation, connectivity signal or empty-corpus bootstrap), a commit that carries `relation_disposition`, `relation_review_hash` or `relation_review_reason` SHALL drop them, commit without a review artifact, and return one warning: "This page needs no relation review; relation_disposition and relation_review_reason were ignored." When the page needs a review, a commit that omits any of the three fields SHALL be refused with `INVALID_RELATION_REVIEW`, whose `missing` list names each absent field and whose message says to copy `relation_review_hash` from the `validate_only` result. Validation SHALL NOT compare the hash. When the caller supplied any review field, validation SHALL report the fields a commit would find missing or ignored under `relation_review_fields`.

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

#### Scenario: A page that needs review names the missing hash
- **WHEN** a commit for a page that needs review carries the disposition and reason but no `relation_review_hash`
- **THEN** the write is refused with `INVALID_RELATION_REVIEW` and `missing=["relation_review_hash"]`
- **AND** the message tells the caller to copy the hash from the `validate_only` result

#### Scenario: Validation names fields a commit would find missing or ignored
- **WHEN** `validate_only` carries a review field for a page that needs review but omits the hash
- **THEN** `relation_review_fields.missing` lists `relation_review_hash`
- **AND** the same call for a page that needs no review lists the supplied fields under `relation_review_fields.ignored`
