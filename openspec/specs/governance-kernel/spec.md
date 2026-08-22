# governance-kernel Specification

## Purpose
TBD - created by archiving change add-governance-kernel. Update Purpose after archive.
## Requirements
### Requirement: Canonical policy source in the vault

Governance policy SHALL be authored as strict YAML documents under
`Knowledge Base/_Governance/` — `scopes/*.yaml`, `rules/*.yaml`, `grants/*.yaml` —
one document per file, each carrying an immutable ULID `id` and
`governance_version: 1`. The policy source SHALL be the authority; any compiled
representation SHALL be derived and rebuildable from it. `_Governance/` SHALL be
excluded from the content index so policy files are never returned as knowledge.

#### Scenario: Policy files are not indexed as content

- **WHEN** a `find`/`ask_memory` query runs on a vault with `_Governance/` policy
  files
- **THEN** no policy file appears as a hit

#### Scenario: Unknown version fails closed

- **WHEN** a policy file declares a `governance_version` the kernel does not
  recognize, or carries an unknown field
- **THEN** the compile fails closed with a clear finding and the last good policy
  remains in effect

### Requirement: Fingerprinted compile with empty-policy fast path

The kernel SHALL load policy through a content fingerprint that changes whenever
any policy file's bytes change, even under a timestamp-preserving replacement.
When no `_Governance/` directory exists, the kernel SHALL yield a cached OPEN
(empty) policy with a stable "missing" fingerprint and SHALL short-circuit all
downstream governance work. The compile SHALL refuse when a `(conflicted copy)`
sibling policy file is present, keeping the last good compiled snapshot and
surfacing the conflict.

#### Scenario: Empty policy short-circuits

- **WHEN** the kernel loads a vault with no `_Governance/` directory
- **THEN** it returns the OPEN singleton without opening the governance sidecar,
  and any decision resolves to full disclosure (L6)

#### Scenario: Timestamp-preserving edit still invalidates

- **WHEN** a policy file's content changes but its mtime is preserved
- **THEN** the fingerprint changes and the recompiled policy takes effect

#### Scenario: Conflicted copy refuses compile

- **WHEN** a `(conflicted copy)` policy sibling is present
- **THEN** the compile refuses, the prior compiled snapshot remains in effect,
  and the conflict is reported for resolution

### Requirement: Query-time scope membership

Scope membership SHALL be evaluated at query time against an already-parsed page
using the scope's selectors (path globs, projects, tags, types, detector classes,
explicit refs) minus its `exclude` selectors, memoized per
`(policy_fingerprint, path, mtime)`. Membership SHALL NOT be materialized as an
index-time table and SHALL NOT add a component to the deletion/upsert fan-out. A
policy change SHALL invalidate membership by fingerprint mismatch.

#### Scenario: Selector kinds resolve membership

- **WHEN** a page matches a scope by any selector kind and is not caught by an
  `exclude` selector
- **THEN** the page is a member of that scope

#### Scenario: Policy change invalidates the memo

- **WHEN** the policy fingerprint changes
- **THEN** previously memoized membership is recomputed against the new policy

### Requirement: Pure order-free disclosure evaluator

The kernel SHALL expose a pure function mapping `(item, audience, purpose,
active grants)` to a disclosure ceiling, computed as
`min(org_cap, max(grants_and_exceptions, min(standing_rules)))` with a default of
full disclosure when no rule matches. The function SHALL be free of IO beyond its
compiled-policy and grant inputs, SHALL be order-independent (no rule priority),
and SHALL treat an undeclared purpose deterministically: a purpose-conditioned
allowance does not apply, while an "outside purpose" restriction does.

#### Scenario: Most restrictive standing rule wins, a grant lifts it, org caps all

- **WHEN** multiple standing rules, a grant, and an org rule all match an item
- **THEN** the ceiling is the org cap applied over the grant applied over the
  minimum standing rule, independent of the order the rules were authored

#### Scenario: Default is full disclosure

- **WHEN** no rule matches an item for a given audience
- **THEN** the ceiling is full (L6)

#### Scenario: Undeclared purpose is deterministic

- **WHEN** a rule allows an item only for purpose P and no purpose is declared
- **THEN** the allowance does not fire; and an "outside P" restriction does fire

