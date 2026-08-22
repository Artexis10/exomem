## ADDED Requirements

### Requirement: Packaged Local-First Studio Entry Point

The system SHALL serve an Epistemic Review Studio at `/studio/` from versioned static assets packaged in the Exomem Python distribution. The Studio MUST operate without a CDN, external JavaScript, a Node runtime, a resident frontend process, or network access beyond its same-origin Exomem service. Missing or invalid Studio assets MUST soft-fail with a bounded diagnostic and MUST NOT prevent MCP, REST, CLI, health, or retrieval operations from starting.

#### Scenario: Installed wheel serves the Studio offline

- **WHEN** Exomem is installed from its wheel and the service starts without internet access
- **THEN** `/studio/` loads its HTML, CSS, JavaScript, and icon assets from that installed wheel
- **AND** the existing MCP, REST, and CLI surfaces retain their prior behavior

#### Scenario: Missing assets do not break the service

- **WHEN** the Studio asset manifest or an expected asset is unavailable
- **THEN** the Studio route returns a clear bounded error state
- **AND** service readiness and non-Studio operations remain healthy

### Requirement: Authenticated Data Boundary

The Studio shell SHALL contain no vault-derived titles, paths, counts, note text, graph data, or review state. Every vault data read and write MUST use same-origin authenticated REST commands and MUST preserve the existing bearer-key or Cloudflare Access authorization boundary. The Studio MUST NOT place a bearer secret in URL parameters, rendered HTML, application logs, persistent cookies, or `localStorage`.

#### Scenario: Unauthenticated browser receives no vault data

- **WHEN** a browser loads the Studio without valid REST or Cloudflare authorization
- **THEN** it can render only the inert application shell and authentication guidance
- **AND** every attempted data command is rejected without returning vault-derived content

#### Scenario: Session-scoped bearer authentication

- **WHEN** a user supplies a valid personal REST bearer key for the Studio session
- **THEN** the client may retain it only in memory or `sessionStorage` and send it in the authorization header to same-origin `/api/*` routes
- **AND** reloading after the browser session ends requires the credential again

### Requirement: Ranked Review Worklists

The Studio SHALL render the daily Epistemic Inbox as its default worklist using the server-provided deterministic order, stable review reference, categories, exact reasons, state, counts, and truncation. Corpus activation SHALL appear only as a separately selected opt-in worklist and SHALL show its denominator-backed coverage. The client MUST NOT recompute rank, infer severity, merge activation into the default Inbox, or conceal server-reported truncation.

#### Scenario: Inbox preserves server meaning

- **WHEN** the authenticated Studio loads a non-empty attention response
- **THEN** items appear in the exact server order with their measurement reasons, stable state, and visible result counts
- **AND** any truncation or upstream cap is shown to the user

#### Scenario: Activation remains opt-in

- **WHEN** the user opens the Studio without selecting corpus activation
- **THEN** activation-only findings are absent from the daily worklist
- **AND** selecting Activation shows its separate categories and coverage denominators without changing Inbox state

### Requirement: Bounded Review Workspace

Selecting a review item SHALL open a workspace keyed by its stable `exomem://review/<id>` reference and render the bounded target, related-page summaries, source/evidence provenance, graph neighborhood, history, review decision, and recorded evolution returned by `review_item_context`. The Studio SHALL label missing and truncated sections independently and MUST NOT fetch unrestricted related document bodies to fill them.

#### Scenario: User can inspect why an item surfaced

- **WHEN** a user selects an open review item
- **THEN** the workspace shows the exact review reasons beside the target and its available supporting context
- **AND** every cited page, source, evidence item, and relation retains its canonical reference

#### Scenario: Partial context remains honest

- **WHEN** graph, evidence, evolution, or related-page context is absent, unavailable, or capped
- **THEN** the affected section shows an explicit empty, unavailable, or truncated state
- **AND** the Studio does not synthesize replacement context or hide the limitation

### Requirement: Explicit Governed Review Actions

The Studio SHALL route dismiss, snooze, and reopen through `triage_memory`; compilation through a read-only `compile_source` proposal before `remember`; relation work through `connect_memory` proposals before an explicit governed edit; and supersession through a preview followed by `replace_memory`. A proposal, draft, or model suggestion MUST NOT mutate the vault. Conclusion-changing actions MUST name the target and require explicit confirmation.

#### Scenario: Triage updates only after success

- **WHEN** a user confirms dismiss or snooze for a review item
- **THEN** the Studio calls `triage_memory` with that stable reference and updates the worklist only after a successful response
- **AND** a failed response leaves the prior state visible with an actionable error

#### Scenario: Suggested relation remains provisional

- **WHEN** `connect_memory` returns a suggested relation
- **THEN** the Studio labels it as a proposal and makes no note or graph change
- **AND** only a separately confirmed governed edit can persist the accepted relation

#### Scenario: Supersession requires preview and confirmation

- **WHEN** a user chooses to replace a conclusion
- **THEN** the Studio shows the current target, proposed successor content, recorded reason, and resulting supersession action before enabling confirmation
- **AND** cancellation performs no write

### Requirement: Recorded Belief Evolution View

The Studio SHALL visualize a target's pointer-ordered supersession chain using recorded version dates, structural claims, transition reasons, provenance, and canonical references. It MUST NOT generate an explanatory narrative, confidence score, authority score, or unrecorded causal link. Single-version targets and capped chains SHALL produce explicit empty or truncated states.

#### Scenario: Multi-version conclusion displays recorded transitions

- **WHEN** a reviewed target belongs to a supersession chain with multiple versions
- **THEN** the Studio displays those versions in recorded pointer order with each available transition reason and provenance reference
- **AND** no transition text is added beyond stored facts

#### Scenario: No supersession is an honest empty state

- **WHEN** a reviewed target has never been superseded
- **THEN** the Evolution panel states that no recorded evolution exists
- **AND** it does not infer change from edit dates or semantic similarity

### Requirement: Accessible And Navigable Review Loop

The primary review loop SHALL be usable with keyboard navigation and programmatically labelled controls, SHALL preserve the selected stable review reference through browser back/forward navigation, and SHALL remain usable at narrow and desktop viewport widths. Status, severity, selection, and errors MUST NOT be communicated by color alone.

#### Scenario: Keyboard-only triage path

- **WHEN** a user navigates the worklist and workspace using only the keyboard
- **THEN** focus order, visible focus, labels, dialogs, and confirmation controls allow the user to inspect and complete a triage action
- **AND** returning to the list restores the selected item's context where possible
