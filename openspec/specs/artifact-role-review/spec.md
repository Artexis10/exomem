# artifact-role-review Specification

## Purpose
Surface bounded, evidence-backed artifact-role transitions and stale current-state wording so an active agent can maintain coherent knowledge through governed edits.

## Requirements

### Requirement: Authored role transitions can surface within one topic

The system SHALL detect an explicitly reusable method with a distinct reported-success outcome, or an authored synthesis across at least two distinct resolved source documents, within an eligible compiled experiment. Each signal SHALL identify the qualifying units and the measured role boundary without requiring off-topic vocabulary. Quoted instructions, tentative or failed methods, ordinary experiment results without reusable intent, source inventories and single-source summaries SHALL NOT qualify. A method and its outcome MUST share an exact unit reference or a nonempty authored context with a unique compatible method/result pair; topical overlap alone SHALL NOT bind them. The supported deterministic cue grammar and exclusions SHALL be documented and paired with discriminating fixtures; unsupported or ambiguous wording SHALL remain quiet.

#### Scenario: Two same-topic methods have separate identities
- **WHEN** an experiment records two separately bound reusable procedures and their successful outcomes on the same subject
- **THEN** the system surfaces distinct `reusable_method` candidates with the appropriate supporting units

#### Scenario: General synthesis differs from citing a protocol
- **WHEN** an experiment contains an authored general synthesis tied to two distinct source documents, alongside a procedure that merely cites those documents
- **THEN** only the synthesis qualifies for `research_synthesis`

#### Scenario: Negative controls remain quiet
- **WHEN** a page contains only a planned method, failed method, quoted recipe, ordinary successful trial or single-source summary
- **THEN** it produces no artifact-role promotion candidate

#### Scenario: Shared batch context cannot lend success to another method
- **WHEN** two procedures share a context and only one has a successful result without an exact reference identifying the method
- **THEN** context equality alone promotes neither procedure

### Requirement: Promotion coverage requires represented units in an eligible role home

A role candidate SHALL resolve only when current, readable, eligible compiled destination content represents its qualifying material in the appropriate role and binds it with unambiguous source-unit provenance, or when its origin no longer qualifies. Version-one representation SHALL require parsed substantive-text equality for the procedure or synthesis with line endings normalized only, exact current links to all required evidence units, and retained synthesis source provenance. Comparison MUST preserve substantive case, Unicode, internal indentation, punctuation, values and units. Missing, stale or ambiguous unit targets MUST NOT establish coverage, including duplicate anchors. A paraphrase without deterministic equivalence SHALL remain for agent review without restricting the writer. Origin history SHALL be allowed to remain intact. Topic similarity, a parent-page backlink, a path name or a recorded decision alone MUST NOT establish coverage. Existing page eligibility and open-category rules SHALL remain unchanged.

#### Scenario: Extraction by addition settles only represented material
- **WHEN** a reusable destination represents and references one method's units while the original experiment retains both methods
- **THEN** that method's candidate resolves and the second method remains eligible

#### Scenario: A similar page cannot suppress a candidate
- **WHEN** a page shares the origin's title terms and links its parent but does not represent the qualifying units
- **THEN** the candidate remains unresolved

#### Scenario: Correct links do not substitute for represented content
- **WHEN** a destination has the right source-unit links but unrelated procedure text, or a required link names a missing or ambiguous anchor
- **THEN** it does not establish coverage

#### Scenario: Formatting cannot erase a procedural difference
- **WHEN** destination procedure text changes a case-sensitive identifier or internal code indentation while retaining the correct source links
- **THEN** it fails the substantive equality check and does not establish coverage

#### Scenario: A custom directory does not create a new page kind
- **WHEN** a destination has a supported compiled type in a custom Notes subdirectory
- **THEN** existing eligibility rules determine whether it can cover the candidate
- **AND** an unsupported or untyped page does not become eligible solely from its directory name

### Requirement: Transient-state review is bound to one authored episode

The system SHALL surface a review signal when an authored unqualified current pending assertion and an observed result coexist for the same experiment episode and outcome subject. Episode identity SHALL come from exact references, matching nonempty context with a unique compatible pair, or a documented finite single-trial grammar. Subject matching SHALL require explicit normalized labels rather than inferred topical or synonym similarity. The system MUST NOT infer temporal order from file timestamps, line positions or projection history, and SHALL claim a later result only when authored chronology supports that claim. Historical, quoted, conditional, future-trial and ambiguous multi-episode statements SHALL remain quiet. The signal SHALL not assert a general factual contradiction.