### Requirement: Collection and record authorization precedes reduction
Every Records discovery, read, structured query, pagination total, aggregate, profile, rendered view, export-shaped response, template return, graph/reference expansion, and mutation receipt SHALL pass the existing governance and release boundary at the correct source or item granularity. Structured Records operations SHALL require `LEVEL_FULL` (L6) for every artifact whose complete values, hashes, or contents contribute to the response; the existing L5 excerpt floor SHALL NOT authorize full rows or reductions. A below-L6 collection or item SHALL behave as missing to the structured operation. Authorization SHALL occur before any public count, cap, sort, parse, ambiguity decision, snapshot, grouping, latest selection, min/max, sum/average, distinct set, profile, pagination, or observed-state reduction is computed.

#### Scenario: Withheld items do not affect aggregate
- **WHEN** a file-per-item collection contains both released and withheld record files
- **THEN** filtering by release decision occurs before totals or aggregates and no withheld value affects the result

#### Scenario: Withheld collection is indistinguishable from absent
- **WHEN** the collection manifest or sole canonical log/dataset is below L6 for a structured Records operation
- **THEN** Records reads and queries use the same public missing shape as a nonexistent collection

#### Scenario: Excerpt permission cannot authorize full Records values
- **WHEN** a caller has L5 but not L6 permission for a manifest, source, item, or template
- **THEN** `record_memory` does not return its complete rows, hashes, contents, aggregates, or existence while ordinary recall may still apply its existing excerpt projection independently

#### Scenario: UUID discovery hides withheld duplicates
- **WHEN** UUID resolution encounters both releasable and withheld candidate manifest paths
- **THEN** each path is authorized at L6 before identity-bearing content is parsed, internal raw-walk safety caps remain content-free, public candidate caps and ambiguity are computed only among authorized candidates, and withheld candidates remain indistinguishable from absence

#### Scenario: Hidden link targets cannot create public ambiguity
- **WHEN** an authorized Record link has a bare wikilink title or stable memory identifier that collides with a withheld page
- **THEN** candidate paths are authorized before their titles or identifiers are parsed, and adding or removing the withheld collision cannot change the projected public Record response

#### Scenario: Aggregate cannot reveal concealed rows
- **WHEN** rows that would be withheld contain an extreme value or unique category
- **THEN** count, min/max, latest, distinct, profile, progress, and pagination metadata reveal no contribution from those rows

### Requirement: Governance granularity follows canonical representation
Canonical source and template paths SHALL be governed vault-relative paths resolved without symlink escape. For one-file-per-item storage, the adapter SHALL receive an immediate path-authorization callback and SHALL authorize each candidate at L6 before it can affect public file/byte counts, caps, ordering, parsing, diagnostics, identity ambiguity, source versions, continuation snapshots, or reductions. Internal raw-walk bounds remain fail-closed and reveal no count. Authorized-only snapshots SHALL mean a hidden-only edit does not invalidate a caller's continuation. For a chronological log or dataset, the canonical file SHALL be the first-delivery governance boundary and all contained items SHALL share its release classification. Collections requiring mixed sensitivity SHALL use separately governed item files or collections until explicit row-level policy exists.

#### Scenario: Log is authorized as one canonical artifact
- **WHEN** a log-backed collection is queried
- **THEN** Exomem authorizes the manifest and log path before parsing any block and does not claim unsupported per-row secrecy inside that file

#### Scenario: Item files can have mixed release decisions
- **WHEN** a file-per-item collection contains paths in different governed scopes
- **THEN** only authorized item files reach filter, sort, pagination, aggregate, or view computation

#### Scenario: Hidden malformed item is not parsed
- **WHEN** a below-L6 item is malformed or would exceed a public released-item cap
- **THEN** the structured result is identical to that item being absent and the hidden file cannot cause a parse error, ambiguity, count, continuation change, or public-cap exhaustion

#### Scenario: Mixed-release collection mutation refuses
- **WHEN** a caller can read only a subset of a file-per-item collection and requests append or update
- **THEN** mutation refuses without publishing because an authorized-subset snapshot cannot safely substitute for the full canonical container CAS hash

