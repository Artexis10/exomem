## ADDED Requirements

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
