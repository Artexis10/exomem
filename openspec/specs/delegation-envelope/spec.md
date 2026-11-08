# delegation-envelope Specification

## Purpose
Define the per-action authority envelope that keeps conversational prominence
separate from permission to act: hard ceilings bound every class, explicit
overrides may only reduce autonomy, confirmation-required actions stay human
controlled, and repeated signal families can be quieted without autonomous
behavioural inference.

## Requirements

### Requirement: Hard authority ceilings bound every action class

The product SHALL define a closed v1 set of envelope action classes —
`hygiene_writes`, `proactive_capture`, `link_acceptance`,
`structural_suggestions`, `restructure_execution`, `disclosure` — each with a
hard ceiling: `hygiene_writes` silent; `proactive_capture` silent-capable;
`link_acceptance` confirm; `structural_suggestions` advisory (surface only);
`restructure_execution` — covering restructure application, supersession
commit, entity creation, and deletion — confirm-required; `disclosure` governed
exclusively by the governance plane. No prominence level, envelope setting,
disposition, configuration value, or adaptation SHALL authorize behaviour above
a class's ceiling.

Confirm-required SHALL bind at three tiers: the served envelope marks the class
confirm-required; the agent contract requires explicit in-conversation user
confirmation before any surface of the class is invoked; and every server-side
confirmation or preview-first gate that exists today — deletion's explicit
confirm parameter, the adoption apply surface's preview-first default — SHALL
remain required. v1 SHALL add no new server-side confirmation parameter and no
tool-schema change; a server-side confirm for supersession and entity creation
is named future work behind the documented tool-surface rollout, and its
absence today SHALL be stated in the served contract rather than implied away.

A request to set a disposition for an unknown class id SHALL be refused with a
class-specific error and no state change. A request to configure `disclosure`
through the envelope SHALL be refused naming the governance plane as the owner.
An unknown class id or disposition found in the STORED configuration (for
example, written by a newer runtime) SHALL be reported and ignored at read
time, never refused — reading the envelope never breaks bootstrap.

#### Scenario: Maximal prominence cannot lift a ceiling

- **WHEN** prominence is `maximal` and every envelope override is set as
  permissive as its range allows
- **THEN** the served envelope still marks `restructure_execution`
  confirm-required, deletion still requires its explicit confirm parameter, and
  the adoption apply surface still defaults to preview-first

#### Scenario: An unknown class is refused at write and tolerated at read

- **WHEN** an envelope disposition is requested for a class id outside the
  closed v1 set, and separately a stored configuration carries an unknown id
- **THEN** the write is refused with a class-specific error and no state
  change, while the read serves the known classes and reports the unknown id

#### Scenario: Disclosure is not envelope-configurable

- **WHEN** an envelope disposition is requested for `disclosure`
- **THEN** the request is refused naming the governance plane as the owner

### Requirement: The envelope derives from prominence with explicit overrides

Three classes SHALL carry a configurable disposition drawn from a class range:
`proactive_capture` {off, advisory, silent}; `link_acceptance` {off, advisory,
confirm-shortcut}; `structural_suggestions` {off, advisory}. Two SHALL be
fixed: `hygiene_writes` silent and `restructure_execution` confirm — any
attempt to change `restructure_execution` is governed solely by the
standing-delegation refusal below. `disclosure` SHALL carry no disposition and
SHALL be served marked governance-owned.

`confirm-shortcut` SHALL mean an inline single-action confirmation rendered
with the surfaced item — one action approving that one named acceptance. The
confirmation step itself is never skipped, so `confirm-shortcut` sits below the
`confirm` ceiling.

A disposition SHALL govern agent-initiated behaviour only: an explicitly
requested read or review is always served, matching the family-disposition
rule that quiet is silent, not clean.

Absent an explicit override, each configurable disposition SHALL be a pure
derivation from the active prominence level: `proactive_capture`
off/off/silent/silent for off/light/balanced/maximal; `link_acceptance` and
`structural_suggestions` off at prominence `off` and advisory otherwise. An
explicit override SHALL persist across engine restarts and prominence changes
until reset, SHALL be stored in the shared engagement configuration (the same
per-machine file prominence lives in — the envelope is machine posture by
design, while family dispositions travel with the vault), and SHALL be refused
when outside the class range. The active envelope — every class, its
disposition or governance-owned marker, and whether a disposition is fixed,
derived, or overridden — SHALL be served by bootstrap and SHALL be inspectable
from the same surface that lists family dispositions, structurally separated
from the family rows so the two vocabularies never share a column. A reset
SHALL restore pure derivation for the named class.

