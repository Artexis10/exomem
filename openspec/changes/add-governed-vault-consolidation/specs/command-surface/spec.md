## ADDED Requirements

### Requirement: Existing surfaces retain verified connector provenance

Authenticated surfaces SHALL carry verified connector identity separately from owner identity and audience. They SHALL preserve that binding through normalization and governance authorization sessions. Request fields, raw headers, names, user agents, and a shared bearer SHALL NOT establish connector identity.

#### Scenario: Two connectors authenticate the same owner

- **WHEN** two independently verified clients authenticate the same owner
- **THEN** both retain owner identity and their distinct configured content ceilings
- **AND** a purpose, grant, bridge, or exact release cannot widen either ceiling

#### Scenario: An unknown client presents familiar metadata

- **WHEN** an unknown or re-registered client copies a configured client's name, headers, or tool arguments
- **THEN** the armed destination applies its restrictive unknown-client default
- **AND** no copied metadata upgrades its verified binding

### Requirement: Delegated transfers retain and revalidate originating authentication

A delegated capability SHALL retain bearer-free verified connector and originating authentication bindings. Consumption SHALL verify current session activity, generation, expiry, and refresh-family validity through existing authentication authority. A valid capability signature or governance authorization session SHALL NOT replace that check.

#### Scenario: Authentication is revoked after minting

- **WHEN** the originating authentication session is revoked after a signed download capability is minted
- **THEN** consumption refuses before releasing bytes
- **AND** an unexpired governance authorization session cannot keep the capability usable

#### Scenario: Authentication generation or family changes

- **WHEN** the originating generation, expiry, or refresh-family state no longer validates
- **THEN** capability consumption refuses under the existing session authority
- **AND** replay cannot restore the earlier authority

#### Scenario: Signed nested bindings round-trip

- **WHEN** a valid capability carries connector, originating authentication, and governance session context
- **THEN** decoding reconstructs each verified binding and preserves its meaning
- **AND** malformed nested values confer no authority

### Requirement: Mutation retries separate verified connectors

The shared retry owner SHALL add verified issuer/client identity to the existing logical-vault and principal namespaces before explicit and implicit terminal lookup. Reauthentication SHALL preserve that stable identity. Current session and configuration checks SHALL remain independent consumption requirements. Transfer upload/download issuance SHALL bypass durable terminal caching. The runtime SHALL NOT adopt legacy audience-only terminals as verified-client results.

#### Scenario: Owner connectors submit identical retry arguments

- **WHEN** two verified clients of the same owner submit the same public key or implicit arguments
- **THEN** each client reaches its own terminal namespace
- **AND** a newly authenticated session of the original client can still replay its successful ordinary file write

#### Scenario: A transfer capability is requested twice

- **WHEN** either transfer operation receives repeated explicit or implicit retry keys
- **THEN** each invocation mints under current authority without storing a bearer terminal
- **AND** another client's prior call cannot confer its capability binding

#### Scenario: An old audience-only terminal exists during upgrade

- **WHEN** a verified client repeats a request recorded before client-specific retry identity
- **THEN** the new namespace does not adopt that unverifiable terminal
- **AND** documentation states that an unguarded non-idempotent file operation may execute again

### Requirement: Administrative ingress is explicit when connector ceilings are armed

Only explicit administrative CLI, stdio, or library ingress SHALL receive unrestricted administrative content access. Owner audience, absent connector provenance, a transfer surface, a shared REST key, and a shared cell bearer SHALL NOT imply that ingress. Unarmed operation SHALL retain existing behavior.

#### Scenario: A legacy owner transfer is consumed

- **WHEN** an armed destination consumes a legacy owner-audience capability without verified connector provenance
- **THEN** it applies the restrictive default
- **AND** the owner label does not grant unrestricted administrative access

#### Scenario: A hosted cell has only a shared bearer

- **WHEN** an armed cell receives a valid shared bearer without verified end-client provenance
- **THEN** it applies the restrictive default
- **AND** a tenant-owner binding or passed HTTP header cannot substitute for end-client provenance

#### Scenario: An explicit local administrator accesses admitted data

- **WHEN** the operator uses actual administrative CLI, stdio, or library ingress
- **THEN** the explicit administrative principal retains its existing content authority
- **AND** remote clients cannot select that ingress through request data

