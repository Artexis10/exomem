## MODIFIED Requirements

### Requirement: Startup Cache Warm-Up

When startup warm-up is enabled, the system SHALL first reconcile the maintained lexical catalog for KB and vault scopes. When the active resource policy allows CPU cache preloading, it SHALL then warm the parsed-page cache, wikilink resolver, semantic corpus, and applicable matrices so later `find` calls avoid their first-use construction costs. Quiet mode SHALL retain only the maintained-catalog phase and skip resident full-corpus caches. `EXOMEM_DISABLE_WARMUP` SHALL skip proactive work and report retrieval admission as unverified until a later repair establishes it. Every stage SHALL soft-fail without preventing transport liveness and MUST NOT change returned results.

#### Scenario: Maintained catalog is the first warm stage

- **WHEN** the server starts with warm-up enabled
- **THEN** KB- and vault-scope maintained lexical catalogs are reconciled before any full parsed-page, resolver, semantic, matrix, or model warm
- **AND** ordinary lexical requests become admissible as soon as that catalog phase succeeds

#### Scenario: Warm-up can be disabled truthfully

- **WHEN** the server starts with `EXOMEM_DISABLE_WARMUP` set
- **THEN** no proactive warm-up work is performed
- **AND** runtime readiness does not claim maintained-catalog retrieval admission before it is established
- **AND** a large-corpus ordinary request returns bounded warming rather than building lazily on the request path

#### Scenario: Quiet mode skips resident CPU caches

- **WHEN** the server starts in `quiet` mode with warm-up enabled
- **THEN** maintained lexical catalogs are reconciled
- **AND** startup warm-up does not populate parsed-page, resolver, embedding-matrix, or CLIP-matrix caches solely for warm-up

#### Scenario: A later warm-up stage failure does not revoke catalog admission

- **WHEN** maintained lexical catalogs are ready and a later optional cache or model warm fails
- **THEN** transport and ordinary lexical retrieval remain available
- **AND** the later failure is logged without revoking `retrieval_catalog` readiness

#### Scenario: Warm-up primes caches before the first query

- **WHEN** the server starts with warm-up enabled
- **AND** the active resource policy allows CPU cache preloading
- **THEN** maintained lexical catalogs are reconciled before the first admitted, result-bearing `find` call is served
- **AND** the parsed-page, resolver, semantic, and applicable matrix caches are proactively populated
- **AND** the first `find` call's results are identical to what it would return without warm-up

#### Scenario: Warm-up can be disabled

- **WHEN** the server starts with `EXOMEM_DISABLE_WARMUP` set
- **THEN** no proactive warm-up work is performed at startup
- **AND** a small corpus may still build the required data lazily on first use
- **AND** a large-corpus ordinary request returns bounded warming until background repair establishes catalog admission

#### Scenario: Quiet mode skips CPU cache warm-up

- **WHEN** the server starts in `quiet` mode with warm-up enabled
- **THEN** maintained lexical catalogs are reconciled without populating parsed-page, resolver, embedding-matrix, or CLIP-matrix caches solely for warm-up
- **AND** ordinary retrieval uses the maintained catalog without requiring those resident caches

#### Scenario: A warm-up stage failure does not block startup

- **WHEN** one warm-up stage fails
- **THEN** the server transport still starts successfully
- **AND** the failure is logged without raising, and other allowed warm-up stages still run
- **AND** retrieval admission reflects whether the maintained catalog itself was successfully verified
