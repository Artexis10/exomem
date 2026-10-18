## MODIFIED Requirements

### Requirement: Optional graph expansion does not veto admitted ordinary recall

Ordinary recall SHALL continue over its admitted retrieval projection and servable pending coverage when optional graph expansion cannot obtain a current resolver. The response SHALL disclose omitted graph work. Policy, trustworthy pending-path suppression, catalogue and graph-dependent relation/filter proofs SHALL remain mandatory; unproven graph edges SHALL NOT be represented as current.

#### Scenario: Resolver lags an ordinary write

- **WHEN** a managed hybrid lookup has valid catalogue and servable pending coverage but its optional resolver checkpoint is stale
- **THEN** it returns eligible direct matches with graph warming disclosed and performs no whole-vault resolver scan on the reader

#### Scenario: Relation semantics need the pending graph

- **WHEN** the caller requests a relation-dependent predicate whose graph proof is unavailable
- **THEN** retrieval retains its explicit warming refusal rather than returning an incomplete relation answer

#### Scenario: Pending coverage is unprovable

- **WHEN** a committed page cannot be hydrated but a complete pending-path footprint remains trustworthy
- **THEN** ordinary managed retrieval suppresses that page's persistent evidence and serves unaffected eligible results
- **AND** it discloses incomplete pending coverage without exposing unreleased paths or counts

#### Scenario: Pending custody is unknown

- **WHEN** authoritative pending custody is unreadable, malformed, or inconsistent with its receipts
- **THEN** retrieval returns an explicit unavailable outcome instead of treating unknown custody as empty

### Requirement: Published catalogue admission survives strict readiness demotion

After managed warm-up, ordinary recall SHALL re-prove a policy-compatible published catalogue even when strict live-projection readiness has been demoted. Every catalogue read SHALL use the admitted checkpoint, suppress every pending identity, and add only proven pending content. A stale projection SHALL load its paths from the matching catalogue without walking canonical files. Request admission SHALL NOT promote strict health readiness or escape into another request, vault or scope.

#### Scenario: Repair demotes readiness while a pending edit is durable

- **WHEN** a compatible published catalogue exists, a committed edit has exact pending custody, and repair or health has demoted strict readiness
- **THEN** keyword and hybrid recall return the committed edit with projection lag disclosed, without walking the corpus

#### Scenario: Identity changes after request admission

- **WHEN** access policy, semantic catalogue identity or the published content checkpoint changes before an admitted catalogue read
- **THEN** that read refuses rather than serving rows under the previous proof

#### Scenario: Nested recall or an exception ends a request

- **WHEN** another recall begins inside a request or a request exits through an exception
- **THEN** its catalogue binding stays isolated and the prior scope is restored without leaking admission

#### Scenario: Requested outside-KB widening needs a live catalogue

- **WHEN** ordinary KB recall is admitted but the vault catalogue used by an explicitly requested widening is lagging
- **THEN** widening declines without a scan and preserves the KB results
- **AND** an earlier cached widening cannot bypass that decline

## ADDED Requirements

### Requirement: Pending hydration limits reduce coverage without disabling recall

Pending content hydration SHALL remain bounded. A legitimate pending burst beyond that bound SHALL use a complete trustworthy path footprint to suppress pending identities and serve unaffected admitted results. Partial answers SHALL disclose incomplete coverage and SHALL NOT enter shared result caches. Public status SHALL NOT reveal unreleased pending paths or counts.

#### Scenario: A pending burst exceeds content hydration capacity

- **WHEN** pending custody exceeds the content hydration bound and its complete path footprint is valid
- **THEN** keyword and hybrid recall serve unaffected eligible pages and suppress every pending persistent identity
- **AND** the response reports incomplete coverage without reading every pending page body

#### Scenario: Pending references cannot prove current identity

- **WHEN** a pending identity lacks a proven current reference mapping
- **THEN** reference lookup cannot revive its persistent mapping or claim an unresolved identifier is unique
- **AND** unrelated released recall hits remain available

#### Scenario: Concurrent writes invalidate body hydration

- **WHEN** ordinary pending writes invalidate a body-hydration fence
- **THEN** recall obtains a fresh complete path footprint and serves degraded unaffected results without repeating full body hydration

#### Scenario: A hidden page is pending

- **WHEN** pending custody covers a page that the caller cannot access
- **THEN** the response reveals no additional page content, identity, path, count, or diagnostic about that page

### Requirement: The service recovers committed pending custody

The existing bounded service worker SHALL recover committed pending custody without manual reconciliation. Recovery SHALL cover ready live rows and stranded batches, preserve exact per-path supersession and unfinished component demand, and apply retry backoff. It SHALL repair only receipt-covered derived state. Unproven mixed canonical state SHALL remain explicit and SHALL NOT gain committed proof.

#### Scenario: A ready row names superseded bytes

- **WHEN** a ready live pending row names older bytes and both recall lanes hold the current bytes
- **THEN** service recovery retires that row through exact per-path proof
- **AND** remaining paths and component demand retain their custody

#### Scenario: A committed stranded batch needs current derived state

- **WHEN** a previously proven committed batch remains stranded after canonical bytes change
- **THEN** service recovery converges its current present files or deletions through existing derived owners
- **AND** it retires custody only after the required publication proof succeeds

#### Scenario: Work completes above the hydration bound

- **WHEN** required publications complete while pending bodies exceed the hydration bound
- **THEN** the service retires their exact custody without waiting for another foreground recall

#### Scenario: Restart follows component completion

- **WHEN** the process stops after component completion but before retirement, with more pending paths than the hydration bound
- **THEN** the restarted service retires completed live custody through exact targeted proof without a foreground request

#### Scenario: Restart follows a canonical deletion

- **WHEN** a proven deletion commits before the process stops and its index removal has not run
- **THEN** the restarted component worker removes stale index evidence before declaring its publication complete
- **AND** the same guarantee applies to tombstones inside a mixed batch

#### Scenario: A callback fails

- **WHEN** one batch's recovery callback fails
- **THEN** the worker retains its demand, applies bounded backoff, and continues unrelated due work

#### Scenario: An unrecoverable prefix precedes recoverable work

- **WHEN** earlier batches cannot establish their publication proof while later batches are recoverable
- **THEN** unsuccessful proof advances retry eligibility and later due work receives its bounded service turn

#### Scenario: A crash leaves unproven mixed canonical bytes

- **WHEN** a prepared receipt has no committed proof and its canonical paths match neither a complete before-state nor a complete after-state
- **THEN** automatic recovery retains the transactional reconciliation demand and does not declare the intended mutation committed
