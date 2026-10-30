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

After managed warm-up, ordinary recall SHALL re-prove a policy-compatible published catalogue even when strict live-projection readiness has been demoted. Catalogue selection SHALL retain one transaction that binds both scope checkpoints, schema, semantic identity, and policy identity. Every catalogue read SHALL use that view, suppress every pending identity, and add only proven pending content. Exact and stale projections SHALL load their paths from that view without walking canonical files. Request admission SHALL NOT promote strict health readiness or escape into another request, vault, thread, or scope. Strict health retrieval readiness (`ready`, `admitted=true`) SHALL stay strict; only the serving status MAY follow a servable lag, after this process has made a strict proof, as `install-readiness` defines.

Selection SHALL materialize the evidence needed after the transaction closes. Query encoding, model loading, and reranking SHALL NOT retain the transaction. Canonical hydration SHALL match selected content hashes or exact request-owned pending evidence. A mismatch SHALL omit affected evidence and disclose incomplete coverage. Catalogue admission SHALL NOT weaken existing generation checks for independent indexes or claim a snapshot across separate stores. Partial results SHALL NOT enter shared result caches. Current disclosure authority and terminal byte checks SHALL remain mandatory.

#### Scenario: Repair demotes readiness while a pending edit is durable

- **WHEN** a compatible published catalogue exists, a committed edit has exact pending custody, and repair or health has demoted strict readiness
- **THEN** keyword and hybrid recall return the committed edit with projection lag disclosed, without walking the corpus

#### Scenario: Ordinary publication advances after request admission

- **WHEN** an ordinary data publication advances the content checkpoint after a compatible catalogue is admitted
- **THEN** selection continues from the retained catalogue without mixing rows, membership, or statistics with the newer publication
- **AND** the response discloses known lag without disabling unrelated recall

#### Scenario: Identity changes after request admission

- **WHEN** access policy or semantic catalogue identity changes while a request selects or finishes its admitted evidence
- **THEN** that read refuses rather than serving rows under an invalid interpretation

#### Scenario: Canonical bytes advance beyond selected evidence

- **WHEN** a selected page's current bytes differ from its admitted content hash and no exact request-owned pending page proves them
- **THEN** recall omits that evidence, preserves unaffected results, and discloses incomplete coverage without caching the partial result

#### Scenario: Global retirement follows pending admission

- **WHEN** publication retires global custody after a request obtained exact pending pages and their complete suppression footprint
- **THEN** that request retains its proven pending evidence and does not revive suppressed persistent identities

#### Scenario: Provider work follows catalogue selection

- **WHEN** recall loads a model, encodes a query, or reranks selected candidates
- **THEN** it holds no retained catalogue transaction during that work

#### Scenario: Strict health observes a newer publication

- **WHEN** a request retains an older admitted catalogue while strict readiness checks the current publication
- **THEN** readiness uses its independent current proof and cannot borrow the request's older transaction

#### Scenario: Nested recall or an exception ends a request

- **WHEN** another recall begins inside a request or a request exits through an exception
- **THEN** its catalogue binding stays isolated and the prior scope is restored without leaking admission

#### Scenario: Requested outside-KB widening needs a live catalogue

- **WHEN** ordinary KB recall is admitted but the vault catalogue used by an explicitly requested widening is lagging
- **THEN** widening declines without a scan and preserves the KB results
- **AND** an earlier cached widening cannot bypass that decline

#### Scenario: A served lag does not promote strict readiness

- **WHEN** a proven process serves recall from a published catalogue that trails a live projection and health reports retrieval as `lagging`
- **THEN** health reports `admitted=false` for that retrieval state
- **AND** the request still discloses the projection lag it serves

## ADDED Requirements

### Requirement: Pending hydration limits reduce coverage without disabling recall

Pending content hydration SHALL remain bounded. A legitimate pending burst beyond that bound SHALL use a complete trustworthy path footprint to suppress pending identities and serve unaffected admitted results. Partial answers SHALL disclose incomplete coverage and SHALL NOT enter shared result caches. Public status SHALL NOT reveal unreleased pending paths or counts.

#### Scenario: A pending burst exceeds content hydration capacity

- **WHEN** pending custody exceeds the content hydration bound and its complete path footprint is valid
- **THEN** keyword and hybrid recall serve unaffected eligible pages and suppress every pending persistent identity
- **AND** the response reports incomplete coverage without reading every pending page body

#### Scenario: Pending references cannot prove current identity

- **WHEN** a pending identity lacks a proven current reference mapping and its canonical bytes cannot be read
- **THEN** reference lookup cannot revive its persistent mapping or claim an unresolved identifier is unique
- **AND** unrelated released recall hits remain available

#### Scenario: A pending page body is unproven but readable

