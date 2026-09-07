# recall-read-path Specification

## Purpose
TBD - created by archiving change bound-the-cold-recall-resolver. Update Purpose after archive.

## Requirements

### Requirement: Idle Memory Reclamation Keeps What Is Expensive To Rebuild

Reclaiming memory from an idle process SHALL retain the recall resolver.

Measured on a 2,400-page vault: the resolver holds 3.05 MiB, and rebuilding it
costs a vault walk, an admission pass and a read of every admitted page —
39 seconds of page reads alone, paid on the thread of the next reader. Releasing
three megabytes from a process that is also holding a roughly one-gigabyte
embedding model does not justify that.

Eviction for correctness SHALL remain unchanged and SHALL continue to clear it,
because there a stale resolver is a wrong answer rather than a slow one.

#### Scenario: The idle reaper keeps the recall resolver

- **WHEN** an idle process reclaims rebuildable RAM
- **THEN** the page cache and the hot find caches are cleared
- **AND** the recall resolver remains resident

#### Scenario: Correctness eviction still clears it

- **WHEN** a caller evicts caches to force a re-derivation
- **THEN** the recall resolver is cleared

#### Scenario: A reader after idle reclamation does not rebuild

- **GIVEN** a resident recall resolver
- **WHEN** idle reclamation runs and a reader then requests a resolver for the
  same identity
- **THEN** it is served from memory without walking the vault

### Requirement: Server Recall Never Rebuilds Projection On The Reader Thread

An activated managed server request SHALL NOT rebuild a recall projection, projected resolver, referent projection, or catalogue allowlist by walking the vault. It SHALL consume one exact checkpoint-bound proof tying maintained catalogues to the live projections used by that request, or return an explicit retryable warming/unavailable outcome. Correctness-triggered cache eviction and downstream resolver or referent acquisition MUST NOT silently convert the next server reader into the rebuild worker.

#### Scenario: Missing projection fails fast

- **WHEN** a server reader requests recall after correctness eviction and no replacement projection is live
- **THEN** it receives the retryable retrieval-warming outcome
- **AND** the full-vault walk and resolver rebuild seams are not invoked on that reader

#### Scenario: Background recovery restores readers

- **WHEN** background seed and catalogue repair publish matching authoritative checkpoints after eviction
- **THEN** subsequent server readers resume using the maintained projection without a process restart

#### Scenario: Downstream resolver miss preserves the no-walk boundary

- **WHEN** top-level admission succeeds but a hybrid, vector, resolver, or referent stage cannot obtain the matching maintained generation
- **THEN** that stage declines or returns the retryable warming outcome
- **AND** it does not rebuild from a vault walk on the request thread

#### Scenario: Cold optional referents warm away from the request

- **WHEN** a managed request has an entity cue but no registry cached for its exact live projection
- **THEN** the response omits the optional referent block and schedules one single-flight background build
- **AND** the request thread does not enumerate entity folders or parse the full entity registry

### Requirement: Offline Recall Retains A Correct Fallback

An explicit offline or CLI invocation with no activated runtime SHALL retain a bounded source-of-truth walk fallback so watcher-free maintenance remains correct. The fallback SHALL apply the same access, structured-record, alias, and no-follow policy as maintained publication.

#### Scenario: Offline maintenance can reconstruct recall

- **WHEN** an offline command runs without an event-maintained projection
- **THEN** it may reconstruct the projection from canonical Markdown
- **AND** the result excludes every path the maintained publication policy would exclude

### Requirement: Optional graph expansion does not veto admitted ordinary recall
Ordinary recall SHALL continue over its admitted retrieval projection and exact pending-write overlay when optional graph expansion cannot obtain a current resolver. The response SHALL disclose omitted graph work. Policy, pending-visibility, catalogue and graph-dependent relation/filter proofs SHALL remain mandatory; unproven graph edges SHALL NOT be represented as current.

#### Scenario: Resolver lags an ordinary write
- **WHEN** a managed hybrid lookup has valid catalogue/pending visibility but its optional resolver checkpoint is stale
- **THEN** it returns the eligible direct matches with graph warming disclosed and performs no whole-vault resolver scan on the reader

#### Scenario: Relation semantics need the pending graph
- **WHEN** the caller requests a relation-dependent predicate whose graph proof is unavailable
- **THEN** retrieval retains its explicit warming/refusal rather than returning an incomplete relation answer

#### Scenario: Pending coverage is unprovable
- **WHEN** a committed write cannot be represented by a valid bounded pending overlay
- **THEN** ordinary managed retrieval still refuses before consulting a stale catalogue

### Requirement: Published catalogue admission survives strict readiness demotion
After managed warm-up, ordinary recall SHALL re-prove a policy-compatible published catalogue even when strict live-projection readiness has been demoted. Every catalogue read in that request SHALL use the admitted checkpoint, with exact pending custody overlaid. A stale projection SHALL load its paths from the matching catalogue rather than walking current canonical files. Request-local admission SHALL NOT promote strict health readiness or escape into another request, vault or catalogue scope.

#### Scenario: Repair demotes readiness while a pending edit is durable
- **WHEN** a compatible published catalogue exists, a committed edit has exact pending custody, and repair or health has demoted strict readiness
- **THEN** keyword and hybrid recall return the committed edit with projection lag disclosed, without walking the corpus

#### Scenario: Identity changes after request admission
- **WHEN** access policy, semantic catalogue identity or the published content checkpoint changes before an admitted catalogue read
- **THEN** that read refuses rather than serving rows under the previous proof

#### Scenario: Nested recall or an exception ends a request
- **WHEN** another recall begins inside a request or a request exits through an exception
- **THEN** its catalogue binding is isolated and the prior scope is restored without leaking admission to later unscoped queries

#### Scenario: Requested outside-KB widening needs a live catalogue
- **WHEN** ordinary KB recall is admitted but the vault catalogue used by an explicitly requested widening is lagging
- **THEN** widening declines without a scan and preserves the KB results; a cached result from an earlier live widening cannot bypass that decline