The existing triage surface SHALL support `exomem://envelope/<action-class>`
before family and item references. Its `action` SHALL be an allowed disposition
or `reset`; it SHALL refuse irrelevant `until` and `expected_fingerprint`,
delegate the canonical class refusal unchanged, and return class, ceiling,
disposition, provenance, and the stable envelope ref. This adds no tool input
parameter.

#### Scenario: An override outlives a prominence change and a restart

- **WHEN** `proactive_capture` is explicitly set `advisory` while prominence is
  `maximal`, prominence is then changed to `balanced`, and the engine restarts
- **THEN** the served envelope still carries `proactive_capture: advisory`,
  marked as an override, and every other configurable class re-derives from
  `balanced`

#### Scenario: Out-of-range dispositions are refused

- **WHEN** `link_acceptance` is set to `silent`, or `structural_suggestions`
  to `confirm-shortcut`
- **THEN** each request is refused naming the class's range and nothing changes

#### Scenario: Reset restores derivation

- **WHEN** the `proactive_capture` override is reset at prominence `balanced`
- **THEN** the served disposition returns to `silent` and is marked derived

#### Scenario: Class off never blocks an explicit request

- **WHEN** `structural_suggestions` is `off` and the user explicitly requests a
  structural review
- **THEN** the review is served, and only agent-initiated surfacing is absent

### Requirement: Envelope adaptation is deterministic and consent-shaped

Envelope and family-disposition adaptation SHALL derive only from explicit
triage decisions. When the durable review-state records hold three
manual-origin dismissal events in one registered family — dismissal events,
counted from the records themselves so the count never decreases when items
later vanish, with automatic-origin and pre-normal-reset decisions excluded —
the next surfacing of that family SHALL carry exactly one offer to quiet it. A
normal reset SHALL atomically remove the disposition row and record that
family's UTC adaptation epoch; only records with `updated_at` strictly after it
are eligible. The offer SHALL be recorded durably against the family, SHALL
change nothing by itself, and SHALL
be made at most once: it is cleared only when the family is explicitly reset to
`normal`, which clears the family's slate; a decline without a reset never
re-offers. Usage logs, query history, read counts, and any engagement measure
SHALL NOT be inputs to any adaptation. Nothing SHALL be quieted, turned off, or
made more permissive except by an explicit decision.

When a write advisory is actually surfaced, its registered kind SHALL be stored
on that exact first-surfaced ledger row. Write-advisory triage SHALL resolve the
family from that key and pass it to the review-state decision. The first
eligible post-third warning SHALL contain one bounded quiet-offer clause while
retaining its existing terminal review ref and fingerprint unchanged; it SHALL
not increase warning count or exceed the 300-character warning budget. Ledger
or offer persistence failure SHALL remain fail-open and SHALL not spend the
offer marker.

#### Scenario: Three dismissals earn one offer, once

- **WHEN** a user dismisses three items of one family and then dismisses two
  more without acting on the offer
- **THEN** the family's next surfacing after the third dismissal carried the
  quiet offer, and no later surfacing repeats it

#### Scenario: A decline is durable until an explicit reset

- **WHEN** the user declines the offer, later quiets and then resets the family
  to `normal`, and dismisses three more items
- **THEN** no offer appeared between the decline and the reset, and exactly one
  new offer may appear after the post-reset third dismissal

#### Scenario: Nothing adapts on its own

- **WHEN** a family's items are repeatedly surfaced and ignored without triage
- **THEN** the family's disposition and every envelope cell are unchanged

### Requirement: Standing delegation is not a v1 envelope cell

The envelope SHALL NOT offer, accept, or store any non-confirm disposition for
`restructure_execution`, however the request is phrased — including "always
allow" and "do this kind of thing from now on". The refusal SHALL name the
founder gate: standing delegation would be an envelope cell above the current
ceiling, does not exist in v1, and only a deliberate founder ratification may
ever create one. This refusal is the sole specified error for the class; the
range-refusal rule above does not apply to it.

#### Scenario: A standing-delegation request is refused by name

- **WHEN** any surface attempts to set `restructure_execution` to a
  non-confirm disposition, however phrased
- **THEN** the refusal names the founder gate and the envelope is unchanged

### Requirement: The envelope teaching closes the hookless quiet loop

The served contract SHALL teach a hookless client how to route a
plain-language request to stop a kind of suggestion: the registered-family
vocabulary is discoverable from the served surfaces, the mapping from the
user's words to a registered family is the agent's judgment, and the resulting
decision lands through the existing family-disposition surface. The
server-side half of the loop — the quiet persisting with reason and origin
across sessions and engine restarts, inspection, and reset — is the
family-disposition requirement of the attention queue and SHALL hold unchanged
with the envelope present; the envelope's class dispositions SHALL be
unaffected by any family decision.

