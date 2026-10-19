## ADDED Requirements

### Requirement: Full Local-Core Capability Inventory Is Accounted For
The repository SHALL define a versioned inventory for the local knowledge engine and reconcile it with Exomem's generated command registry plus each pinned contender's runtime MCP tool list and public CLI inventory. Every supported in-scope Exomem or Basic Memory capability SHALL map to an executed public-path runtime probe backed by a representative deterministic fixture. Only a verified unsupported result or justified boundary exclusion MAY replace execution; a fixture alone MUST NOT count as coverage. Hosting, accounts, billing, teams, cloud sync, deployment operations, and graphical interfaces SHALL be excluded. Agent-facing shared behavior SHALL be exercised over MCP; a product-native CLI MAY cover a genuinely CLI-only maintenance capability but SHALL be labelled by surface and MUST NOT substitute for a missing MCP capability. Inventory entries MUST NOT disappear without a manifest-version change and rationale, and a newly discovered public operation SHALL fail validation until classified.

#### Scenario: Capability omission is visible
- **WHEN** a contender exposes a documented local-core operation not represented by a benchmark case
- **THEN** inventory validation fails until it gains an executed public-path probe plus fixture, a verified unsupported mapping, or a justified exclusion

#### Scenario: Hosting breadth does not distort core comparison
- **WHEN** the benchmark report is generated
- **THEN** it states the local-engine boundary and does not award or penalize either contender for excluded hosting capabilities

#### Scenario: New public tool cannot evade classification
- **WHEN** a pinned contender's runtime MCP list or public CLI exposes an operation absent from the capability inventory
- **THEN** preflight fails and names the unclassified operation before any comparative claim is computed

### Requirement: Product-Neutral Corpus And Native Renderers
The repository SHALL define a small deterministic product-neutral corpus covering notes/entities, atomic observations, duplicate content with different categories, tags/context, nested frontmatter, schemas, typed/directional relations, provenance, lifecycle/history, distractors, mutations, graph paths, datasets, and representative media extension fixtures. Native renderers SHALL express equivalent facts using each contender's public grammar and SHALL record every unsupported fact explicitly.

#### Scenario: Renderer parity is inspectable
- **WHEN** the benchmark renders Exomem and Basic Memory corpora
- **THEN** the report lists how every neutral fact was represented or why it is unsupported
- **AND** neither renderer silently drops a required shared fact

### Requirement: Isolated Reproducible Public-Path Execution
The direct benchmark SHALL pin contender revisions and dependency lock/config hashes, create benchmark-managed environments and disposable project/home/config/database paths, perform required full indexing, and execute agent-facing cases through persistent public MCP sessions. Explicitly classified CLI-only maintenance probes MAY use the public CLI and SHALL record that surface. Model-backed probes SHALL record resolved model revisions and artifact hashes, backend, device, dtype/quantization, relevant runtime versions, deterministic seeds where supported, and predeclared numeric/order tolerances for embeddings, rerankers, CLIP, ASR, and other learned components. The benchmark SHALL record corpus/manifest hashes, raw requests/responses, cold/warm latency, response bytes, index duration, and filesystem/database mutation evidence. It MUST NOT pass either contender a live user vault or live product configuration.

#### Scenario: Basic Memory setup is isolated
- **WHEN** the direct benchmark prepares the sibling Basic Memory checkout
- **THEN** its environment, project, database, home, configuration, and caches are benchmark-owned and disposable

#### Scenario: Mutations cannot escape the fixture
- **WHEN** edit, direct-filesystem, move, delete, recovery, schema, reconcile, or reindex cases run
- **THEN** only disposable contender state changes and the report includes the before/after diff

### Requirement: Shared Retrieval And Authoring Matrix Is Executed
The shared-core layer SHALL execute public create/read/update of notes, entities, atomic observations, and relations; title/permalink/exact lookup; rare-token, phrase, stemming, and full-text cases where supported; semantic paraphrase without lexical overlap; hybrid adversarial distractors; type/project/tag/status/date/nested-number/category/kind filters; combined text-plus-filter and filter-only retrieval; one-to-three-hop typed/directional graph traversal; and bounded context assembly. Expected identity sets and order constraints SHALL be predeclared before observing contender output.

