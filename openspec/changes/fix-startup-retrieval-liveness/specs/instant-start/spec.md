## MODIFIED Requirements

### Requirement: Non-Blocking Boot

The system SHALL NOT block the MCP transport on any model preload or cache warm-up. `build_server()` SHALL return, and `mcp.run()` SHALL begin serving, before the embedding model, reranker, CLIP model, maintained lexical catalog, or optional lexical caches have necessarily finished loading. This SHALL hold identically for the stdio transport and the http transport. A request that needs an incomplete maintained lexical catalog MUST return the bounded typed `RETRIEVAL_INDEX_WARMING` outcome rather than waiting for warm-up or scanning the corpus.

#### Scenario: Stdio client is answered immediately

- **WHEN** exomem is started with `--transport stdio` against a vault whose models and lexical catalog are not yet ready
- **THEN** the MCP `initialize` handshake completes without waiting for any model load or cache warm-up to finish
- **AND** the maintained lexical catalog, embedding model, reranker, CLIP model, and optional lexical caches continue loading after `initialize` has already returned

#### Scenario: Http transport begins serving immediately

- **WHEN** exomem is started with an http transport
- **THEN** the server accepts connections and responds to liveness requests before any model preload or lexical warm-up has necessarily finished
- **AND** an ordinary keyword or hybrid request that arrives before maintained-catalog readiness returns `RETRIEVAL_INDEX_WARMING` promptly without a corpus walk

### Requirement: Lexical-First Warm Ordering

The background warm sequence SHALL reconcile the maintained lexical catalog for KB and vault scopes before warming parsed pages, the wikilink resolver, the semantic corpus, embedding/CLIP matrices, or any model. It SHALL mark `retrieval_catalog` ready immediately after both maintained scopes succeed. It SHALL mark the broader `lexical` component after the remaining lexical/derived caches finish, then mark enabled model components in their existing order.

#### Scenario: Retrieval catalog readiness lands before optional cache readiness

- **WHEN** background warm-up runs against a vault with Markdown content
- **THEN** `retrieval_catalog` becomes ready before parsed-page and resolver warm-up completes
- **AND** `lexical` becomes ready before enabled model readiness completes

#### Scenario: Keyword find is available as soon as the maintained catalog is ready

- **WHEN** `retrieval_catalog` is ready while optional caches or models are still warming
- **THEN** keyword-mode `find` returns exact full results from the maintained catalog without waiting for those later stages

#### Scenario: Quiet mode still prepares bounded lexical admission

- **WHEN** the resource policy is `quiet` and startup warm-up is enabled
- **THEN** the maintained lexical catalog phase still runs and may mark `retrieval_catalog` ready
- **AND** full parsed-page, resolver, matrix, and model cache preloading remains skipped

#### Scenario: Lazy empty-query recall remains bounded

- **WHEN** startup warm-up is disabled and an empty keyword or hybrid query reaches a production-sized incomplete catalog
- **THEN** the request returns `RETRIEVAL_INDEX_WARMING` without walking or parsing the corpus

#### Scenario: Small lazy repair converges readiness

- **WHEN** a bounded small-corpus request repairs and serves one maintained scope during lazy startup
- **THEN** the runtime arranges repair of any missing sibling scope
- **AND** retrieval admission becomes ready once both scopes are proven current

#### Scenario: Lexical readiness lands before model readiness

- **WHEN** background warm-up runs against a vault with Markdown content
- **THEN** the `retrieval_catalog` readiness component becomes ready before optional model readiness components
- **AND** ordinary lexical retrieval does not wait for embeddings, reranker, or CLIP readiness

#### Scenario: Keyword find is available as soon as lexical warm completes

- **WHEN** the `retrieval_catalog` readiness component is ready but embeddings are not yet ready
- **THEN** a keyword-mode `find` call returns full results without deferring on an optional model lane