#### Scenario: A taught mapping quiets a real family and the envelope stands

- **WHEN** a hookless session, following the served contract, quiets a
  registered review family (for example `note_cohesion`) with a reason, a new
  session starts, and the dispositions view is read
- **THEN** that family is `quiet` with the recorded reason and origin, other
  families are untouched, every envelope class disposition is unchanged, and a
  reset restores `normal`

#### Scenario: The vocabulary is discoverable without hooks

- **WHEN** the hookless custom-instructions block and compact bootstrap are
  generated
- **THEN** each names the family-disposition surface and how to list the
  registered families, rather than hardcoding a family table

### Requirement: Collection creation is restructure execution
Creating a Records or Planning collection SHALL be an action of the `restructure_execution` class: confirm-required at every prominence level, satisfiable by one inline confirmation of the named proposal in the conversation, never by a standing delegation. Appending to an existing collection SHALL remain `proactive_capture`. The `records_routing` advisory and the `collection_candidate` finding SHALL be advisory material under `structural_suggestions` and SHALL never create, append, move or delete anything. A `disposition` on the advisory SHALL be derived from the effective capture level and SHALL direct only the agent's own `proactive_capture` append through `record_memory`; at no prominence SHALL the runtime append. A grouped backfill item SHALL be asked about once at every prominence, `maximal` included, and SHALL NOT be filed from a standing prominence. Bootstrap SHALL serve this classification with the envelope.

#### Scenario: A candidate never creates a collection on its own
- **WHEN** a strong `collection_candidate` is served at prominence `maximal`
- **THEN** no collection exists until the agent proposes it and the user confirms in the conversation

#### Scenario: The routing advisory never appends
- **WHEN** a `records_routing` advisory is returned on a committed write
- **THEN** no record has been appended by the runtime; the agent's subsequent append follows the served `proactive_capture` disposition

#### Scenario: Standing delegation for creation is refused by name
- **WHEN** a user asks that collections be created automatically from now on
- **THEN** the request is refused as a standing delegation above the v1 ceiling, and the one-action confirmation remains available

#### Scenario: A backfill is a question even at maximal
- **WHEN** a grouped backfill item is served at prominence `maximal`
- **THEN** the agent asks the user once before filing any of the named notes

#### Scenario: A file disposition is the agent's append
- **WHEN** a `records_routing` advisory carries `disposition: file` at prominence `maximal`
- **THEN** no record exists until the agent performs the returned payload, which is governed like any other append

### Requirement: Curation proposals and execution occupy different authority classes

Agent-initiated curation context and proposal surfacing SHALL be governed by the
existing `structural_suggestions` disposition. Forward-plan apply, resume, and
compensation execution SHALL be governed by the existing
`restructure_execution` class and SHALL remain confirm-required regardless of
prominence, family disposition, repeated prior approvals, or stored plan state.
The served bootstrap and curation workflow contract SHALL teach this split and
require explicit in-conversation user confirmation before the first apply of
each immutable forward or compensation plan.

These requirements and confirmation scenarios define the unchanged v1 mode.
`activate-agent-led-vocabulary-evolution` SHALL be the sole dependency that may
authorize supported additive steps in an explicitly activated v2 vault. A plan,
candidate, disposition, or prior confirmation SHALL NOT activate that mode or
replace its exact effect, current authority, and receipt checks. Compensation
and other non-additive steps retain their existing confirmation boundary.

#### Scenario: Structural suggestions are off

- **WHEN** `structural_suggestions` is off and the user has not explicitly
  requested a curation review
- **THEN** the agent does not proactively surface or propose curation work, while
  an explicit user request remains available

#### Scenario: Plan was proposed at maximal prominence

- **WHEN** a valid curation plan exists while prominence is maximal
- **THEN** the agent still obtains explicit confirmation before apply and no
  setting turns the plan into silent execution

#### Scenario: Compensation follows a previously approved plan

- **WHEN** the user already approved the forward plan and a compensation plan is
  later derived
- **THEN** the compensation plan requires its own explicit confirmation because
  it is a new immutable `restructure_execution` action