### Requirement: Records egress and receipts remain content-safe
Records responses SHALL use default-deny typed envelope projectors per response shape rather than allowlisting an arbitrary nested `rows` value. Ordinary schema values require L6; schema-declared links, Planning descriptors, template targets, provenance, paths, identities, hashes, history, conflicts, continuations, counts, and aggregates SHALL be recursively projected before they can reach reduction or rendering. Errors and receipts SHALL not echo withheld values, template contents, plan titles, record bodies, or sensitive identifiers beyond their authorized projection.

Mutation authorization and disclosure SHALL run through a precommit hook inside the guarded Records mutation, after the final canonical re-read and target resolution but before `batch_atomic_write`. Failure SHALL leave canonical files and activity history untouched. A successful authorization receipt records the disclosure decision, not a false claim that later publication committed; no fallible postcommit disclosure step may turn a committed mutation into an apparent refusal. Governance receipts, activity events, operational journals, and terminal mutation receipts remain distinct.

#### Scenario: Stale refusal leaks no current item content
- **WHEN** an unauthorized or stale update is refused
- **THEN** the response provides a bounded remediation and safe hashes/identifiers only at the caller’s release level

#### Scenario: Mutation receipt names authorized affected paths
- **WHEN** a Records mutation commits
- **THEN** the terminal receipt includes only the authorized collection/item/path metadata and records disclosure outcomes through the existing receipt system

#### Scenario: Disclosure failure precedes publication
- **WHEN** the precommit governance or receipt hook refuses or fails
- **THEN** no canonical item, manifest audit head, or activity event is published

#### Scenario: Publication failure does not forge governance commit evidence
- **WHEN** precommit disclosure succeeds but guarded publication later rolls back
- **THEN** the governance receipt records only the authorization attempt and does not claim that the Record mutation committed

### Requirement: Guarded Fallback Never Serves An Open Policy

The last-good policy cache exists so a transient authoring guard does not drop a governed
vault to the fail-closed floor. It MUST NOT be able to reopen one. The cache SHALL only
retain a compile that produced governance — a policy whose fingerprint is neither the
empty sentinel nor the blocked sentinel. When a load is taken behind a pending or changed
authoring guard and no such last-good compile exists for that vault, the loader SHALL
return the blocked fail-closed policy rather than an empty-looking open one.

The empty fast path is unaffected for a vault that has no governance tree at all: that
load never consults the guarded fallback and MUST keep its existing byte-identical
behaviour and latency budget.

#### Scenario: a pending mutation on a previously ungoverned vault fails closed

- **WHEN** a vault's policy has previously loaded as the empty open singleton, an authoring
  guard is subsequently pending, and a content read arrives
- **THEN** the loader returns a blocked policy carrying the guard finding
- **AND** the returned policy is not empty, so no caller takes the open fast path
- **AND** every content-returning surface withholds rather than releasing at full level

#### Scenario: the cache refuses to retain an open compile

- **WHEN** a load resolves to the empty open singleton with no error findings
- **THEN** the last-good cache for that vault is left unchanged
- **AND** a later guarded load for the same vault does not return an empty policy

#### Scenario: a governed last-good compile is still served through the guard

- **WHEN** a vault has previously loaded a successfully compiled policy and an authoring
  guard is then pending
- **THEN** the loader returns that compiled policy with the guard finding appended
- **AND** its scopes, rules and grants are unchanged

#### Scenario: two processes agree during activation

- **WHEN** a policy mutation is pending and the same content read is issued from a process
  that has served the vault since before governance existed and from a freshly started
  process
- **THEN** both return a policy that is neither empty nor open
- **AND** the disclosure decision for a given item and audience is identical in both

### Requirement: Sync Conflict Copies Refuse Policy Compile

Policy authority is the document set on disk, so a file-synchronisation conflict copy MUST
NOT be able to act as a policy document. Conflict-copy detection SHALL recognise the naming
conventions of the synchronisation tools the vault supports, including both the parenthesised
Obsidian marker and the hyphenated sync-conflict marker, and SHALL apply that detection
consistently to policy document discovery, the governance file walk, and the receipt tree.

