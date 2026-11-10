## MODIFIED Requirements

### Requirement: Activation Items Use Stable Review Lifecycle

Activation items SHALL receive the same stable review reference, content-bound signal fingerprint, open/all/snoozed/dismissed filtering, and triage behavior as daily attention items. Activation references SHALL be deterministically distinct from daily-attention references for the same target so item lookup and triage resolve the intended queue. Item lookup and triage SHALL resolve activation-only references. A materially changed signal SHALL resurface even if its previous fingerprint was dismissed.

#### Scenario: Activation-only item can be triaged

- **WHEN** an activation item that is absent from default attention is dismissed through `decide_review_item`
- **THEN** it is hidden from the open activation view and visible in the dismissed or all view
- **AND** default attention behavior is unaffected

#### Scenario: Changed knowledge resurfaces

- **WHEN** a dismissed page is edited so its measured activation signal version changes while a deficit remains
- **THEN** the new activation fingerprint is open again

#### Scenario: Overlapping queues retain independent triage

- **WHEN** the same page appears in both daily attention and corpus activation
- **THEN** the two items have distinct stable review references
- **AND** dismissing the activation item does not dismiss or resolve to the daily-attention item

### Requirement: Unreflected outcomes is a default family derived from authored bindings

The audit SHALL register an `unreflected_outcomes` category. For each Records manifest whose Planning link carries a `join` and whose reference resolves to a Planning manifest, each open Planning item — lifecycle `active` and status not `completed` or `cancelled` — that at least one record joins to on the declared fields SHALL be one finding carrying the item reference, the Records collection, the joined record references (the first eight plus the total) and the binding that produced it, with the severity of a review candidate and a `signal_version` derived from authored state only. The finding's fingerprint SHALL be the item reference plus the sorted joined record keys, so a new joined record resurfaces a dismissed item under the existing material-change rule while the dismissal record stands. The finding SHALL resolve only when the item leaves the open state, the binding is removed, or the joined records are gone — never by time and never by the runtime mutating either side. The category SHALL be in the default attention union, in the due-state projection categories and in the delta categories, and therefore a registered family for dispositions. An unresolvable reference — or a join naming a plan-side field the target does not declare — SHALL produce no finding and SHALL be reported as unevaluated, never silently skipped. Disclosure SHALL follow every other category: a withheld record or item contributes nothing to the finding, the count or the reference list. Because the projection is shared across audiences and written by whichever audience wrote last, a withheld joined record SHALL be removed at serve and the served fingerprint SHALL be recomposed from the surviving records through the same composer a full audit uses, so the served view equals the vault with the withheld record absent; an entry whose joined records are all withheld SHALL be dropped rather than served with a smaller count.

#### Scenario: A recorded event on an open item is a candidate

- **WHEN** a Records collection joined on `title` holds a `produced` event for a deliverable whose Planning work item is still queued
- **THEN** the audit reports one `unreflected_outcomes` finding for that item naming the event, and the item appears in the default attention listing

#### Scenario: Twins stay quiet

- **WHEN** the same event exists in a Records collection without a `join`, or the joined work item is already `completed`
- **THEN** no finding is produced

#### Scenario: The transition clears it by state change

- **WHEN** the work item is triaged to `completed` or archived
- **THEN** the finding is gone on the next read without any dismissal being recorded

#### Scenario: A new event resurfaces a dismissed item

- **WHEN** a user dismissed the finding and a second joined record lands on the same open item
- **THEN** the item resurfaces with the new fingerprint and the earlier dismissal record is retained

#### Scenario: Family disposition applies

- **WHEN** the family's disposition is `quiet`
- **THEN** the audit still measures it, and it leaves the default union, the carriers and the write advisories exactly as any other quiet family

#### Scenario: The runtime never performs the transition

- **WHEN** a finding is open
- **THEN** no command, sweep or carrier edits the Planning item or the record; the decider acts through `add_plan_item` or `edit_plan_item`

### Requirement: Fingerprint-bound rejection via triage

`decide_review_item` SHALL accept relation-queue refs through the existing dismiss,
snooze, and reopen actions, binding decisions to the review ID and signal
fingerprint. Newly returned items SHALL echo their source hint for bounded
revalidation. Relation identities SHALL remain namespaced so triage never
resolves an activation or attention item, or vice versa. For a contradiction pair it SHALL also
accept the `competing` decision, which records the competing-alternatives stance.

#### Scenario: A contradiction pair is held as competing alternatives
- **WHEN** an agent calls `decide_review_item` with `decision` `competing` on a contradiction pair's id
- **THEN** the pair records the competing-alternatives stance, as the audit's fix text directs

#### Scenario: Dismissed candidate stays gone
- **WHEN** a candidate is dismissed and the unchanged queue is reread
- **THEN** the candidate remains absent while that decision is effective

#### Scenario: Changed signal resurfaces
- **WHEN** a dismissed candidate's underlying signal changes its fingerprint
- **THEN** the new candidate may reappear without inheriting the old dismissal

#### Scenario: Review namespaces remain isolated
- **WHEN** a relation candidate is triaged
- **THEN** activation and attention review decisions remain unchanged

### Requirement: Note cohesion answers use the existing review and curation surfaces

The served guidance for `note_cohesion` SHALL direct the agent to read the whole page and name the independent questions it answers. A page that answers one question, however long, SHALL be kept whole by dismissing the item with an `intentional:` reason that names that question. A page that answers several questions with their own material SHALL be split only through the curation lane, which requires the user's exact plan confirmation. A page that is a hub by design SHALL receive a breadth tag through a governed edit.

The item SHALL be complete without any sensor reading. A future sensor MAY attach readings to an item as `evidence_refs`, each with a reading id and the content hash of the page it read. A reading whose content hash no longer matches the page SHALL be ignored.

#### Scenario: Keeping a coherent page whole needs one call

- **WHEN** an agent judges that a page with an open item answers one question
- **THEN** one `decide_review_item` dismissal with the item's ref, fingerprint and an `intentional:` reason settles the item

#### Scenario: A split never happens without the user's confirmation

- **WHEN** an agent judges that a page answers several independent questions
- **THEN** the page changes only through a curation plan that the user confirms by its exact plan id and fingerprint

#### Scenario: The item stands without a sensor

- **WHEN** sensing is off for a vault
- **THEN** `note_cohesion` items are produced, served and answerable exactly as when sensing is on