#### Scenario: Semantic retrieval requires nonlexical evidence
- **WHEN** a paraphrase query shares no meaningful tokens with its expected note
- **THEN** semantic credit requires the public result to return the expected identity rather than a source-code claim that embeddings exist

#### Scenario: Structured filters compose with text
- **WHEN** a query combines semantic text, active status, nested numeric range, and observation category
- **THEN** only identities satisfying every structural axis are eligible and the expected set is checked exactly

#### Scenario: Graph depth and direction are distinct
- **WHEN** cases request outgoing one-, two-, and three-hop paths plus the inverse direction
- **THEN** each returned identity/edge must match the predeclared relation type, direction, and depth

### Requirement: Score And Explanation Truth Is Verified
Every supported retrieval lane SHALL have an isolated probe plus a public hybrid probe. The benchmark SHALL verify that returned BM25 ranks/raw values/direction, vector or CLIP cosine values/model/range, graph/keyword/temporal ranks, fusion contributions, boosts, reranker model/values/direction, degradation, deterministic tie-breaks, and final order retain their documented meaning and agree with isolated lane evidence. A mode-dependent generic score MUST NOT receive truth credit.

#### Scenario: Hybrid lane evidence agrees with isolation
- **WHEN** a hit appears in isolated BM25 and vector runs and in public hybrid recall
- **THEN** its explained lane membership/ranks and deterministic fusion contributions agree across the recorded envelopes

#### Scenario: Missing metric is not converted to zero
- **WHEN** a contender does not expose a raw BM25 value or a lane is unavailable
- **THEN** the report records unavailable/unsupported rather than interpreting zero as a measurement

### Requirement: Schema And Lifecycle Integrity Is Exercised End To End
The benchmark SHALL attempt public schema infer/diff/validate/save behavior, valid and invalid public writes, direct filesystem edits, missed-watcher recovery, reconcile/full reindex, moves, deletes, recovery where supported, and current/superseded history. It SHALL verify content preservation, stale old text/category/path removal, current derived state, and absence of mixed generations. Documentation or source inspection alone MUST NOT count as a pass.

#### Scenario: Strict schema claims are runtime-tested
- **WHEN** a contender claims strict schema enforcement
- **THEN** the benchmark attempts invalid public write, direct edit plus sync/reconcile, and full reindex and reports each observed behavior independently

#### Scenario: Edit cleanup is complete
- **WHEN** an observation's category and content change
- **THEN** old category/text results disappear from every supported retrieval path and the new identity/state appears

#### Scenario: External invalid edit is not destroyed
- **WHEN** a direct filesystem edit violates a schema and recovery runs
- **THEN** the benchmark records whether content is preserved, how drift is surfaced, and whether repair restores clean state idempotently

### Requirement: Performance Envelope Is Controlled And Independent
Performance probes SHALL record host/OS/CPU/RAM, compute mode, resolved model revisions/artifact hashes, backend/device/dtype/quantization, relevant runtime versions, deterministic seeds where supported, dependency/config hashes, cold/warm cache state, warm-up count, timeout policy, and repeated counterbalanced contender order. Numeric/order tolerances, repetition counts, ordering method, and paired non-inferiority bands SHALL be predeclared in the hashed manifest. Reports SHALL include median and p95 query latency, index duration, response bytes, and bounded-context size. Performance SHALL run on a quiesced machine, SHALL remain separate from correctness, and MUST NOT compensate for a failed functional outcome.

#### Scenario: Warm latency samples are comparable
- **WHEN** a direct performance run completes
- **THEN** both contenders used the predeclared warm-up/sample counts in counterbalanced order under the recorded machine/config/cache state

#### Scenario: Timeout remains a visible result
- **WHEN** a contender exceeds the predeclared timeout
- **THEN** the report records the timeout as a failed performance sample and does not discard or replace it after observing the other contender