A recognised conflict copy SHALL refuse compile and preserve the last good governed policy,
exactly as an Obsidian conflict copy does today. It MUST NOT be admitted as a second,
differently-named policy document, and it MUST NOT be able to reintroduce a deleted document.

#### Scenario: a sync conflict copy of a deleted grant does not restore access

- **WHEN** a grant document is deleted to revoke access and the synchronisation tool later
  lands a conflict copy of that grant under a sync-conflict filename
- **THEN** the compile is refused with a conflict finding
- **AND** the revoked grant does not take effect
- **AND** the previously compiled governed policy continues to be served

#### Scenario: a sync conflict copy alongside its original is a conflict, not a duplicate

- **WHEN** a policy document and a sync-conflict copy of it are both present
- **THEN** the load is refused as a conflict before duplicate-identifier compilation is
  attempted
- **AND** the refusal does not fall back to a policy weaker than the last good governed one

#### Scenario: a sync conflict copy in the receipt tree does not fork the chain

- **WHEN** a sync-conflict copy appears inside the per-machine receipt tree
- **THEN** the conflict is detected and receipt append fails closed
- **AND** the hash chain is not extended from the conflicted record

#### Scenario: an ordinary policy document with a similar name still compiles

- **WHEN** a policy document has a name that contains neither conflict marker
- **THEN** it compiles normally
- **AND** no conflict finding is emitted

### Requirement: A Conflict Copy Refuses Policy Authoring While Reads Continue

A recognised conflict copy leaves the current policy ambiguous: the author cannot know
which of the two documents a later compile will select, and prospective compilation
excludes conflict copies from the tree it evaluates, so an authoring operation would be
validated against a policy the live vault does not have. Every authoring operation SHALL
therefore be refused while a conflict copy is present under the governance tree.

Reads SHALL NOT be refused on that basis. A warm vault continues to serve its last good
compiled policy, because flooring every read to the most restrictive level over a
file-synchronisation artefact is a larger harm than the ambiguity it guards against.

The refusal SHALL NOT create a sidecar, policy directory, receipt, or marker, preserving
the guarantee that a rejected authoring operation leaves no state behind.

#### Scenario: authoring is refused while a conflict copy is present

- **WHEN** a conflict copy is present under the governance tree and any authoring
  operation is invoked
- **THEN** the operation is refused with a distinct conflict code
- **AND** no policy document, sidecar row, receipt or marker is created

#### Scenario: reads continue to serve the last good policy

- **WHEN** the same vault serves a content read while that conflict copy is present
- **THEN** the last good compiled policy is applied
- **AND** the read is not floored to the most restrictive level

#### Scenario: resolving the conflict restores authoring

- **WHEN** the conflict copy is removed and an authoring operation is retried
- **THEN** the operation proceeds normally

### Requirement: A Scope May Deny Audiences It Does Not Name

A scope MAY declare that the standing default for an audience is the most restrictive
level rather than the most permissive one. Where a scope carries that declaration and an
item is a member of it, an audience for which no standing rule matches SHALL resolve to no
disclosure, instead of to full release.

The declaration changes the DEFAULT only. It SHALL NOT override an authored rule: where a
standing rule names the audience for that scope, that rule's ceiling applies exactly as it
does today. Grants SHALL continue to only raise, organisation caps SHALL continue to only
lower, and a declared purpose SHALL continue to only narrow.

The owner SHALL never be subject to the declaration. An owner locked out of their own
scope is a vault that has lost its own contents, which is not the confidentiality this
expresses.

Where an item is a member of several scopes and any one of them carries the declaration,
the restrictive default applies — a scope cannot be widened by adding an undeclared scope
alongside it.

A vault with no governance tree SHALL remain on the empty fast path, unaffected.

Authored audience-bearing fields SHALL NOT enter the evaluator's reserved NUL-prefixed
namespace. A NUL in `audience` or `to_audience` SHALL produce an ERROR finding and refuse
the compile, including the values reserved for unresolved principals and the unnamed-
audience transition probe.

Policy transition previews SHALL expose the post-change ceiling for the unnamed-audience
default as `unnamed_audience_ceiling`, separately from the authored-audience
`target_ceiling`. The field SHALL be present and nullable when no concrete membership can
be evaluated.

