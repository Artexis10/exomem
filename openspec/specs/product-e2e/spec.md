# product-e2e Specification

## Purpose
Require black-box release proof through the installed wheel and real product transports, with deterministic lean coverage and explicit heavy-capability tiers.
## Requirements
### Requirement: Installed-wheel stdio product loop

The system SHALL provide a black-box test that builds and installs the wheel, initializes a temporary vault through the installed CLI, connects through a real stdio MCP client, and completes source capture, source-backed memory, recall, read, graph context, evidence preservation, supersession, evolution review, reconcile, Records authoring/recovery, restart, and persistence checks. The Records portion SHALL begin without a pre-written collection manifest and SHALL exercise `describe`, create-mode `validate`, `create` with a no-filter saved view, `inspect`, `append`, `query`, `update`, revision-mode `validate`, `revise`, a direct-edit audit gap, and `rebaseline` through the installed command surface.

#### Scenario: Governed lifecycle survives restart
- **WHEN** the lean product E2E completes the governed lifecycle and restarts the stdio server
- **THEN** the active conclusion, preserved source/evidence links, supersession history, stable references, Record collection, revised saved view, appended/updated items, and persistent `acknowledged_gap` Record discontinuity remain resolvable

#### Scenario: Human-owned Records lifecycle survives restart
- **WHEN** the lean product E2E manually inserts a template block, performs guarded Records append/update calls, edits the canonical log while stopped, and restarts the stdio server
- **THEN** the installed `record_memory` surface returns the manual and guarded state, observes the later edit, reports its audit gap, preserves the Planning descriptor, and does not expose raw rows through ordinary recall

#### Scenario: Records fixture cannot bypass authoring
- **WHEN** the installed-wheel Records lifecycle starts in its temporary vault
- **THEN** no test helper writes `_collection.md` or its canonical source before `record_memory(action="create")` commits them

#### Scenario: Validate and inspect acceptance stay identical
- **WHEN** the E2E validates and creates a manifest whose saved view omits optional filters
- **THEN** immediate inspect and saved-view query succeed without manifest repair

### Requirement: HTTP lifecycle and timeout safety
The system SHALL exercise the actual HTTP application lifecycle, REST authentication, MCP initialization, a read, a write, auth-required remote Records refusal, and clean shutdown. Every transport test SHALL have a bounded timeout and MUST fail rather than hang. The existing no-auth local HTTP harness SHALL remain explicit owner mode and SHALL NOT be reclassified by fabricated authorization headers.

#### Scenario: HTTP server starts and stops cleanly
- **WHEN** the HTTP E2E starts the server, performs authenticated operations, and requests shutdown
- **THEN** every request completes within its timeout and the server exits without a leaked lifespan task

#### Scenario: Unauthenticated Records call is rejected at remote ingress
- **WHEN** installed `python -m exomem --transport http` starts with isolated temporary OAuth anchors and receives a protocol-valid unauthenticated raw `POST /mcp` JSON-RPC `tools/call` payload naming `record_memory`
- **THEN** it returns exactly 401 with a Bearer challenge naming the local protected-resource metadata URL, its raw response discloses no collection path, row, Planning reference, or aggregate value, and the E2E does not accept another HTTP error or claim command-level governance executed

### Requirement: Tiered model and media gates
Lean product E2E SHALL run without optional models on every pull request. Real embeddings/reranking SHALL run in the model job, and real OCR, PDF, ASR, CLIP, and video fixtures SHALL run scheduled or opt-in with explicit soft-fail reporting when their configured dependencies are unavailable.

#### Scenario: Lean CI remains deterministic
- **WHEN** optional model and media extras are absent in the pull-request job
- **THEN** the lean E2E still proves the complete text/governance lifecycle and reports optional lanes as unavailable rather than failing implicitly

### Requirement: Installed-wheel Planning product journey
The lean installed-wheel product E2E SHALL exercise first-class Planning through real installed stdio MCP mutation/query calls and an installed CLI inspect/query call, without source-tree imports or optional models. In a temporary human-owned vault it SHALL create a Planning collection, capture and triage software intent, build an outcome-to-initiative-to-work-item hierarchy, query week/quarter/year/multi-year views, round-trip one opaque Records saved-view pointer and one thin OpenSpec/repository execution pointer, perform a guarded targeted update, restart the server, observe a direct canonical-file edit with a positive audit gap, and prove persistence. It SHALL also exercise a materially different non-software outcome/initiative fixture and SHALL keep raw Planning items out of ordinary recall. The installed auth-required HTTP lane SHALL prove unauthenticated refusal only; authenticated HTTP behavior remains covered by generated-surface and governance tests rather than being claimed by this journey.

#### Scenario: Software intent survives the public lifecycle
- **WHEN** the installed stdio journey captures a bug and feature candidate, promotes one into a committed quarterly initiative under an outcome, links an OpenSpec change, updates one exact item, and restarts
- **THEN** every item retains stable Planning identity, hierarchy, authored horizon/commitment, thin execution pointer, and guarded mutation history without copying OpenSpec requirements or tasks

#### Scenario: Direct edit is current truth after restart
- **WHEN** the journey edits one canonical Planning Markdown item with an ordinary file write between installed-server sessions
- **THEN** the restarted query returns the edit, `inspect` reports a bounded positive agent-audit gap, and no reconciliation step rewrites the file or invents history

#### Scenario: Records evidence remains opaque
- **WHEN** the installed journey stores a bounded Records collection/saved-view pointer on a plan
- **THEN** Planning round-trips the authorized pointer without resolving the collection, running Records, inferring progress, or mutating the Record collection

#### Scenario: Installed CLI reads stdio-created Planning state
- **WHEN** the stdio journey has committed Planning state and the installed CLI then runs `plan_memory inspect` and one bounded query against the same vault
- **THEN** both surfaces return the same collection identity, snapshot contract, and stable Planning item identities from the installed wheel

#### Scenario: Non-software fixture proves generic semantics
- **WHEN** the journey creates an unrelated personal, health, financial, household, or creative multi-year outcome with a shorter initiative beneath it
- **THEN** the same Planning command, schema, horizon, hierarchy, direct-edit, and query contracts work without software-specific fields or repository assumptions

#### Scenario: Ordinary recall is not a backlog dump
- **WHEN** the journey asks ordinary semantic recall about the Planning domain after adding raw items
- **THEN** the collection manifest may be discoverable but raw work-item files and generated Planning responses do not appear as semantic hits

#### Scenario: Unauthorized HTTP Planning request discloses nothing
- **WHEN** the installed auth-required HTTP server receives a protocol-valid unauthenticated `plan_memory` request
- **THEN** it returns the exact authentication refusal contract and no collection, item, horizon, title, relationship, evidence, execution pointer, hash, or existence appears in the raw response

#### Scenario: Planning journey remains bounded
- **WHEN** the lean product E2E runs on a pull request without embeddings or media extras
- **THEN** it completes within the existing product-gate timeout, reports optional lanes as unavailable where relevant, and cannot hang during server startup, request, restart, or shutdown
