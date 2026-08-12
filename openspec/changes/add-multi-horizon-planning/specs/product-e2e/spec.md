## ADDED Requirements

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