#### Scenario: an audience no rule names receives nothing

- **WHEN** an item belongs to a scope carrying the declaration and a request arrives from
  an audience for which no standing rule matches that scope and no matching grant applies
- **THEN** the decision is no disclosure
- **AND** outside the relevance-ranking signals `bm25_rank`, `keyword_rank`, `vector_rank`,
  and `graph_in_degree`, its projected representation is indistinguishable from one that
  does not exist
- **AND** those signals reflect corpus position and are a known pre-existing channel
  tracked separately from this change

#### Scenario: non-owner inspection does not reveal a default-denied path

- **WHEN** a non-owner uses `explain` or `simulate` for one path, first while it exists and
  is denied at no disclosure and again after the same path is deleted
- **THEN** both requests receive the same error class and text
- **AND** owner inspection behaviour is unchanged
- **AND** an established terminal `release_reason` remains inspectable

#### Scenario: reserved audience ids refuse compilation

- **WHEN** a rule or grant authors an `audience` or `to_audience` containing a NUL
- **THEN** the compiler emits an ERROR finding
- **AND** the policy compile is refused

#### Scenario: a transition preview exposes the unnamed default

- **WHEN** a declared scope has an L1 rule for `external` and a proposal removes the
  declaration
- **THEN** `target_ceiling` remains 1 for the authored audience
- **AND** `unnamed_audience_ceiling` is 6 for the post-change default

#### Scenario: a newly minted audience id is denied by default

- **WHEN** a principal's credential is rotated so it resolves to an audience id that
  appears in no policy document, and it requests an item in a declared scope
- **THEN** the decision is no disclosure
- **AND** the outcome is identical to the pre-rotation audience having been unnamed

#### Scenario: an authored rule still governs the audience it names

- **WHEN** a standing rule names an audience for a declared scope with a ceiling above no
  disclosure
- **THEN** that audience receives the rule's ceiling
- **AND** the declaration does not lower it

#### Scenario: a grant still raises above the default

- **WHEN** an audience unnamed by any standing rule holds a grant for a declared scope
- **THEN** the grant's ceiling applies
- **AND** the declaration does not suppress it

#### Scenario: an organisation cap still lowers

- **WHEN** an organisation cap applies to a declared scope alongside a rule permitting a
  higher level
- **THEN** the lower of the two applies, unchanged by the declaration

#### Scenario: the owner reads a declared scope

- **WHEN** the owner requests an item in a declared scope for which no rule names the owner
- **THEN** the owner receives full release

#### Scenario: one declared scope denies across an overlapping undeclared scope

- **WHEN** an item belongs to both a declared scope and an undeclared scope, and the
  audience is named by no standing rule
- **THEN** the decision is no disclosure

#### Scenario: an undeclared scope keeps today's default

- **WHEN** an item belongs only to scopes carrying no declaration and no standing rule
  matches the audience
- **THEN** the decision is full release, exactly as before this change

#### Scenario: inspection explains a default denial

- **WHEN** the owner explains the decision for an item withheld by the declaration
- **THEN** the explanation identifies the declaring scope
- **AND** it does not attribute the outcome to a standing rule that does not exist

### Requirement: Planning authorization precedes identity and reduction
Every Planning discovery, inspection, read, structured query, lifecycle total, horizon grouping, hierarchy assembly, rendered view, export-shaped response, template return, history projection, and mutation receipt SHALL pass the existing governance and release boundary at the correct manifest or item granularity. Structured Planning SHALL require `LEVEL_FULL` (L6) for every artifact whose complete values, hashes, identity, contents, or relationships contribute to a response. Authorization SHALL occur before public count, cap, ordering, parsing, schema finding, identity ambiguity, snapshot, continuation, grouping, hierarchy, latest selection, or rendering.

#### Scenario: Withheld Planning collection is indistinguishable from absent
- **WHEN** a Planning manifest is below L6 for the caller
- **THEN** discovery, inspection, query, and mutation use the same public missing shape as a nonexistent collection

#### Scenario: Hidden item cannot shape a horizon view
- **WHEN** a Planning collection contains both released and withheld items
- **THEN** only authorized items contribute to counts, horizon groups, ordering, pagination, derived renderings, and truncation metadata