### Requirement: Connector admission preserves the existing command contract

Delivery A SHALL enforce connector ceilings through existing product surfaces without adding a consolidation command, hosted profile, or routine confirmation workflow. Allowed-target owner operations SHALL retain their existing contracts. Documentation SHALL distinguish unavailable private-dependent operations from completed private vocabulary support.

#### Scenario: A limited owner captures allowed content

- **WHEN** a limited owner captures admitted content in a configured capture namespace or edits admitted existing content
- **THEN** the command retains legitimate owner write behavior
- **AND** no new approval queue or per-step confirmation is required

### Requirement: Capture admission precedes namespace observations

The host SHALL configure capture namespaces and establish their visibility invariant under stopped maintenance. All canonical writers, imports, restores, moves, replacement, reclassification, and Scope or configuration changes SHALL preserve that invariant. Private metadata SHALL NOT become visible because it occupies a capture namespace.

#### Scenario: A limited owner creates in an eligible namespace

- **WHEN** the caller supplies admitted content and an independently eligible configured capture destination
- **THEN** the existing capture command creates the content without a new approval queue
- **AND** the command reports its actual destination

#### Scenario: A mixed namespace contains a hidden collision

- **WHEN** a limited caller proposes creation outside the configured capture namespaces
- **THEN** the command returns the same unavailable outcome for hidden-present and absent destinations
- **AND** eligibility precedes existence checks and generated-name allocation

#### Scenario: An unrestricted owner introduces private metadata

- **WHEN** any canonical writer proposes content that violates a capture namespace's visibility invariant
- **THEN** publication refuses before changing the namespace
- **AND** existing admitted edits outside the capture namespace remain available

#### Scenario: A caller supplies an explicit destination

- **WHEN** the explicit destination is ineligible for creation
- **THEN** the command returns an unavailable outcome
- **AND** it does not silently redirect the content to another folder

### Requirement: Admitted canonical point writes preserve complete field authority

After the actual S1 successor merges, Delivery A SHALL support admitted existing-row point writes with the shared complete-field mutation obligation. It SHALL preserve RAW field authority separately from connector admission. Current target admission SHALL precede values decoding, conflict probes, and retry lookup. Global-dependent operations without an admitted namespace SHALL return existing unavailable results before observations.

#### Scenario: An owner updates an admitted existing row

- **WHEN** a verified limited owner updates an admitted target with complete field authority and unchanged global validator inputs
- **THEN** the writer checks target concurrency and old/proposed admission before commit
- **AND** hidden siblings cannot affect guards, conflicts, or returned receipts

#### Scenario: A key or hierarchy change requires hidden global inputs

- **WHEN** the operation changes inputs to a global-dependent constraint without an independent admitted namespace
- **THEN** it returns the existing unavailable outcome before probing those inputs
- **AND** a configured file capture folder does not confer SQL key authority

#### Scenario: A held candidate is resumed

- **WHEN** a request identifies a held candidate
- **THEN** its canonical held subject requires admission before decoding and before deletion or publication
- **AND** hidden and absent held IDs have the same result

### Requirement: Scoped point retries preserve committed outcomes without stale disclosure

Scoped retry identity SHALL use verified logical vault/store, issuer/client, canonical collection, and target identity. It SHALL exclude session generation and configuration revision. A server-only outer `point:v1:` domain and nullable independently unique inner `scoped_request_id` SHALL separate legacy terminals. New scoped transactions SHALL retain `request_id=NULL` without legacy fallback or historical rewrites.

#### Scenario: A successful request retries after its own write

- **WHEN** a currently admitted client and complete target fields reach a scoped hit with the original argument digest
- **THEN** the shared historical envelope projector revalidates current contributor authority and serves the original admitted snapshots
- **AND** the request's old precondition is not compared with today's snapshot

#### Scenario: A scoped retry has no terminal

- **WHEN** current admission succeeds but the scoped lookup misses
- **THEN** the writer checks the current admitted v2 guard before any effect or held refusal
- **AND** an unchanged legacy retry with an old guard creates no second mutation or held candidate

#### Scenario: Authority no longer permits a historical receipt

- **WHEN** the historical envelope cannot prove current contributor or complete-field admission
- **THEN** both inner and outer replay return the existing unavailable outcome
- **AND** the committed mutation remains committed without reconstructing its old guard from current rows