- **WHEN** a pending page's body cannot be proven against its receipt but its canonical bytes can be read
- **THEN** exact reference lookup takes that page's identity from those bytes, as the canonical scan does, and reads every such page whichever identifiers were asked
- **AND** a page with no well-formed identifier owns none, so an ordinary write by reference proceeds
- **AND** a page holding the requested identifier beside another owner refuses as ambiguous
- **AND** more such pages than one hydration holds refuse the lookup with the warming outcome
- **AND** recall keeps suppressing that page's persistent evidence; only exact reference lookups, including the response gate that checks memory references in governed output, read identities

#### Scenario: Concurrent writes invalidate body hydration

- **WHEN** ordinary pending writes invalidate a body-hydration fence
- **THEN** recall obtains a fresh complete path footprint and serves degraded unaffected results without repeating full body hydration
- **AND** the next request re-hydrates once, re-reading only bodies without retained proof, instead of serving the footprint until custody next changes

#### Scenario: A hidden page is pending

- **WHEN** pending custody covers a page that the caller cannot access
- **THEN** the response reveals no additional page content, identity, path, count, or diagnostic about that page

### Requirement: Optional reference projection preserves admitted material

Optional reference projection SHALL preserve admitted material when trustworthy pending suppression exists but current reference identity remains incomplete. Free-text projection SHALL omit uncertain well-formed stable references uniformly rather than establish uniqueness or cancel the whole response. Structured pointer projection SHALL omit whole entries whose stable origins remain uncertain. Unknown authoritative custody SHALL remain unavailable.

The redactor SHALL retain raw canonical-owner evidence separately from current mappings after pending withdrawal. It MAY use that evidence only for redaction, with current disclosure checks. It MUST NOT use it to resolve an identifier, revive a suppressed mapping, admit content, or claim current uniqueness. Source selection SHALL retain the existing vault-wide projection-or-scan rule and current-snapshot recheck.

The redactor SHALL apply maintained body-free authority before excluding hidden pending uncertainty. It SHALL retain every custody withdrawal and every unresolved membership or possible widening grant. Equal canonical bytes and authority SHALL produce the same protected-reference redaction when only hidden pending custody changes. Public diagnostics SHALL expose no protected identifiers, paths, counts, or new per-reference state.

Structured pointer origins SHALL use current reference mappings and their existing unique-visible-owner proof when coverage is complete. Raw redaction evidence SHALL NOT admit an origin. When origin admission applies during incomplete caller-filtered coverage, a non-abstained guarded packet SHALL report one generic `missing` entry with `role: pointers` and `reason: identity_incomplete`. That entry SHALL depend on coverage alone, including an empty origin batch, and SHALL NOT assert that any pointer exists. Independently hidden uncertainty SHALL change neither the entry nor the packet. Pointer omission SHALL preserve admitted anchors and units and refund the existing pointer charge.

#### Scenario: Optional identity is unprovable but coverage is servable

- **WHEN** an admitted response contains an optional stable reference and potentially visible pending identity remains unprovable
- **THEN** the response retains its admitted material and replaces the uncertain token with the existing withheld marker
- **AND** exact identifier resolution still refuses when it cannot prove uniqueness

#### Scenario: Pending withdrawal covers a known hidden owner

- **WHEN** canonical bytes and authority remain equal while a known hidden owner enters unprovable pending custody
- **THEN** raw ownership preserves its existing redaction without reviving its withdrawn current mapping
- **AND** an unrelated absent reference remains unchanged

#### Scenario: A budget pointer has an uncertain optional origin

- **WHEN** activation compiles admitted anchors and units with a stable-origin budget pointer while caller-filtered identity coverage is incomplete
- **THEN** the guarded packet retains admitted anchors and units, omits the whole pointer, refunds its charge, and reports incomplete origin coverage
- **AND** exact reference operations retain strict uniqueness proof

#### Scenario: A packet has no visible origin pointers

- **WHEN** caller-filtered identity coverage is incomplete and a non-abstained guarded packet has an empty or wholly hidden origin batch
- **THEN** both packets report the same generic incomplete origin coverage without revealing pointer membership

#### Scenario: Only hidden custody changes for a pointer origin

- **WHEN** canonical bytes and authority remain equal while an independently hidden owner enters unprovable pending custody
- **THEN** the structured pointer projection remains unchanged and adds no incomplete origin coverage marker

#### Scenario: New hidden uncertainty accompanies an absent reference

- **WHEN** independently hidden pending uncertainty appears without changing the canonical owner set of an absent optional reference
- **THEN** maintained authority excludes that hidden uncertainty and the optional reference remains unchanged

#### Scenario: Current mapping proves a pending move

- **WHEN** pending custody proves a unique current mapping at a new path
- **THEN** the redactor uses that current path and its authority without reviving the withdrawn old mapping

#### Scenario: Custody is unknown during optional projection

- **WHEN** authoritative pending custody cannot provide trustworthy suppression
- **THEN** optional projection preserves the unavailable outcome instead of serving material after citation omission

#### Scenario: The caller supplied an error reference

- **WHEN** an error echoes a stable reference that the caller supplied
- **THEN** the existing input exemption preserves that token without exposing its existence through redaction

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
