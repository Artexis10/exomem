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