#### Scenario: Current pending wording coexists with a result
- **WHEN** a single-trial experiment says no result exists yet and also records that trial's observed result without authored dates
- **THEN** the signal names both units as current-state review evidence without asserting which was written later

#### Scenario: A different trial remains pending
- **WHEN** a completed trial has results and a separately identified future trial is awaiting results
- **THEN** no transient-state finding joins the two episodes

#### Scenario: Different outcomes in one trial stay distinct
- **WHEN** a batch has taste results but is still awaiting safety results
- **THEN** the taste outcome produces no transient-state finding against the safety assertion

#### Scenario: History does not masquerade as current state
- **WHEN** the earlier assertion is explicitly historical or has a valid governed supersession relation
- **THEN** the current-state review is resolved without deleting the historical fact

### Requirement: Review evidence has stable material identity and bounded coverage

Findings SHALL carry stable origin and unit references, a role or episode partition and a signal version derived from predicate-bearing authored state. Unrelated page edits, line movement and elapsed time SHALL NOT change that version. Material evidence changes SHALL change it. Each candidate SHALL expose at most eight evidence references, with deterministic ordering and no numeric confidence. Input or output limits and stale dependencies MUST persist capped or unknown coverage and expose it as `meta.coverage` on explicit audit/review results, even when no finding is available. Compact carriers SHALL omit invalid advice without changing their existing shape; compact omission SHALL NOT establish complete absence.

#### Scenario: Unrelated edits do not revive a dismissed finding
- **WHEN** an unrelated paragraph changes while the same supporting units and provenance remain intact
- **THEN** the review identity and signal version stay stable

#### Scenario: Bound exhaustion cannot report a clean page
- **WHEN** the detector reaches its documented input bound before establishing complete page coverage
- **THEN** the result marks coverage capped or unknown and does not claim that the page has no role or state issues

### Requirement: Projection maintenance is bounded and audience-relative

Both families SHALL be maintained after relevant origin or destination writes using already-held state and healed by explicit reconciliation. Synchronous writes SHALL add no corpus walk, destination-file read, Records census, model call or generation wait. A detector failure MUST NOT alter a durable mutation outcome. Every served finding, threshold, support decision and count SHALL be recomposed from evidence visible to that audience, under current generation and policy constraints. Missing or stale support SHALL be unknown; it MUST NOT establish resolution. Shared projection state SHALL retain support needed by other audiences.

Support detail limits SHALL apply after audience filtering and MUST NOT discard the shared dependency information needed to find a visible support. Dependent-origin fanout exceeding the synchronous bound SHALL invalidate remaining support through a constant-cost epoch or equivalent mechanism, without enumerating all dependent origins. Source-document changes SHALL invalidate affected support as well as origin and destination changes.

#### Scenario: Private evidence cannot create or hide a public finding
- **WHEN** one source of a two-source synthesis is private or its covering destination is private
- **THEN** the private source does not contribute to a public synthesis threshold and the private destination does not settle a candidate otherwise supported publicly
- **AND** no private identifiers or hidden-support counts appear in the public result

#### Scenario: Destination removal reopens coverage
- **WHEN** a covering destination is deleted or its source-unit provenance changes
- **THEN** affected candidates are recomposed or invalidated within the bounded write delta and reconciliation restores the current uncovered state

#### Scenario: Support beyond private details remains discoverable
- **WHEN** eight covering destinations are private and a ninth is public
- **THEN** the public support remains available for the caller-relative coverage decision and private details do not consume its output limit

#### Scenario: Fanout overflow invalidates without a scan
- **WHEN** a changed destination affects thousands of origin candidates
- **THEN** the synchronous delta recomposes only its bounded set, invalidates older support through a constant-cost marker and leaves remaining coverage unknown until reconciliation

#### Scenario: Failure preserves the write receipt
- **WHEN** signal maintenance raises after a canonical write commits
- **THEN** the write retains its committed terminal result and the signal projection reports unknown or omits stale advice