### Requirement: Exomem Local-Core Extensions Are Proved Separately
The Exomem extension layer SHALL runtime-test durable references, returned Sources/Evidence provenance, active/superseded lifecycle, governed note/block relations, semantic units, review/audit/adoption/reconcile, context packs, dataset-card/query behavior, and deterministic local PDF/image/audio/video ingestion/search/read fixtures where the corresponding extras are installed. Missing optional extras SHALL fail or mark that extension unavailable under predeclared policy; they SHALL NOT affect shared-core scoring. Basic Memory SHALL receive explicit unsupported results rather than emulated behavior for absent public capabilities.

#### Scenario: Provenance requires public evidence
- **WHEN** an Exomem result is expected to carry evidence or source provenance
- **THEN** extension credit requires the public response to identify the source/evidence and anchor rather than inferring it from storage

#### Scenario: Media extension is capability-gated
- **WHEN** a media extra is not installed in the pinned Exomem environment
- **THEN** the media gate reports unavailable with environment evidence and shared-core gates still execute

### Requirement: Independent Gates For Correctness, Integrity, Truth, Performance, And Extensions
The report SHALL keep `shared_core`, `lifecycle_integrity`, `explanation_truth`, `performance_envelope`, and `exomem_extensions` independent with no compensating weighted aggregate. Thresholds, expected results, normalization, and unsupported/error policy SHALL be immutable for a run and recorded by manifest hash. Unsupported behavior on a contender-neutral shared case SHALL count as not passed. A recorded local-core advantage MAY be claimed only for the pinned revisions/corpus when preflight proves both environments valid and every required probe completes as pass, behavioral fail, or verified unsupported; every required Exomem shared-core case and outcome passes; Exomem passes every individual case that Basic Memory passes; every required Exomem invariant and paired performance/no-regression threshold passes; every advertised in-scope Exomem extension designated required by the full profile passes in the pinned extras environment; and at least one such extension is publicly absent from Basic Memory. A shared case both contenders fail SHALL still block the full claim, and case failures MUST NOT be hidden by a passing coarse dimension ratio. A valid public operation returning an error SHALL be classified under the immutable behavioral policy; harness, setup, adapter, or environment failure SHALL invalidate the claim rather than count as a contender loss. A lean run with missing required extras MAY report shared-core gates but MUST NOT emit the full local-core-advantage claim. The report MUST NOT generalize that claim to hosting or overall product superiority.

#### Scenario: One shared regression blocks the advantage gate
- **WHEN** Basic Memory passes a shared filter or retrieval case that Exomem fails
- **THEN** the recorded local-core-advantage gate is false regardless of Exomem extension breadth

#### Scenario: Shared mutual failure blocks the advantage gate
- **WHEN** both contenders fail the same required shared-core case
- **THEN** the case remains failed and the recorded local-core-advantage gate is false

#### Scenario: Coarse dimension cannot hide case-level regression
- **WHEN** Exomem and Basic Memory fail different cases inside an otherwise passing dimension ratio
- **THEN** case-level paired evaluation blocks the advantage gate until every required Exomem case passes

#### Scenario: Harness failure is not a competitive loss
- **WHEN** a required contender probe cannot execute because setup, adapter, environment, or harness preflight fails
- **THEN** the report records an invalid comparison run and emits no local-core-advantage claim

#### Scenario: Performance cannot compensate for wrong answers
- **WHEN** a contender is faster but returns an incorrect identity set
- **THEN** the correctness gate fails and no weighted score hides it

#### Scenario: Missing required extension blocks the full claim
- **WHEN** shared-core gates pass but a required advertised media or dataset extension is unavailable or fails in the full pinned environment
- **THEN** the report preserves shared results but the local-core-advantage gate is false

### Requirement: Fast Native Gate And Explicit Direct Gate
The repository SHALL provide a fast Exomem-only fixture gate suitable for normal tests and an explicit desk-side direct-contender command. The direct command SHALL set up or verify the isolated pinned Basic Memory environment and SHALL soft-report an unavailable sibling checkout without making the normal suite depend on it.

#### Scenario: Normal suite has no contender dependency
- **WHEN** focused or lean Exomem tests run without a Basic Memory checkout
- **THEN** native fixture invariants run and the direct contender is not required

#### Scenario: Direct setup is reproducible
- **WHEN** the sibling checkout exists but its benchmark environment is absent
- **THEN** the documented direct command creates the pinned isolated environment, records its lock hash, and proceeds without using global Basic Memory state