### Requirement: Entity lifecycle actions follow the delegation-envelope classes
Agent-initiated entity-candidate surfacing and curation-plan proposals SHALL be `structural_suggestions` and SHALL only surface when that class disposition permits. Entity merge, supersession or deletion, and curation apply, resume, or compensation SHALL be `restructure_execution` and SHALL remain exactly confirm-required. On a personal vault, an owner's entity-type registry save, including an unknown kind, SHALL follow `proactive_capture` as the `delegation-envelope` ceiling requirement states (owner decision O2, 2026-10-10, in `promote-referents-without-nudges`). On a personal vault, additive Entity creation through the entity writer's resolve-before-create path SHALL follow `proactive_capture` as the `delegation-envelope` ceiling requirement states; a promotion or hydration curation plan remains a confirmed apply. Acceptance of a relation that the server's relation queue suggested SHALL be `link_acceptance` and SHALL remain confirm-required, while a typed edge that the owner's agent authors SHALL follow `proactive_capture` as the `delegation-envelope` ceiling requirement states; when relation acceptance is a step inside curation apply, the stricter enclosing `restructure_execution` confirmation SHALL govern the whole apply. Candidate detection and work-item reads grant no write authority and this change SHALL add no standing-delegation cell.

These authority classes and confirmation scenarios define the v1 mode. `activate-agent-led-vocabulary-evolution` SHALL be the sole v2 authority dependency. Only its explicit activation, supported canonical effects, fresh authority checks, and receipt binding may authorize additive steps; lifecycle detection, stored proposals, prominence, and prior confirmations SHALL NOT grant that authority. Non-additive steps retain their existing confirmation requirements.

#### Scenario: Candidate surfacing is advisory only
- **WHEN** a recurring identity qualifies without an explicit user request
- **THEN** the active agent may surface it only under the `structural_suggestions` disposition
- **AND** detection does not create a registry type, Entity, relation, or curation plan

#### Scenario: Unknown kind registration follows proactive capture for the owner
- **WHEN** the owner's active agent judges that no registered type fits and saves a vault type with rationale
- **THEN** the registry save follows `proactive_capture` with every existing guarded-save check and no in-conversation confirmation
- **AND** a resolved non-owner's save of the same type stays a pending item for the owner

#### Scenario: Promotion and hydration apply remain confirmed
- **WHEN** a promotion or hydration curation plan is ready to apply, resume, or compensate
- **THEN** the action is `restructure_execution` and remains confirm-required
- **AND** no prominence or family disposition lifts that ceiling

#### Scenario: Relation acceptance keeps its own confirmation boundary
- **WHEN** the agent accepts a relation that the relation queue suggested, outside curation
- **THEN** it uses `link_acceptance` with confirmation
- **AND** the same step inside curation is covered by the stricter confirmed `restructure_execution` apply

### Requirement: Handing over an artifact does not bypass write authority
Handing a generated file to the user, or a definite report that an offered artifact was sent or published, SHALL be the adoption trigger but SHALL NOT count as explicit confirmation to write Exomem. Agent-initiated exact-byte Source or Evidence adoption and any later compatible delivery Record SHALL be `proactive_capture` and SHALL obey its active off, advisory, or silent disposition. A user's explicit request to save or preserve the artifact SHALL remain an ordinary requested action and SHALL not create standing authority for later artifacts.

When no compatible Records collection exists, a proposed collection or schema change SHALL be `structural_suggestions`; any later creation or schema mutation SHALL be separate confirmed `restructure_execution`. Adoption success SHALL never depend on that structural work.

#### Scenario: Hand-over under silent capture preserves exact bytes
- **WHEN** the agent hands the user one generated file and `proactive_capture` is `silent`
- **THEN** the active agent adopts exactly that file through the semantically correct Source or Evidence lane
- **AND** the hand-over grants no standing authority for files the agent never hands over or for future files

#### Scenario: Advisory capture surfaces before writing
- **WHEN** the agent hands the user one generated file and `proactive_capture` is `advisory`
- **THEN** the agent surfaces the proposed exact-byte adoption before invoking the write
- **AND** it does not describe the hand-over alone as write confirmation

#### Scenario: Off capture leaves a handed-over file unwritten
- **WHEN** the agent hands the user one generated file but `proactive_capture` is `off` and the user has not explicitly requested preservation
- **THEN** the agent performs no Source, Evidence, adoption-receipt, or delivery-Record write

#### Scenario: Explicit save remains requested action
- **WHEN** the user explicitly asks to save a generated file in Exomem
- **THEN** the agent may invoke the correct existing preservation lane as a requested action
- **AND** that request creates no standing delegation for another file

#### Scenario: Delivery observation obeys the same capture posture
- **WHEN** a local adoption receipt exists and a definite delivery event is eligible for an existing compatible Records collection
- **THEN** the agent-initiated Record follows `proactive_capture`
- **AND** absence of a compatible collection permits only a structural suggestion, not implicit schema creation
