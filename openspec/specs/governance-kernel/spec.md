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