#### Scenario: Hidden parent cannot reveal hierarchy
- **WHEN** a released work item names a parent or area that is withheld
- **THEN** the target is authorized before identity, title, or kind is parsed and the public result/refusal is indistinguishable from a missing target

#### Scenario: Excerpt permission cannot authorize full Planning values
- **WHEN** a caller has L5 but not L6 for a Planning manifest, item, template, or linked governed value
- **THEN** `plan_memory` returns none of its full values, hashes, identity, hierarchy, counts, or existence while ordinary recall may still apply its independent excerpt rules to an eligible manifest

### Requirement: Planning item granularity and mutation require complete authorized state
The Markdown-item adapter SHALL receive an immediate path-authorization callback and SHALL authorize each candidate at L6 before it can affect public file/byte caps, ordering, parsing, diagnostics, identity ambiguity, relationship validation, source versions, continuation snapshots, or reductions. Internal raw-walk bounds SHALL remain fail-closed and reveal no count. Authorized-only snapshots SHALL mean a hidden-only edit does not invalidate another caller's continuation. Planning mutation SHALL refuse when the caller cannot receive the complete canonical collection snapshot required for safe hierarchy and container-CAS validation.

#### Scenario: Hidden malformed item is never parsed
- **WHEN** a below-L6 Planning item is malformed, duplicates an ID, or would exceed a public candidate cap
- **THEN** the structured result is identical to that item being absent and the file cannot cause a public parse error, ambiguity, count, or cap exhaustion

#### Scenario: Hidden-only edit preserves released continuation
- **WHEN** only a withheld Planning item changes after a released-only first page
- **THEN** the authorized continuation identity remains stable and reveals no hidden change

#### Scenario: Partial-view mutation refuses
- **WHEN** a caller can read only a subset of a Planning collection and requests add, update, or triage
- **THEN** mutation refuses before publication because an authorized subset cannot substitute for the complete canonical snapshot and relationship graph

### Requirement: Planning egress and precommit receipts are default-deny
Planning responses SHALL use shape-specific typed default-deny projectors. Ordinary schema values require L6; Planning references, parent/area edges, exact Records saved-view pointers, external execution pointers, templates, paths, identities, hashes, audit/history, conflicts, continuations, counts, groupings, and derived provenance SHALL be recursively shape-validated and projected before egress. Opaque Records collection references and execution references SHALL remain opaque and SHALL NOT trigger local stable-ID, wikilink, path, or remote-system resolution. Their disclosure authority is exactly the containing L6 Planning item; target existence or authorization SHALL NOT change their shape.

Mutation authorization and disclosure SHALL run through a precommit hook inside the guarded Planning mutation after final canonical re-read and relationship resolution but before guarded batch publication. Failure SHALL leave canonical files and activity history untouched. Governance receipts, Planning audit events, operational journals, and terminal mutation receipts SHALL remain distinct and SHALL not claim publication that did not commit.

#### Scenario: Stale refusal leaks no current Planning content
- **WHEN** an unauthorized or stale Planning update is refused
- **THEN** the response provides only bounded remediation and metadata allowed at the caller's release level, without current title, body, relationships, evidence, or execution references

#### Scenario: Evidence descriptor cannot leak a governed link
- **WHEN** an authorized Planning item contains a syntactically valid opaque Records collection and saved-view pointer whose target is hidden or absent
- **THEN** the exact pointer round-trips because Planning neither resolves nor target-authorizes it, while withholding the containing item suppresses the entire pointer

#### Scenario: Opaque execution reference is not resolved
- **WHEN** an execution pointer resembles a private vault path, memory reference, or external URL
- **THEN** Planning validates only its bounded opaque syntax and does not use target existence or authorization to change the public shape

#### Scenario: Disclosure failure precedes publication
- **WHEN** the precommit governance or receipt hook refuses or fails
- **THEN** no Planning item, `plan_audit` head, activity event, or committed terminal is published

#### Scenario: Publication failure does not forge governance commit evidence
- **WHEN** precommit disclosure succeeds but guarded publication later rolls back
- **THEN** the governance receipt records only the authorization attempt and does not claim that the Planning mutation committed
